from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .store import EvidenceStore
from .util import normalize_text, stable_id, utcnow

VERSION = "property-intelligence-resolver/1.4.2"
SEARCH_PLAN_VERSION = "property-search-plan/1.0.0"

TAXONOMY = {
    "retail": {
        "shopping_center": {
            "neighborhood_center": ["SHOPPING CENTER", "NEIGHBORHOOD CENTER", "SUPERMARKET", "GROCERY"],
            "community_center": ["COMMUNITY CENTER", "POWER CENTER", "BIG BOX"],
            "regional_center": ["REGIONAL MALL", "REGIONAL CENTER", "SHOPPING MALL"],
            "strip_center": ["STRIP CENTER", "RETAIL STRIP", "STRIP RETAIL"],
        },
        "large_format": {
            "discount_department_store": ["DISCOUNT STORE", "JUNIOR DEPARTMENT STORE", "DEPARTMENT STORE"],
        },
        "single_tenant": {"freestanding_retail": ["FREESTANDING", "RETAIL STORE"]},
        "general": {"general_retail": ["RETAIL"]},
    },
    "industrial": {"warehouse": {"distribution": ["WAREHOUSE", "DISTRIBUTION", "LOGISTICS"]},
                   "manufacturing": {"general": ["MANUFACTURING", "FACTORY"]}},
    "office": {"general_office": {"multi_tenant": ["OFFICE", "MEDICAL OFFICE"]}},
    "multifamily": {"apartments": {"market_rate": ["APARTMENT", "MULTIFAMILY", "MULTI FAMILY", "3 FAMILY", "THREE FAMILY"]}},
    "residential": {
        "single_family": {"detached": ["SINGLE FAMILY", "ONE FAMILY", "DETACHED HOUSE"]},
        "two_family": {"duplex": ["TWO FAMILY", "TWO-HOUSEHOLD", "DUPLEX"]},
        "condominium": {"residential_condominium": ["CONDOMINIUM", "CONDO"]},
    },
    "hospitality": {"lodging": {"hotel": ["HOTEL", "MOTEL"]}},
    "mixed_use": {"commercial_residential": {"mixed_use": ["MIXED USE"]}},
}


def fingerprint(store: EvidenceStore, target_id: str) -> str:
    facts = store.rows(
        "SELECT f.fact_id,f.value_json,f.status FROM facts f JOIN sources s ON s.source_id=f.source_id "
        "WHERE f.status='current' AND s.source_name<>'DealSynq property intelligence engines' ORDER BY f.fact_id")
    rels = store.rows("SELECT relationship_id,effective_date FROM relationships ORDER BY relationship_id")
    return stable_id("input", VERSION, target_id, [tuple(r) for r in facts], [tuple(r) for r in rels])


def search_plan_fingerprint(store: EvidenceStore, target_id: str) -> str:
    target = store.db.execute("SELECT name,address FROM targets WHERE target_id=?", (target_id,)).fetchone()
    aliases = [tuple(row) for row in store.rows(
        "SELECT alias_type,raw_value,normalized_value FROM entity_aliases ORDER BY alias_type,normalized_value")]
    entities = [tuple(row) for row in store.rows(
        "SELECT entity_type,canonical_name FROM entities WHERE entity_type IN ('organization','tenant') ORDER BY entity_type,canonical_name")]
    return stable_id("input", SEARCH_PLAN_VERSION, tuple(target) if target else None, aliases, entities)


def plan_searches(store: EvidenceStore, target_id: str) -> dict[str, int]:
    return _search_plan(store, target_id)


def _source(store: EvidenceStore, payload: dict[str, Any]) -> tuple[str, str]:
    raw = store.put_raw(payload)
    source = store.source(name="DealSynq property intelligence engines", url=None,
                          authority="calculation and inference", parser_version=VERSION,
                          raw_sha256=raw,
                          access_note="Deterministic taxonomy, event, capital-stack, and claim-resolution engines over sourced evidence.")
    return source, raw


def _asset_classification(store: EvidenceStore, target_id: str, source: str, raw: str) -> dict[str, Any]:
    rows = store.rows(
        "SELECT f.predicate,f.value_json,e.entity_type FROM facts f LEFT JOIN entities e ON e.entity_id=f.subject_id "
        "WHERE f.status='current' AND f.predicate IN "
        "('use_class','use_code','use_description','building_use','building_style',"
        "'structure_type','tenant_name','document_classification','zoning_description')")
    entity_rows = store.rows(
        "SELECT canonical_name FROM entities WHERE entity_type IN ('building','tenant')")
    corpus = " ".join(
        [normalize_text(json.loads(r["value_json"])) for r in rows] +
        [normalize_text(r["canonical_name"]) for r in entity_rows]
    )
    matches = []
    for primary, secondary in TAXONOMY.items():
        for subtype, variants in secondary.items():
            for leaf, terms in variants.items():
                hits = sorted({term for term in terms if normalize_text(term) in corpus})
                if hits:
                    specificity = sum(len(normalize_text(term).split()) for term in hits)
                    matches.append({"primary": primary, "subtype": subtype, "leaf": leaf,
                                    "matched_terms": hits, "score": specificity})
    matches.sort(key=lambda item: (-item["score"], item["primary"], item["subtype"], item["leaf"]))
    top_score = matches[0]["score"] if matches else None
    top_candidates = [item for item in matches if item["score"] == top_score]
    ambiguous = len(top_candidates) > 1
    preferred = top_candidates[0] if top_candidates and not ambiguous else None
    result = {"taxonomy_version": "dealsynq-asset-taxonomy/1.0.0", "preferred": preferred,
              "candidates": top_candidates if ambiguous else [],
              "alternatives": [item for item in matches if item not in top_candidates][:10],
              "signals_considered": len(rows) + len(entity_rows),
              "status": "ambiguous" if ambiguous else "classified" if preferred else "insufficient_evidence"}
    store.fact(subject_id=target_id, category="asset_classification", predicate="hierarchical_asset_classification",
               value=result, fact_class="inference", confidence=0.78 if preferred else 0.55 if ambiguous else 0.3,
               source_id=source, parser_version=VERSION, raw_sha256=raw,
               evidence_locator="Deterministic taxonomy matching over current use/building/tenant evidence")
    return result


def _capital_stack(store: EvidenceStore, target_id: str, source: str, raw: str) -> dict[str, Any]:
    subjects = [target_id] + [row[0] for row in store.rows(
        "SELECT parcel_id FROM grouping_decisions WHERE target_id=? AND included=1", (target_id,))]
    marks = ",".join("?" for _ in subjects)
    docs = store.rows(
        "SELECT DISTINCT e.entity_id,e.canonical_name,e.attributes_json,r.effective_date "
        "FROM entities e JOIN relationships r ON r.from_entity_id=e.entity_id "
        "WHERE e.entity_type='recorded_document' "
        f"AND r.to_entity_id IN ({marks}) ORDER BY r.effective_date", tuple(subjects))
    events = []
    for row in docs:
        attrs = json.loads(row["attributes_json"])
        fact_values = [json.loads(item[0]) for item in store.rows(
            "SELECT value_json FROM facts WHERE subject_id=? AND status='current' "
            "AND predicate IN ('document_type','instrument_type','document_description','party_role')",
            (row["entity_id"],),
        )]
        text = normalize_text(" ".join(
            [row["canonical_name"]] +
            [str(attrs.get(k) or "") for k in ("document_type", "document_desc", "party_role")] +
            [str(value) for value in fact_values]
        ))
        kind = None
        if any(term in text for term in ("MORTGAGE", "OPEN END", "SECURITY")):
            kind = "financing_recorded"
        elif any(term in text for term in ("ASSIGNMENT", "ASSIGN")):
            kind = "financing_assignment"
        elif any(term in text for term in ("DISCHARGE", "RELEASE", "SATISFACTION")):
            kind = "financing_release"
        elif "UCC" in text or "FINANCING STATEMENT" in text:
            kind = "ucc_filing"
        elif any(term in text for term in ("FORECLOSURE", "LIS PENDENS")):
            kind = "distress_signal"
        if kind:
            events.append({"document_id": row["entity_id"], "date": row["effective_date"],
                           "event_type": kind, "document": row["canonical_name"],
                           "book_page": attrs.get("book_page"), "counterparty": attrs.get("reverse_party")})
    result = {"classified_recording_events": events, "known_event_count": len(events),
              "current_principal": None, "interest_rate": None, "maturity_date": None,
              "current_lien_status": "unresolved",
              "limitations": ["Registry index labels do not establish current unpaid principal.",
                              "Instrument images/exhibits are required for terms, collateral, assignments, and release adjudication."]}
    store.fact(subject_id=target_id, category="deeds_liens", predicate="capital_stack_reconstruction",
               value=result, fact_class="inference", confidence=0.58 if events else 0.3,
               source_id=source, parser_version=VERSION, raw_sha256=raw,
               evidence_locator="Recorded-document type/date/counterparty classification; unresolved fields remain null")
    return result


def _normalized_event_date(value: str | None) -> tuple[str, str] | None:
    """Return an ISO-sortable date and its precision, or reject prose/non-dates."""
    if not value:
        return None
    text = str(value).strip()
    for pattern, fmt, precision in (
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d", "day"),
        (r"\d{2}-\d{2}-\d{4}", "%m-%d-%Y", "day"),
        (r"\d{1,2}/\d{1,2}/\d{4}", "%m/%d/%Y", "day"),
    ):
        if re.fullmatch(pattern, text):
            try:
                return datetime.strptime(text, fmt).date().isoformat(), precision
            except ValueError:
                return None
    if re.fullmatch(r"\d{4}-\d{2}", text):
        try:
            datetime.strptime(text, "%Y-%m")
            return text, "month"
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}", text):
        return text, "year"
    fiscal = re.fullmatch(r"FY\s*(\d{4})", text, flags=re.IGNORECASE)
    if fiscal:
        return fiscal.group(1), "fiscal_year"
    return None


def _event_summary(event_type: str, subject_name: str | None, values: dict[str, Any]) -> str:
    preferred = {
        "permit_or_entitlement": ("permit_number", "permit_purpose", "declared_value"),
        "ownership_or_financing": ("document_type", "book_page", "party_name", "counterparty"),
        "tenant_or_occupancy": ("tenant_name", "occupancy_status", "space_area"),
        "market_listing": ("listing_status", "transaction_type", "asking_price", "asking_rent"),
        "assessment_or_tax": ("assessed_value", "land_value", "building_value"),
        "ownership": ("sale_price", "buyer", "seller"),
        "parcels": ("assessed_value", "land_area", "situs_address"),
    }.get(event_type, ())
    keys = [key for key in preferred if key in values]
    if not keys:
        keys = [key for key in sorted(values) if not key.endswith("_date") and key not in {"effective_date"}][:4]
    details = "; ".join(f"{key}={values[key]}" for key in keys[:4])
    label = subject_name or event_type.replace("_", " ").title()
    return f"{label}: {details}" if details else label


def _timeline(store: EvidenceStore, target_id: str) -> int:
    # Rebuild resolver-generated events while preserving explicit source events
    # ingested through the tool-neutral canonical contract.
    store.db.execute(
        "DELETE FROM events WHERE target_id=? AND (event_type='detected_source_change' "
        "OR json_extract(evidence_json,'$.fact_id') IS NOT NULL "
        "OR json_extract(evidence_json,'$.fact_ids') IS NOT NULL)",
        (target_id,),
    )
    rows = store.rows(
        "SELECT f.*,e.canonical_name,s.source_name FROM facts f LEFT JOIN entities e ON e.entity_id=f.subject_id "
        "JOIN sources s ON s.source_id=f.source_id WHERE f.effective_date IS NOT NULL AND f.status='current' "
        "ORDER BY f.effective_date,f.category,f.predicate")
    category_map = {"deeds_liens": "ownership_or_financing", "permits_planning": "permit_or_entitlement",
                    "tenants": "tenant_or_occupancy", "market": "market_listing",
                    "tax": "assessment_or_tax", "zoning": "zoning"}
    grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for row in rows:
        normalized = _normalized_event_date(row["effective_date"])
        if normalized is None:
            continue
        event_date, _ = normalized
        event_type = category_map.get(row["category"], row["category"])
        grouped[(row["subject_id"], event_date, event_type)].append(row)
    class_rank = {"confirmed_official": 5, "reported": 4, "calculation": 3,
                  "inference": 2, "prediction": 1}
    for (subject_id, event_date, event_type), facts in sorted(grouped.items(), key=lambda item: item[0][1:]):
        values = {row["predicate"]: json.loads(row["value_json"]) for row in facts}
        subject_name = next((row["canonical_name"] for row in facts if row["canonical_name"]), None)
        precision = _normalized_event_date(facts[0]["effective_date"])[1]
        fact_class = max((row["fact_class"] for row in facts), key=lambda value: class_rank.get(value, 0))
        store.event(target_id=target_id, event_type=event_type,
                    event_date=event_date, date_precision=precision,
                    subject_id=subject_id, summary=_event_summary(event_type, subject_name, values),
                    fact_class=fact_class, confidence=max(float(row["confidence"]) for row in facts),
                    source_ids=sorted({row["source_id"] for row in facts}),
                    evidence={"fact_ids": sorted(row["fact_id"] for row in facts),
                              "source_effective_dates": sorted({row["effective_date"] for row in facts})})
    # Many assessor exports expose a last-sale date as the value of a field
    # rather than as the observation's effective_date. Convert that structured
    # pair into an assessor-reported sale event without claiming deed proof.
    assessor_sales: dict[str, list[Any]] = defaultdict(list)
    for row in store.rows(
        "SELECT f.*,e.canonical_name FROM facts f JOIN entities e ON e.entity_id=f.subject_id "
        "WHERE f.status='current' AND f.predicate IN ('last_sale_date','last_sale_price') "
        "AND f.subject_id IN (SELECT parcel_id FROM grouping_decisions WHERE target_id=? AND included=1)",
        (target_id,),
    ):
        assessor_sales[row["subject_id"]].append(row)
    for subject_id, facts in assessor_sales.items():
        values = {row["predicate"]: json.loads(row["value_json"]) for row in facts}
        normalized = _normalized_event_date(str(values.get("last_sale_date") or ""))
        if not normalized:
            continue
        event_date, precision = normalized
        store.event(
            target_id=target_id, event_type="assessor_last_sale",
            event_date=event_date, date_precision=precision, subject_id=subject_id,
            summary=_event_summary("ownership", facts[0]["canonical_name"], values),
            fact_class="confirmed_official",
            confidence=max(float(row["confidence"]) for row in facts),
            source_ids=sorted({row["source_id"] for row in facts}),
            evidence={"fact_ids": sorted(row["fact_id"] for row in facts),
                      "qualification": "Official assessor observation; deed instrument not connected"},
        )
    changes_by_subject_day: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for change in store.rows("SELECT * FROM fact_changes ORDER BY detected_at"):
        normalized = _normalized_event_date(change["detected_at"][:10])
        if normalized:
            changes_by_subject_day[(change["subject_id"], normalized[0])].append(change)
    for (subject_id, event_date), changes in sorted(changes_by_subject_day.items(), key=lambda item: item[0][1]):
        predicates = sorted({change["predicate"] for change in changes})
        store.event(target_id=target_id, event_type="detected_source_change",
                    event_date=event_date, date_precision="day",
                    subject_id=subject_id, summary=f"Changed fields: {', '.join(predicates)}",
                    fact_class="confirmed_official", confidence=1.0,
                    source_ids=[], evidence={"change_ids": [change["change_id"] for change in changes],
                                             "predicates": predicates})
    return store.db.execute("SELECT COUNT(*) FROM events WHERE target_id=?", (target_id,)).fetchone()[0]


def _search_plan(store: EvidenceStore, target_id: str) -> dict[str, int]:
    target = store.db.execute("SELECT name,address FROM targets WHERE target_id=?", (target_id,)).fetchone()
    if not target:
        return {"queries": 0}
    store.db.execute("DELETE FROM search_queries WHERE target_id=? AND status='planned'", (target_id,))
    identifiers: list[tuple[str, str]] = [("property_name", target["name"]), ("address", target["address"])]
    identifiers += [(r["alias_type"], r["raw_value"]) for r in store.rows(
        "SELECT DISTINCT alias_type,raw_value FROM entity_aliases WHERE alias_type IN ('parcel_identifier','organization_name','situs_address')")]
    identifiers += [(r["entity_type"], r["canonical_name"]) for r in store.rows(
        "SELECT entity_type,canonical_name FROM entities WHERE entity_type IN ('organization','tenant') ORDER BY entity_type,canonical_name LIMIT 100")]
    seen: set[str] = set()
    planned = []
    templates = [
        ("identity", '"{name}" "{address}"', "high", "Property-name/address identity and general documents"),
        ("official_documents", '"{address}" (permit OR zoning OR planning OR variance OR site plan)', "high", "Entitlement and property-document discovery"),
        ("capital_stack", '"{address}" (deed OR mortgage OR lien OR UCC OR foreclosure)', "high", "Land records and capital-stack discovery"),
        ("tenant_history", '"{name}" (tenant OR lease OR opening OR closing OR relocated)', "medium", "Temporal tenant history"),
        ("environmental", '"{address}" (environmental OR spill OR remediation OR wetlands OR flood)', "medium", "Environmental document discovery"),
        ("market", '"{name}" "{address}" (sale OR sold OR lease OR available)', "medium", "Market and transaction evidence"),
    ]
    for query_type, template, priority, rationale in templates:
        query = template.format(name=target["name"], address=target["address"])
        normalized = normalize_text(query)
        if normalized in seen:
            continue
        seen.add(normalized)
        planned.append((query_type, query, priority, rationale, [target["name"], target["address"]]))
    for kind, value in identifiers:
        if kind == "parcel_identifier":
            planned.append(("parcel_identifier", f'"{value}" (parcel OR assessor OR deed OR mortgage)', "high",
                            "Parcel-specific official/open-web discovery", [value]))
        elif kind == "organization_name":
            planned.append(("entity", f'"{value}" (property OR subsidiary OR mortgage OR acquisition)', "medium",
                            "Owner/entity/portfolio discovery", [value]))
    now = utcnow()
    count = 0
    for query_type, query, priority, rationale, ids in planned:
        normalized = normalize_text(query)
        if normalized in seen and query_type not in {"identity", "official_documents", "capital_stack", "tenant_history", "environmental", "market"}:
            continue
        seen.add(normalized)
        qid = stable_id("query", target_id, query_type, query)
        store.db.execute(
            "INSERT INTO search_queries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(query_id) DO UPDATE SET query_text=excluded.query_text,"
            "query_type=excluded.query_type,identifiers_json=excluded.identifiers_json,"
            "priority=excluded.priority,rationale=excluded.rationale,updated_at=excluded.updated_at",
            (qid, target_id, query, query_type, json.dumps(ids), priority, "planned", None,
             None, None, rationale, now, now),
        )
        count += 1
    return {"queries": count, "identifiers": len(identifiers)}


def _freshness_score(row: Any) -> float:
    if not row["freshness_days"] or not row["retrieved_at"]:
        return 0.8
    try:
        observed = datetime.fromisoformat(str(row["retrieved_at"]).replace("Z", "+00:00"))
        age = max(0.0, (datetime.now(timezone.utc) - observed).total_seconds() / 86400)
        return math.pow(0.5, age / float(row["freshness_days"]))
    except (ValueError, TypeError):
        return 0.7


def _resolve_claims(store: EvidenceStore, target_id: str) -> dict[str, int]:
    store.db.execute("DELETE FROM resolved_claims WHERE target_id=?", (target_id,))
    subjects = {target_id} | {r[0] for r in store.rows(
        "SELECT parcel_id FROM grouping_decisions WHERE target_id=? AND included=1", (target_id,))}
    if not subjects:
        return {"claims": 0, "contested": 0}
    marks = ",".join("?" for _ in subjects)
    rows = store.rows(
        f"SELECT f.*,s.authority,s.retrieved_at FROM facts f JOIN sources s ON s.source_id=f.source_id "
        f"WHERE f.status='current' AND f.subject_id IN ({marks}) ORDER BY f.subject_id,f.predicate", tuple(subjects))
    groups: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        groups.setdefault((row["subject_id"], row["predicate"]), []).append(row)
    class_weight = {"confirmed_official": 1.0, "calculation": 0.84, "reported": 0.72,
                    "inference": 0.55, "prediction": 0.35}
    contested = 0
    for (subject, predicate), facts in groups.items():
        scored = []
        for fact in facts:
            authority = normalize_text(fact["authority"])
            authority_weight = 1.0 if any(term in authority for term in ("OFFICIAL", "FEDERAL", "MUNICIPAL", "COUNTY", "STATE")) else (0.8 if "OWNER" in authority else 0.65)
            score = float(fact["confidence"]) * class_weight[fact["fact_class"]] * authority_weight * _freshness_score(fact)
            scored.append((score, fact))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["fact_id"]))
        distinct = {fact["value_json"] for _, fact in scored}
        status = "single_source" if len(scored) == 1 else ("corroborated" if len(distinct) == 1 else "preferred_with_conflict")
        if status == "preferred_with_conflict":
            contested += 1
        preferred = scored[0][1]
        claim_id = stable_id("claim", target_id, subject, predicate)
        store.db.execute("INSERT INTO resolved_claims VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (claim_id, target_id, subject, predicate, preferred["fact_id"], status,
                          scored[0][0], json.dumps([f["fact_id"] for _, f in scored]),
                          json.dumps({"formula": "confidence * fact_class_weight * authority_weight * freshness",
                                      "scores": [{"fact_id": f["fact_id"], "score": round(s, 6)} for s, f in scored]}), utcnow()))
    return {"claims": len(groups), "contested": contested}


def analyze(store: EvidenceStore, target_id: str) -> dict[str, Any]:
    payload = {"taxonomy_version": "dealsynq-asset-taxonomy/1.0.0", "resolver_version": VERSION}
    source, raw = _source(store, payload)
    classification = _asset_classification(store, target_id, source, raw)
    capital = _capital_stack(store, target_id, source, raw)
    timeline_count = _timeline(store, target_id)
    search_plan = _search_plan(store, target_id)
    claims = _resolve_claims(store, target_id)
    return {"asset_classification": classification, "capital_stack": capital,
            "timeline_events": timeline_count, "search_plan": search_plan,
            "claim_resolution": claims}
