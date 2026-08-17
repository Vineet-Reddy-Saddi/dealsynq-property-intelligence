from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from property_intel.municipality import activate_property, precompute
from property_intel.derive import calculate
from property_intel.geometry import site_geometry
from property_intel.http_client import FetchResult
from property_intel.store import EvidenceStore
from property_intel.adapters.arcgis_context import collect as collect_arcgis_context
from property_intel.adapters.ct_registry import collect as collect_ct_registry


class EngineRobustnessTests(unittest.TestCase):
    def test_ct_registry_records_honest_negative_business_and_ucc_screens(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "registry.sqlite")
            try:
                target_id = "property:registry"
                owner_id = "organization:registry"
                store.entity("property_site", "Registry Property", entity_id=target_id)
                store.entity("organization", "EXAMPLE OWNER LLC", entity_id=owner_id)
                raw_sha = store.put_raw([])
                source_id = store.source(
                    name="Assessor fixture", url="https://example.gov/assessor",
                    authority="official fixture", parser_version="test/1",
                    raw_sha256=raw_sha,
                )
                store.relationship(
                    from_id=owner_id, relationship_type="assessor_owner_of",
                    to_id=target_id, fact_class="confirmed_official", confidence=1.0,
                    source_id=source_id, parser_version="test/1", raw_sha256=raw_sha,
                )
                empty = FetchResult(
                    url="https://data.ct.gov/resource/fixture.json", status_code=200,
                    content_type="application/json", content=b"[]",
                    raw_sha256=raw_sha, retrieved_at="2026-08-16T00:00:00+00:00",
                    from_cache=False,
                )
                with patch("property_intel.adapters.ct_registry.PublicHttpClient.fetch",
                           side_effect=[empty, empty]):
                    stats = collect_ct_registry(
                        store, target_id, {"address": "1 Example Road"},
                        {"refresh_days": 1},
                    )
                self.assertEqual(stats["registry_matches"], 0)
                self.assertEqual(stats["active_ucc_records"], 0)
                self.assertFalse(stats["errors"])
                predicates = {row[0] for row in store.rows(
                    "SELECT predicate FROM facts WHERE subject_id=?", (owner_id,)
                )}
                self.assertIn("ct_business_registry_match_screen", predicates)
                self.assertIn("ct_active_ucc_lien_screen", predicates)
                capabilities = {row["capability"]: row["status"] for row in store.rows(
                    "SELECT capability,status FROM source_capabilities WHERE target_id=?",
                    (target_id,),
                )}
                self.assertEqual(capabilities["entity_registry"], "working")
                self.assertEqual(capabilities["mortgages_liens"], "partial")
            finally:
                store.close()

    def test_arcgis_context_materializes_overlay_and_development_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "arcgis.sqlite")
            try:
                target_id = "property:arcgis"
                parcel_id = "parcel:arcgis"
                store.upsert_collection_scope("neutral", "municipality", "neutral", "Neutral", {})
                store.upsert_indexed_property(
                    property_id=target_id, scope_id="neutral", name="Neutral Property",
                    address="1 Example Road", normalized_address="1 EXAMPLE RD",
                )
                store.entity("property_site", "Neutral Property", entity_id=target_id)
                store.entity("parcel", "P-1", entity_id=parcel_id)
                store.record_decision(target_id, parcel_id, True, 1.0, 0.6,
                                      {"basis": "fixture"}, "test/1")
                raw = store.put_raw({"geometry": "fixture"})
                source = store.source(
                    name="Official parcel fixture", url="https://example.gov/parcel",
                    authority="official fixture", parser_version="test/1", raw_sha256=raw,
                )
                store.fact(
                    subject_id=parcel_id, category="spatial", predicate="parcel_geometry",
                    value={"type": "Polygon", "coordinates": [[
                        [-71.4, 41.8], [-71.399, 41.8], [-71.399, 41.801],
                        [-71.4, 41.801], [-71.4, 41.8],
                    ]]}, fact_class="confirmed_official", confidence=1.0,
                    source_id=source, parser_version="test/1", raw_sha256=raw,
                )
                responses = [
                    {"features": [{"attributes": {"ZONE": "C-1"}}]},
                    {"features": [{"attributes": {
                        "ID": "D-1", "NAME": "Neutral Development",
                        "APPROVED": 1735689600000,
                    }}]},
                    {"features": [{"attributes": {
                        "ID": "T-1", "NAME": "Neutral Tenant",
                        "MOVED_IN": "2026-04-01", "SPACE": 12000,
                    }}]},
                ]
                fake = Mock()
                fake.raise_for_status.return_value = None
                fake.json.side_effect = responses
                with patch("property_intel.adapters.arcgis_context.requests.Session.get",
                           return_value=fake):
                    stats = collect_arcgis_context(store, target_id,
                        {"address": "1 Example Road"}, {"layers": [
                            {"key": "zoning", "url": "https://example.gov/zoning",
                             "category": "zoning", "predicate": "official_zoning_overlay",
                             "out_fields": ["ZONE"]},
                            {"key": "development", "url": "https://example.gov/development",
                             "category": "permits_planning",
                             "predicate": "surrounding_development_pipeline",
                             "out_fields": ["ID", "NAME", "APPROVED"], "radius_m": 1000,
                             "entity": {"type": "development_case", "id_field": "ID",
                                        "name_field": "NAME", "event": {
                                            "type": "development_approved",
                                            "date_fields": ["APPROVED"],
                                            "summary_field": "NAME"}},
                             "capabilities": [{"capability": "development_pipeline",
                                               "status": "working"}]},
                            {"key": "leases", "url": "https://example.gov/leases",
                             "category": "market", "predicate": "lease_observations",
                             "out_fields": ["ID", "NAME", "MOVED_IN", "SPACE"],
                             "entity": {"type": "tenant", "id_field": "ID",
                                        "name_field": "NAME", "temporal_state": {
                                            "type": "tenant_occurrence",
                                            "valid_from_fields": ["MOVED_IN"],
                                            "value_fields": ["NAME", "SPACE"]}}},
                        ]})
                self.assertEqual(stats["features"], 3)
                self.assertFalse(stats["failed"])
                self.assertEqual(store.db.execute(
                    "SELECT COUNT(*) FROM entities WHERE entity_type='development_case'"
                ).fetchone()[0], 1)
                self.assertEqual(store.db.execute(
                    "SELECT event_date FROM events WHERE event_type='development_approved'"
                ).fetchone()[0], "2025-01-01")
                self.assertEqual(store.db.execute(
                    "SELECT COUNT(*) FROM facts WHERE predicate='official_zoning_overlay'"
                ).fetchone()[0], 1)
                state = store.db.execute(
                    "SELECT valid_from FROM temporal_states WHERE state_type='tenant_occurrence'"
                ).fetchone()
                self.assertEqual(state["valid_from"], "2026-04-01")
                capability = store.db.execute(
                    "SELECT status FROM source_capabilities WHERE capability='development_pipeline'"
                ).fetchone()
                self.assertEqual(capability["status"], "working")
            finally:
                store.close()

    def test_projected_area_does_not_contradict_sourced_area_in_other_units(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "area.sqlite")
            try:
                target_id = "property:area"
                store.entity("property_site", "Area Property", entity_id=target_id)
                raw = store.put_raw({"area": "fixture"})
                official = store.source(
                    name="Official parcel fixture", url="https://example.gov/gis",
                    authority="official fixture", parser_version="test/1", raw_sha256=raw,
                )
                legacy_derived = store.source(
                    name="DealSynq derived property metrics", url=None,
                    authority="calculation", parser_version="property-derived-metrics/1.3.1",
                    raw_sha256=raw,
                )
                store.fact(
                    subject_id=target_id, category="spatial",
                    predicate="parcel_geometry_union_area", value=0.2417, unit="acres",
                    fact_class="calculation", confidence=1.0, source_id=official,
                    parser_version="test/1", raw_sha256=raw,
                )
                store.fact(
                    subject_id=target_id, category="parcels",
                    predicate="parcel_geometry_union_area", value=10530, unit="sqft",
                    fact_class="calculation", confidence=0.96, source_id=legacy_derived,
                    parser_version="property-derived-metrics/1.3.1", raw_sha256=raw,
                )
                store.fact(
                    subject_id=target_id, category="parcels",
                    predicate="analysis_geometry_summary",
                    value={"area_sqft": 10530, "analysis_crs": "EPSG:32619"},
                    fact_class="calculation", confidence=0.96, source_id=official,
                    parser_version="test/1", raw_sha256=raw,
                )

                self.assertEqual(store.detect_contradictions(), 1)
                calculate(store, target_id)
                self.assertEqual(store.detect_contradictions(), 0)
                legacy = store.db.execute(
                    "SELECT status FROM facts WHERE subject_id=? "
                    "AND predicate='parcel_geometry_union_area' AND source_id=?",
                    (target_id, legacy_derived),
                ).fetchone()
                self.assertEqual(legacy["status"], "superseded")
                projected = store.db.execute(
                    "SELECT value_json,unit,status FROM facts WHERE subject_id=? "
                    "AND predicate='projected_parcel_geometry_union_area'",
                    (target_id,),
                ).fetchone()
                self.assertEqual(json.loads(projected["value_json"]), 10530)
                self.assertEqual(projected["unit"], "sqft")
                self.assertEqual(projected["status"], "current")
            finally:
                store.close()

    def test_spatial_engines_accept_fact_backed_parcel_geometry(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "geometry.sqlite")
            try:
                target_id = "property:geometry"
                parcel_id = "parcel:geometry"
                store.entity("property_site", "Geometry Property", entity_id=target_id)
                store.entity("parcel", "P-1", entity_id=parcel_id)
                store.record_decision(target_id, parcel_id, True, 1.0, 0.6,
                                      {"basis": "fixture"}, "test/1")
                raw = store.put_raw({"geometry": "fixture"})
                source = store.source(name="Official geometry fixture", url="https://example.gov/gis",
                                      authority="official fixture", parser_version="test/1",
                                      raw_sha256=raw)
                store.fact(
                    subject_id=parcel_id, category="spatial", predicate="parcel_geometry",
                    value={"type": "Polygon", "coordinates": [[
                        [-71.4, 41.8], [-71.399, 41.8], [-71.399, 41.801],
                        [-71.4, 41.801], [-71.4, 41.8],
                    ]]}, fact_class="confirmed_official", confidence=1.0,
                    source_id=source, parser_version="test/1", raw_sha256=raw,
                )
                geometry = site_geometry(store, target_id)
                self.assertIsNotNone(geometry)
                self.assertGreater(geometry.snapshot()["area_sqft"], 0)
            finally:
                store.close()

    def _config(self, root: Path) -> Path:
        base = root / "base.csv"
        geometry = json.dumps({
            "type": "Polygon",
            "coordinates": [[[-71.4, 41.8], [-71.399, 41.8], [-71.399, 41.801],
                             [-71.4, 41.801], [-71.4, 41.8]]],
        })
        with base.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "parcel", "address", "owner", "zone", "use", "year", "value", "geometry",
            ])
            writer.writeheader()
            writer.writerow({
                "parcel": "P-101", "address": "77 RANDOM AVENUE",
                "owner": "RANDOM OWNER LLC", "zone": "C-2", "use": "Retail",
                "year": "2025", "value": "1250000", "geometry": geometry,
            })

        def linked(name, entity, relationships=(), facts=(), event=None):
            item = {
                "name": name,
                "rows": [{
                    "parcel": "P-101", "record_id": f"{name}-1", "title": f"{name} record",
                    "kind": name, "date": "2025-03-15", "amount": "500000",
                }],
                "source": {"name": f"Official {name} fixture",
                           "url": f"https://example.gov/{name}",
                           "authority": "official public fixture"},
                "match": {"parcel_id_field": "parcel", "required": True},
            }
            if entity:
                item["entity"] = {"type": entity, "id_field": "record_id",
                                  "name_field": "title", "property_role": entity}
            if relationships:
                item["relationships"] = list(relationships)
            if facts:
                item["facts"] = list(facts)
            if event:
                item["event"] = event
            return {"datasets": [item], "maximum_unmatched": 0}

        source = {"name": "Official neutral municipal fixture",
                  "authority": "official public fixture"}
        engines = [{
            "key": "assessor", "adapter": "tabular_municipality",
            "coverage_status": "complete", "completion_basis": "One-row fixture reconciled",
            "config": {
                "path": str(base), "address_suffix": ", Neutral, RI 00000",
                "fields": {"parcel_id": "parcel", "address": "address", "owner": "owner",
                           "zoning": "zone", "use_description": "use",
                           "assessment_year": "year", "assessed_total": "value",
                           "geometry": "geometry"},
                "assessor_source": source, "parcel_source": source,
            },
        }]
        for key in ("parcel_gis", "zoning", "owner_entities", "site_assembly"):
            engines.append({
                "key": key, "coverage_from": "assessor", "coverage_status": "complete",
                "completion_basis": f"Fixture {key} output reconciled to source row",
            })
        engines += [
            {"key": "deeds", "adapter": "linked_records", "coverage_status": "complete",
             "completion_basis": "Fixture deed index reconciled", "config": linked(
                 "deed", "recorded_document",
                 relationships=[{"type": "affects", "from": "entity", "to": "parcel",
                                 "effective_date_field": "date"}],
                 facts=[{"field": "kind", "target": "entity", "category": "deeds_liens",
                         "predicate": "document_type", "effective_date_field": "date"}],
                 event={"type": "deed_recorded", "date_field": "date", "subject": "entity"})},
            {"key": "mortgages_liens", "adapter": "linked_records", "coverage_status": "complete",
             "completion_basis": "Fixture mortgage index reconciled", "config": linked(
                 "mortgage", "recorded_document",
                 relationships=[{"type": "encumbers", "from": "entity", "to": "parcel",
                                 "effective_date_field": "date"}],
                 facts=[{"field": "amount", "value_type": "number", "target": "entity",
                         "category": "deeds_liens", "predicate": "original_principal",
                         "unit": "usd", "effective_date_field": "date"}],
                 event={"type": "mortgage_recorded", "date_field": "date", "subject": "entity"})},
            {"key": "permits_planning", "adapter": "linked_records", "coverage_status": "complete",
             "completion_basis": "Fixture permit register reconciled", "config": linked(
                 "permit", "permit",
                 relationships=[{"type": "affects", "from": "entity", "to": "parcel",
                                 "effective_date_field": "date"}],
                 facts=[{"field": "kind", "target": "entity", "category": "permits_planning",
                         "predicate": "permit_type", "effective_date_field": "date"}],
                 event={"type": "permit_issued", "date_field": "date", "subject": "entity"})},
            {"key": "hazards", "adapter": "linked_records", "coverage_status": "complete",
             "completion_basis": "Fixture hazard overlay reconciled", "config": linked(
                 "hazard", None, facts=[{"field": "kind", "target": "property",
                                         "category": "hazards", "predicate": "hazard_screen"}])},
            {"key": "infrastructure", "adapter": "linked_records", "required": False,
             "coverage_status": "complete", "completion_basis": "Fixture access layer reconciled",
             "config": linked("access", None, facts=[{
                 "field": "kind", "target": "property", "category": "access",
                 "predicate": "road_access_screen"}])},
        ]
        config = {
            "scope": {"id": "neutral_ri", "scope_type": "municipality",
                      "jurisdiction_id": "neutral_ri", "name": "Neutral, Rhode Island",
                      "state_code": "RI"},
            "database": str(root / "neutral.sqlite"), "output_root": str(root),
            "batch_engines": engines,
        }
        path = root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_every_batch_engine_has_independent_evidence_and_activates(self):
        with tempfile.TemporaryDirectory() as td:
            config = self._config(Path(td))
            result = precompute(config)
            self.assertTrue(result["complete"])
            self.assertTrue(all(row["coverage_status"] == "complete"
                                for row in result["batch_engines"]))
            activated = activate_property(config, address="77 Random Avenue")
            self.assertTrue(activated["validation"]["passed"])
            payload = json.loads(Path(activated["reports"]["json"]).read_text(encoding="utf-8"))
            entity_types = {row["entity_type"] for row in payload["entities"]}
            self.assertIn("recorded_document", entity_types)
            self.assertIn("permit", entity_types)
            fact_categories = {row["category"] for row in payload["facts"]}
            self.assertIn("hazards", fact_categories)
            self.assertIn("access", fact_categories)
            classification = next(
                row for row in payload["facts"]
                if row["predicate"] == "hierarchical_asset_classification"
            )
            self.assertEqual(json.loads(classification["value_json"])["status"], "classified")
            capital = next(
                row for row in payload["facts"]
                if row["predicate"] == "capital_stack_reconstruction"
            )
            self.assertGreaterEqual(json.loads(capital["value_json"])["known_event_count"], 1)
            self.assertGreaterEqual(len(payload["events"]), 3)

    def test_assessor_output_cannot_masquerade_as_deed_engine(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = self._config(Path(td))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            assessor = config["batch_engines"][0]
            deed = next(item for item in config["batch_engines"] if item["key"] == "deeds")
            deed["adapter"] = "tabular_municipality"
            deed["config"] = assessor["config"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "output contract failed"):
                precompute(config_path)


if __name__ == "__main__":
    unittest.main()
