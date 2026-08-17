"""Reconcile assessor-only Stamford records against the current City parcel inventory.

This is deliberately a *negative* reconciliation: an assessor row is never
given a polygon merely because a current parcel has a similar owner, street, or
adjacent account number.  When the official City service has no feature for an
old identifier, the row remains in the evidence store as ``legacy_unmappable``
and is excluded from the active spatial inventory.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

from property_intel.store import EvidenceStore
from property_intel.util import canonical_json, utcnow


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "pilots" / "stamford_ct" / "data" / "stamford_ct_precomputed.sqlite"
OUTPUT = ROOT / "pilots" / "stamford_ct" / "reports" / "stamford_ct_parcel_inventory_reconciliation.json"
CITY_LAYER = (
    "https://services3.arcgis.com/V28nrLFIbcYZbnPl/arcgis/rest/services/"
    "Parcels_AssessorJoin_20260130/FeatureServer/0"
)
LEGACY_IDS = (
    "730", "3697", "4649", "4837", "4838", "4839", "5493", "9344", "18083",
    "20197", "20525", "20582", "24208", "24209", "24248", "24342", "27470",
    "33736", "190997",
)
PARSER_VERSION = "stamford-parcel-inventory-reconciliation/1.0.0"


def _city_lookup(parcel_id: str) -> dict:
    response = requests.get(
        f"{CITY_LAYER}/query",
        params={
            "f": "json", "where": f"ParcelID = {int(parcel_id)}",
            "outFields": "OBJECTID,ParcelID,AccountNumber,PropertyAddress,Owner",
            "returnGeometry": "false",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload


def main() -> None:
    lookups = {parcel_id: _city_lookup(parcel_id) for parcel_id in LEGACY_IDS}
    missing = [parcel_id for parcel_id, payload in lookups.items() if not payload.get("features")]
    if set(missing) != set(LEGACY_IDS):
        found = sorted(set(LEGACY_IDS) - set(missing))
        raise RuntimeError(f"Current City parcel IDs unexpectedly found: {found}")

    store = EvidenceStore(DB)
    try:
        raw_sha = store.put_raw({"layer": CITY_LAYER, "lookups": lookups})
        source_id = store.source(
            name="City of Stamford 2026 parcel-assessor inventory reconciliation",
            url=CITY_LAYER, authority="City of Stamford",
            parser_version=PARSER_VERSION, raw_sha256=raw_sha,
            access_note="Direct current-ParcelID checks for retained legacy assessor rows.",
        )
        rows = store.rows(
            "SELECT p.property_id,p.address,p.external_id,e.external_id AS parcel_external_id "
            "FROM property_index p JOIN property_entity_links l ON l.property_id=p.property_id AND l.role='parcel' "
            "JOIN entities e ON e.entity_id=l.entity_id "
            "WHERE p.scope_id='stamford_ct' AND e.external_id IN (%s) "
            "ORDER BY p.address,e.external_id" % ",".join("?" for _ in LEGACY_IDS),
            tuple(f"stamford_ct:{parcel_id}" for parcel_id in LEGACY_IDS),
        )
        properties: dict[str, dict] = {}
        for row in rows:
            record = properties.setdefault(row["property_id"], {
                "property_id": row["property_id"], "address": row["address"],
                "external_id": row["external_id"], "legacy_parcel_ids": [],
            })
            record["legacy_parcel_ids"].append(row["parcel_external_id"].split(":", 1)[1])

        if len(properties) != 14:
            raise RuntimeError(f"Expected 14 legacy sites, found {len(properties)}")
        with store.transaction():
            for record in properties.values():
                store.db.execute(
                    "UPDATE property_index SET status='legacy_unmappable',updated_at=? WHERE property_id=?",
                    (utcnow(), record["property_id"]),
                )
                store.fact(
                    subject_id=record["property_id"], category="spatial",
                    predicate="parcel_inventory_reconciliation",
                    value={
                        "status": "legacy_unmappable",
                        "legacy_parcel_ids": sorted(record["legacy_parcel_ids"], key=int),
                        "rule": "no current City ParcelID feature; no boundary inferred",
                    }, fact_class="confirmed_official", confidence=0.99,
                    source_id=source_id, parser_version=PARSER_VERSION, raw_sha256=raw_sha,
                    evidence_locator="direct City ParcelID query returned zero features",
                )
            active = store.db.execute(
                "SELECT COUNT(*) FROM property_index WHERE scope_id='stamford_ct' AND status!='legacy_unmappable'"
            ).fetchone()[0]
            geometries = store.db.execute(
                "SELECT COUNT(DISTINCT p.property_id) FROM property_index p JOIN facts f "
                "ON f.subject_id=p.property_id AND f.predicate='analysis_geometry' AND f.status='current' "
                "WHERE p.scope_id='stamford_ct' AND p.status!='legacy_unmappable'"
            ).fetchone()[0]
            if geometries != active:
                raise RuntimeError(f"Active geometry coverage is {geometries}/{active}, not complete")
            prior = store.db.execute(
                "SELECT metrics_json FROM scope_engine_states WHERE scope_id='stamford_ct' AND engine_key='parcel_gis'"
            ).fetchone()
            metrics = json.loads(prior[0]) if prior and prior[0] else {}
            metrics.update({
                "indexed_properties": active,
                "properties_with_analysis_geometry": geometries,
                "active_geometry_coverage": 1.0,
                "legacy_assessor_only_property_sites": len(properties),
                "legacy_assessor_only_parcel_rows": len(LEGACY_IDS),
                "reconciliation_rule": "No current City ParcelID feature; never infer a boundary",
            })
            store.scope_engine_state(
                scope_id="stamford_ct", engine_key="parcel_gis", execution_mode="batch",
                coverage_status="complete", required=True, adapter="arcgis_municipality",
                dependencies=["assessor"], metrics=metrics,
                reason="All active City parcel sites have official analysis geometry; retained legacy assessor-only rows are explicit exclusions.",
            )
        report = {
            "scope": "stamford_ct", "official_layer": CITY_LAYER,
            "active_property_sites": active, "active_sites_with_analysis_geometry": geometries,
            "active_geometry_coverage": geometries / active if active else 0,
            "legacy_assessor_only_property_sites": len(properties),
            "legacy_assessor_only_parcel_rows": len(LEGACY_IDS),
            "rule": "No current City ParcelID feature; no boundary inferred.",
            "properties": sorted(properties.values(), key=lambda item: item["address"]),
        }
        OUTPUT.write_text(canonical_json(report) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
