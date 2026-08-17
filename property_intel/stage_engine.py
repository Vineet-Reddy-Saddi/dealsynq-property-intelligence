from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .store import EvidenceStore

CONTRACT_VERSION = "rahul-property-pipeline/2.0.0"
STATE_RESOLVER_VERSION = "current-property-state/1.0.0"


@dataclass(frozen=True)
class StageDefinition:
    key: str
    label: str
    execution_mode: str
    dependencies: tuple[str, ...]
    description: str


@dataclass
class Evaluation:
    has_output: bool
    metrics: dict[str, Any]
    evidence_refs: list[str]
    missing: list[str]
    blocked: bool = False

    @property
    def coverage_status(self) -> str:
        if self.has_output:
            return "partial" if self.missing else "complete"
        return "blocked" if self.blocked else "missing"


STAGES = (
    StageDefinition("jurisdiction", "Jurisdiction", "batch", (), "Resolve the collection scope and its source registry."),
    StageDefinition("source_discovery", "Source Discovery", "batch", ("jurisdiction",), "Map every required capability to an approved source, gap, or block."),
    StageDefinition("raw_ingestion", "Raw Ingestion", "batch", ("source_discovery",), "Retain immutable raw source payloads and retrieval metadata."),
    StageDefinition("normalization", "Normalization", "batch", ("raw_ingestion",), "Preserve raw identities while generating canonical aliases."),
    StageDefinition("parcel_database", "Parcel Database", "batch", ("normalization",), "Create canonical jurisdiction-scoped parcel records for the collection scope."),
    StageDefinition("spatial_data", "Spatial Data", "batch", ("parcel_database",), "Attach parcel geometry and reusable spatial overlays."),
    StageDefinition("land_records", "Land Records", "batch", ("parcel_database",), "Connect deeds, transactions, easements, leases, mortgages, and liens in bulk where sources allow."),
    StageDefinition("entity_resolution", "Entity Resolution", "batch", ("land_records",), "Resolve owners, organizations, aliases, and counterparties once across the scope."),
    StageDefinition("property_site_assembly", "Property/Site Assembly", "batch", ("spatial_data", "entity_resolution"), "Group parcels into explainable property sites before user search."),
    StageDefinition("physical_discovery", "Physical Discovery", "batch", ("property_site_assembly",), "Describe buildings, footprints, terrain, parking, access, and physical improvements."),
    StageDefinition("asset_classifier", "Asset Classifier", "batch", ("physical_discovery",), "Assign a versioned hierarchical asset classification."),
    StageDefinition("owner_graph", "Owner Graph", "batch", ("entity_resolution",), "Connect legal owners, parents, principals, affiliates, and portfolio candidates."),
    StageDefinition("permit_planning", "Permit/Planning", "batch", ("property_site_assembly",), "Build permit, entitlement, and surrounding-development histories in bulk where sources allow."),
    StageDefinition("zoning_analysis", "Zoning Analysis", "batch", ("spatial_data", "permit_planning"), "Convert zoning text and geometry into computational rules and capacity screens."),
    StageDefinition("environmental_hazard", "Environmental / Hazard", "batch", ("spatial_data",), "Precompute parcel/site environmental and natural-hazard screens."),
    StageDefinition("infrastructure_data", "Infrastructure Data", "batch", ("spatial_data",), "Model roads, access, frontage, transit, and utilities where evidence exists."),
    StageDefinition("web_intelligence", "Web Intelligence", "on_demand", ("asset_classifier",), "Run current property-specific searches through replaceable, approved providers."),
    StageDefinition("document_pipeline", "Document Pipeline", "on_demand", ("web_intelligence",), "Retain, classify, parse, and link property-specific public documents and mentions."),
    StageDefinition("tenant_engine", "Tenant Engine", "on_demand", ("document_pipeline",), "Model current and historical tenant/space occurrences as temporal, sourced relationships."),
    StageDefinition("market_intel", "Market Intelligence", "on_demand", ("asset_classifier", "tenant_engine"), "Connect current listings, asking evidence, executed comps, and market changes."),
    StageDefinition("event_engine", "Event Engine", "materialize", ("land_records", "tenant_engine", "permit_planning", "market_intel"), "Normalize dated batch and on-demand observations into events."),
    StageDefinition("property_timeline", "Property Timeline", "materialize", ("event_engine",), "Order evidence-backed events into a property history."),
    StageDefinition("claim_resolution", "Claim Resolution", "materialize", ("property_timeline",), "Rank preferred current claims without deleting alternatives or conflicts."),
    StageDefinition("current_property_state", "Current Property State", "materialize", ("claim_resolution",), "Materialize a time-stamped state snapshot linked to claims and contradictions."),
    StageDefinition("property_intelligence_profile", "Property Intelligence Profile", "materialize", ("current_property_state",), "Publish the graph, evidence, coverage, gaps, and current state."),
)


def stage_catalog() -> list[dict[str, Any]]:
    return [
        {"order": index, "key": stage.key, "label": stage.label,
         "execution_mode": stage.execution_mode,
         "dependencies": list(stage.dependencies), "description": stage.description,
         "contract_version": CONTRACT_VERSION}
        for index, stage in enumerate(STAGES, 1)
    ]


def _count(store: EvidenceStore, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = store.db.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def _ids(store: EvidenceStore, sql: str, params: tuple[Any, ...] = ()) -> list[str]:
    return [str(row[0]) for row in store.rows(sql, params)]


def _gap_rows(store: EvidenceStore, target_id: str, category: str | None = None) -> list[Any]:
    sql = "SELECT * FROM gaps WHERE target_id=? AND status!='resolved'"
    params: tuple[Any, ...] = (target_id,)
    if category:
        sql += " AND category=?"
        params += (category,)
    return store.rows(sql, params)


def _missing_capabilities(store: EvidenceStore, target_id: str, capabilities: set[str]) -> tuple[list[str], bool]:
    rows = store.rows(
        "SELECT capability,status,reason FROM source_capabilities WHERE target_id=?",
        (target_id,),
    )
    by_name = {row["capability"]: row for row in rows}
    missing: list[str] = []
    blocked = False
    for capability in sorted(capabilities):
        row = by_name.get(capability)
        status = row["status"] if row else "missing"
        if status not in {"configured", "working", "not_applicable"}:
            missing.append(f"{capability}: {status}" + (f" - {row['reason']}" if row and row["reason"] else ""))
            blocked = blocked or status == "blocked"
    return missing, blocked


def _evaluate_jurisdiction(store: EvidenceStore, tid: str) -> Evaluation:
    target = store.db.execute("SELECT target_id,config_json FROM targets WHERE target_id=?", (tid,)).fetchone()
    jurisdiction_id = None
    if target:
        config = json.loads(target["config_json"])
        jurisdiction_id = (config.get("_source_resolution") or {}).get("jurisdiction_id")
    return Evaluation(bool(target and jurisdiction_id), {"jurisdiction_id": jurisdiction_id},
                      [tid] if target else [], [] if jurisdiction_id else ["Jurisdiction resolution"])


def _evaluate_source_discovery(store: EvidenceStore, tid: str) -> Evaluation:
    rows = store.rows("SELECT * FROM source_capabilities WHERE target_id=? ORDER BY capability", (tid,))
    counts: dict[str, int] = {}
    missing = []
    blocked = False
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        if row["status"] not in {"configured", "working", "not_applicable"}:
            missing.append(f"{row['capability']}: {row['status']}")
            blocked = blocked or row["status"] == "blocked"
    return Evaluation(bool(rows), {"capability_count": len(rows), "status_counts": counts},
                      [row["capability_id"] for row in rows], missing, blocked)


def _evaluate_raw(store: EvidenceStore, tid: str) -> Evaluation:
    sources = _count(store, "SELECT COUNT(*) FROM sources")
    raw = _count(store, "SELECT COUNT(*) FROM raw_evidence")
    missing_raw = _count(store, "SELECT COUNT(*) FROM facts f LEFT JOIN raw_evidence r ON r.raw_sha256=f.raw_sha256 WHERE f.raw_sha256 IS NOT NULL AND r.raw_sha256 IS NULL")
    missing = [f"{missing_raw} facts reference missing raw evidence"] if missing_raw else []
    return Evaluation(raw > 0 and sources > 0, {"sources": sources, "raw_objects": raw, "missing_raw": missing_raw},
                      _ids(store, "SELECT source_id FROM sources ORDER BY source_id"), missing)


def _evaluate_normalization(store: EvidenceStore, tid: str) -> Evaluation:
    entities = _count(store, "SELECT COUNT(*) FROM entities")
    aliases = _count(store, "SELECT COUNT(*) FROM entity_aliases")
    types = _count(store, "SELECT COUNT(DISTINCT alias_type) FROM entity_aliases")
    missing = [] if aliases else ["Canonical aliases for addresses, parcels, owners, and tenants"]
    return Evaluation(entities > 0, {"entities": entities, "aliases": aliases, "alias_types": types},
                      _ids(store, "SELECT alias_id FROM entity_aliases ORDER BY alias_id"), missing)


def _evaluate_parcels(store: EvidenceStore, tid: str) -> Evaluation:
    parcels = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='parcel'")
    included = _count(store, "SELECT COUNT(*) FROM grouping_decisions WHERE target_id=? AND included=1", (tid,))
    missing = [] if included else ["Approved local parcel geometry and canonical parcel records"]
    return Evaluation(parcels > 0, {"candidate_parcels": parcels, "site_parcels": included},
                      _ids(store, "SELECT entity_id FROM entities WHERE entity_type='parcel' ORDER BY entity_id"), missing)


def _evaluate_spatial(store: EvidenceStore, tid: str) -> Evaluation:
    predicates = {row["predicate"] for row in store.rows(
        "SELECT predicate FROM facts WHERE subject_id=? AND status='current'", (tid,))}
    expected = {"analysis_geometry", "nfhl_site_overlay", "nwi_site_overlay", "ssurgo_site_overlay"}
    present = sorted(expected & predicates)
    missing = [name for name in sorted(expected - predicates)]
    area_predicates = {"parcel_geometry_union_area", "projected_parcel_geometry_union_area"}
    present.extend(sorted(area_predicates & predicates))
    if not area_predicates & predicates:
        missing.append("parcel geometry union area")
    return Evaluation(bool(present), {"spatial_outputs": present},
                      _ids(store, "SELECT fact_id FROM facts WHERE subject_id=? AND predicate IN ('analysis_geometry','parcel_geometry_union_area','projected_parcel_geometry_union_area','nfhl_site_overlay','nwi_site_overlay','ssurgo_site_overlay') AND status='current'", (tid,)), missing)


def _evaluate_land_records(store: EvidenceStore, tid: str) -> Evaluation:
    documents = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='recorded_document'")
    transactions = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='transaction'")
    capital = _count(store, "SELECT COUNT(*) FROM facts WHERE subject_id=? AND predicate='capital_stack_reconstruction' AND status='current'", (tid,))
    missing, blocked = _missing_capabilities(store, tid, {"deeds_land_records", "mortgages_liens", "subdivision_plans", "easements"})
    return Evaluation(documents > 0 or transactions > 0, {"recorded_documents": documents, "transactions": transactions, "capital_stack_screens": capital},
                      _ids(store, "SELECT entity_id FROM entities WHERE entity_type IN ('recorded_document','transaction') ORDER BY entity_id"), missing, blocked)


def _evaluate_entities(store: EvidenceStore, tid: str) -> Evaluation:
    organizations = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='organization'")
    rels = _count(store, "SELECT COUNT(*) FROM relationships WHERE relationship_type IN ('owns','owns_or_owned','subsidiary_of','has_counterparty','assessor_owner_of','candidate_owner_of')")
    aliases = _count(store, "SELECT COUNT(*) FROM entity_aliases WHERE alias_type='organization_name'")
    missing, blocked = _missing_capabilities(store, tid, {"entity_registry", "corporate_filings"})
    return Evaluation(organizations > 0 and rels > 0, {"organizations": organizations, "resolution_edges": rels, "organization_aliases": aliases},
                      _ids(store, "SELECT entity_id FROM entities WHERE entity_type='organization' ORDER BY entity_id"), missing, blocked)


def _evaluate_assembly(store: EvidenceStore, tid: str) -> Evaluation:
    decisions = _count(store, "SELECT COUNT(*) FROM grouping_decisions WHERE target_id=?", (tid,))
    included = _count(store, "SELECT COUNT(*) FROM grouping_decisions WHERE target_id=? AND included=1", (tid,))
    explanations = _count(store, "SELECT COUNT(*) FROM grouping_decisions WHERE target_id=? AND evidence_json!='{}'", (tid,))
    missing, blocked = _missing_capabilities(store, tid, {"property_site_assembly"})
    if not included or explanations != decisions:
        missing.append("Explainable parcel membership decisions")
    return Evaluation(included > 0, {"decisions": decisions, "included": included, "explained": explanations},
                      _ids(store, "SELECT decision_id FROM grouping_decisions WHERE target_id=? ORDER BY parcel_id", (tid,)), missing, blocked)


def _evaluate_physical(store: EvidenceStore, tid: str) -> Evaluation:
    buildings = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='building'")
    footprints = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='building_footprint'")
    physical_predicates = {row[0] for row in store.rows(
        "SELECT DISTINCT predicate FROM facts WHERE status='current' AND predicate IN "
        "('parking_area','impervious_area','entrance_count','road_frontage',"
        "'elevation_screening','usa_structures_footprints',"
        "'official_city_building_footprint_intersections')")}
    expected = {"parking_area", "impervious_area", "entrance_count", "road_frontage", "elevation_screening"}
    has_footprint_screen = bool({"usa_structures_footprints", "official_city_building_footprint_intersections"} & physical_predicates)
    if not has_footprint_screen:
        expected.add("official building-footprint screen")
    missing = sorted(expected - physical_predicates)
    return Evaluation(buildings > 0 or footprints > 0 or has_footprint_screen, {"assessor_buildings": buildings, "footprints": footprints, "physical_outputs": sorted(physical_predicates)},
                      _ids(store, "SELECT entity_id FROM entities WHERE entity_type IN ('building','building_footprint') ORDER BY entity_id"), missing)


def _evaluate_asset(store: EvidenceStore, tid: str) -> Evaluation:
    rows = store.rows(
        "SELECT fact_id,value_json FROM facts WHERE subject_id=? "
        "AND predicate='hierarchical_asset_classification' AND status='current'", (tid,))
    statuses = [json.loads(row["value_json"]).get("status") for row in rows]
    resolved = any(status == "classified" for status in statuses)
    missing = [] if resolved else [
        "Sufficient sourced use/building/tenant evidence for a resolved asset classification"
    ]
    return Evaluation(bool(rows), {"classifications": len(rows), "statuses": statuses},
                      [row["fact_id"] for row in rows], missing)


def _evaluate_web(store: EvidenceStore, tid: str) -> Evaluation:
    discoveries = _count(store, "SELECT COUNT(*) FROM web_discoveries WHERE target_id=?", (tid,))
    jurisdiction_references = _count(
        store,
        "SELECT COUNT(*) FROM web_discoveries WHERE target_id=? AND json_extract(attributes_json,'$.scope')='jurisdiction_reference'",
        (tid,),
    )
    property_specific = discoveries - jurisdiction_references
    total = _count(store, "SELECT COUNT(*) FROM search_queries WHERE target_id=?", (tid,))
    completed = _count(store, "SELECT COUNT(*) FROM search_queries WHERE target_id=? AND status='completed'", (tid,))
    with_results = _count(store, "SELECT COUNT(*) FROM search_queries WHERE target_id=? AND status='completed' AND result_count>0", (tid,))
    missing = []
    if not discoveries:
        missing.append("Approved search-provider results")
    if completed < total:
        missing.append(f"{total-completed} planned searches have not executed")
    if completed and with_results < completed:
        missing.append(f"{completed-with_results} executed searches returned no approved discovery candidates")
    if discoveries and not property_specific:
        missing.append("Property-specific discovery candidates beyond the approved jurisdiction source catalog")
    blocked = any(row["status"] == "blocked" for row in _gap_rows(store, tid, "web_intelligence"))
    return Evaluation(discoveries > 0, {"planned_queries": total, "executed_queries": completed, "queries_with_results": with_results, "discoveries": discoveries,
                                      "jurisdiction_references": jurisdiction_references,
                                      "property_specific_discoveries": property_specific},
                      _ids(store, "SELECT discovery_id FROM web_discoveries WHERE target_id=? ORDER BY discovery_id", (tid,)), missing, blocked)


def _evaluate_documents(store: EvidenceStore, tid: str) -> Evaluation:
    documents = _count(store, "SELECT COUNT(*) FROM documents WHERE target_id=?", (tid,))
    jurisdiction_references = _count(
        store,
        "SELECT COUNT(*) FROM documents WHERE target_id=? AND json_extract(attributes_json,'$.scope')='jurisdiction_reference'",
        (tid,),
    )
    property_specific = documents - jurisdiction_references
    mentions = _count(store, "SELECT COUNT(*) FROM document_mentions m JOIN documents d ON d.document_id=m.document_id WHERE d.target_id=?", (tid,))
    document_gaps = _gap_rows(store, tid, "documents")
    ocr_gaps = [row for row in document_gaps if "OCR" in (row["description"] or "").upper()]
    missing = [f"{row['description']}: {row['reason']}" if row["reason"] else row["description"]
               for row in document_gaps]
    if documents and not property_specific:
        missing.append("Property-specific permit, entitlement, deed, mortgage, or environmental documents")
    return Evaluation(documents > 0, {"documents": documents, "jurisdiction_references": jurisdiction_references,
                                      "property_specific_documents": property_specific, "located_mentions": mentions,
                                      "ocr_gaps": len(ocr_gaps), "failed_or_partial_documents": len(document_gaps)},
                      _ids(store, "SELECT document_id FROM documents WHERE target_id=? ORDER BY document_id", (tid,)), missing)


def _evaluate_tenants(store: EvidenceStore, tid: str) -> Evaluation:
    tenants = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='tenant'")
    spaces = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='tenant_space'")
    state_types = "('occupancy','tenant_occurrence','tenant_occupancy')"
    states = _count(store, f"SELECT COUNT(*) FROM temporal_states WHERE target_id=? AND state_type IN {state_types}", (tid,))
    dated = _count(store, f"SELECT COUNT(*) FROM temporal_states WHERE target_id=? AND state_type IN {state_types} AND (valid_from IS NOT NULL OR first_seen IS NOT NULL)", (tid,))
    historical = _count(store, f"SELECT COUNT(*) FROM temporal_states WHERE target_id=? AND state_type IN {state_types} AND valid_to IS NOT NULL", (tid,))
    historical_screens = _count(store, "SELECT COUNT(*) FROM facts WHERE subject_id=? AND predicate='official_city_historical_commercial_lease_observations' AND status='current' AND json_extract(value_json,'$.feature_count')>0", (tid,))
    screened = _count(store, "SELECT COUNT(*) FROM facts WHERE subject_id=? AND predicate='official_city_commercial_lease_observations' AND status='current'", (tid,))
    classification_row = store.db.execute(
        "SELECT value_json FROM facts WHERE subject_id=? AND predicate='hierarchical_asset_classification' "
        "AND status='current' ORDER BY confidence DESC LIMIT 1", (tid,)).fetchone()
    classification = json.loads(classification_row[0]) if classification_row else {}
    primary = ((classification.get("preferred") or {}).get("primary") if isinstance(classification, dict) else None)
    tenant_history_applicable = primary not in {"residential"}
    missing = [] if not tenant_history_applicable or (states and dated == states and (historical or historical_screens)) else ["Complete first-seen/last-seen and prior-tenant history"]
    return Evaluation(not tenant_history_applicable or tenants > 0 or screened > 0 or historical_screens > 0, {"tenants": tenants, "spaces": spaces, "temporal_states": states, "dated_states": dated, "closed_or_historical_states": historical, "official_city_screens": screened, "historical_city_screens_with_results": historical_screens, "tenant_history_applicable": tenant_history_applicable},
                      _ids(store, "SELECT entity_id FROM entities WHERE entity_type IN ('tenant','tenant_space') ORDER BY entity_id"), missing)


def _evaluate_owner_graph(store: EvidenceStore, tid: str) -> Evaluation:
    owners = _count(store, "SELECT COUNT(*) FROM relationships WHERE relationship_type IN ('owns','owns_or_owned','assessor_owner_of')")
    parents = _count(store, "SELECT COUNT(*) FROM relationships WHERE relationship_type IN ('subsidiary_of','principal_of')")
    portfolio = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='portfolio_property_candidate'")
    owner_screens = store.rows(
        "SELECT f.value_json FROM facts f JOIN relationships r ON r.from_entity_id=f.subject_id "
        "WHERE r.to_entity_id=? AND r.relationship_type='assessor_owner_of' "
        "AND f.predicate='ct_business_registry_match_screen' AND f.status='current'", (tid,))
    business_owner = any(bool((json.loads(row[0]) or {}).get("eligible_business_name")) for row in owner_screens)
    registry_screened = bool(owner_screens)
    missing = []
    if owners and not registry_screened:
        missing.append("Owner registry/name-eligibility screen")
    if business_owner and not parents:
        missing.append("Verified parent/principal relationship")
    return Evaluation(owners > 0, {"ownership_edges": owners, "parent_or_principal_edges": parents, "portfolio_candidates": portfolio,
                                   "registry_screened": registry_screened, "business_owner": business_owner,
                                   "portfolio_screen_result": "matches_found" if portfolio else "no_additional_exact-owner_matches"},
                      _ids(store, "SELECT relationship_id FROM relationships WHERE relationship_type IN ('owns','owns_or_owned','assessor_owner_of','subsidiary_of','principal_of','candidate_owner_of') ORDER BY relationship_id"), missing)


def _evaluate_permits(store: EvidenceStore, tid: str) -> Evaluation:
    permits = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='permit'")
    development = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='development_case'")
    missing, blocked = _missing_capabilities(store, tid, {"building_permits", "planning_applications", "zoning_variances", "certificates_of_occupancy", "code_enforcement", "development_pipeline"})
    screened = _count(store, "SELECT COUNT(*) FROM facts WHERE subject_id=? AND predicate='surrounding_development_pipeline' AND status='current'", (tid,))
    return Evaluation(permits > 0 or development > 0 or screened > 0, {"permits": permits, "surrounding_development_cases": development, "official_city_screens": screened},
                      _ids(store, "SELECT entity_id FROM entities WHERE entity_type IN ('permit','development_case') ORDER BY entity_id"), missing, blocked)


def _evaluate_zoning(store: EvidenceStore, tid: str) -> Evaluation:
    facts = _count(store, "SELECT COUNT(*) FROM facts WHERE category='zoning' AND status='current'")
    envelope = _count(store, "SELECT COUNT(*) FROM facts WHERE subject_id=? AND predicate IN ('gross_coverage_envelope_known_parcels','zoning_envelope_land_coverage') AND status='current'", (tid,))
    gaps = _gap_rows(store, tid, "zoning")
    missing, capability_blocked = _missing_capabilities(store, tid, {"zoning_gis", "zoning_ordinance"})
    if envelope:
        missing = [item for item in missing if not item.lower().startswith("zoning_ordinance:")]
    missing.extend(row["description"] for row in gaps)
    if not envelope:
        missing.append("Interpreted dimensional rules and a computational zoning capacity envelope")
    return Evaluation(facts > 0, {"zoning_facts": facts, "capacity_screens": envelope},
                      _ids(store, "SELECT fact_id FROM facts WHERE category='zoning' AND status='current' ORDER BY fact_id"),
                      missing, capability_blocked or any(row["status"] == "blocked" for row in gaps))


def _evaluate_environment(store: EvidenceStore, tid: str) -> Evaluation:
    environmental = _count(store, "SELECT COUNT(*) FROM facts WHERE category='environmental' AND status='current'")
    hazards = _count(store, "SELECT COUNT(*) FROM facts WHERE category='hazards' AND status='current'")
    gaps = _gap_rows(store, tid, "environmental")
    missing, capability_blocked = _missing_capabilities(
        store, tid, {"environmental_hazards"})
    missing.extend(row["description"] for row in gaps)
    return Evaluation(environmental > 0 or hazards > 0, {"environmental_facts": environmental, "hazard_facts": hazards},
                      _ids(store, "SELECT fact_id FROM facts WHERE category IN ('environmental','hazards') AND status='current' ORDER BY fact_id"),
                      missing, capability_blocked or any(row["status"] == "blocked" for row in gaps))


def _evaluate_infrastructure(store: EvidenceStore, tid: str) -> Evaluation:
    access = _count(store, "SELECT COUNT(*) FROM facts WHERE category='access' AND status='current'")
    infrastructure = _count(store, "SELECT COUNT(*) FROM facts WHERE subject_id=? AND category='infrastructure' AND status='current'", (tid,))
    missing, blocked = _missing_capabilities(store, tid, {"roads_access_frontage", "infrastructure", "utilities_water_sewer"})
    return Evaluation(access > 0 or infrastructure > 0, {"access_facts": access, "infrastructure_facts": infrastructure},
                      _ids(store, "SELECT fact_id FROM facts WHERE category IN ('access','infrastructure') AND status='current' ORDER BY fact_id"), missing, blocked)


def _evaluate_market(store: EvidenceStore, tid: str) -> Evaluation:
    subject = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='listing'")
    nearby = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='market_comparable_listing'")
    official_sales = _count(store, "SELECT COUNT(*) FROM entities WHERE entity_type='executed_sale_observation'")
    executed = _count(store, "SELECT COUNT(*) FROM facts WHERE category='market' AND predicate IN ('executed_sale_comp','executed_lease_comp') AND status='current'")
    screened = _count(store, "SELECT COUNT(*) FROM facts WHERE subject_id=? AND predicate IN ('official_city_commercial_inventory_observations','official_city_sales_comparable_screen','official_city_historical_sales_screen') AND status='current'", (tid,))
    missing = [] if executed else ["Normalized executed sale and lease comparables"]
    return Evaluation(subject > 0 or nearby > 0 or official_sales > 0 or screened > 0, {"subject_listings": subject, "nearby_asking_records": nearby, "official_sale_observations": official_sales, "normalized_executed_comps": executed, "official_city_screens": screened},
                      _ids(store, "SELECT entity_id FROM entities WHERE entity_type IN ('listing','market_comparable_listing','executed_sale_observation') ORDER BY entity_id"), missing)


def _evaluate_events(store: EvidenceStore, tid: str) -> Evaluation:
    events = _count(store, "SELECT COUNT(*) FROM events WHERE target_id=?", (tid,))
    types = _count(store, "SELECT COUNT(DISTINCT event_type) FROM events WHERE target_id=?", (tid,))
    return Evaluation(events > 0, {"events": events, "event_types": types},
                      _ids(store, "SELECT event_id FROM events WHERE target_id=? ORDER BY event_id", (tid,)), [] if events else ["Normalized property events"])


def _evaluate_timeline(store: EvidenceStore, tid: str) -> Evaluation:
    events = _count(store, "SELECT COUNT(*) FROM events WHERE target_id=?", (tid,))
    dated = _count(store, "SELECT COUNT(*) FROM events WHERE target_id=? AND event_date IS NOT NULL", (tid,))
    missing = [] if events and dated == events else [f"{events-dated} events lack a usable date"]
    return Evaluation(dated > 0, {"events": events, "dated_events": dated},
                      _ids(store, "SELECT event_id FROM events WHERE target_id=? AND event_date IS NOT NULL ORDER BY event_date,event_id", (tid,)), missing)


def _evaluate_claims(store: EvidenceStore, tid: str) -> Evaluation:
    claims = _count(store, "SELECT COUNT(*) FROM resolved_claims WHERE target_id=?", (tid,))
    contested = _count(store, "SELECT COUNT(*) FROM resolved_claims WHERE target_id=? AND resolution_status='preferred_with_conflict'", (tid,))
    return Evaluation(claims > 0, {"resolved_claims": claims, "contested_claims": contested},
                      _ids(store, "SELECT claim_id FROM resolved_claims WHERE target_id=? ORDER BY claim_id", (tid,)), [] if claims else ["Preferred current claims"])


EVALUATORS: dict[str, Callable[[EvidenceStore, str], Evaluation]] = {
    "jurisdiction": _evaluate_jurisdiction,
    "source_discovery": _evaluate_source_discovery,
    "raw_ingestion": _evaluate_raw,
    "normalization": _evaluate_normalization,
    "parcel_database": _evaluate_parcels,
    "spatial_data": _evaluate_spatial,
    "land_records": _evaluate_land_records,
    "entity_resolution": _evaluate_entities,
    "property_site_assembly": _evaluate_assembly,
    "physical_discovery": _evaluate_physical,
    "asset_classifier": _evaluate_asset,
    "web_intelligence": _evaluate_web,
    "document_pipeline": _evaluate_documents,
    "tenant_engine": _evaluate_tenants,
    "owner_graph": _evaluate_owner_graph,
    "permit_planning": _evaluate_permits,
    "zoning_analysis": _evaluate_zoning,
    "environmental_hazard": _evaluate_environment,
    "infrastructure_data": _evaluate_infrastructure,
    "market_intel": _evaluate_market,
    "event_engine": _evaluate_events,
    "property_timeline": _evaluate_timeline,
    "claim_resolution": _evaluate_claims,
}


def _store_evaluation(store: EvidenceStore, tid: str, stage: StageDefinition,
                      order: int, evaluation: Evaluation) -> dict[str, Any]:
    store.pipeline_stage_state(
        target_id=tid, stage_key=stage.key, stage_order=order, label=stage.label,
        implementation_status="implemented", coverage_status=evaluation.coverage_status,
        dependencies=list(stage.dependencies), metrics=evaluation.metrics,
        evidence_refs=evaluation.evidence_refs,
        missing_requirements=evaluation.missing, contract_version=CONTRACT_VERSION,
    )
    return {"stage": stage.key, "implementation_status": "implemented",
            "coverage_status": evaluation.coverage_status,
            "metrics": evaluation.metrics, "missing_requirements": evaluation.missing}


def _materialize_current_state(store: EvidenceStore, tid: str) -> tuple[str, dict[str, Any]]:
    target = store.db.execute("SELECT target_id,name,address FROM targets WHERE target_id=?", (tid,)).fetchone()
    claims = store.rows(
        "SELECT c.claim_id,c.subject_id,c.predicate,c.resolution_status,c.score,f.value_json,f.unit,f.fact_class,f.confidence,f.source_id "
        "FROM resolved_claims c LEFT JOIN facts f ON f.fact_id=c.preferred_fact_id "
        "WHERE c.target_id=? ORDER BY c.subject_id,c.predicate", (tid,))
    current: dict[str, dict[str, Any]] = {}
    for row in claims:
        subject = current.setdefault(row["subject_id"], {})
        subject[row["predicate"]] = {
            "value": json.loads(row["value_json"]) if row["value_json"] is not None else None,
            "unit": row["unit"], "fact_class": row["fact_class"],
            "confidence": row["confidence"], "source_id": row["source_id"],
            "claim_id": row["claim_id"], "resolution_status": row["resolution_status"],
            "resolution_score": row["score"],
        }
    contradictions = _ids(store, "SELECT contradiction_id FROM contradictions WHERE status='open' ORDER BY contradiction_id")
    stages = {row["stage_key"]: row["coverage_status"] for row in store.rows(
        "SELECT stage_key,coverage_status FROM pipeline_stage_states WHERE target_id=? ORDER BY stage_order", (tid,))}
    state = {
        "target": dict(target) if target else {"target_id": tid},
        "current_claims_by_subject": current,
        "open_contradiction_ids": contradictions,
        "unresolved_gap_ids": _ids(store, "SELECT gap_id FROM gaps WHERE target_id=? AND status!='resolved' ORDER BY gap_id", (tid,)),
        "stage_coverage": stages,
        "graph_counts": {row["entity_type"]: row["n"] for row in store.rows(
            "SELECT entity_type,COUNT(*) n FROM entities GROUP BY entity_type ORDER BY entity_type")},
    }
    snapshot_id = store.property_state_snapshot(
        target_id=tid, state=state, source_claim_ids=[row["claim_id"] for row in claims],
        contradiction_ids=contradictions, stage_coverage=stages,
        resolver_version=STATE_RESOLVER_VERSION,
    )
    return snapshot_id, state


def evaluate_pipeline(store: EvidenceStore, target_id: str) -> list[dict[str, Any]]:
    """Evaluate every semantic stage and materialize the current property state.

    This does not collect sources itself. It is the stable contract between any
    collection tool and the DealSynq property-intelligence output model.
    """
    store.db.execute("DELETE FROM pipeline_stage_states WHERE target_id=?", (target_id,))
    results: list[dict[str, Any]] = []
    for order, stage in enumerate(STAGES, 1):
        if stage.key in {"current_property_state", "property_intelligence_profile"}:
            continue
        evaluation = EVALUATORS[stage.key](store, target_id)
        results.append(_store_evaluation(store, target_id, stage, order, evaluation))
    snapshot_id, state = _materialize_current_state(store, target_id)
    current_stage = next(stage for stage in STAGES if stage.key == "current_property_state")
    current_order = next(index for index, stage in enumerate(STAGES, 1) if stage.key == current_stage.key)
    current_eval = Evaluation(True, {"snapshot_id": snapshot_id, "subjects": len(state["current_claims_by_subject"])},
                              [snapshot_id], [])
    results.append(_store_evaluation(store, target_id, current_stage, current_order, current_eval))
    profile_stage = next(stage for stage in STAGES if stage.key == "property_intelligence_profile")
    profile_order = len(STAGES)
    profile_eval = Evaluation(False, {"status": "pending_report_write"}, [], ["Markdown/JSON profile output"])
    results.append(_store_evaluation(store, target_id, profile_stage, profile_order, profile_eval))
    store.db.commit()
    return results


def record_profile_output(store: EvidenceStore, target_id: str, report_stats: dict[str, Any]) -> dict[str, Any]:
    stage = next(item for item in STAGES if item.key == "property_intelligence_profile")
    evaluation = Evaluation(True, dict(report_stats),
                            [str(report_stats.get("markdown")), str(report_stats.get("json"))], [])
    result = _store_evaluation(store, target_id, stage, len(STAGES), evaluation)
    store.db.commit()
    return result
