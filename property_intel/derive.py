from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from typing import Any

from .store import EvidenceStore
from .util import stable_id

VERSION = "property-derived-metrics/1.4.0"


def fingerprint(store: EvidenceStore, target_id: str, config: dict[str, Any] | None = None) -> str:
    rows = store.rows(
        "SELECT f.fact_id,f.value_json,f.status FROM facts f JOIN sources s ON s.source_id=f.source_id "
        "WHERE f.status='current' AND s.source_name NOT IN ('DealSynq derived property metrics','DealSynq property intelligence engines') AND "
        "(f.subject_id=? OR f.subject_id IN (SELECT parcel_id FROM grouping_decisions WHERE target_id=? AND included=1)) "
        "ORDER BY f.fact_id", (target_id, target_id))
    return stable_id("input", VERSION, config or {}, [(r[0], r[1], r[2]) for r in rows])


def _first(record: dict[str, Any], fields: list[str]) -> Any:
    for field in fields:
        value = record.get(field)
        if value not in (None, ""):
            return value
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _date(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(text, fmt).date().isoformat()
                except ValueError:
                    pass
    return None


def _records_for_predicates(store: EvidenceStore, target_id: str, predicates: list[str]) -> list[dict[str, Any]]:
    if not predicates:
        return []
    marks = ",".join("?" for _ in predicates)
    rows = store.rows(
        f"SELECT value_json FROM facts WHERE subject_id=? AND status='current' AND predicate IN ({marks})",
        (target_id, *predicates),
    )
    records: list[dict[str, Any]] = []
    for row in rows:
        value = json.loads(row["value_json"])
        if isinstance(value, dict):
            records.extend(record for record in value.get("records", []) if isinstance(record, dict))
    return records


def _materialize_executed_sales(store: EvidenceStore, target_id: str, source_id: str,
                                config: dict[str, Any]) -> int:
    sale_config = config.get("executed_sales") or {}
    predicates = sale_config.get("source_predicates") or []
    aliases = sale_config.get("field_aliases") or {}
    normalized: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in _records_for_predicates(store, target_id, predicates):
        address = _first(record, aliases.get("address", []))
        if not address:
            number = _first(record, aliases.get("street_number", []))
            street = _first(record, aliases.get("street_name", []))
            unit = _first(record, aliases.get("unit", []))
            address = " ".join(str(item).strip() for item in (number, street) if item not in (None, ""))
            if unit not in (None, ""):
                address = f"{address} Unit {unit}".strip()
        price = _number(_first(record, aliases.get("price", [])))
        sale_date = _date(_first(record, aliases.get("date", [])))
        area = _number(_first(record, aliases.get("area", [])))
        distance = _number(record.get("distance_m"))
        if not address or not price or price <= 0 or not sale_date:
            continue
        comp = {
            "address": str(address).strip(), "sale_date": sale_date,
            "sale_price_usd": round(price),
            "living_area_sqft": round(area, 1) if area and area > 0 else None,
            "price_per_sqft": round(price / area, 2) if area and area > 0 else None,
            "property_type": _first(record, aliases.get("property_type", [])),
            "distance_m": round(distance, 1) if distance is not None else None,
        }
        key = (comp["address"].upper(), sale_date, comp["sale_price_usd"])
        existing = normalized.get(key)
        if existing is None or (existing.get("distance_m") or float("inf")) > (comp.get("distance_m") or float("inf")):
            normalized[key] = comp
    comps = sorted(normalized.values(), key=lambda item: (
        item.get("distance_m") is None, item.get("distance_m") or 0, item["sale_date"], item["address"]))
    limit = int(sale_config.get("max_comparables", 25))
    comps = comps[:limit]
    if not comps:
        return 0
    prices = [item["sale_price_usd"] for item in comps]
    ppsf = [item["price_per_sqft"] for item in comps if item.get("price_per_sqft") is not None]
    result = {
        "comparable_count": len(comps),
        "median_sale_price_usd": round(statistics.median(prices)),
        "median_price_per_sqft": round(statistics.median(ppsf), 2) if ppsf else None,
        "comparables": comps,
        "normalization": "Configured field aliases; exact address/date/price deduplication; distance ordering.",
        "limitation": "Officially reported transfers are normalized screening observations, not adjusted appraisal selections; deed terms and arm's-length status require verification.",
    }
    raw = store.put_raw(result)
    store.fact(subject_id=target_id, category="market", predicate="executed_sale_comp",
               value=result, fact_class="calculation", confidence=0.86,
               source_id=source_id, parser_version=VERSION, raw_sha256=raw,
               evidence_locator="Normalized configured official executed-sale observations")
    return len(comps)


def _materialize_zoning_envelope(store: EvidenceStore, target_id: str, source_id: str,
                                 land: float, config: dict[str, Any]) -> bool:
    zoning_config = config.get("zoning_capacity") or {}
    rules = zoning_config.get("rules") or {}
    if not land or not rules:
        return False
    predicates = zoning_config.get("source_predicates") or ["official_city_zoning_map_intersection"]
    district_fields = zoning_config.get("district_fields") or []
    districts: list[str] = []
    for record in _records_for_predicates(store, target_id, predicates):
        value = _first(record, district_fields)
        if value not in (None, ""):
            districts.append(str(value).strip().upper())
    matched = [(district, rules[district]) for district in dict.fromkeys(districts) if district in rules]
    if not matched:
        return False
    alternatives = []
    for district, rule in matched:
        variants = rule if isinstance(rule, list) else [rule]
        for variant in variants:
            far = _number(variant.get("far"))
            coverage = _number(variant.get("lot_coverage_percent"))
            max_floor = round(land * far) if far is not None else None
            max_footprint = round(land * coverage / 100.0) if coverage is not None else None
            alternatives.append({
                "district": district, "variant": variant.get("variant"),
                "site_land_sqft": round(land), "far": far,
                "lot_coverage_percent": coverage,
                "screening_max_floor_area_sqft": max_floor,
                "screening_max_building_footprint_sqft": max_footprint,
                "height": variant.get("height"), "minimum_lot_sqft": variant.get("minimum_lot_sqft"),
            })
    result = {
        "districts_observed": list(dict.fromkeys(districts)), "alternatives": alternatives,
        "rules_source": zoning_config.get("rules_source"),
        "limitation": zoning_config.get("limitation") or "Dimensional-rule screening only; overlays, use permissions, bonuses, setbacks, parking, special permits, variances, legal lot status, and agency interpretation may change capacity.",
    }
    raw = store.put_raw(result)
    store.fact(subject_id=target_id, category="zoning", predicate="zoning_envelope_land_coverage",
               value=result, fact_class="calculation", confidence=0.72,
               source_id=source_id, parser_version=VERSION, raw_sha256=raw,
               evidence_locator="Configured zoning district rules applied to current mapped district and site land area")
    return True


def calculate(store: EvidenceStore, target_id: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    parcels = [r[0] for r in store.rows(
        "SELECT parcel_id FROM grouping_decisions WHERE target_id=? AND included=1 ORDER BY parcel_id", (target_id,))]
    building_total = 0.0
    for parcel in parcels:
        row = store.db.execute(
            "SELECT value_json FROM facts WHERE subject_id=? AND predicate='building_area' AND status='current' "
            "ORDER BY confidence DESC, observed_at DESC LIMIT 1", (parcel,)).fetchone()
        if row:
            building_total += float(json.loads(row[0]))
    land_row = store.db.execute(
        "SELECT value_json FROM facts WHERE subject_id=? AND predicate='site_land_area' AND status='current' "
        "ORDER BY observed_at DESC LIMIT 1", (target_id,)).fetchone()
    land = float(json.loads(land_row[0])) if land_row else 0.0
    raw = store.put_raw({"parcel_building_area_sum": building_total, "site_land_area": land,
                         "formulae": {"assessor_building_to_land_area_ratio":
                                      "sum(assessor building_area) / sum(assessor land_area)"},
                         "limitation": "Assessor building area is not proven gross floor area; this is a screening ratio, not zoning FAR."})
    source_id = store.source(name="DealSynq derived property metrics", url=None,
                             authority="calculation", parser_version=VERSION, raw_sha256=raw,
                             access_note="Deterministic calculations from cited current facts.")
    sourced_building_area = store.db.execute(
        "SELECT 1 FROM facts f JOIN sources s ON s.source_id=f.source_id "
        "WHERE f.subject_id=? AND f.predicate='site_building_area' AND f.status='current' "
        "AND s.source_name<>'DealSynq derived property metrics' LIMIT 1",
        (target_id,),
    ).fetchone()
    if sourced_building_area:
        store.db.execute(
            "UPDATE facts SET status='superseded' WHERE subject_id=? AND predicate='site_building_area' "
            "AND source_id IN (SELECT source_id FROM sources WHERE source_name='DealSynq derived property metrics')",
            (target_id,),
        )
    elif building_total:
        store.fact(subject_id=target_id, category="buildings", predicate="site_building_area",
                   value=round(building_total, 4), unit="sq_ft", fact_class="calculation", confidence=0.93,
                   source_id=source_id, parser_version=VERSION, raw_sha256=raw)
    if building_total and land:
        # Earlier builds called this `existing_far`, which implied a zoning/GFA
        # measurement the assessor source does not establish. Preserve the old
        # row for history but remove it from the current-state resolver.
        store.db.execute(
            "UPDATE facts SET status='superseded' WHERE subject_id=? AND predicate='existing_far' "
            "AND source_id IN (SELECT source_id FROM sources WHERE source_name='DealSynq derived property metrics')",
            (target_id,),
        )
        store.fact(subject_id=target_id, category="zoning", predicate="assessor_building_to_land_area_ratio",
                   value=round(building_total / land, 4), fact_class="calculation", confidence=0.9,
                   source_id=source_id, parser_version=VERSION, raw_sha256=raw,
                   evidence_locator="sum(assessor building_area) / site_land_area; screening proxy, not legal FAR")
        store.fact(subject_id=target_id, category="buildings", predicate="building_to_land_ratio",
                   value=round(building_total / land * 100, 2), unit="percent",
                   fact_class="calculation", confidence=0.9, source_id=source_id,
                   parser_version=VERSION, raw_sha256=raw)

    def current(predicate: str) -> Any:
        row = store.db.execute(
            "SELECT value_json FROM facts WHERE subject_id=? AND predicate=? AND status='current' "
            "ORDER BY confidence DESC,observed_at DESC LIMIT 1", (target_id, predicate)).fetchone()
        return json.loads(row[0]) if row else None

    marketed_gla = current("owner_reported_gla")
    marketed_available = current("marketed_available_area")
    if marketed_available is None:
        marketed_available = current("available_area")
    if isinstance(marketed_gla, (int, float)) and isinstance(marketed_available, (int, float)) and marketed_gla > 0:
        marketed_occupancy = max(0.0, min(100.0, (marketed_gla - marketed_available) / marketed_gla * 100.0))
        occupancy_raw = store.put_raw({
            "owner_reported_gla_sqft": marketed_gla,
            "marketed_available_area_sqft": marketed_available,
            "formula": "(owner_reported_gla - marketed_available_area) / owner_reported_gla * 100",
            "limitation": "Marketing availability is not audited physical occupancy and may differ from a public-company leased percentage.",
        })
        store.fact(subject_id=target_id, category="tenants",
                   predicate="calculated_marketed_occupancy_percent",
                   value=round(marketed_occupancy, 1), unit="percent",
                   fact_class="calculation", confidence=0.91, source_id=source_id,
                   parser_version=VERSION, raw_sha256=occupancy_raw,
                   evidence_locator="(owner_reported_gla - marketed_available_area) / owner_reported_gla * 100; not audited occupancy")

    assessed_value = current("site_assessed_value")
    commercial_tax_rate = current("commercial_tax_rate_per_1000")
    tax_fiscal_year = current("tax_rate_fiscal_year")
    if isinstance(assessed_value, (int, float)) and isinstance(commercial_tax_rate, (int, float)):
        estimated_tax = round(assessed_value / 1000.0 * commercial_tax_rate)
        tax_raw = store.put_raw({
            "site_assessed_value_usd": assessed_value,
            "commercial_tax_rate_per_1000": commercial_tax_rate,
            "tax_rate_fiscal_year": tax_fiscal_year,
            "formula": "site_assessed_value / 1000 * commercial_tax_rate_per_1000",
            "limitation": "Gross estimate before abatements, exemptions, special assessments, and bill-specific adjustments.",
        })
        store.fact(subject_id=target_id, category="tax", predicate="estimated_annual_tax",
                   value=estimated_tax, unit="USD", fact_class="calculation", confidence=0.94,
                   source_id=source_id, parser_version=VERSION, raw_sha256=tax_raw,
                   evidence_locator="site_assessed_value / 1000 * commercial_tax_rate_per_1000; gross estimate, not observed bill")

    geometry = current("analysis_geometry_summary") or current("analysis_geometry") or {}
    # Older builds reused the municipal-source predicate for a value calculated
    # from projected geometry. That made equivalent areas in different units
    # look contradictory (for example, sourced acres versus derived square
    # feet). Keep those rows for history, but remove them from current claims.
    store.db.execute(
        "UPDATE facts SET status='superseded' WHERE subject_id=? "
        "AND predicate='parcel_geometry_union_area' "
        "AND source_id IN (SELECT source_id FROM sources "
        "WHERE source_name='DealSynq derived property metrics')",
        (target_id,),
    )
    if geometry.get("area_sqft"):
        store.fact(subject_id=target_id, category="parcels",
                   predicate="projected_parcel_geometry_union_area",
                   value=geometry["area_sqft"], unit="sqft", fact_class="calculation", confidence=0.96,
                   source_id=source_id, parser_version=VERSION, raw_sha256=raw,
                   evidence_locator=f"Equal-area calculation in {geometry.get('analysis_crs')}")
    wetland = current("nwi_site_overlay") or {}
    flood = current("nfhl_site_overlay") or {}
    sfha_sqft = sum(float(item.get("intersection_sqft") or 0) for item in flood.get("classes", [])
                    if str(item.get("class") or "").upper().startswith(("A", "V")))
    wetland_sqft = float(wetland.get("intersection_sqft") or 0)
    analysis_land = float(geometry.get("area_sqft") or land or 0)
    if analysis_land and (wetland or flood):
        upper_bound_constraints = min(analysis_land, wetland_sqft + sfha_sqft)
        constraint_screen = {
            "analysis_land_sqft": round(analysis_land),
            "mapped_nwi_wetland_sqft": round(wetland_sqft),
            "mapped_sfha_sqft": round(sfha_sqft),
            "combined_constraint_area_upper_bound_sqft": round(upper_bound_constraints),
            "mapped_screen_remainder_sqft": round(max(0, analysis_land - upper_bound_constraints)),
            "method_note": "Wetland and SFHA areas are summed without overlap removal. The remainder is only the land not intersecting these two mapped layers; it is not net buildable area and does not account for unmapped or uncollected constraints.",
        }
        constraint_raw = store.put_raw(constraint_screen)
        store.fact(subject_id=target_id, category="zoning", predicate="mapped_constraint_screen",
                   value=constraint_screen, fact_class="calculation", confidence=0.62,
                   source_id=source_id, parser_version=VERSION, raw_sha256=constraint_raw,
                   evidence_locator="NWI mapped wetlands + FEMA SFHA overlay; overlap not removed")
    zoning_envelope = _materialize_zoning_envelope(store, target_id, source_id, land, config)
    executed_sales = _materialize_executed_sales(store, target_id, source_id, config)
    return {"site_building_sqft": round(building_total),
            "site_land_sqft": round(land), "geometry_union_sqft": geometry.get("area_sqft"),
            "assessor_building_to_land_area_ratio": round(building_total / land, 4) if land else None,
            "zoning_capacity_screen": zoning_envelope, "normalized_executed_sales": executed_sales}
