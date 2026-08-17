import json
import tempfile
import unittest
from pathlib import Path

from property_intel.derive import calculate
from property_intel.stage_engine import evaluate_pipeline
from property_intel.store import EvidenceStore


class PartialAreaCompletionTests(unittest.TestCase):
    def test_normalizes_executed_sales_and_calculates_zoning_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "derived.sqlite")
            try:
                target_id = "property:derived"
                store.upsert_target(target_id, "10 Main Street", "10 Main Street, Test, CT", {"_source_resolution": {"jurisdiction_id": "test_ct"}})
                store.entity("property_site", "10 Main Street", entity_id=target_id)
                raw = store.put_raw({"fixture": True})
                source = store.source(name="Official fixture", url="https://example.gov/gis", authority="official fixture", parser_version="test/1", raw_sha256=raw)
                store.fact(subject_id=target_id, category="spatial", predicate="site_land_area", value=10000,
                           unit="sq_ft", fact_class="confirmed_official", confidence=1, source_id=source,
                           parser_version="test/1", raw_sha256=raw)
                store.fact(subject_id=target_id, category="zoning", predicate="official_city_zoning_map_intersection",
                           value={"records": [{"ZoningDistrict": "R-10"}]}, fact_class="confirmed_official",
                           confidence=1, source_id=source, parser_version="test/1", raw_sha256=raw)
                records = [
                    {"Number": "8", "StreetName": "MAIN ST", "SaleDate": 1711929600000,
                     "SalePrice": "$500,000", "LivingArea": "2,000", "distance_m": 100},
                    {"Number": "8", "StreetName": "MAIN ST", "SaleDate": 1711929600000,
                     "SalePrice": 500000, "LivingArea": 2000, "distance_m": 110},
                    {"Addr_Full": "12 MAIN ST", "SLH_SALE_DATE": "03/01/2023",
                     "SLH_PRICE": 400000, "CNS_AREA_LIVING": 1600, "distance_m": 200},
                ]
                store.fact(subject_id=target_id, category="market", predicate="official_city_historical_sales_screen",
                           value={"records": records}, fact_class="confirmed_official", confidence=1,
                           source_id=source, parser_version="test/1", raw_sha256=raw)
                config = {
                    "executed_sales": {
                        "source_predicates": ["official_city_historical_sales_screen"],
                        "field_aliases": {
                            "address": ["Addr_Full"], "street_number": ["Number"],
                            "street_name": ["StreetName"], "date": ["SaleDate", "SLH_SALE_DATE"],
                            "price": ["SalePrice", "SLH_PRICE"], "area": ["LivingArea", "CNS_AREA_LIVING"],
                        },
                    },
                    "zoning_capacity": {
                        "source_predicates": ["official_city_zoning_map_intersection"],
                        "district_fields": ["ZoningDistrict"],
                        "rules": {"R-10": {"far": 0.5, "lot_coverage_percent": 20}},
                    },
                }
                result = calculate(store, target_id, config)
                self.assertEqual(result["normalized_executed_sales"], 2)
                comp = json.loads(store.db.execute(
                    "SELECT value_json FROM facts WHERE subject_id=? AND predicate='executed_sale_comp' AND status='current'",
                    (target_id,)).fetchone()[0])
                self.assertEqual(comp["comparable_count"], 2)
                self.assertEqual(comp["median_price_per_sqft"], 250)
                envelope = json.loads(store.db.execute(
                    "SELECT value_json FROM facts WHERE subject_id=? AND predicate='zoning_envelope_land_coverage' AND status='current'",
                    (target_id,)).fetchone()[0])
                self.assertEqual(envelope["alternatives"][0]["screening_max_floor_area_sqft"], 5000)
                self.assertEqual(envelope["alternatives"][0]["screening_max_building_footprint_sqft"], 2000)
            finally:
                store.close()

    def test_residential_tenant_history_and_natural_person_owner_are_not_applicable(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "semantics.sqlite")
            try:
                target_id = "property:home"
                owner_id = "organization:owner"
                store.upsert_target(target_id, "1 Home Road", "1 Home Road, Test, CT", {"_source_resolution": {"jurisdiction_id": "test_ct"}})
                store.entity("property_site", "1 Home Road", entity_id=target_id)
                store.entity("organization", "Jane Example", entity_id=owner_id)
                raw = store.put_raw({"fixture": True})
                source = store.source(name="Official fixture", url="https://example.gov", authority="official fixture", parser_version="test/1", raw_sha256=raw)
                store.relationship(from_id=owner_id, relationship_type="assessor_owner_of", to_id=target_id,
                                   fact_class="confirmed_official", confidence=1, source_id=source,
                                   parser_version="test/1", raw_sha256=raw)
                store.fact(subject_id=owner_id, category="ownership", predicate="ct_business_registry_match_screen",
                           value={"eligible_business_name": False, "match_count": 0}, fact_class="confirmed_official",
                           confidence=1, source_id=source, parser_version="test/1", raw_sha256=raw)
                store.fact(subject_id=target_id, category="asset_classification", predicate="hierarchical_asset_classification",
                           value={"preferred": {"primary": "residential", "subtype": "single_family"}}, fact_class="inference",
                           confidence=.9, source_id=source, parser_version="test/1", raw_sha256=raw)
                evaluate_pipeline(store, target_id)
                statuses = {row["stage_key"]: row["coverage_status"] for row in store.rows(
                    "SELECT stage_key,coverage_status FROM pipeline_stage_states WHERE target_id=?", (target_id,))}
                self.assertEqual(statuses["tenant_engine"], "complete")
                self.assertEqual(statuses["owner_graph"], "complete")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
