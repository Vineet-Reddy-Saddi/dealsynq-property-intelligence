import csv
import json
import tempfile
import unittest
from pathlib import Path

from property_intel.municipality import BATCH_ENGINES, activate_property, precompute
from property_intel.stage_engine import stage_catalog
from property_intel.store import EvidenceStore


class MunicipalityArchitectureTests(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        municipal_bundle = {
            "schema_version": "municipality-evidence-bundle/1.0.0",
            "default_source": {
                "ref": "official", "name": "Example municipal open data",
                "url": "https://example.gov/open-data",
                "authority": "official municipal source", "source_date": "2026-08-01",
            },
            "properties": [
                {
                    "ref": "site", "name": "Example Center",
                    "address": "10 Main Street, Example, MA 01000",
                    "external_id": "example-10-main",
                },
                {
                    "ref": "neighbor_site", "name": "Neighbor Center",
                    "address": "20 Main Street, Example, MA 01000",
                    "external_id": "example-20-main",
                },
            ],
            "entities": [
                {"ref": "parcel", "entity_type": "parcel", "canonical_name": "P-100",
                 "external_id": "example_ma:P-100", "property_refs": ["site"],
                 "aliases": [
                     {"alias_type": "parcel_identifier", "raw_value": "P-100", "normalized_value": "P 100"},
                     {"alias_type": "situs_address", "raw_value": "10 MAIN STREET", "normalized_value": "10 MAIN ST"},
                 ]},
                {"ref": "owner", "entity_type": "organization", "canonical_name": "Example Owner LLC",
                 "external_id": "ma:example-owner", "property_refs": ["site"]},
                {"ref": "deed", "entity_type": "recorded_document", "canonical_name": "Deed 1001",
                 "external_id": "deed:1001", "property_refs": ["site"]},
                {"ref": "mortgage", "entity_type": "recorded_document", "canonical_name": "Mortgage 2001",
                 "external_id": "mortgage:2001", "property_refs": ["site"]},
                {"ref": "permit", "entity_type": "permit", "canonical_name": "Permit BP-1",
                 "external_id": "permit:BP-1", "property_refs": ["site"]},
                {"ref": "neighbor_parcel", "entity_type": "parcel",
                 "canonical_name": "P-200", "external_id": "example_ma:P-200",
                 "property_refs": ["neighbor_site"],
                 "aliases": [{"alias_type": "situs_address", "raw_value": "20 MAIN STREET",
                              "normalized_value": "20 MAIN ST"}]},
            ],
            "facts": [
                {"subject_ref": "parcel", "category": "assessor", "predicate": "land_area",
                 "value": 10000, "unit": "sq_ft", "fact_class": "confirmed_official",
                 "confidence": 1.0, "source_ref": "official"},
                {"subject_ref": "site", "category": "calculation", "predicate": "site_land_area",
                 "value": 10000, "unit": "sq_ft", "fact_class": "calculation",
                 "confidence": 1.0, "source_ref": "official"},
                {"subject_ref": "site", "category": "spatial", "predicate": "analysis_geometry",
                 "value": {"type": "Polygon", "area_sqft": 10000},
                 "fact_class": "calculation", "confidence": 1.0, "source_ref": "official"},
                {"subject_ref": "site", "category": "spatial", "predicate": "parcel_geometry_union_area",
                 "value": 10000, "unit": "sq_ft", "fact_class": "calculation",
                 "confidence": 1.0, "source_ref": "official"},
                {"subject_ref": "site", "category": "hazards", "predicate": "nfhl_site_overlay",
                 "value": {"site_percent": 0, "classes": []},
                 "fact_class": "calculation", "confidence": 1.0, "source_ref": "official"},
                {"subject_ref": "parcel", "category": "zoning", "predicate": "zoning_code",
                 "value": "B-1", "fact_class": "confirmed_official",
                 "confidence": 1.0, "source_ref": "official"},
            ],
            "relationships": [
                {"from_ref": "owner", "relationship_type": "assessor_owner_of",
                 "to_ref": "parcel", "fact_class": "confirmed_official", "confidence": 1.0,
                 "source_ref": "official"},
                {"from_ref": "deed", "relationship_type": "affects",
                 "to_ref": "parcel", "fact_class": "confirmed_official", "confidence": 1.0,
                 "source_ref": "official"},
                {"from_ref": "mortgage", "relationship_type": "encumbers",
                 "to_ref": "parcel", "fact_class": "confirmed_official", "confidence": 1.0,
                 "source_ref": "official"},
                {"from_ref": "permit", "relationship_type": "affects",
                 "to_ref": "parcel", "fact_class": "confirmed_official", "confidence": 1.0,
                 "source_ref": "official"},
            ],
            "memberships": [{
                "property_ref": "site", "parcel_ref": "parcel", "included": True,
                "score": 1.0, "threshold": 0.6,
                "evidence": {"classification": "confirmed_anchor", "address_match": {"matched": True}},
            }],
            "events": [{
                "property_ref": "site", "subject_ref": "deed", "event_type": "deed_recorded",
                "event_date": "2020-01-01", "date_precision": "day",
                "summary": "Deed recorded", "fact_class": "confirmed_official",
                "confidence": 1.0, "source_refs": ["official"],
            }],
        }
        on_demand_bundle = {
            "schema_version": "property-evidence-bundle/1.0.0",
            "default_source": {"ref": "web", "name": "Approved tenant fixture",
                               "url": "https://example.com/tenant",
                               "authority": "approved public source"},
            "entities": [{"ref": "tenant", "entity_type": "tenant",
                          "canonical_name": "Example Tenant"}],
            "relationships": [{"from_ref": "tenant", "relationship_type": "reported_occupant_of",
                               "to_ref": "$target", "fact_class": "reported",
                               "confidence": 0.8, "source_ref": "web"}],
        }
        engine_config = [
            {"key": engine.key, "adapter": "municipality_bundle",
             "coverage_status": "complete",
             "completion_basis": "Fixture provides complete rows for this synthetic scope",
             "config": {"bundles": [municipal_bundle]}}
            for engine in BATCH_ENGINES if engine.required
        ]
        engine_config.append({"key": "infrastructure", "status": "not_applicable", "required": False,
                              "reason": "Fixture has no infrastructure source"})
        config = {
            "scope": {"id": "example_ma", "scope_type": "municipality",
                      "jurisdiction_id": "example_ma", "name": "Example Municipality, Massachusetts",
                      "state_code": "MA"},
            "database": str(root / "municipality.sqlite"),
            "output_root": str(root),
            "batch_engines": engine_config,
            "on_demand_engines": [{"key": "tenant_fixture", "adapter": "canonical_bundle",
                                   "live": False, "config": {"bundles": [on_demand_bundle]}}],
        }
        path = root / "scope.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_batch_precompute_then_property_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self._fixture(root)
            first = precompute(config)
            second = precompute(config)
            self.assertTrue(first["complete"])
            self.assertEqual(first["counts"]["properties"], 2)
            self.assertTrue(all(row["status"] == "skipped_unchanged"
                                for row in second["engines"] if row["engine"] != "infrastructure"))

            # The batch source stores a situs-only alias while a production
            # search may contain either the situs or full postal form.
            result = activate_property(config, address="10 Main St")
            self.assertEqual(result["precomputed_scope_id"], "example_ma")
            self.assertTrue(result["validation"]["passed"])
            self.assertTrue(Path(result["reports"]["markdown"]).exists())
            payload = json.loads(Path(result["reports"]["json"]).read_text(encoding="utf-8"))
            entity_types = {row["entity_type"] for row in payload["entities"]}
            self.assertIn("parcel", entity_types)
            self.assertIn("recorded_document", entity_types)
            self.assertIn("permit", entity_types)
            self.assertIn("tenant", entity_types)
            self.assertNotIn("P-200", {row["canonical_name"] for row in payload["entities"]})

            source = EvidenceStore(root / "municipality.sqlite")
            try:
                property_id = result["property_id"]
                parcel_id = source.db.execute(
                    "SELECT parcel_id FROM grouping_decisions WHERE target_id=?",
                    (property_id,),
                ).fetchone()[0]
                source.record_decision(
                    property_id, parcel_id, True, 1.0, 0.6,
                    {"classification": "upgraded fixture"}, "fixture-upgrade/2",
                )
                source.db.commit()
            finally:
                source.close()
            refreshed = activate_property(config, address="10 Main St")
            activation = EvidenceStore(refreshed["database"])
            try:
                self.assertEqual(activation.db.execute(
                    "SELECT COUNT(*) FROM grouping_decisions WHERE target_id=?",
                    (refreshed["property_id"],),
                ).fetchone()[0], 1)
            finally:
                activation.close()

    def test_successful_collection_is_not_silently_called_complete(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = self._fixture(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            assessor = next(item for item in config["batch_engines"] if item["key"] == "assessor")
            assessor.pop("coverage_status")
            assessor.pop("completion_basis")
            config_path.write_text(json.dumps(config), encoding="utf-8")

            result = precompute(config_path)
            state = next(row for row in result["batch_engines"] if row["engine_key"] == "assessor")
            self.assertEqual(state["coverage_status"], "partial")
            self.assertFalse(result["complete"])

    def test_mapped_tabular_adapter_precomputes_without_property_hardcoding(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "municipality.csv"
            columns = ["status", "pid", "address", "owner", "zone", "use",
                       "year", "land", "building", "total", "sale_date", "sale_price",
                       "geometry", "geometry_area"]
            geometry = json.dumps({
                "type": "Polygon",
                "coordinates": [[[-73.54, 41.05], [-73.5399, 41.05],
                                 [-73.5399, 41.0501], [-73.54, 41.0501],
                                 [-73.54, 41.05]]],
            })
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerow({"status": "matched", "pid": "P-1",
                                 "address": "10 MAIN STREET", "owner": "OWNER LLC",
                                 "zone": "C", "use": "Commercial", "year": "2025",
                                 "land": "1.5", "building": "10000", "total": "500000",
                                 "sale_date": "01/15/2025", "sale_price": "750000",
                                 "geometry": geometry, "geometry_area": "71000"})
            fields = {
                "join_status": "status", "parcel_id": "pid", "address": "address",
                "owner": "owner", "zoning": "zone", "use_description": "use",
                "assessment_year": "year", "land_area": "land",
                "building_area": "building", "assessed_total": "total",
                "last_sale_date": "sale_date", "last_sale_price": "sale_price",
                "geometry": "geometry", "geometry_area": "geometry_area",
            }
            config = {
                "scope": {"id": "mapped_ma", "scope_type": "municipality",
                          "jurisdiction_id": "mapped_ma", "name": "Mapped, Massachusetts",
                          "state_code": "MA"},
                "database": str(root / "mapped.sqlite"), "output_root": str(root),
                "batch_engines": [{
                    "key": "assessor", "adapter": "tabular_municipality",
                    "coverage_status": "partial",
                    "reason": "Synthetic mapped fixture",
                    "config": {
                        "path": str(csv_path), "address_suffix": ", Mapped, MA 01000",
                        "fields": fields,
                        "transaction_document": {
                            "date_field": "sale_date", "price_field": "sale_price",
                        },
                        "assessor_source": {"name": "Mapped assessor fixture",
                                            "authority": "official fixture"},
                        "parcel_source": {"name": "Mapped parcel fixture",
                                          "authority": "official fixture"},
                    },
                }],
            }
            config_path = root / "mapped.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            result = precompute(config_path)
            self.assertEqual(result["counts"]["properties"], 1)
            activated = activate_property(config_path, address="10 Main Street")
            self.assertTrue(activated["validation"]["passed"])
            self.assertEqual(activated["contradictions"], 0)
            payload = json.loads(Path(activated["reports"]["json"]).read_text(encoding="utf-8"))
            self.assertIn("recorded_document", {
                row["entity_type"] for row in payload["entities"]
            })
            self.assertEqual(
                activated["validation"]["semantic_checks"]["timeline"]["events"], 2)

    def test_stage_catalog_declares_batch_and_on_demand_modes(self):
        catalog = stage_catalog()
        by_key = {row["key"]: row for row in catalog}
        self.assertEqual(by_key["parcel_database"]["execution_mode"], "batch")
        self.assertEqual(by_key["land_records"]["execution_mode"], "batch")
        self.assertEqual(by_key["permit_planning"]["execution_mode"], "batch")
        self.assertEqual(by_key["environmental_hazard"]["execution_mode"], "batch")
        self.assertEqual(by_key["tenant_engine"]["execution_mode"], "on_demand")
        self.assertEqual(by_key["market_intel"]["execution_mode"], "on_demand")
        self.assertEqual(by_key["current_property_state"]["execution_mode"], "materialize")


if __name__ == "__main__":
    unittest.main()
