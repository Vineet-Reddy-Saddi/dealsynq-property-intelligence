from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import EvidenceStore
from .util import utcnow


def _json(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif isinstance(value, float):
        text = f"{value:,.4f}".rstrip("0").rstrip(".")
    else:
        text = str(value if value is not None else "—")
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= 1800 else text[:1797] + "…"


def _freshness(row: Any) -> str:
    half_life = row["freshness_days"]
    retrieved = row["source_retrieved_at"]
    if not half_life or not retrieved:
        return "not scored"
    try:
        observed = datetime.fromisoformat(str(retrieved).replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - observed).total_seconds() / 86400)
        score = math.pow(0.5, age / float(half_life))
        return f"{score:.2f} ({age:.0f}d old; {half_life}d half-life)"
    except (ValueError, TypeError):
        return "not scored"


def _fact(store: EvidenceStore, subject: str, predicate: str) -> Any:
    row = store.db.execute(
        "SELECT value_json FROM facts WHERE subject_id=? AND predicate=? AND status='current' "
        "ORDER BY confidence DESC, observed_at DESC LIMIT 1", (subject, predicate)).fetchone()
    return _json(row[0]) if row else None


def build_payload(store: EvidenceStore, target_id: str) -> dict[str, Any]:
    target = store.db.execute("SELECT * FROM targets WHERE target_id=?", (target_id,)).fetchone()
    if not target:
        raise ValueError(f"Unknown target {target_id}")
    tables: dict[str, list[dict[str, Any]]] = {}
    for name, sql, params in [
        ("entities", "SELECT * FROM entities ORDER BY entity_type,canonical_name", ()),
        ("sources", "SELECT * FROM sources ORDER BY retrieved_at,source_name", ()),
        ("facts", "SELECT * FROM facts ORDER BY category,subject_id,predicate,observed_at", ()),
        ("relationships", "SELECT * FROM relationships ORDER BY relationship_type,from_entity_id", ()),
        ("grouping_decisions", "SELECT * FROM grouping_decisions WHERE target_id=? ORDER BY included DESC,score DESC,parcel_id", (target_id,)),
        ("contradictions", "SELECT * FROM contradictions ORDER BY severity,predicate", ()),
        ("gaps", "SELECT * FROM gaps WHERE target_id=? ORDER BY category,status", (target_id,)),
        ("stage_runs", "SELECT * FROM stage_runs WHERE target_id=? ORDER BY started_at", (target_id,)),
        ("source_capabilities", "SELECT * FROM source_capabilities WHERE target_id=? ORDER BY capability", (target_id,)),
        ("entity_aliases", "SELECT * FROM entity_aliases ORDER BY alias_type,normalized_value", ()),
        ("fact_changes", "SELECT * FROM fact_changes ORDER BY detected_at", ()),
        ("documents", "SELECT * FROM documents WHERE target_id=? ORDER BY published_date,title", (target_id,)),
        ("document_mentions", "SELECT m.* FROM document_mentions m JOIN documents d ON d.document_id=m.document_id WHERE d.target_id=? ORDER BY m.document_id,m.page_number,m.character_start", (target_id,)),
        ("events", "SELECT * FROM events WHERE target_id=? ORDER BY event_date,event_type", (target_id,)),
        ("resolved_claims", "SELECT * FROM resolved_claims WHERE target_id=? ORDER BY subject_id,predicate", (target_id,)),
        ("refresh_policies", "SELECT * FROM refresh_policies WHERE target_id=? ORDER BY priority,stage_key", (target_id,)),
        ("search_queries", "SELECT * FROM search_queries WHERE target_id=? ORDER BY priority,query_type,query_text", (target_id,)),
        ("temporal_states", "SELECT * FROM temporal_states WHERE target_id=? ORDER BY valid_from,subject_id,state_type", (target_id,)),
        ("web_discoveries", "SELECT * FROM web_discoveries WHERE target_id=? ORDER BY provider,published_date,title", (target_id,)),
        ("pipeline_stage_states", "SELECT * FROM pipeline_stage_states WHERE target_id=? ORDER BY stage_order", (target_id,)),
        ("property_state_snapshots", "SELECT * FROM property_state_snapshots WHERE target_id=? ORDER BY as_of", (target_id,)),
        ("raw_evidence", "SELECT raw_sha256,media_type,byte_length,stored_at FROM raw_evidence ORDER BY stored_at", ()),
    ]:
        tables[name] = [dict(r) for r in store.rows(sql, params)]
    return {
        "schema_version": "property-intelligence-report/3.0.0",
        "generated_at": utcnow(), "target": dict(target), **tables,
    }


def render_markdown(store: EvidenceStore, target_id: str) -> str:
    target = store.db.execute("SELECT * FROM targets WHERE target_id=?", (target_id,)).fetchone()
    decisions = store.rows(
        "SELECT g.*,e.external_id,e.attributes_json FROM grouping_decisions g JOIN entities e ON e.entity_id=g.parcel_id "
        "WHERE g.target_id=? ORDER BY g.included DESC,g.score DESC,e.external_id", (target_id,))
    entity_counts = {r["entity_type"]: r["n"] for r in store.rows(
        "SELECT entity_type,COUNT(*) n FROM entities GROUP BY entity_type ORDER BY entity_type")}
    current_facts = store.rows(
        "SELECT f.*,s.source_name,s.source_url,s.authority,s.retrieved_at source_retrieved_at FROM facts f JOIN sources s ON s.source_id=f.source_id "
        "WHERE f.status='current' ORDER BY f.fact_class,f.category,f.predicate")
    target_facts = [r for r in current_facts if r["subject_id"] == target_id]
    class_groups: dict[str, list[Any]] = defaultdict(list)
    for row in target_facts:
        class_groups[row["fact_class"]].append(row)

    anchors = [json.loads(r["attributes_json"]) for r in decisions
               if json.loads(r["attributes_json"]).get("is_anchor")]
    anchor = anchors[0] if anchors else {}
    nfhl_overlay = _fact(store, target_id, "nfhl_site_overlay")
    flood_zones = _fact(store, target_id, "site_flood_zones")
    if not flood_zones and isinstance(nfhl_overlay, dict):
        flood_zones = sorted({item.get("class") for item in nfhl_overlay.get("classes", [])
                              if item.get("class")})
    summary = {
        "Census matched address": (_fact(store, target_id, "geocoder_match") or {}).get("matched_address") if isinstance(_fact(store, target_id, "geocoder_match"), dict) else None,
        "Algorithm-included candidate parcels": _fact(store, target_id, "site_parcel_count"),
        "Land area (sq ft)": _fact(store, target_id, "site_land_area"),
        "Assessed value (USD)": _fact(store, target_id, "site_assessed_value"),
        "Assessor/record-card building area (sq ft)": _fact(store, target_id, "site_building_area"),
        "Assessor building/land ratio (not legal FAR)": _fact(store, target_id, "assessor_building_to_land_area_ratio"),
        "Anchor assessor owner": anchor.get("owner"),
        "Parent entity": _fact(store, target_id, "parent_entity"),
        "Owner-marketed center address": _fact(store, target_id, "owner_marketed_address"),
        "Owner-reported GLA (sq ft)": _fact(store, target_id, "owner_reported_gla"),
        "Marketed available area (sq ft)": _fact(store, target_id, "marketed_available_area") or _fact(store, target_id, "available_area"),
        "Calculated marketed occupancy (%)": _fact(store, target_id, "calculated_marketed_occupancy_percent"),
        "Estimated annual tax (USD; not a bill)": _fact(store, target_id, "estimated_annual_tax"),
        "FEMA flood zones": flood_zones,
        "NFHL parcel overlay (%)": nfhl_overlay.get("site_percent") if isinstance(nfhl_overlay, dict) else None,
        "NWI wetland overlay (%)": (_fact(store, target_id, "nwi_site_overlay") or {}).get("site_percent") if isinstance(_fact(store, target_id, "nwi_site_overlay"), dict) else None,
        "Asset classification": (lambda asset: ((asset.get("preferred") or {}).get("leaf") if asset.get("preferred") else
                                                  {"status": asset.get("status"), "candidates": asset.get("candidates", [])}))
                                (_fact(store, target_id, "hierarchical_asset_classification") or {})
                                if isinstance(_fact(store, target_id, "hierarchical_asset_classification"), dict) else None,
    }
    lines = [
        f"# {target['name']} — deep-property intelligence report",
        "",
        f"**Research input / assessor anchor:** {target['address']}  ",
        f"**Generated:** {utcnow()}  ",
        "**Evidence policy:** official records are distinguished from stakeholder/market reporting; calculations, inferences, and predictions are never presented as confirmed facts.",
        "",
        "## Executive snapshot",
        "",
        "| Measure | Current result |",
        "|---|---:|",
    ]
    lines += [f"| {_cell(k)} | {_cell(v)} |" for k, v in summary.items()]
    if decisions:
        probable = sum(1 for row in decisions if _json(row["evidence_json"]).get("classification") in
                       {"probable association", "probable_association"})
        if probable:
            reading = ("The input address identifies the assessor anchor parcel; a property/site may be marketed under another address. "
                       f"Only the anchor is address-confirmed. The other {probable} included parcels are explainable probable associations and still require deed/plan confirmation for a legal assemblage conclusion.")
        else:
            reading = "The input address resolved to one confirmed assessor anchor; the grouping engine did not infer any additional site parcels."
        lines += ["", f"**Validation reading:** {reading}"]
        if _fact(store, target_id, "calculated_marketed_occupancy_percent") is not None:
            lines += ["", "The calculated marketed-occupancy percentage is derived from owner-reported GLA less currently marketed available area. It is not audited physical occupancy and is not directly interchangeable with a public-company `percent leased` disclosure."]
    stage_states = store.rows(
        "SELECT * FROM pipeline_stage_states WHERE target_id=? ORDER BY stage_order", (target_id,))
    if stage_states:
        lines += ["", "## Rahul pipeline stage coverage", "",
                  "Every stage below is implemented as a reusable contract. Coverage describes the evidence currently available for this property, not whether the software stage exists.", "",
                  "| # | Stage | Implementation | Property coverage | Outputs | Remaining requirements |",
                  "|---:|---|---|---|---|---|"]
        for stage in stage_states:
            lines.append(
                f"| {stage['stage_order']} | {_cell(stage['label'])} | {_cell(stage['implementation_status'])} | "
                f"{_cell(stage['coverage_status'])} | {_cell(_json(stage['metrics_json']))} | "
                f"{_cell(_json(stage['missing_requirements_json']))} |"
            )
    lines += [
        "",
        ("The candidate property site above was assembled by the reusable scoring algorithm. No parcel list is present in the target configuration. "
         "Protected listing sources are not collected; validation-only artifacts are retained as raw context and are not promoted into facts."
         if decisions else
         "No approved local parcel source was available, so this report is national-context-only. No parcel group or property-site boundary is implied."),
        "",
        "## Explainable parcel grouping",
        "",
        "| Parcel | Address | Included | Score | Classification | Evidence |",
        "|---|---|---:|---:|---|---|",
    ]
    for row in decisions:
        attrs = json.loads(row["attributes_json"])
        evidence = json.loads(row["evidence_json"])
        matched = []
        for name, detail in evidence.items():
            if isinstance(detail, dict) and detail.get("matched"):
                extra = f" ({detail.get('centroid_distance_m')} m)" if detail.get("centroid_distance_m") is not None else ""
                matched.append(name.replace("_", " ") + extra)
        lines.append(f"| {_cell(attrs.get('parcel_number'))} | {_cell(attrs.get('address'))} | "
                     f"{'yes' if row['included'] else 'no'} | {row['score']:.2f} | "
                     f"{_cell(evidence.get('classification'))} | {_cell(', '.join(matched))} |")
    if decisions:
        lines += ["", f"Threshold: `{decisions[0]['threshold']:.2f}`; algorithm: `{decisions[0]['algorithm_version']}`. "
                  "Every decision and its component evidence are stored in SQLite."]

    lines += ["", "## Connected evidence coverage", "", "| Node type | Count |", "|---|---:|"]
    display_order = ["parcel", "building", "organization", "tenant", "tenant_space",
                     "recorded_document", "document", "building_footprint", "transaction", "permit", "listing", "market_comparable_listing",
                     "portfolio_property_candidate", "development_case"]
    for kind in display_order:
        lines.append(f"| {kind.replace('_', ' ').title()} | {entity_counts.get(kind, 0)} |")

    labels = [
        ("confirmed_official", "Confirmed official facts"), ("reported", "Reported facts"),
        ("calculation", "Calculations"), ("inference", "Inferences"), ("prediction", "Predictions"),
    ]
    for fact_class, title in labels:
        lines += ["", f"## {title}", ""]
        rows = class_groups.get(fact_class, [])
        if not rows:
            lines.append("None produced. This is intentional for predictions unless a prediction model is explicitly configured.")
            continue
        lines += ["| Category | Fact | Value | Confidence | Freshness | Source |", "|---|---|---|---:|---|---|"]
        for row in rows:
            value = _json(row["value_json"])
            source = row["source_name"]
            if row["source_url"]:
                source = f"[{source}]({row['source_url']})"
            lines.append(f"| {_cell(row['category'])} | {_cell(row['predicate'])} | {_cell(value)}"
                         f"{(' ' + row['unit']) if row['unit'] else ''} | {row['confidence']:.2f} | "
                         f"{_cell(_freshness(row))} | {source} |")

    resolved = store.rows(
        "SELECT c.*,f.value_json,f.fact_class,s.source_name FROM resolved_claims c "
        "LEFT JOIN facts f ON f.fact_id=c.preferred_fact_id LEFT JOIN sources s ON s.source_id=f.source_id "
        "WHERE c.target_id=? AND c.subject_id=? ORDER BY c.predicate", (target_id, target_id))
    if resolved:
        lines += ["", "## Preferred current-state view", "",
                  "This view ranks claims without deleting competing evidence. `preferred_with_conflict` requires review.", "",
                  "| Field | Preferred value | Resolution | Score | Source |", "|---|---|---|---:|---|"]
        for row in resolved:
            lines.append(f"| {_cell(row['predicate'])} | {_cell(_json(row['value_json']))} | "
                         f"{_cell(row['resolution_status'])} | {_cell(row['score'])} | {_cell(row['source_name'])} |")

    capabilities = store.rows(
        "SELECT * FROM source_capabilities WHERE target_id=? ORDER BY "
        "CASE status WHEN 'configured' THEN 1 WHEN 'partial' THEN 2 WHEN 'blocked' THEN 3 ELSE 4 END,capability",
        (target_id,))
    if capabilities:
        lines += ["", "## Rahul source-coverage contract", "",
                  "Coverage is explicit. `Configured` means an approved adapter/source route exists; it does not imply every field is available for every property.", "",
                  "| Capability | Status | Source/adapter | Limitation |", "|---|---|---|---|"]
        for cap in capabilities:
            source = cap["source_name"] or cap["adapter"] or "—"
            if cap["source_url"]:
                source = f"[{source}]({cap['source_url']})"
            lines.append(f"| {_cell(cap['capability'])} | {_cell(cap['status'])} | {source} | {_cell(cap['reason'])} |")

    tenant_rows = store.rows(
        "SELECT e.canonical_name AS space,MAX(CASE WHEN f.predicate='tenant_name' THEN f.value_json END) tenant,"
        "MAX(CASE WHEN f.predicate='space_area' THEN f.value_json END) area,"
        "MAX(CASE WHEN f.predicate='occupancy_status' THEN f.value_json END) status "
        "FROM entities e LEFT JOIN facts f ON f.subject_id=e.entity_id AND f.status='current' "
        "WHERE e.entity_type='tenant_space' GROUP BY e.entity_id ORDER BY e.external_id")
    if tenant_rows:
        lines += ["", "## Reported tenant/space schedule", "",
                  "Owner/manager leasing material is reported evidence, not government confirmation. Individual effective dates are unknown unless separately recorded.", "",
                  "| Space | Tenant | Area (sq ft) | Status |", "|---|---|---:|---|"]
        for row in tenant_rows:
            lines.append(f"| {_cell(row['space'])} | {_cell(_json(row['tenant']))} | {_cell(_json(row['area']))} | {_cell(_json(row['status']))} |")

    docs = store.rows(
        "SELECT e.canonical_name,e.external_id,e.attributes_json FROM entities e WHERE e.entity_type='recorded_document' ORDER BY e.external_id")
    if docs:
        lines += ["", "## Recorded documents", "", "| Date | Type | Book/page | Counterparty |", "|---|---|---|---|"]
        for row in docs:
            d = json.loads(row["attributes_json"])
            lines.append(f"| {_cell(d.get('date_received'))} | {_cell(d.get('document_type'))} | "
                         f"{_cell(d.get('book_page'))} | {_cell(d.get('reverse_party'))} |")

    parsed_docs = store.rows(
        "SELECT d.*,s.source_name,s.source_url,(SELECT COUNT(*) FROM document_mentions m WHERE m.document_id=d.document_id) mention_count "
        "FROM documents d JOIN sources s ON s.source_id=d.source_id WHERE d.target_id=? ORDER BY d.published_date,d.title", (target_id,))
    if parsed_docs:
        lines += ["", "## Parsed document corpus", "",
                  "Every document retains original bytes, extracted text, parser version, classification, and page/character-located mentions.", "",
                  "| Document | Type | Date | Pages | Mentions | Source |", "|---|---|---|---:|---:|---|"]
        for row in parsed_docs:
            source = f"[{row['source_name']}]({row['source_url']})" if row["source_url"] else row["source_name"]
            lines.append(f"| {_cell(row['title'])} | {_cell(row['document_type'])} | {_cell(row['published_date'])} | "
                         f"{_cell(row['page_count'])} | {_cell(row['mention_count'])} | {source} |")

    listings = store.rows(
        "SELECT e.canonical_name,e.attributes_json,s.source_name,s.source_url FROM entities e "
        "JOIN relationships r ON r.from_entity_id=e.entity_id AND r.relationship_type='markets' "
        "JOIN sources s ON s.source_id=r.source_id WHERE e.entity_type='listing' AND r.to_entity_id=? "
        "ORDER BY e.canonical_name", (target_id,))
    if listings:
        lines += ["", "## Market/listing evidence", "",
                  "These are public listing observations from the existing DealSynq lifecycle inventory. They are reported evidence, not proof of executed lease or sale terms.", "",
                  "| Type | Status/terms | Area | First seen | Last seen | Source |", "|---|---|---:|---|---|---|"]
        for row in listings:
            d = json.loads(row["attributes_json"])
            source = f"[{row['source_name']}]({row['source_url']})" if row["source_url"] else row["source_name"]
            lines.append(f"| {_cell(d.get('transaction_type'))} | {_cell(d.get('source_status') or d.get('price'))} | "
                         f"{_cell(d.get('sqft'))} | {_cell(d.get('first_seen'))} | {_cell(d.get('last_seen'))} | {source} |")

    contradictions = store.rows("SELECT * FROM contradictions WHERE status='open' ORDER BY severity,predicate")
    lines += ["", "## Conflicts and contradictions", ""]
    if contradictions:
        for row in contradictions:
            facts = store.rows(
                "SELECT f.value_json,f.fact_class,f.confidence,s.source_name,s.source_url FROM facts f JOIN sources s ON s.source_id=f.source_id "
                f"WHERE f.fact_id IN ({','.join('?' for _ in json.loads(row['fact_ids_json']))})",
                tuple(json.loads(row["fact_ids_json"])))
            note = ""
            if row["predicate"] == "site_building_area":
                note = " Measurement conventions likely differ (assessor building area versus landlord GLA); both values remain current observations."
            lines.append(f"- **{row['predicate']}** ({row['severity']}): " + "; ".join(
                f"{_cell(_json(f['value_json']))} — {f['source_name']}" for f in facts) + note)
    else:
        lines.append("No unresolved contradiction among current confirmed/reported observations. Superseded observations remain in the database for change history.")

    gaps = store.rows("SELECT * FROM gaps WHERE target_id=? AND status!='resolved' ORDER BY category,status,description", (target_id,))
    lines += ["", "## Missing, partial, or blocked", ""]
    if gaps:
        for gap in gaps:
            lines.append(f"- **{gap['status'].upper()} — {gap['category']}:** {gap['description']}"
                         + (f" — {gap['reason']}" if gap["reason"] else ""))
    else:
        lines.append("No recorded gaps (this does not imply exhaustive coverage).")

    temporal = store.rows(
        "SELECT t.*,s.source_name,s.source_url FROM temporal_states t JOIN sources s ON s.source_id=t.source_id "
        "WHERE t.target_id=? ORDER BY COALESCE(t.valid_from,t.first_seen) DESC,t.state_type,t.subject_id",
        (target_id,),
    )
    if temporal:
        lines += ["", "## Point-in-time states", "",
                  "These observations preserve their stated time and concept; an older leased percentage is not silently treated as today's occupancy.", "",
                  "| Valid from | Valid to | State | Value | Class | Source |", "|---|---|---|---|---|---|"]
        for state in temporal:
            source = f"[{state['source_name']}]({state['source_url']})" if state["source_url"] else state["source_name"]
            lines.append(f"| {_cell(state['valid_from'] or state['first_seen'])} | {_cell(state['valid_to'])} | "
                         f"{_cell(state['state_type'])} | {_cell(_json(state['state_value_json']))} | "
                         f"{_cell(state['fact_class'])} | {source} |")

    timeline = store.rows(
        "SELECT f.effective_date,f.category,f.predicate,f.value_json,e.canonical_name "
        "FROM facts f LEFT JOIN entities e ON e.entity_id=f.subject_id "
        "WHERE f.effective_date IS NOT NULL AND f.effective_date NOT LIKE 'unknown%' "
        "ORDER BY f.effective_date DESC LIMIT 60")
    lines += ["", "## Dated source facts (raw-level)", "",
              "This table retains individual dated assertions. The normalized timeline below groups fields that describe the same subject/date/event.", ""]
    if timeline:
        lines += ["| Effective/source date | Subject | Event/fact | Value |", "|---|---|---|---|"]
        for row in timeline:
            lines.append(f"| {_cell(row['effective_date'])} | {_cell(row['canonical_name'])} | "
                         f"{_cell(row['predicate'])} | {_cell(_json(row['value_json']))} |")
    else:
        lines.append("No effective dates were parsed.")

    events = store.rows("SELECT * FROM events WHERE target_id=? ORDER BY event_date DESC,event_type LIMIT 100", (target_id,))
    if events:
        lines += ["", "## Normalized property timeline", "",
                  "| Date | Event type | Summary | Class | Confidence |", "|---|---|---|---|---:|"]
        for event in events:
            lines.append(f"| {_cell(event['event_date'])} | {_cell(event['event_type'])} | {_cell(event['summary'])} | "
                         f"{_cell(event['fact_class'])} | {event['confidence']:.2f} |")

    changes = store.rows("SELECT * FROM fact_changes ORDER BY detected_at DESC")
    lines += ["", "## Detected evidence and parser changes", ""]
    if changes:
        lines += ["| Detected | Type | Subject | Field | Before | After | Source |", "|---|---|---|---|---|---|---|"]
        for change in changes:
            lines.append(f"| {_cell(change['detected_at'])} | {_cell(change['change_type'])} | {_cell(change['subject_id'])} | {_cell(change['predicate'])} | "
                         f"{_cell(_json(change['old_value_json']))} | {_cell(_json(change['new_value_json']))} | {_cell(change['source_name'])} |")
    else:
        lines.append("No same-source fact changes have been detected yet. Unchanged stage fingerprints prevented redundant collection.")

    sources = store.rows("SELECT * FROM sources ORDER BY authority,source_name,retrieved_at")
    lines += ["", "## Sources and provenance", "",
              "| Authority | Source | Source date | Retrieved | Raw SHA-256 | Parser |", "|---|---|---|---|---|---|"]
    for source in sources:
        name = source["source_name"]
        if source["source_url"]:
            name = f"[{name}]({source['source_url']})"
        lines.append(f"| {_cell(source['authority'])} | {name} | {_cell(source['source_date'])} | "
                     f"{_cell(source['retrieved_at'])} | `{(source['raw_sha256'] or '—')[:16]}` | `{source['parser_version']}` |")

    refresh = store.rows("SELECT * FROM refresh_policies WHERE target_id=? ORDER BY priority,stage_key", (target_id,))
    if refresh:
        lines += ["", "## Refresh plan", "", "| Stage | Cadence | Priority | Last success | Next due |", "|---|---:|---|---|---|"]
        for policy in refresh:
            lines.append(f"| {_cell(policy['stage_key'])} | {policy['cadence_days']} days | {_cell(policy['priority'])} | "
                         f"{_cell(policy['last_success_at'])} | {_cell(policy['next_due_at'])} |")

    runs = store.rows("SELECT * FROM stage_runs WHERE target_id=? ORDER BY started_at", (target_id,))
    lines += ["", "## Incremental run ledger", "", "| Stage | Status | Started | Input fingerprint | Stats |", "|---|---|---|---|---|"]
    for run in runs:
        lines.append(f"| {_cell(run['stage_key'])} | {_cell(run['status'])} | {_cell(run['started_at'])} | "
                     f"`{run['input_hash'][:16]}` | {_cell(_json(run['stats_json']))} |")
    lines += ["", "## Interpretation cautions", "",
              "- The assessor-address anchor is confirmed; every other included parcel is a probable site association until a deed exhibit, recorded plan, or equivalent legal source confirms the assemblage.",
              "- Owner GLA, assessor building area, footprint area, marketed availability, physical occupancy, and percent leased are different measurements and must not be substituted for one another.",
              "- Estimated annual tax is assessment multiplied by the stated municipal rate; it is not an observed bill and excludes bill-specific adjustments.",
              "- Gross zoning coverage is a screening calculation, not net buildable area. Split-zone geometry, setbacks, parking, utilities, access, environmental conditions, and discretionary review can materially reduce capacity.",
              "- A clean exact-address listing or regulatory search is not proof of absence; aliases, parcel addresses, entity names, and database coverage can differ.",
              "- No prediction was generated in this pilot.", ""]
    return "\n".join(lines)


def write_reports(store: EvidenceStore, target_id: str, markdown_path: str | Path,
                  json_path: str | Path) -> dict[str, Any]:
    md = Path(markdown_path)
    js = Path(json_path)
    md.parent.mkdir(parents=True, exist_ok=True)
    js.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(render_markdown(store, target_id), encoding="utf-8")
    js.write_text(json.dumps(build_payload(store, target_id), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return {"markdown": str(md.resolve()), "json": str(js.resolve()),
            "markdown_bytes": md.stat().st_size, "json_bytes": js.stat().st_size}
