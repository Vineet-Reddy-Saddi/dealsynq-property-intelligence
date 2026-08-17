from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

from ..store import EvidenceStore
from ..util import haversine_m, normalize_address, normalize_text, stable_id

PARSER_VERSION = "on-market-canonical-csv/1.2.0"


def _target_property_type(store: EvidenceStore, target_id: str) -> tuple[str | None, list[str]]:
    parcel_ids = [row[0] for row in store.rows(
        "SELECT parcel_id FROM grouping_decisions WHERE target_id=? AND included=1", (target_id,))]
    subjects = [target_id] + parcel_ids
    marks = ",".join("?" for _ in subjects)
    use_rows = store.rows(
        f"SELECT predicate,value_json FROM facts WHERE subject_id IN ({marks}) "
        "AND predicate IN ('use_class','structure_type') AND status='current' "
        "ORDER BY CASE predicate WHEN 'use_class' THEN 0 ELSE 1 END,confidence DESC",
        tuple(subjects),
    )
    signals = [str(json.loads(row["value_json"])) for row in use_rows]
    # Parcel use class takes precedence over a building-card label because a
    # mixed building can contain incidental retail/office space.
    primary = [str(json.loads(row["value_json"])) for row in use_rows if row["predicate"] == "use_class"]
    corpus = normalize_text(" ".join(primary or signals))
    rules = (
        ("industrial", ("INDUSTRIAL", "MANUFACTURING", "WAREHOUSE", "DISTRIBUTION")),
        ("retail", ("RETAIL", "SHOPPING", "SUPERMARKET", "STORE")),
        ("office", ("OFFICE",)),
        ("multifamily", ("MULTIFAMILY", "APARTMENT")),
        ("hospitality", ("HOTEL", "MOTEL", "LODGING")),
    )
    return next((kind for kind, terms in rules if any(term in corpus for term in terms)), None), signals


def fingerprint(config: dict[str, Any]) -> str:
    files = []
    for name in config.get("paths", []):
        p = Path(name)
        stat = p.stat()
        files.append((str(p.resolve()), stat.st_size, stat.st_mtime_ns))
    return stable_id("input", PARSER_VERSION, files, config)


def collect(store: EvidenceStore, target_id: str, target: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    store.db.execute(
        "UPDATE facts SET status='superseded' WHERE subject_id=? AND predicate='nearby_listing_comparable_screen'",
        (target_id,),
    )
    old_ids = [row[0] for row in store.rows(
        "SELECT DISTINCT e.entity_id FROM entities e JOIN relationships r ON r.from_entity_id=e.entity_id "
        "WHERE r.to_entity_id=? AND e.entity_type IN ('listing','market_comparable_listing')",
        (target_id,),
    )]
    if old_ids:
        marks = ",".join("?" for _ in old_ids)
        store.db.execute(f"DELETE FROM temporal_states WHERE subject_id IN ({marks})", tuple(old_ids))
        store.db.execute(f"DELETE FROM facts WHERE subject_id IN ({marks})", tuple(old_ids))
        store.db.execute(f"DELETE FROM relationships WHERE from_entity_id IN ({marks}) OR to_entity_id IN ({marks})",
                         tuple(old_ids) + tuple(old_ids))
        store.db.execute(f"DELETE FROM entities WHERE entity_id IN ({marks})", tuple(old_ids))
    target_street = normalize_address(target["address"].split(",")[0])
    matches: list[dict[str, str]] = []
    nearby: list[tuple[float, dict[str, str]]] = []
    center_row = store.db.execute(
        "SELECT value_json FROM facts WHERE subject_id=? AND predicate IN ('geocoded_centroid','analysis_geometry') "
        "AND status='current' ORDER BY CASE predicate WHEN 'analysis_geometry' THEN 0 ELSE 1 END LIMIT 1", (target_id,)).fetchone()
    center = None
    if center_row:
        value = json.loads(center_row[0])
        center = value.get("centroid") if isinstance(value, dict) and value.get("centroid") else value
    target_type, classification_signals = _target_property_type(store, target_id)
    radius_m = float(config.get("comparable_radius_miles", 5)) * 1609.344
    csv.field_size_limit(sys.maxsize)
    scanned = 0
    inventory_types: set[str] = set()
    for name in config.get("paths", []):
        with Path(name).open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                scanned += 1
                row_type = normalize_text(row.get("property_type")).lower()
                if row_type:
                    inventory_types.add(row_type)
                if normalize_address(row.get("address")) == target_street:
                    matches.append(row)
                elif center and row.get("lat") and row.get("lng"):
                    try:
                        distance = haversine_m((float(center["lat"]), float(center["lon"])),
                                               (float(row["lat"]), float(row["lng"])))
                    except (TypeError, ValueError, KeyError):
                        distance = None
                    if distance is not None and distance <= radius_m and target_type and row_type == target_type:
                        nearby.append((distance, row))
    nearby.sort(key=lambda pair: pair[0])

    def ingest(row: dict[str, str], *, comparable_distance_m: float | None = None) -> None:
        raw = store.put_raw(row)
        source_id = store.source(name=f"{row.get('source_site') or 'public'} market listing",
                                 url=row.get("source_url"), authority="public market listing",
                                 parser_version=PARSER_VERSION, raw_sha256=raw,
                                 source_date=row.get("updated_on") or row.get("listed_on"),
                                 retrieved_at=row.get("last_seen"),
                                 access_note="Read from existing canonical listing inventory; no protected source accessed by this pipeline.")
        kind = "listing" if comparable_distance_m is None else "market_comparable_listing"
        listing_id = store.entity(kind, row.get("name") or row.get("address") or "Listing",
                                  external_id=f"{row.get('source_site')}:{row.get('source_listing_id')}", attributes=row)
        store.relationship(from_id=listing_id,
                           relationship_type="markets" if comparable_distance_m is None else "nearby_market_evidence_for",
                           to_id=target_id, fact_class="reported",
                           confidence=0.9 if comparable_distance_m is None else 0.7, source_id=source_id,
                           parser_version=PARSER_VERSION, raw_sha256=raw)
        store.temporal_state(target_id=target_id, subject_id=listing_id, state_type="listing_lifecycle",
                             value={"status": row.get("source_status"), "transaction_type": row.get("transaction_type"),
                                    "price": row.get("price"), "price_basis": row.get("price_basis")},
                             source_id=source_id, confidence=0.8, fact_class="reported",
                             parser_version=PARSER_VERSION, valid_from=row.get("listed_on") or row.get("first_seen"),
                             valid_to=row.get("delisted_on"), first_seen=row.get("first_seen"), last_seen=row.get("last_seen"))
        for predicate in ("transaction_type", "price", "price_basis", "sqft", "lot_size_acres",
                          "cap_rate", "first_seen", "last_seen", "delisted_on", "source_status"):
            if row.get(predicate) not in (None, ""):
                store.fact(subject_id=listing_id, category="market", predicate=predicate,
                           value=row[predicate], fact_class="reported", confidence=0.8,
                           source_id=source_id, parser_version=PARSER_VERSION, raw_sha256=raw)
        if comparable_distance_m is not None:
            store.fact(subject_id=listing_id, category="market", predicate="distance_from_subject",
                       value=round(comparable_distance_m / 1609.344, 3), unit="miles",
                       fact_class="calculation", confidence=0.95, source_id=source_id,
                       parser_version=PARSER_VERSION, raw_sha256=raw,
                       evidence_locator="Haversine distance between listing coordinate and subject analysis centroid")

    for row in matches:
        ingest(row)
    max_comps = int(config.get("max_comparable_listings", 50))
    for distance, row in nearby[:max_comps]:
        ingest(row, comparable_distance_m=distance)
    missing_description = "No exact-address listing found in the existing public listing inventory"
    if not matches:
        store.gap(target_id, "market", "missing", missing_description,
                  reason="Absence is not proof that the property is off market; aliases and suite-level addresses may differ.")
    else:
        store.resolve_gap(target_id, missing_description,
                          reason=f"Resolved: {len(matches)} exact-address listing(s) found in latest inventory scan.")
    type_gap = "No compatible property type is available for nearby listing comparison"
    if not target_type:
        store.gap(target_id, "market", "partial", type_gap,
                  reason=f"Subject type could not be resolved from source signals: {classification_signals}")
    elif target_type not in inventory_types:
        store.gap(target_id, "market", "partial", type_gap,
                  reason=f"Subject type is {target_type}; inventory currently contains {sorted(inventory_types)}. Incompatible listings were excluded.")
    else:
        store.resolve_gap(target_id, type_gap,
                          reason=f"Inventory contains the subject type {target_type}.")
    summary = {"rows_scanned": scanned, "exact_address_matches": len(matches),
               "nearby_listing_comparables": min(len(nearby), max_comps),
               "comparable_radius_miles": radius_m / 1609.344,
               "target_property_type_filter": target_type,
               "classification_signals": classification_signals,
               "available_inventory_types": sorted(inventory_types),
               "comparables_note": "Nearby asking/listing evidence only; not verified executed sale or lease comparables."}
    if nearby:
        source = store.source(name="DealSynq listing comparable screen", url=None, authority="calculation",
                              parser_version=PARSER_VERSION, raw_sha256=store.put_raw(summary),
                              access_note="Spatial screen over existing canonical public listing inventory.")
        store.fact(subject_id=target_id, category="market", predicate="nearby_listing_comparable_screen",
                   value=summary, fact_class="calculation", confidence=0.72,
                   source_id=source, parser_version=PARSER_VERSION,
                   evidence_locator="Configured radius, optional inferred asset-type filter")
    return summary
