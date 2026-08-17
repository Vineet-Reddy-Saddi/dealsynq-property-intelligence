"""Produce the exact current Stamford property sites lacking analysis geometry."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "pilots" / "stamford_ct" / "data" / "stamford_ct_precomputed.sqlite"
OUTPUT = ROOT / "pilots" / "stamford_ct" / "reports" / "stamford_ct_parcel_geometry_gaps.json"


def main() -> None:
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in connection.execute("""
            SELECT p.property_id, p.canonical_name, p.address, p.external_id
            FROM property_index p
            WHERE p.scope_id='stamford_ct'
              AND p.status!='legacy_unmappable'
              AND NOT EXISTS (
                  SELECT 1 FROM facts f INDEXED BY idx_facts_subject_predicate_status
                  WHERE f.subject_id=p.property_id AND f.status='current'
                    AND f.predicate='analysis_geometry'
              )
            ORDER BY p.address
        """).fetchall()]
        for row in rows:
            aliases = connection.execute("""
                SELECT DISTINCT COALESCE(e.external_id, e.canonical_name)
                FROM property_entity_links l JOIN entities e ON e.entity_id=l.entity_id
                WHERE l.property_id=? AND l.role='parcel'
            """, (row["property_id"],)).fetchall()
            row["parcel_ids"] = ",".join(item[0] for item in aliases if item[0])
        active = connection.execute(
            "SELECT COUNT(*) FROM property_index WHERE scope_id='stamford_ct' AND status!='legacy_unmappable'"
        ).fetchone()[0]
        legacy = connection.execute(
            "SELECT COUNT(*) FROM property_index WHERE scope_id='stamford_ct' AND status='legacy_unmappable'"
        ).fetchone()[0]
        payload = {
            "scope": "stamford_ct", "active_property_sites": active,
            "legacy_assessor_only_property_sites": legacy,
            "missing_analysis_geometry": len(rows), "properties": rows,
        }
        OUTPUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps(payload, indent=2), flush=True)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
