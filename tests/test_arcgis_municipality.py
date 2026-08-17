from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from property_intel.adapters.arcgis_municipality import collect
from property_intel.store import EvidenceStore


class ArcGISMunicipalityTests(unittest.TestCase):
    def test_bulk_adapter_records_a_result_for_every_indexed_property(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "bulk.sqlite")
            try:
                scope = {"id": "fixture_ct", "jurisdiction_id": "fixture_ct",
                         "scope_type": "municipality", "name": "Fixture, CT"}
                store.upsert_collection_scope(
                    scope["id"], scope["scope_type"], scope["jurisdiction_id"],
                    scope["name"], {}, state_code="CT",
                )
                raw = store.put_raw({"fixture": True})
                source = store.source(
                    name="Fixture assessor", url="https://example.gov/assessor",
                    authority="official fixture", parser_version="test/1", raw_sha256=raw,
                )
                for number in (1, 2):
                    property_id = f"property:{number}"
                    parcel_id = f"parcel:{number}"
                    store.upsert_indexed_property(
                        property_id=property_id, scope_id=scope["id"],
                        name=f"{number} Example Road Property",
                        address=f"{number} Example Road, Fixture, CT",
                        normalized_address=f"{number} EXAMPLE RD FIXTURE CT",
                    )
                    store.entity("property_site", f"Property {number}", entity_id=property_id)
                    store.entity("parcel", str(number), entity_id=parcel_id)
                    store.alias(parcel_id, "parcel_identifier", str(number), str(number),
                                source_id=source)
                    store.alias(parcel_id, "parcel_identifier", "SHARED", "SHARED",
                                source_id=source)
                    store.link_property_entity(property_id=property_id, entity_id=property_id,
                                               role="property_site", source_id=source)
                    store.link_property_entity(property_id=property_id, entity_id=parcel_id,
                                               role="parcel", source_id=source)
                    store.record_decision(property_id, parcel_id, True, 1.0, 0.6,
                                          {"basis": "fixture"}, "test/1")

                polygon = {"type": "Polygon", "coordinates": [[
                    [-73.54, 41.05], [-73.53, 41.05], [-73.53, 41.06],
                    [-73.54, 41.06], [-73.54, 41.05],
                ]]}
                parcel_features = [{
                    "geometry": polygon, "properties": {"ParcelID": 1, "UNQ_CARD": "SHARED", "OBJECTID": 10},
                    "object_id": 10, "raw_sha256": raw,
                }]
                zoning_features = [{
                    "geometry": polygon,
                    "properties": {"OBJECTID": 20, "ZoningDistrict": "C-B"},
                    "object_id": 20, "raw_sha256": raw,
                }]
                config = {
                    "geocode_unmatched": False,
                    "parcel_layer": {
                        "key": "parcels", "label": "Parcels",
                        "url": "https://example.gov/parcels", "identifier_fields": ["ParcelID", "UNQ_CARD"],
                        "out_fields": ["ParcelID", "UNQ_CARD", "OBJECTID"],
                        "source": {"name": "Fixture parcels", "authority": "official fixture"},
                    },
                    "layers": [{
                        "key": "zoning", "label": "Zoning",
                        "url": "https://example.gov/zoning", "mode": "intersection",
                        "category": "zoning", "predicate": "official_city_zoning_map_intersection",
                        "out_fields": ["OBJECTID", "ZoningDistrict"],
                        "source": {"name": "Fixture zoning", "authority": "official fixture"},
                    }],
                }
                with patch(
                    "property_intel.adapters.arcgis_municipality._download_features",
                    side_effect=[(parcel_features, raw), (zoning_features, raw)],
                ):
                    stats = collect(store, scope, config)

                self.assertEqual(stats["properties_with_analysis_geometry"], 1)
                self.assertEqual(stats["layers"]["zoning"]["properties"], 2)
                self.assertEqual(stats["layers"]["zoning"]["evaluated"], 1)
                rows = store.rows(
                    "SELECT subject_id,value_json FROM facts WHERE predicate=? ORDER BY subject_id",
                    ("official_city_zoning_map_intersection",),
                )
                self.assertEqual(len(rows), 2)
                values = {row["subject_id"]: json.loads(row["value_json"]) for row in rows}
                self.assertTrue(values["property:1"]["evaluated"])
                self.assertEqual(values["property:1"]["feature_count"], 1)
                self.assertFalse(values["property:2"]["evaluated"])
                self.assertEqual(values["property:2"]["feature_count"], 0)

                refreshed_raw = store.put_raw({"fixture": "refreshed"})
                refreshed_zoning = [{**zoning_features[0], "raw_sha256": refreshed_raw}]
                with patch(
                    "property_intel.adapters.arcgis_municipality._download_features",
                    side_effect=[(parcel_features, raw), (refreshed_zoning, refreshed_raw)],
                ):
                    collect(store, scope, config)
                current = store.db.execute(
                    "SELECT COUNT(*) FROM facts WHERE predicate=? AND status='current'",
                    ("official_city_zoning_map_intersection",),
                ).fetchone()[0]
                superseded = store.db.execute(
                    "SELECT COUNT(*) FROM facts WHERE predicate=? AND status='superseded'",
                    ("official_city_zoning_map_intersection",),
                ).fetchone()[0]
                self.assertEqual(current, 2)
                self.assertEqual(superseded, 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
