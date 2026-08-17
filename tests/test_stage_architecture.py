import json
import tempfile
import unittest
from pathlib import Path

from property_intel.adapters import canonical_bundle, web_intelligence
from property_intel.adapter_runtime import builtin_adapters
from property_intel.stage_engine import STAGES, evaluate_pipeline, record_profile_output, stage_catalog
from property_intel.store import EvidenceStore
from property_intel.util import stable_id, utcnow


class StageArchitectureTests(unittest.TestCase):
    def test_collection_tools_share_one_replaceable_adapter_contract(self):
        keys = builtin_adapters().keys()
        self.assertEqual(keys, sorted({
            "web_intelligence", "documents", "listings_csv", "canonical_bundle",
            "national_public", "arcgis_context", "ct_registry",
        }))

    def test_canonical_bundle_accepts_any_tool_output_without_property_logic(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "bundle.sqlite")
            try:
                tid = "property:bundle-test"
                store.upsert_target(tid, "Bundle Test", "2 Main St, Test, MA 00000",
                                    {"target": {"name": "Bundle Test", "address": "2 Main St, Test, MA 00000"}})
                bundle = {
                    "schema_version": "property-evidence-bundle/1.0.0",
                    "default_source": {"ref": "src", "name": "Official fixture",
                                       "url": "https://example.gov/record/2",
                                       "authority": "official municipal source"},
                    "entities": [{"ref": "tenant", "entity_type": "tenant",
                                  "canonical_name": "Example Tenant"}],
                    "facts": [{"subject_ref": "$target", "category": "identity",
                               "predicate": "fixture_value", "value": 42,
                               "fact_class": "confirmed_official", "confidence": 1.0,
                               "source_ref": "src"}],
                    "relationships": [{"from_ref": "tenant", "relationship_type": "reported_occupant_of",
                                       "to_ref": "$target", "fact_class": "reported",
                                       "confidence": 0.8, "source_ref": "src"}],
                    "temporal_states": [{"subject_ref": "tenant", "state_type": "tenant_occupancy",
                                         "value": "open", "fact_class": "reported", "confidence": 0.8,
                                         "source_ref": "src", "first_seen": "2026-01-01"}],
                }
                stats = canonical_bundle.collect(store, tid, {"bundles": [bundle]})
                self.assertEqual(stats["facts"], 1)
                self.assertEqual(stats["relationships"], 1)
                self.assertEqual(store.db.execute(
                    "SELECT COUNT(*) FROM facts WHERE subject_id=? AND predicate='fixture_value'",
                    (tid,)).fetchone()[0], 1)
            finally:
                store.close()

    def test_rahul_stage_contract_is_complete_and_dependency_ordered(self):
        catalog = stage_catalog()
        self.assertEqual(len(catalog), 25)
        self.assertEqual(catalog[0]["key"], "jurisdiction")
        self.assertEqual(catalog[-1]["key"], "property_intelligence_profile")
        positions = {row["key"]: row["order"] for row in catalog}
        for row in catalog:
            for dependency in row["dependencies"]:
                self.assertIn(dependency, positions)
                self.assertLess(positions[dependency], row["order"])

    def test_semantic_engine_materializes_every_stage_and_current_state(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "property.sqlite")
            try:
                tid = "property:test"
                config = {"target": {"name": "Generic Property", "address": "1 Main St, Test, MA 00000"},
                          "_source_resolution": {"jurisdiction_id": "test_ma"}}
                store.upsert_target(tid, "Generic Property", "1 Main St, Test, MA 00000", config)
                store.entity("property_site", "Generic Property", entity_id=tid)
                raw = store.put_raw({"value": "test"})
                source = store.source(name="Test official source", url="https://example.gov/property",
                                      authority="official municipal source", parser_version="test/1",
                                      raw_sha256=raw)
                store.fact(subject_id=tid, category="identity", predicate="test_identity",
                           value="Generic Property", fact_class="confirmed_official", confidence=1.0,
                           source_id=source, parser_version="test/1", raw_sha256=raw)
                results = evaluate_pipeline(store, tid)
                self.assertEqual(len(results), len(STAGES))
                self.assertEqual(store.db.execute(
                    "SELECT COUNT(*) FROM pipeline_stage_states WHERE target_id=?",
                    (tid,)).fetchone()[0], len(STAGES))
                self.assertEqual(store.db.execute(
                    "SELECT COUNT(*) FROM property_state_snapshots WHERE target_id=?",
                    (tid,)).fetchone()[0], 1)
                record_profile_output(store, tid, {"markdown": "generic.md", "json": "generic.json"})
                profile = store.db.execute(
                    "SELECT implementation_status,coverage_status FROM pipeline_stage_states "
                    "WHERE target_id=? AND stage_key='property_intelligence_profile'", (tid,)).fetchone()
                self.assertEqual(tuple(profile), ("implemented", "complete"))
            finally:
                store.close()

    def test_manifest_web_provider_is_replaceable_and_property_neutral(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "web.sqlite")
            try:
                tid = "property:unrelated"
                target = {"name": "Unrelated Center", "address": "9 Other Ave, Test, MA 00000"}
                store.upsert_target(tid, target["name"], target["address"], {"target": target})
                qid = stable_id("query", tid, "official_documents", "test query")
                now = utcnow()
                store.db.execute("INSERT INTO search_queries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (qid, tid, "test query", "official_documents", json.dumps([target["address"]]),
                                  "high", "planned", None, None, None, "test", now, now))
                config = {"providers": [{"id": "fixture", "type": "manifest", "results": [{
                    "query_type": "official_documents", "url": "https://example.gov/case/1",
                    "title": "Public case", "authority": "official public source",
                    "document_type": "planning_case",
                }]}]}
                result = web_intelligence.collect(store, tid, target, config)
                self.assertEqual(result["discoveries"], 1)
                row = store.db.execute("SELECT canonical_url,provider FROM web_discoveries").fetchone()
                self.assertEqual(row["canonical_url"], "https://example.gov/case/1")
                self.assertEqual(row["provider"], "fixture")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
