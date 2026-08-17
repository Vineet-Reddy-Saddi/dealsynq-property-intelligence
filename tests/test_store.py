import json
import tempfile
import unittest
from pathlib import Path

from property_intel.store import EvidenceStore


class StoreTests(unittest.TestCase):
    def test_grouping_algorithm_upgrade_replaces_current_decision(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "test.sqlite")
            try:
                store.record_decision("property:1", "parcel:1", True, 0.8, 0.6,
                                      {"basis": "address"}, "grouping/1")
                store.record_decision("property:1", "parcel:1", False, 0.4, 0.6,
                                      {"basis": "deed"}, "grouping/2")
                rows = store.rows(
                    "SELECT included,algorithm_version FROM grouping_decisions "
                    "WHERE target_id=? AND parcel_id=?", ("property:1", "parcel:1"))
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]["included"], rows[0]["algorithm_version"]),
                                 (0, "grouping/2"))
            finally:
                store.close()

    def test_source_family_upgrade_replaces_logical_edges_and_states(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "test.sqlite")
            try:
                src1 = store.source(name="Owner schedule", url="https://example.com",
                                    authority="owner reporting", parser_version="p1",
                                    raw_sha256=store.put_raw({"version": 1}))
                src2 = store.source(name="Owner schedule", url="https://example.com",
                                    authority="owner reporting", parser_version="p2",
                                    raw_sha256=store.put_raw({"version": 2}))
                store.entity("tenant", "Example Tenant", entity_id="tenant")
                for source, parser in ((src1, "p1"), (src2, "p2")):
                    store.alias("tenant", "organization_name", "Example Tenant", "EXAMPLE TENANT",
                                source_id=source, confidence=.9)
                    store.relationship(from_id="tenant", relationship_type="occupies", to_id="space",
                                       fact_class="reported", confidence=.9, source_id=source,
                                       parser_version=parser, effective_date="2025-01-01")
                    store.temporal_state(target_id="site", subject_id="space", state_type="occupancy",
                                         value={"status": "occupied"}, source_id=source,
                                         confidence=.9, fact_class="reported", parser_version=parser,
                                         valid_from="2025-01-01")
                    store.event(target_id="site", event_type="tenant_move_in",
                                event_date="2025-01-01", date_precision="day",
                                subject_id="tenant", summary="Example Tenant moved in",
                                fact_class="reported", confidence=.9,
                                source_ids=[source], evidence={"parser": parser})
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM relationships").fetchone()[0], 1)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM temporal_states").fetchone()[0], 1)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0], 1)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
                self.assertEqual(store.db.execute("SELECT source_id FROM relationships").fetchone()[0], src2)
                self.assertEqual(store.db.execute("SELECT source_id FROM temporal_states").fetchone()[0], src2)
                self.assertEqual(store.db.execute("SELECT source_id FROM entity_aliases").fetchone()[0], src2)
                self.assertEqual(json.loads(store.db.execute(
                    "SELECT source_ids_json FROM events").fetchone()[0]), [src2])
            finally:
                store.close()

    def test_raw_is_content_addressed_and_source_change_supersedes(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "test.sqlite")
            try:
                raw1 = store.put_raw({"value": 1})
                self.assertEqual(raw1, store.put_raw({"value": 1}))
                src1 = store.source(name="Assessor", url="https://example.gov", authority="official",
                                    parser_version="p1", raw_sha256=raw1)
                store.fact(subject_id="p1", category="tax", predicate="value", value=1,
                           fact_class="confirmed_official", confidence=1, source_id=src1,
                           parser_version="p1", raw_sha256=raw1)
                raw2 = store.put_raw({"value": 2})
                src2 = store.source(name="Assessor", url="https://example.gov", authority="official",
                                    parser_version="p1", raw_sha256=raw2)
                store.fact(subject_id="p1", category="tax", predicate="value", value=2,
                           fact_class="confirmed_official", confidence=1, source_id=src2,
                           parser_version="p1", raw_sha256=raw2)
                statuses = [r[0] for r in store.rows("SELECT status FROM facts ORDER BY value_json")]
                self.assertEqual(statuses, ["superseded", "current"])
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM fact_changes").fetchone()[0], 1)
                change_type = store.db.execute("SELECT change_type FROM fact_changes").fetchone()[0]
                self.assertEqual(change_type, "source_observation_change")
            finally:
                store.close()

    def test_cross_source_disagreement_is_contradiction(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "test.sqlite")
            try:
                for name, value in (("Assessor", 100), ("Owner flyer", 110)):
                    raw = store.put_raw({"value": value})
                    src = store.source(name=name, url=None, authority=name, parser_version="p", raw_sha256=raw)
                    store.fact(subject_id="site", category="buildings", predicate="area", value=value,
                               fact_class="confirmed_official" if name == "Assessor" else "reported",
                               confidence=.9, source_id=src, parser_version="p", raw_sha256=raw)
                self.assertEqual(store.detect_contradictions(), 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
