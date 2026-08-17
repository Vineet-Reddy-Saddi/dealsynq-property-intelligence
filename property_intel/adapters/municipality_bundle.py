from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from ..store import EvidenceStore
from ..util import file_fingerprint, normalize_address, stable_id

PARSER_VERSION = "municipality-evidence-bundle/1.0.0"
BUNDLE_SCHEMA = "municipality-evidence-bundle/1.0.0"


def fingerprint(config: dict[str, Any]) -> str:
    files = [file_fingerprint(Path(path)) for path in config.get("paths", [])]
    return stable_id("input", PARSER_VERSION, files, config.get("bundles", []), config)


def _bundles(config: dict[str, Any]) -> list[dict[str, Any]]:
    values = [dict(value) for value in config.get("bundles", [])]
    for path_value in config.get("paths", []):
        payload = json.loads(Path(path_value).read_text(encoding="utf-8"))
        if isinstance(payload, list):
            values.extend(dict(item) for item in payload)
        else:
            values.append(dict(payload))
    return values


def collect(store: EvidenceStore, scope: dict[str, Any],
            config: dict[str, Any]) -> dict[str, Any]:
    """Ingest normalized municipality-wide evidence from any approved collector.

    The interchange format is intentionally collector-neutral. A source-specific
    process can run outside this project and emit parcels, sites, owners, land
    records, permits, hazards, facts and relationships without introducing
    property-specific logic into the batch orchestrator.
    """
    scope_id = scope["id"]
    totals = {
        "bundles": 0, "properties": 0, "sources": 0, "entities": 0,
        "facts": 0, "relationships": 0, "property_links": 0,
        "memberships": 0, "events": 0, "temporal_states": 0, "gaps": 0,
        "output_profile": {
            "entity_types": Counter(), "fact_categories": Counter(),
            "fact_predicates": Counter(), "relationship_types": Counter(),
            "property_roles": Counter(), "event_types": Counter(),
            "alias_types": Counter(), "memberships": 0,
        },
    }
    for bundle in _bundles(config):
        schema = bundle.get("schema_version", BUNDLE_SCHEMA)
        if schema != BUNDLE_SCHEMA:
            raise ValueError(f"Unsupported municipality evidence bundle schema: {schema}")

        raw = store.put_raw(bundle)
        default_source = bundle.get("default_source") or {
            "ref": "default",
            "name": config.get("source_name", "Municipality evidence bundle"),
            "url": config.get("source_url"),
            "authority": config.get("authority", "configured evidence provider"),
        }
        sources = [default_source] + [
            item for item in bundle.get("sources", [])
            if item.get("ref") != default_source.get("ref")
        ]
        source_map: dict[str, str] = {}
        for item in sources:
            ref = item.get("ref") or stable_id("source-ref", item)
            source_map[ref] = store.source(
                name=item.get("name", "Municipality evidence source"),
                url=item.get("url"),
                authority=item.get("authority", "configured evidence provider"),
                parser_version=item.get("parser_version", PARSER_VERSION),
                raw_sha256=raw,
                source_date=item.get("source_date"),
                retrieved_at=item.get("retrieved_at"),
                access_note=item.get("access_note"),
            )
            totals["sources"] += 1
        default_ref = default_source.get("ref", "default")

        store.entity(
            "jurisdiction", scope.get("name", scope_id), external_id=scope_id,
            attributes={"scope_type": scope.get("scope_type"),
                        "jurisdiction_id": scope.get("jurisdiction_id")},
            entity_id=scope_id,
        )
        entity_map: dict[str, str] = {"$scope": scope_id}
        property_map: dict[str, str] = {}

        for item in bundle.get("properties", []):
            ref = item.get("ref")
            if not ref:
                raise ValueError("Municipality bundle property requires ref")
            address = item["address"]
            property_id = item.get("property_id") or stable_id(
                "property", scope_id, item.get("external_id") or item["name"],
                normalize_address(address),
            )
            store.upsert_indexed_property(
                property_id=property_id, scope_id=scope_id, name=item["name"],
                address=address, normalized_address=normalize_address(address),
                external_id=item.get("external_id"),
                status=item.get("status", "precomputed"),
                attributes=item.get("attributes"),
            )
            store.entity(
                "property_site", item["name"], external_id=item.get("external_id"),
                attributes={"address": address, **(item.get("attributes") or {})},
                entity_id=property_id,
            )
            store.link_property_entity(
                property_id=property_id, entity_id=property_id,
                role="property_site", confidence=1.0,
                source_id=source_map.get(item.get("source_ref", default_ref)),
                evidence={"basis": "municipality bundle property record"},
            )
            for alias in item.get("aliases", []):
                store.alias(
                    property_id, alias["alias_type"], alias["raw_value"],
                    alias.get("normalized_value") or normalize_address(alias["raw_value"]),
                    source_id=source_map.get(alias.get("source_ref", default_ref)),
                    confidence=float(alias.get("confidence", 1.0)),
                )
            property_map[ref] = property_id
            entity_map[ref] = property_id
            totals["properties"] += 1
            totals["property_links"] += 1
            totals["output_profile"]["entity_types"]["property_site"] += 1
            totals["output_profile"]["property_roles"]["property_site"] += 1

        entity_items: dict[str, dict[str, Any]] = {}
        for item in bundle.get("entities", []):
            ref = item.get("ref")
            if not ref:
                raise ValueError("Municipality bundle entity requires ref")
            entity_id = store.entity(
                item["entity_type"], item["canonical_name"],
                external_id=item.get("external_id"), attributes=item.get("attributes"),
                entity_id=item.get("entity_id"),
            )
            entity_map[ref] = entity_id
            entity_items[ref] = item
            for alias in item.get("aliases", []):
                normalized = alias.get("normalized_value")
                if normalized is None:
                    normalized = (normalize_address(alias["raw_value"])
                                  if alias["alias_type"] == "situs_address"
                                  else str(alias["raw_value"]).upper().strip())
                store.alias(
                    entity_id, alias["alias_type"], alias["raw_value"], normalized,
                    source_id=source_map.get(alias.get("source_ref", default_ref)),
                    confidence=float(alias.get("confidence", 1.0)),
                )
                totals["output_profile"]["alias_types"][alias["alias_type"]] += 1
            totals["entities"] += 1
            totals["output_profile"]["entity_types"][item["entity_type"]] += 1

        for ref, item in entity_items.items():
            for property_ref in item.get("property_refs", []):
                if property_ref not in property_map:
                    raise ValueError(f"Unknown property ref {property_ref!r} on entity {ref!r}")
                store.link_property_entity(
                    property_id=property_map[property_ref], entity_id=entity_map[ref],
                    role=item.get("property_role", item["entity_type"]),
                    confidence=float(item.get("property_confidence", 1.0)),
                    source_id=source_map.get(item.get("source_ref", default_ref)),
                    evidence=item.get("property_evidence"),
                )
                totals["property_links"] += 1
                totals["output_profile"]["property_roles"][
                    item.get("property_role", item["entity_type"])
                ] += 1

        for item in bundle.get("property_links", []):
            store.link_property_entity(
                property_id=property_map[item["property_ref"]],
                entity_id=entity_map[item["entity_ref"]],
                role=item["role"], confidence=float(item.get("confidence", 1.0)),
                source_id=source_map.get(item.get("source_ref", default_ref)),
                evidence=item.get("evidence"),
            )
            totals["property_links"] += 1
            totals["output_profile"]["property_roles"][item["role"]] += 1

        for item in bundle.get("facts", []):
            store.fact(
                subject_id=entity_map[item.get("subject_ref", "$scope")],
                category=item["category"], predicate=item["predicate"],
                value=item.get("value"), fact_class=item["fact_class"],
                confidence=float(item["confidence"]),
                source_id=source_map[item.get("source_ref", default_ref)],
                parser_version=item.get("parser_version", PARSER_VERSION),
                unit=item.get("unit"), freshness_days=item.get("freshness_days"),
                effective_date=item.get("effective_date"),
                observed_at=item.get("observed_at"), raw_sha256=raw,
                evidence_locator=item.get("evidence_locator"),
            )
            totals["facts"] += 1
            totals["output_profile"]["fact_categories"][item["category"]] += 1
            totals["output_profile"]["fact_predicates"][item["predicate"]] += 1

        for item in bundle.get("relationships", []):
            store.relationship(
                from_id=entity_map[item["from_ref"]],
                relationship_type=item["relationship_type"],
                to_id=entity_map[item["to_ref"]],
                fact_class=item["fact_class"],
                confidence=float(item["confidence"]),
                source_id=source_map[item.get("source_ref", default_ref)],
                parser_version=item.get("parser_version", PARSER_VERSION),
                raw_sha256=raw, effective_date=item.get("effective_date"),
                explanation=item.get("explanation"),
            )
            totals["relationships"] += 1
            totals["output_profile"]["relationship_types"][item["relationship_type"]] += 1

        for item in bundle.get("memberships", []):
            property_id = property_map[item["property_ref"]]
            parcel_id = entity_map[item["parcel_ref"]]
            store.record_decision(
                property_id, parcel_id, bool(item.get("included", True)),
                float(item.get("score", 1.0)), float(item.get("threshold", 0.6)),
                item.get("evidence", {"classification": item.get("classification", "precomputed")}),
                item.get("algorithm_version", "municipality-bundle/1.0.0"),
            )
            totals["memberships"] += 1
            totals["output_profile"]["memberships"] += 1

        for item in bundle.get("temporal_states", []):
            property_id = property_map[item["property_ref"]]
            store.temporal_state(
                target_id=property_id,
                subject_id=entity_map[item.get("subject_ref", item["property_ref"])],
                state_type=item["state_type"], value=item.get("value"),
                source_id=source_map[item.get("source_ref", default_ref)],
                confidence=float(item["confidence"]), fact_class=item["fact_class"],
                parser_version=item.get("parser_version", PARSER_VERSION),
                valid_from=item.get("valid_from"), valid_to=item.get("valid_to"),
                first_seen=item.get("first_seen"), last_seen=item.get("last_seen"),
            )
            totals["temporal_states"] += 1

        for item in bundle.get("events", []):
            property_id = property_map[item["property_ref"]]
            store.event(
                target_id=property_id, event_type=item["event_type"],
                event_date=item.get("event_date"),
                date_precision=item.get("date_precision", "unknown"),
                subject_id=entity_map[item.get("subject_ref", item["property_ref"])],
                summary=item["summary"], fact_class=item["fact_class"],
                confidence=float(item["confidence"]),
                source_ids=[source_map[ref] for ref in item.get("source_refs", [default_ref])],
                evidence=item.get("evidence"),
            )
            totals["events"] += 1
            totals["output_profile"]["event_types"][item["event_type"]] += 1

        for item in bundle.get("gaps", []):
            target = (property_map[item["property_ref"]]
                      if item.get("property_ref") else scope_id)
            store.gap(target, item["category"], item["status"], item["description"],
                      reason=item.get("reason"), source_url=item.get("source_url"))
            totals["gaps"] += 1

        totals["bundles"] += 1
    totals["output_profile"] = {
        key: dict(value) if isinstance(value, Counter) else value
        for key, value in totals["output_profile"].items()
    }
    return totals
