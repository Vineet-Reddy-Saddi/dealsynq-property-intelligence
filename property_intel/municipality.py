from __future__ import annotations

import json
import random
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapter_runtime import AdapterContext, builtin_adapters
from .adapters import (arcgis_municipality, ct_registry_municipality,
                       linked_records, municipality_bundle, tabular_municipality)
from .derive import calculate as calculate_metrics
from .derive import fingerprint as derive_fingerprint
from .intelligence import analyze as analyze_intelligence
from .intelligence import fingerprint as intelligence_fingerprint
from .intelligence import plan_searches, search_plan_fingerprint
from .report import write_reports
from .runtime import configure_activation_refresh_policies, run_stage, validate_store
from .stage_engine import evaluate_pipeline, record_profile_output
from .store import EvidenceStore
from .util import normalize_address, normalize_text, stable_id, utcnow


@dataclass(frozen=True)
class BatchEngineDefinition:
    key: str
    label: str
    dependencies: tuple[str, ...]
    capabilities: tuple[str, ...]
    required: bool = True


BATCH_ENGINES = (
    BatchEngineDefinition("assessor", "Assessor", (), ("assessor_records",)),
    BatchEngineDefinition("parcel_gis", "Parcel / GIS", (), ("parcel_geometry", "parcel_identifiers")),
    BatchEngineDefinition("zoning", "Zoning", ("parcel_gis",), ("zoning_gis", "zoning_ordinance")),
    BatchEngineDefinition("owner_entities", "Owners / Entities", ("assessor",), ("entity_registry", "corporate_filings")),
    BatchEngineDefinition("deeds", "Deeds / Land Records", ("parcel_gis", "owner_entities"), ("deeds_land_records", "subdivision_plans", "easements")),
    BatchEngineDefinition("mortgages_liens", "Mortgages / Liens", ("deeds",), ("mortgages_liens",)),
    BatchEngineDefinition("site_assembly", "Property / Site Assembly", ("parcel_gis", "owner_entities", "deeds"), ("property_site_assembly",)),
    BatchEngineDefinition("permits_planning", "Permits / Planning", ("parcel_gis",), ("building_permits", "planning_applications", "zoning_variances", "certificates_of_occupancy")),
    BatchEngineDefinition("hazards", "Environmental / Hazards", ("parcel_gis",), ("environmental_hazards",)),
    BatchEngineDefinition("infrastructure", "Infrastructure", ("parcel_gis",), ("roads_access_frontage", "infrastructure", "utilities_water_sewer"), required=False),
)

ON_DEMAND_ADAPTERS = {
    "web_intelligence", "documents", "listings_csv", "canonical_bundle",
    "national_public", "arcgis_context", "ct_registry",
}
BATCH_ADAPTERS = {
    "municipality_bundle": municipality_bundle,
    "tabular_municipality": tabular_municipality,
    "linked_records": linked_records,
    "arcgis_municipality": arcgis_municipality,
    "ct_registry_municipality": ct_registry_municipality,
}


def _profile_has(profile: dict[str, Any], section: str, *names: str) -> bool:
    values = profile.get(section) or {}
    return any(int(values.get(name, 0) or 0) > 0 for name in names)


def _validate_engine_output(engine_key: str, profile: dict[str, Any]) -> list[str]:
    """Return unmet minimum evidence requirements for a batch engine.

    This is deliberately a minimum software-output contract, not a jurisdiction
    completeness test. Completeness still requires a separately documented
    publisher/row-count basis in configuration.
    """
    checks: dict[str, list[tuple[bool, str]]] = {
        "assessor": [
            (_profile_has(profile, "entity_types", "parcel"), "parcel entities"),
            (_profile_has(profile, "fact_categories", "assessor", "tax"),
             "assessor or tax facts"),
        ],
        "parcel_gis": [
            (_profile_has(profile, "entity_types", "parcel"), "parcel entities"),
            (_profile_has(profile, "alias_types", "parcel_identifier"),
             "jurisdiction-scoped parcel identifiers"),
            (_profile_has(profile, "fact_predicates", "parcel_geometry", "analysis_geometry"),
             "parcel/site geometry"),
        ],
        "zoning": [
            (_profile_has(profile, "fact_categories", "zoning") or
             _profile_has(profile, "fact_predicates", "assessor_zoning_code", "zoning_code"),
             "zoning observations"),
        ],
        "owner_entities": [
            (_profile_has(profile, "entity_types", "organization", "person"),
             "owner/entity records"),
            (_profile_has(profile, "relationship_types", "assessor_owner_of", "owns", "owns_or_owned"),
             "ownership relationships"),
        ],
        "deeds": [
            (_profile_has(profile, "entity_types", "recorded_document", "transaction"),
             "recorded-document or transaction entities"),
            (_profile_has(profile, "relationship_types", "affects", "conveys", "grantor_of", "grantee_of"),
             "deed-to-parcel/party relationships"),
        ],
        "mortgages_liens": [
            (_profile_has(profile, "entity_types", "recorded_document", "lien", "ucc_lien_record"),
             "mortgage/lien document entities"),
            (_profile_has(profile, "relationship_types", "encumbers", "secures", "borrower_of", "lender_of", "named_debtor_in"),
             "mortgage/lien relationships"),
        ],
        "site_assembly": [
            (_profile_has(profile, "entity_types", "property_site"), "property-site entities"),
            (int(profile.get("memberships", 0) or 0) > 0, "explainable parcel memberships"),
        ],
        "permits_planning": [
            (_profile_has(profile, "entity_types", "permit", "development_case"),
             "permit or planning-case entities"),
            (_profile_has(profile, "relationship_types", "affects", "applies_to", "located_at"),
             "permit/case-to-property relationships"),
        ],
        "hazards": [
            (_profile_has(profile, "fact_categories", "hazards", "environmental"),
             "hazard or environmental facts"),
        ],
        "infrastructure": [
            (_profile_has(profile, "fact_categories", "access", "infrastructure", "utilities"),
             "access, infrastructure, or utility facts"),
        ],
    }
    return [label for passed, label in checks.get(engine_key, []) if not passed]


def _scope_output_profile(store: EvidenceStore, scope_id: str) -> dict[str, Any]:
    linked = (
        "SELECT DISTINCT l.entity_id FROM property_entity_links l "
        "JOIN property_index p ON p.property_id=l.property_id WHERE p.scope_id=?"
    )

    def grouped(sql: str) -> dict[str, int]:
        return {str(row[0]): int(row[1]) for row in store.rows(sql, (scope_id,))}

    return {
        "entity_types": grouped(
            f"SELECT e.entity_type,COUNT(*) FROM entities e WHERE e.entity_id IN ({linked}) GROUP BY e.entity_type"),
        "fact_categories": grouped(
            f"SELECT f.category,COUNT(*) FROM facts f WHERE f.status='current' AND f.subject_id IN ({linked}) GROUP BY f.category"),
        "fact_predicates": grouped(
            f"SELECT f.predicate,COUNT(*) FROM facts f WHERE f.status='current' AND f.subject_id IN ({linked}) GROUP BY f.predicate"),
        "relationship_types": grouped(
            f"SELECT r.relationship_type,COUNT(*) FROM relationships r WHERE r.from_entity_id IN ({linked}) GROUP BY r.relationship_type"),
        "property_roles": grouped(
            "SELECT l.role,COUNT(*) FROM property_entity_links l JOIN property_index p ON p.property_id=l.property_id WHERE p.scope_id=? GROUP BY l.role"),
        "alias_types": grouped(
            f"SELECT a.alias_type,COUNT(*) FROM entity_aliases a WHERE a.entity_id IN ({linked}) GROUP BY a.alias_type"),
        "memberships": int(store.db.execute(
            "SELECT COUNT(*) FROM grouping_decisions g JOIN property_index p ON p.property_id=g.target_id WHERE p.scope_id=?",
            (scope_id,),
        ).fetchone()[0]),
    }


def _resolve_paths(value: Any, base: Path) -> Any:
    if isinstance(value, dict):
        resolved = {key: _resolve_paths(item, base) for key, item in value.items()}
        for key in ("path", "database", "output_root", "coverage_report"):
            if key in resolved and isinstance(resolved[key], str):
                path = Path(resolved[key])
                if not path.is_absolute():
                    resolved[key] = str((base / path).resolve())
        if "paths" in resolved and isinstance(resolved["paths"], list):
            resolved["paths"] = [
                str((base / item).resolve()) if not Path(item).is_absolute() else str(Path(item))
                for item in resolved["paths"]
            ]
        return resolved
    if isinstance(value, list):
        return [_resolve_paths(item, base) for item in value]
    return value


def load_scope_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).resolve()
    config = _resolve_paths(json.loads(config_path.read_text(encoding="utf-8")), config_path.parent)
    scope = config.get("scope") or {}
    missing = [key for key in ("name", "scope_type", "jurisdiction_id") if not scope.get(key)]
    if missing:
        raise ValueError(f"Scope config missing: {', '.join(missing)}")
    if scope["scope_type"] not in {"municipality", "county", "recording_district", "state"}:
        raise ValueError("scope_type must be municipality, county, recording_district, or state")
    scope["id"] = scope.get("id") or stable_id(
        "scope", scope["scope_type"], scope["jurisdiction_id"], scope["name"])
    config["scope"] = scope
    output_root = Path(config.get("output_root") or config_path.parent.parent)
    config["output_root"] = str(output_root.resolve())
    config["database"] = str(Path(config.get("database") or
                                  output_root / "data" / f"{scope['id']}.sqlite").resolve())
    config["coverage_report"] = str(Path(config.get("coverage_report") or
                                         output_root / "reports" / f"{scope['id']}_coverage.json").resolve())
    config["_config_path"] = str(config_path)
    return config


def _engine_config(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = {}
    valid = {engine.key for engine in BATCH_ENGINES}
    for item in config.get("batch_engines", []):
        key = item.get("key")
        if key not in valid:
            raise ValueError(f"Unknown batch engine {key!r}; expected one of {sorted(valid)}")
        if key in items:
            raise ValueError(f"Duplicate batch engine config: {key}")
        items[key] = item
    return items


def _run_batch_engine(store: EvidenceStore, scope: dict[str, Any],
                      definition: BatchEngineDefinition, item: dict[str, Any],
                      *, force: bool,
                      profile_cache: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    required = bool(item.get("required", definition.required))
    coverage_from = item.get("coverage_from")
    if coverage_from:
        if coverage_from not in {engine.key for engine in BATCH_ENGINES}:
            raise ValueError(f"Unknown coverage_from engine {coverage_from!r}")
        upstream = store.db.execute(
            "SELECT coverage_status,adapter,metrics_json FROM scope_engine_states "
            "WHERE scope_id=? AND engine_key=?",
            (scope["id"], coverage_from),
        ).fetchone()
        if not upstream or upstream["coverage_status"] not in {"complete", "partial"}:
            raise ValueError(
                f"Batch engine {definition.key!r} cannot reuse {coverage_from!r}; "
                "the upstream engine has no successful evidence state"
            )
        upstream_metrics = json.loads(upstream["metrics_json"] or "{}")
        upstream_profile = upstream_metrics.get("output_profile")
        if upstream_profile:
            # A successful upstream state already stores the exact contract
            # profile produced by that adapter. Reusing it avoids rescanning a
            # jurisdiction-scale facts graph merely to validate coverage_from.
            profile = upstream_profile
        elif profile_cache is not None and scope["id"] in profile_cache:
            profile = profile_cache[scope["id"]]
        else:
            profile = _scope_output_profile(store, scope["id"])
            if profile_cache is not None:
                profile_cache[scope["id"]] = profile
        missing_output = _validate_engine_output(definition.key, profile)
        if missing_output:
            raise ValueError(
                f"Batch engine {definition.key!r} output contract failed while reusing "
                f"{coverage_from!r}: missing {', '.join(missing_output)}"
            )
        coverage = item.get("coverage_status", "partial")
        if coverage not in {"complete", "partial"}:
            raise ValueError("coverage_from coverage_status must be complete or partial")
        completion_basis = item.get("completion_basis")
        if coverage == "complete" and not completion_basis:
            raise ValueError(
                f"Batch engine {definition.key!r} claims complete coverage without completion_basis"
            )
        metrics = {"coverage_from": coverage_from, "output_profile": profile,
                   "output_contract": "passed"}
        store.scope_engine_state(
            scope_id=scope["id"], engine_key=definition.key,
            execution_mode="batch", coverage_status=coverage,
            required=required, adapter=f"reuse:{coverage_from}:{upstream['adapter']}",
            dependencies=list(definition.dependencies), metrics=metrics,
            reason=item.get("reason") or completion_basis,
        )
        store.db.commit()
        return {"engine": definition.key, "status": "reused_output",
                "coverage_status": coverage, "stats": metrics}

    explicit_status = item.get("status")
    if explicit_status:
        if explicit_status not in {"missing", "blocked", "partial", "not_applicable"}:
            raise ValueError(f"Explicit batch status cannot be {explicit_status!r}; successful coverage requires an adapter run")
        store.scope_engine_state(
            scope_id=scope["id"], engine_key=definition.key,
            execution_mode="batch", coverage_status=explicit_status,
            required=required, adapter=None, dependencies=list(definition.dependencies),
            reason=item.get("reason"),
        )
        store.db.commit()
        return {"engine": definition.key, "status": explicit_status, "reason": item.get("reason")}

    adapter_key = item.get("adapter")
    if adapter_key not in BATCH_ADAPTERS:
        raise ValueError(
            f"Unknown batch adapter {adapter_key!r}; available={sorted(BATCH_ADAPTERS)}"
        )
    adapter = BATCH_ADAPTERS[adapter_key]
    adapter_config = item.get("config", {})
    input_hash = stable_id(
        "scope-input", definition.key,
        adapter_key, adapter.fingerprint(adapter_config),
        bool(item.get("required", definition.required)),
        item.get("coverage_status", "partial"),
        item.get("completion_basis"), item.get("reason"),
    )
    if not force and store.latest_scope_success_hash(scope["id"], definition.key) == input_hash:
        prior = store.db.execute(
            "SELECT coverage_status,metrics_json FROM scope_engine_states WHERE scope_id=? AND engine_key=?",
            (scope["id"], definition.key),
        ).fetchone()
        return {
            "engine": definition.key, "status": "skipped_unchanged",
            "coverage_status": prior["coverage_status"] if prior else "complete",
            "input_hash": input_hash,
        }

    run_id = store.begin_scope_run(scope["id"], definition.key, input_hash)
    try:
        with store.transaction():
            stats = adapter.collect(store, scope, adapter_config)
            if profile_cache is not None:
                # A collector may have changed any part of the scope graph, so
                # later coverage_from contracts must not reuse a stale profile.
                profile_cache.pop(scope["id"], None)
            profile = stats.get("output_profile") or _scope_output_profile(store, scope["id"])
            missing_output = _validate_engine_output(definition.key, profile)
            if missing_output:
                raise ValueError(
                    f"Batch engine {definition.key!r} output contract failed: missing "
                    f"{', '.join(missing_output)}"
                )
            stats["output_contract"] = "passed"
        # Successful parsing proves that the adapter ran, not that the source
        # covered an entire jurisdiction. Completion must be an explicit,
        # reviewable assertion; otherwise the honest default is partial.
        coverage = item.get("coverage_status", "partial")
        if coverage not in {"complete", "partial"}:
            raise ValueError("coverage_status after a successful adapter run must be complete or partial")
        completion_basis = item.get("completion_basis")
        if coverage == "complete" and not completion_basis:
            raise ValueError(
                f"Batch engine {definition.key!r} claims complete coverage without completion_basis"
            )
        store.finish_scope_run(run_id, "success", stats)
        store.scope_engine_state(
            scope_id=scope["id"], engine_key=definition.key,
            execution_mode="batch", coverage_status=coverage,
            required=required, adapter=adapter_key,
            dependencies=list(definition.dependencies), metrics=stats,
            reason=item.get("reason") or completion_basis,
        )
        store.db.commit()
        return {"engine": definition.key, "status": "success",
                "coverage_status": coverage, "stats": stats, "input_hash": input_hash}
    except Exception as exc:
        error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        store.finish_scope_run(run_id, "failed", error=error)
        store.scope_engine_state(
            scope_id=scope["id"], engine_key=definition.key,
            execution_mode="batch", coverage_status="blocked",
            required=required, adapter=adapter_key,
            dependencies=list(definition.dependencies), reason=error,
        )
        store.db.commit()
        if required:
            raise
        return {"engine": definition.key, "status": "failed_optional", "error": error}


def scope_status(store: EvidenceStore, scope_id: str) -> dict[str, Any]:
    scope = store.db.execute("SELECT * FROM collection_scopes WHERE scope_id=?", (scope_id,)).fetchone()
    if not scope:
        raise ValueError(f"Unknown collection scope {scope_id}")
    states = [dict(row) for row in store.rows(
        "SELECT * FROM scope_engine_states WHERE scope_id=? ORDER BY rowid", (scope_id,))]
    required_incomplete = [
        row["engine_key"] for row in states
        if row["required"] and row["coverage_status"] not in {"complete", "not_applicable"}
    ]
    configured_keys = {row["engine_key"] for row in states}
    required_incomplete.extend(
        engine.key for engine in BATCH_ENGINES
        if engine.required and engine.key not in configured_keys
    )
    counts = {
        "properties": store.db.execute(
            "SELECT COUNT(*) FROM property_index WHERE scope_id=?", (scope_id,)).fetchone()[0],
        "property_entity_links": store.db.execute(
            "SELECT COUNT(*) FROM property_entity_links l JOIN property_index p ON p.property_id=l.property_id WHERE p.scope_id=?",
            (scope_id,)).fetchone()[0],
        "entities": store.db.execute(
            "SELECT COUNT(DISTINCT l.entity_id) FROM property_entity_links l JOIN property_index p ON p.property_id=l.property_id WHERE p.scope_id=?",
            (scope_id,)).fetchone()[0],
    }
    return {
        "scope": dict(scope), "batch_engines": states, "counts": counts,
        "complete": not required_incomplete,
        "required_incomplete": sorted(set(required_incomplete)),
        "definition": (
            "Complete means every required batch engine has a complete or not-applicable outcome; "
            "blocked and unavailable sources remain explicit."
        ),
    }


def precompute(config_path: str | Path, *, force: bool = False) -> dict[str, Any]:
    config = load_scope_config(config_path)
    scope = config["scope"]
    store = EvidenceStore(config["database"])
    try:
        with store.transaction():
            store.upsert_collection_scope(
                scope["id"], scope["scope_type"], scope["jurisdiction_id"],
                scope["name"], config, state_code=scope.get("state_code"),
                parent_scope_id=scope.get("parent_scope_id"),
            )
            store.entity(
                "jurisdiction", scope["name"], external_id=scope["jurisdiction_id"],
                attributes={"scope_type": scope["scope_type"], "state_code": scope.get("state_code")},
                entity_id=scope["id"],
            )
        configured = _engine_config(config)
        results = []
        profile_cache: dict[str, dict[str, Any]] = {}
        for definition in BATCH_ENGINES:
            item = configured.get(definition.key)
            if item is None:
                item = {"key": definition.key, "status": "missing",
                        "required": definition.required,
                        "reason": "No municipality-wide source adapter configured"}
            results.append(_run_batch_engine(
                store, scope, definition, item, force=force,
                profile_cache=profile_cache,
            ))
        status = scope_status(store, scope["id"])
        report_path = Path(config["coverage_report"])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"scope_id": scope["id"], "engines": results,
                "coverage_report": str(report_path), **status}
    finally:
        store.close()


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def _rows_for_ids(store: EvidenceStore, table: str, column: str,
                  ids: set[str]) -> list[Any]:
    if not ids:
        return []
    result = []
    values = sorted(ids)
    for start in range(0, len(values), 800):
        part = values[start:start + 800]
        result.extend(store.rows(
            f"SELECT * FROM {table} WHERE {column} IN ({_placeholders(len(part))})",
            tuple(part),
        ))
    return result


def _insert_rows(store: EvidenceStore, table: str, rows: list[Any]) -> None:
    if not rows:
        return
    columns = list(rows[0].keys())
    sql = (f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES "
           f"({_placeholders(len(columns))})")
    store.db.executemany(sql, [tuple(row[column] for column in columns) for row in rows])


def lookup_property(store: EvidenceStore, scope_id: str, *, address: str | None = None,
                    parcel_id: str | None = None, name: str | None = None) -> dict[str, Any]:
    candidates: dict[str, dict[str, Any]] = {}
    if address:
        normalized = normalize_address(address)
        street_key = normalize_address(address.split(",")[0])
        for row in store.rows(
            "SELECT p.*,'property_address' match_basis FROM property_index p "
            "WHERE p.scope_id=? AND p.normalized_address=?",
            (scope_id, normalized),
        ):
            candidates[row["property_id"]] = dict(row)
        if not candidates:
            for row in store.rows(
                "SELECT DISTINCT p.*,'parcel_situs_alias' match_basis FROM property_index p "
                "JOIN property_entity_links l ON l.property_id=p.property_id "
                "JOIN entity_aliases a ON a.entity_id=l.entity_id "
                "WHERE p.scope_id=? AND a.alias_type='situs_address' AND a.normalized_value=?",
                (scope_id, normalized),
            ):
                candidates[row["property_id"]] = dict(row)
        if not candidates:
            # Assessor/GIS publishers often store only the situs street while a
            # user enters a full postal address. The collection scope already
            # fixes the municipality, so a street-only comparison is safe as
            # long as the result is still subject to ambiguity detection.
            for row in store.rows(
                "SELECT p.*,'property_street_address' match_basis FROM property_index p WHERE p.scope_id=?",
                (scope_id,),
            ):
                if normalize_address(row["address"].split(",")[0]) == street_key:
                    candidates[row["property_id"]] = dict(row)
        if not candidates:
            for row in store.rows(
                "SELECT DISTINCT p.*,a.raw_value,'parcel_situs_street' match_basis "
                "FROM property_index p JOIN property_entity_links l ON l.property_id=p.property_id "
                "JOIN entity_aliases a ON a.entity_id=l.entity_id "
                "WHERE p.scope_id=? AND a.alias_type='situs_address'",
                (scope_id,),
            ):
                if normalize_address(row["raw_value"].split(",")[0]) == street_key:
                    candidates[row["property_id"]] = dict(row)
    if parcel_id:
        parcel_key = normalize_text(parcel_id)
        for row in store.rows(
            "SELECT DISTINCT p.*,'parcel_identifier' match_basis FROM property_index p "
            "JOIN property_entity_links l ON l.property_id=p.property_id "
            "JOIN entities e ON e.entity_id=l.entity_id "
            "LEFT JOIN entity_aliases a ON a.entity_id=e.entity_id AND a.alias_type='parcel_identifier' "
            "WHERE p.scope_id=? AND e.entity_type='parcel' "
            "AND (UPPER(COALESCE(e.external_id,''))=? OR a.normalized_value=?)",
            (scope_id, parcel_id.upper(), parcel_key),
        ):
            candidates[row["property_id"]] = dict(row)
    if name and not candidates:
        name_key = normalize_text(name)
        # Canonical names may contain punctuation (for example, "Shops @ 5th").
        # Apply the same normalizer used for the query instead of relying on a
        # SQL UPPER comparison, which would leave punctuation significant.
        for row in store.rows(
            "SELECT p.*,'property_name' match_basis FROM property_index p WHERE p.scope_id=?",
            (scope_id,),
        ):
            if normalize_text(row["canonical_name"]) == name_key:
                candidates[row["property_id"]] = dict(row)
    if not candidates:
        raise ValueError("No precomputed property matched the supplied address, parcel ID, or name")
    if len(candidates) > 1:
        summaries = [{"property_id": row["property_id"], "name": row["canonical_name"],
                      "address": row["address"]} for row in candidates.values()]
        raise ValueError(f"Property lookup is ambiguous; candidates={summaries}")
    return next(iter(candidates.values()))


def _materialize_property(source: EvidenceStore, destination: EvidenceStore,
                          property_row: dict[str, Any], scope: dict[str, Any],
                          activation_config: dict[str, Any]) -> None:
    property_id = property_row["property_id"]
    linked = {row[0] for row in source.rows(
        "SELECT entity_id FROM property_entity_links WHERE property_id=?", (property_id,))}
    linked.add(property_id)

    entities = _rows_for_ids(source, "entities", "entity_id", linked)
    aliases = _rows_for_ids(source, "entity_aliases", "entity_id", linked)
    facts = _rows_for_ids(source, "facts", "subject_id", linked)
    relationships = []
    if linked:
        values = sorted(linked)
        # Query one endpoint in chunks, then filter the other endpoint locally.
        # Requiring both endpoints to be in the same chunk would silently drop
        # cross-chunk edges for large properties.
        for start in range(0, len(values), 800):
            part = values[start:start + 800]
            relationships.extend(source.rows(
                f"SELECT * FROM relationships WHERE from_entity_id IN ({_placeholders(len(part))})",
                tuple(part),
            ))
        relationships = [row for row in relationships if row["to_entity_id"] in linked]

    documents = source.rows("SELECT * FROM documents WHERE target_id=?", (property_id,))
    doc_ids = {row["document_id"] for row in documents}
    mentions = _rows_for_ids(source, "document_mentions", "document_id", doc_ids)
    temporal = source.rows("SELECT * FROM temporal_states WHERE target_id=?", (property_id,))
    events = source.rows("SELECT * FROM events WHERE target_id=?", (property_id,))
    gaps = source.rows("SELECT * FROM gaps WHERE target_id=?", (property_id,))
    grouping = source.rows("SELECT * FROM grouping_decisions WHERE target_id=?", (property_id,))

    # Carry municipality-wide same-owner context into the local activation
    # without copying every fact for every affiliated parcel.  These remain
    # candidates until title/entity adjudication confirms common control.
    owner_ids = [row[0] for row in source.rows(
        "SELECT DISTINCT r.from_entity_id FROM relationships r "
        "WHERE r.relationship_type='assessor_owner_of' AND r.to_entity_id IN "
        f"({_placeholders(len(linked))})",
        tuple(sorted(linked)),
    )] if linked else []
    portfolio_rows = []
    if owner_ids:
        portfolio_rows = source.rows(
            "SELECT DISTINCT r.from_entity_id owner_id,pi.property_id,pi.canonical_name,"
            "pi.address,r.source_id,r.raw_sha256 FROM relationships r "
            "JOIN property_entity_links l ON l.entity_id=r.to_entity_id "
            "JOIN property_index pi ON pi.property_id=l.property_id "
            "WHERE r.relationship_type='assessor_owner_of' "
            f"AND r.from_entity_id IN ({_placeholders(len(owner_ids))}) "
            "AND pi.property_id<>? ORDER BY pi.address,pi.property_id",
            tuple(owner_ids) + (property_id,),
        )

    source_ids = {row["source_id"] for row in facts + relationships + documents + temporal
                  if row["source_id"]}
    source_ids.update(row["source_id"] for row in aliases if row["source_id"])
    source_ids.update(row["source_id"] for row in portfolio_rows if row["source_id"])
    for row in source.rows("SELECT source_id FROM property_entity_links WHERE property_id=? AND source_id IS NOT NULL",
                           (property_id,)):
        source_ids.add(row[0])
    for event in events:
        source_ids.update(json.loads(event["source_ids_json"]))
    sources = _rows_for_ids(source, "sources", "source_id", source_ids)
    raw_ids = {row["raw_sha256"] for row in sources + facts + relationships + documents
               if "raw_sha256" in row.keys() and row["raw_sha256"]}
    raw = _rows_for_ids(source, "raw_evidence", "raw_sha256", raw_ids)

    target = {"name": property_row["canonical_name"], "address": property_row["address"]}
    activation_config = {
        **activation_config,
        "target": target,
        "_source_resolution": {"jurisdiction_id": scope["jurisdiction_id"]},
    }
    with destination.transaction():
        _insert_rows(destination, "raw_evidence", raw)
        _insert_rows(destination, "sources", sources)
        _insert_rows(destination, "entities", entities)
        _insert_rows(destination, "entity_aliases", aliases)
        _insert_rows(destination, "facts", facts)
        _insert_rows(destination, "relationships", relationships)
        stale_portfolio_ids = [row[0] for row in destination.rows(
            "SELECT entity_id FROM entities WHERE entity_type='portfolio_property_candidate'"
        )]
        destination.db.execute(
            "DELETE FROM relationships WHERE relationship_type='candidate_owner_of'"
        )
        if stale_portfolio_ids:
            destination.db.execute(
                f"DELETE FROM entities WHERE entity_id IN ({_placeholders(len(stale_portfolio_ids))})",
                tuple(stale_portfolio_ids),
            )
        for candidate in portfolio_rows:
            candidate_id = stable_id(
                "portfolio-property", candidate["owner_id"], candidate["property_id"]
            )
            destination.entity(
                "portfolio_property_candidate", candidate["canonical_name"],
                external_id=candidate["property_id"], entity_id=candidate_id,
                attributes={
                    "address": candidate["address"],
                    "candidate_basis": "exact municipality assessor-owner entity",
                    "limitation": "Same assessor owner name is a portfolio lead, not proof of current common beneficial control.",
                },
            )
            destination.relationship(
                from_id=candidate["owner_id"], relationship_type="candidate_owner_of",
                to_id=candidate_id, fact_class="confirmed_official", confidence=0.75,
                source_id=candidate["source_id"], parser_version="municipality-portfolio/1.0.0",
                raw_sha256=candidate["raw_sha256"],
                explanation={
                    "basis": "exact shared assessor-owner entity in the Stamford precomputed scope",
                    "source_property_id": candidate["property_id"],
                    "limitation": "Candidate relationship requires title/entity adjudication.",
                },
            )
        # The municipality cache materializes the current logical membership
        # set. Parser upgrades can change decision IDs, so replace the target's
        # prior batch decisions instead of accumulating stale memberships in a
        # persistent activation database.
        destination.db.execute(
            "DELETE FROM grouping_decisions WHERE target_id=?", (property_id,)
        )
        _insert_rows(destination, "grouping_decisions", grouping)
        _insert_rows(destination, "documents", documents)
        _insert_rows(destination, "document_mentions", mentions)
        _insert_rows(destination, "temporal_states", temporal)
        _insert_rows(destination, "events", events)
        _insert_rows(destination, "gaps", gaps)
        destination.upsert_target(property_id, target["name"], target["address"], activation_config)
        # The batch record may contain richer site attributes. Only synthesize
        # the site entity if a collector did not provide one.
        if not destination.db.execute(
            "SELECT 1 FROM entities WHERE entity_id=?", (property_id,)
        ).fetchone():
            destination.entity("property_site", target["name"], entity_id=property_id,
                               attributes={"address": target["address"]})
        for state in source.rows(
            "SELECT * FROM scope_engine_states WHERE scope_id=?", (scope["scope_id"],)):
            definition = next(item for item in BATCH_ENGINES if item.key == state["engine_key"])
            status = "configured" if state["coverage_status"] == "complete" else state["coverage_status"]
            for capability in definition.capabilities:
                destination.register_capability(
                    property_id, scope["jurisdiction_id"],
                    {"capability": capability, "status": status,
                     "source_name": state["adapter"], "adapter": state["adapter"],
                     "reason": state["reason"]},
                    "municipality-batch/1.0.0",
                )


def _format_property(value: Any, property_row: dict[str, Any]) -> Any:
    variables = {
        "$property.id": property_row["property_id"],
        "$property.name": property_row["canonical_name"],
        "$property.address": property_row["address"],
    }
    if isinstance(value, str):
        for token, replacement in variables.items():
            value = value.replace(token, str(replacement))
        return value
    if isinstance(value, dict):
        return {key: _format_property(item, property_row) for key, item in value.items()}
    if isinstance(value, list):
        return [_format_property(item, property_row) for item in value]
    return value


def activate_property(config_path: str | Path, *, address: str | None = None,
                      parcel_id: str | None = None, name: str | None = None,
                      output_root: str | Path | None = None, force: bool = False,
                      skip_live: bool = False) -> dict[str, Any]:
    config = load_scope_config(config_path)
    source = EvidenceStore(config["database"])
    try:
        property_row = lookup_property(source, config["scope"]["id"], address=address,
                                       parcel_id=parcel_id, name=name)
        property_id = property_row["property_id"]
        root = Path(output_root or config["output_root"]) / "activations"
        root.mkdir(parents=True, exist_ok=True)
        readable = normalize_address(property_row["canonical_name"]).lower().replace(" ", "_")
        # A municipality can contain duplicate property names. Keep output
        # paths readable while making collisions impossible.
        slug = f"{readable[:72] or 'property'}_{property_id[-8:]}"
        database = root / "data" / f"{slug}.sqlite"
        markdown = root / "reports" / f"{slug}.md"
        report_json = root / "reports" / f"{slug}.json"
        activation_config = {
            "database": str(database),
            "reports": {"markdown": str(markdown), "json": str(report_json)},
            "on_demand_engines": config.get("on_demand_engines", []),
            "activation_source_scope": config["scope"]["id"],
            "derived_metrics": config.get("derived_metrics", {}),
        }
        destination = EvidenceStore(database)
        try:
            scope_row = source.db.execute(
                "SELECT * FROM collection_scopes WHERE scope_id=?", (config["scope"]["id"],)).fetchone()
            if not scope_row:
                raise ValueError("Run precompute before activating a property")
            _materialize_property(source, destination, property_row, dict(scope_row), activation_config)

            target = {"name": property_row["canonical_name"], "address": property_row["address"]}
            adapters = builtin_adapters()
            context = AdapterContext(store=destination, target_id=property_id, target=target)
            stages: list[dict[str, Any]] = []
            stages.append(run_stage(
                destination, property_id, "web_search_plan",
                search_plan_fingerprint(destination, property_id),
                lambda: plan_searches(destination, property_id), force=force,
            ))
            mapped_config = dict(activation_config)
            for index, raw_item in enumerate(config.get("on_demand_engines", []), 1):
                item = _format_property(raw_item, property_row)
                adapter_key = item["adapter"]
                if adapter_key not in ON_DEMAND_ADAPTERS:
                    raise ValueError(
                        f"Adapter {adapter_key!r} is not allowed in on-demand activation; "
                        f"allowed={sorted(ON_DEMAND_ADAPTERS)}"
                    )
                stage_key = item.get("key") or f"{adapter_key}:{index}"
                adapter_config = item.get("config", {})
                if skip_live and item.get("live", adapter_key in {"web_intelligence", "documents"}):
                    destination.gap(property_id, stage_key, "missing",
                                    "On-demand live engine skipped by command option")
                    destination.db.commit()
                    stages.append({"stage": stage_key, "status": "skipped_live"})
                    continue
                adapter = adapters.get(adapter_key)
                stages.append(run_stage(
                    destination, property_id, stage_key,
                    adapter.fingerprint(context, adapter_config),
                    lambda a=adapter, cfg=adapter_config: a.collect(context, cfg),
                    force=force, required=item.get("required", False),
                ))
                config_key = {
                    "web_intelligence": "web_intelligence",
                    "documents": "documents",
                    "listings_csv": "market_listings",
                    "national_public": "national_public",
                    "arcgis_context": "municipal_arcgis",
                    "ct_registry": "ct_registry",
                }.get(adapter_key)
                if config_key:
                    mapped_config[config_key] = adapter_config

            stages.append(run_stage(
                destination, property_id, "derived_metrics",
                derive_fingerprint(destination, property_id, config.get("derived_metrics", {})),
                lambda: calculate_metrics(destination, property_id, config.get("derived_metrics", {})), force=force,
            ))
            stages.append(run_stage(
                destination, property_id, "intelligence_resolution",
                intelligence_fingerprint(destination, property_id),
                lambda: analyze_intelligence(destination, property_id), force=force,
            ))
            contradictions = destination.detect_contradictions()
            configure_activation_refresh_policies(destination, property_id, mapped_config)
            semantic_stages = evaluate_pipeline(destination, property_id)
            report_stats = write_reports(destination, property_id, markdown, report_json)
            profile_stage = record_profile_output(destination, property_id, report_stats)
            report_stats = write_reports(destination, property_id, markdown, report_json)
            validation = validate_store(destination, property_id)
            return {
                "property_id": property_id,
                "match_basis": property_row.get("match_basis"),
                "precomputed_scope_id": config["scope"]["id"],
                "database": str(database), "reports": report_stats,
                "on_demand_stages": stages, "contradictions": contradictions,
                "semantic_stages": semantic_stages[:-1] + [profile_stage],
                "validation": validation, "activated_at": utcnow(),
            }
        finally:
            destination.close()
    finally:
        source.close()


def validate_sample(config_path: str | Path, *, seed: int | None = None,
                    require_geometry: bool = True, force: bool = False,
                    skip_live: bool = False) -> dict[str, Any]:
    """Select a reproducible random precomputed property and activate it.

    Selection is entirely data-driven within the configured scope. It contains
    no municipality, address, parcel, owner, or property-name special case.
    """
    config = load_scope_config(config_path)
    store = EvidenceStore(config["database"])
    try:
        sql = (
            "SELECT p.* FROM property_index p WHERE p.scope_id=? "
            "AND EXISTS (SELECT 1 FROM grouping_decisions g WHERE g.target_id=p.property_id AND g.included=1)"
        )
        if require_geometry:
            sql += (
                " AND EXISTS (SELECT 1 FROM facts f WHERE f.subject_id=p.property_id "
                "AND f.predicate='analysis_geometry' AND f.status='current')"
            )
        candidates = [dict(row) for row in store.rows(sql + " ORDER BY p.property_id", (config["scope"]["id"],))]
        if not candidates:
            raise ValueError("No precomputed properties satisfy the sample-validation requirements")
        selected = random.Random(seed).choice(candidates)
    finally:
        store.close()
    result = activate_property(
        config_path, address=selected["address"], force=force, skip_live=skip_live)
    return {"sample_seed": seed, "candidate_count": len(candidates),
            "selected": {"property_id": selected["property_id"],
                         "name": selected["canonical_name"],
                         "address": selected["address"]},
            "activation": result}


def status_from_config(config_path: str | Path) -> dict[str, Any]:
    config = load_scope_config(config_path)
    store = EvidenceStore(config["database"])
    try:
        return scope_status(store, config["scope"]["id"])
    finally:
        store.close()
