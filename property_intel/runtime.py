from __future__ import annotations

import json
import re
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable

from .stage_engine import STAGES
from .store import EvidenceStore


def run_stage(store: EvidenceStore, target_id: str, key: str, input_hash: str,
              action: Callable[[], dict[str, Any]], *, force: bool,
              required: bool = True) -> dict[str, Any]:
    """Run one incremental property-activation stage."""
    if not force and store.latest_success_hash(target_id, key) == input_hash:
        return {"stage": key, "status": "skipped_unchanged", "input_hash": input_hash}
    run_id = store.begin_run(target_id, key, input_hash)
    try:
        with store.transaction():
            stats = action()
        store.finish_run(run_id, "success", stats)
        return {"stage": key, "status": "success", "stats": stats, "input_hash": input_hash}
    except Exception as exc:
        error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        store.finish_run(run_id, "failed", error=error)
        if required:
            raise
        store.gap(target_id, key, "partial", f"Optional stage {key} failed", reason=str(exc))
        store.db.commit()
        return {"stage": key, "status": "failed_optional", "error": str(exc),
                "input_hash": input_hash}


def configure_activation_refresh_policies(store: EvidenceStore, target_id: str,
                                          config: dict[str, Any]) -> None:
    """Maintain refresh policies for activation-only work.

    Jurisdiction batch freshness is tracked independently in scope engine runs;
    this ledger covers only the property-specific stages executed after lookup.
    """
    store.db.execute("DELETE FROM refresh_policies WHERE target_id=?", (target_id,))
    policies = [
        ("web_intelligence", 14, "high", "Current approved web discoveries",
         bool(config.get("web_intelligence"))),
        ("documents", 30, "medium", "Property-specific public documents",
         bool(config.get("documents"))),
        ("market_listings", 7, "high", "Listings and asking evidence",
         bool(config.get("market_listings"))),
        ("intelligence_resolution", 7, "high", "Resolve changed batch and on-demand evidence", True),
        ("derived_metrics", 30, "medium", "Recalculate from changed source observations", True),
    ]
    for stage, cadence, priority, rationale, enabled in policies:
        latest = store.db.execute(
            "SELECT finished_at FROM stage_runs WHERE target_id=? AND stage_key=? "
            "AND status='success' ORDER BY finished_at DESC LIMIT 1",
            (target_id, stage),
        ).fetchone()
        next_due = None
        if latest and latest[0]:
            next_due = (datetime.fromisoformat(latest[0]) + timedelta(days=cadence)).isoformat()
        store.refresh_policy(
            target_id=target_id, stage_key=stage, cadence_days=cadence,
            priority=priority, enabled=enabled, rationale=rationale,
            last_success_at=latest[0] if latest else None, next_due_at=next_due,
        )
    store.db.commit()


def _valid_event_date(value: str | None, precision: str) -> bool:
    if value is None:
        return False
    formats = {"day": "%Y-%m-%d", "month": "%Y-%m"}
    if precision in formats:
        try:
            datetime.strptime(value, formats[precision])
            return True
        except ValueError:
            return False
    if precision in {"year", "fiscal_year"}:
        return bool(re.fullmatch(r"\d{4}", value))
    return bool(re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value))


def _valid_temporal_marker(value: str | None) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text or text.lower().startswith("unknown"):
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        return bool(re.fullmatch(r"\d{4}(?:-\d{2})?", text))


def validate_store(store: EvidenceStore, target_id: str) -> dict[str, Any]:
    """Validate one isolated, activated property graph."""
    integrity = store.db.execute("PRAGMA integrity_check").fetchone()[0]
    orphan_facts = store.db.execute(
        "SELECT COUNT(*) FROM facts f LEFT JOIN sources s ON s.source_id=f.source_id "
        "WHERE s.source_id IS NULL"
    ).fetchone()[0]
    raw_missing = store.db.execute(
        "SELECT COUNT(*) FROM facts f LEFT JOIN raw_evidence r ON r.raw_sha256=f.raw_sha256 "
        "WHERE f.raw_sha256 IS NOT NULL AND r.raw_sha256 IS NULL"
    ).fetchone()[0]
    parcels = store.db.execute(
        "SELECT COUNT(*) FROM grouping_decisions WHERE target_id=? AND included=1",
        (target_id,),
    ).fetchone()[0]
    classes = {row[0]: row[1] for row in store.rows(
        "SELECT fact_class,COUNT(*) FROM facts WHERE status='current' GROUP BY fact_class"
    )}
    semantic_stage_count = store.db.execute(
        "SELECT COUNT(*) FROM pipeline_stage_states WHERE target_id=? "
        "AND implementation_status='implemented'", (target_id,),
    ).fetchone()[0]
    current_states = store.db.execute(
        "SELECT COUNT(*) FROM property_state_snapshots WHERE target_id=?", (target_id,),
    ).fetchone()[0]

    event_rows = store.rows(
        "SELECT event_date,date_precision FROM events WHERE target_id=?", (target_id,))
    invalid_event_dates = sum(
        not _valid_event_date(row["event_date"], row["date_precision"])
        for row in event_rows
    )
    duplicate_event_keys = store.db.execute(
        "SELECT COUNT(*) FROM (SELECT subject_id,event_date,event_type,COUNT(*) n FROM events "
        "WHERE target_id=? GROUP BY subject_id,event_date,event_type HAVING n>1)",
        (target_id,),
    ).fetchone()[0]

    temporal_rows = store.rows(
        "SELECT valid_from,valid_to,first_seen,last_seen FROM temporal_states WHERE target_id=?",
        (target_id,),
    )
    invalid_temporal = sum(
        not _valid_temporal_marker(row[column])
        for row in temporal_rows
        for column in ("valid_from", "valid_to", "first_seen", "last_seen")
    )
    semantic_checks = {
        "timeline": {
            "status": "passed" if not invalid_event_dates and not duplicate_event_keys else "failed",
            "events": len(event_rows), "invalid_dates": invalid_event_dates,
            "duplicate_subject_date_type_keys": duplicate_event_keys,
        },
        "temporal_states": {
            "status": "passed" if not invalid_temporal else "failed",
            "states": len(temporal_rows), "invalid_date_markers": invalid_temporal,
        },
    }
    semantic_failures = [
        name for name, result in semantic_checks.items() if result["status"] == "failed"
    ]
    checks = {
        "sqlite_integrity": integrity,
        "orphan_facts": orphan_facts,
        "missing_raw_evidence": raw_missing,
        "included_parcels": parcels,
        "current_fact_classes": classes,
        "semantic_stages_implemented": semantic_stage_count,
        "semantic_stages_expected": len(STAGES),
        "current_property_states": current_states,
        "semantic_checks": semantic_checks,
        "semantic_failures": semantic_failures,
        "depth_status": "local_candidate_site_assembled" if parcels else "identity_unresolved",
    }
    checks["passed"] = (
        integrity == "ok" and orphan_facts == 0 and raw_missing == 0 and parcels > 0
        and semantic_stage_count == len(STAGES) and current_states > 0
        and not semantic_failures
    )
    return checks
