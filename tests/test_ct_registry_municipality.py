from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from property_intel.adapters.ct_registry_municipality import collect
from property_intel.store import EvidenceStore


class CTRegistryMunicipalityTests(unittest.TestCase):
    def test_batch_registry_screens_and_links_related_records(self):
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceStore(Path(td) / "registry.sqlite")
            try:
                scope = {"id": "fixture_ct", "jurisdiction_id": "fixture_ct",
                         "scope_type": "municipality", "name": "Fixture, CT"}
                store.upsert_collection_scope(
                    scope["id"], scope["scope_type"], scope["jurisdiction_id"],
                    scope["name"], {}, state_code="CT",
                )
                property_id = "property:1"
                owner_id = "owner:1"
                store.upsert_indexed_property(
                    property_id=property_id, scope_id=scope["id"], name="1 Example Road",
                    address="1 Example Road, Fixture, CT", normalized_address="1 EXAMPLE RD FIXTURE CT",
                )
                store.entity("property_site", "1 Example Road", entity_id=property_id)
                store.entity("organization", "EXAMPLE OWNER LLC", entity_id=owner_id)
                raw = store.put_raw({"fixture": True})
                source = store.source(
                    name="Fixture assessor", url="https://example.gov/assessor",
                    authority="official fixture", parser_version="test/1", raw_sha256=raw,
                )
                store.link_property_entity(property_id=property_id, entity_id=owner_id,
                                           role="assessor_owner", source_id=source)
                store.relationship(
                    from_id=owner_id, relationship_type="assessor_owner_of", to_id=property_id,
                    fact_class="confirmed_official", confidence=1.0,
                    source_id=source, parser_version="test/1", raw_sha256=raw,
                )

                batches = [
                    [{"query_index": 0, "soql": "master", "rows": [{
                        "id": "B-1", "name": "EXAMPLE OWNER LLC",
                        "status": "ACTIVE", "accountnumber": "ALEI-1",
                    }]}],
                    [{"query_index": 0, "soql": "ucc", "rows": [{
                        "debtor_nm_bus": "EXAMPLE OWNER LLC", "id_ucc_flng_nbr": "U-1",
                        "dt_accept": "2026-01-02",
                    }]}],
                    [{"query_index": 0, "soql": "principals", "rows": [{
                        "business_id": "B-1", "name__c": "Jane Principal", "designation": "MEMBER",
                    }]}],
                    [{"query_index": 0, "soql": "agents", "rows": [{
                        "business_id": "0000001", "name__c": "Agent LLC", "business_key": "B-1",
                    }]}],
                    [{"query_index": 0, "soql": "filings", "rows": [{
                        "account": "B-1", "name": "F-1", "filing_date": "2026-02-03",
                    }]}],
                    [{"query_index": 0, "soql": "names", "rows": [{
                        "unique_key": "B-1", "business_name_old": "OLD OWNER LLC",
                    }]}],
                ]
                with patch(
                    "property_intel.adapters.ct_registry_municipality._fetch_batches",
                    side_effect=batches,
                ):
                    stats = collect(store, scope, {})

                self.assertEqual(stats["registry_matches"], 1)
                self.assertEqual(stats["principals"], 1)
                self.assertEqual(stats["active_ucc_records"], 1)
                predicates = {row[0] for row in store.rows(
                    "SELECT predicate FROM facts WHERE subject_id=?", (owner_id,)
                )}
                self.assertIn("ct_business_registry_match_screen", predicates)
                self.assertIn("ct_active_ucc_lien_screen", predicates)
                roles = {row[0] for row in store.rows(
                    "SELECT role FROM property_entity_links WHERE property_id=?", (property_id,)
                )}
                self.assertTrue({"ct_principal", "ct_registered_agent", "corporate_filing",
                                 "ucc_lien_record"}.issubset(roles))
                screen = json.loads(store.db.execute(
                    "SELECT value_json FROM facts WHERE subject_id=? AND predicate=?",
                    (owner_id, "ct_active_ucc_lien_screen"),
                ).fetchone()[0])
                self.assertEqual(screen["active_record_count"], 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
