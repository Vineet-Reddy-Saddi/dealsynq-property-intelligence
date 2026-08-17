from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..store import EvidenceStore
from ..util import file_fingerprint, stable_id

PARSER_VERSION = "canonical-evidence-bundle/1.0.0"
BUNDLE_SCHEMA = "property-evidence-bundle/1.0.0"


def fingerprint(config: dict[str, Any]) -> str:
    files = [file_fingerprint(Path(path)) for path in config.get("paths", [])]
    return stable_id("input", PARSER_VERSION, files, config.get("bundles", []), config)


def _bundles(config: dict[str, Any]) -> list[dict[str, Any]]:
    values = [dict(value) for value in config.get("bundles", [])]
    for path_value in config.get("paths", []):
        path = Path(path_value)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            values.extend(dict(item) for item in payload)
        else:
            values.append(dict(payload))
    return values


def collect(store: EvidenceStore, target_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Ingest the vendor/tool-neutral evidence interchange format.

    References are local to a bundle. `$target` always resolves to the active
    property, which lets the same bundle-producing tool work for any target.
    Assertions remain subject to the normal fact-class, confidence, provenance,
    raw-evidence, parser-version, and temporal rules.
    """
    totals = {"bundles": 0, "sources": 0, "entities": 0, "facts": 0,
              "relationships": 0, "events": 0, "temporal_states": 0, "gaps": 0}
    for bundle in _bundles(config):
        schema = bundle.get("schema_version", BUNDLE_SCHEMA)
        if schema != BUNDLE_SCHEMA:
            raise ValueError(f"Unsupported canonical evidence bundle schema: {schema}")
        raw = store.put_raw(bundle)
        source_map: dict[str, str] = {}
        default_source = bundle.get("default_source") or {
            "ref": "default", "name": config.get("source_name", "Canonical evidence bundle"),
            "url": config.get("source_url"),
            "authority": config.get("authority", "configured evidence provider"),
        }
        sources = [default_source] + [item for item in bundle.get("sources", [])
                                      if item.get("ref") != default_source.get("ref")]
        for item in sources:
            ref = item.get("ref") or stable_id("source-ref", item)
            source_map[ref] = store.source(
                name=item.get("name", "Canonical evidence source"),
                url=item.get("url"), authority=item.get("authority", "configured evidence provider"),
                parser_version=item.get("parser_version", PARSER_VERSION), raw_sha256=raw,
                source_date=item.get("source_date"), retrieved_at=item.get("retrieved_at"),
                access_note=item.get("access_note"),
            )
            totals["sources"] += 1
        default_ref = default_source.get("ref", "default")
        entity_map = {"$target": target_id}
        for item in bundle.get("entities", []):
            ref = item.get("ref")
            if not ref:
                raise ValueError("Canonical bundle entity requires ref")
            entity_map[ref] = store.entity(
                item["entity_type"], item["canonical_name"],
                external_id=item.get("external_id"), attributes=item.get("attributes"),
                entity_id=item.get("entity_id"),
            )
            for alias in item.get("aliases", []):
                store.alias(entity_map[ref], alias["alias_type"], alias["raw_value"],
                            alias["normalized_value"],
                            source_id=source_map.get(alias.get("source_ref", default_ref)),
                            confidence=float(alias.get("confidence", 1.0)))
            totals["entities"] += 1
        for item in bundle.get("facts", []):
            store.fact(
                subject_id=entity_map[item.get("subject_ref", "$target")],
                category=item["category"], predicate=item["predicate"], value=item.get("value"),
                fact_class=item["fact_class"], confidence=float(item["confidence"]),
                source_id=source_map[item.get("source_ref", default_ref)],
                parser_version=item.get("parser_version", PARSER_VERSION),
                unit=item.get("unit"), freshness_days=item.get("freshness_days"),
                effective_date=item.get("effective_date"), observed_at=item.get("observed_at"),
                raw_sha256=raw, evidence_locator=item.get("evidence_locator"),
            )
            totals["facts"] += 1
        for item in bundle.get("relationships", []):
            store.relationship(
                from_id=entity_map[item["from_ref"]], relationship_type=item["relationship_type"],
                to_id=entity_map[item["to_ref"]], fact_class=item["fact_class"],
                confidence=float(item["confidence"]),
                source_id=source_map[item.get("source_ref", default_ref)],
                parser_version=item.get("parser_version", PARSER_VERSION), raw_sha256=raw,
                effective_date=item.get("effective_date"), explanation=item.get("explanation"),
            )
            totals["relationships"] += 1
        for item in bundle.get("temporal_states", []):
            store.temporal_state(
                target_id=target_id, subject_id=entity_map[item.get("subject_ref", "$target")],
                state_type=item["state_type"], value=item.get("value"),
                source_id=source_map[item.get("source_ref", default_ref)],
                confidence=float(item["confidence"]), fact_class=item["fact_class"],
                parser_version=item.get("parser_version", PARSER_VERSION),
                valid_from=item.get("valid_from"), valid_to=item.get("valid_to"),
                first_seen=item.get("first_seen"), last_seen=item.get("last_seen"),
            )
            totals["temporal_states"] += 1
        for item in bundle.get("events", []):
            store.event(
                target_id=target_id, event_type=item["event_type"], event_date=item.get("event_date"),
                date_precision=item.get("date_precision", "unknown"),
                subject_id=entity_map[item.get("subject_ref", "$target")],
                summary=item["summary"], fact_class=item["fact_class"],
                confidence=float(item["confidence"]),
                source_ids=[source_map[ref] for ref in item.get("source_refs", [default_ref])],
                evidence=item.get("evidence"),
            )
            totals["events"] += 1
        for item in bundle.get("gaps", []):
            store.gap(target_id, item["category"], item["status"], item["description"],
                      reason=item.get("reason"), source_url=item.get("source_url"))
            totals["gaps"] += 1
        totals["bundles"] += 1
    return totals
