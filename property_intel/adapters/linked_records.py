from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from ..store import EvidenceStore
from ..util import file_fingerprint, normalize_address, normalize_text, parse_number, stable_id

PARSER_VERSION = "linked-jurisdiction-records/1.0.0"


def fingerprint(config: dict[str, Any]) -> str:
    files = []
    for dataset in config.get("datasets", []):
        if dataset.get("path"):
            files.append(file_fingerprint(Path(dataset["path"])))
    return stable_id("input", PARSER_VERSION, files, config)


def _rows(dataset: dict[str, Any]) -> Iterable[tuple[int, dict[str, Any]]]:
    for index, row in enumerate(dataset.get("rows", []), 1):
        yield index, dict(row)
    path_value = dataset.get("path")
    if not path_value:
        return
    path = Path(path_value)
    if path.suffix.lower() in {".json", ".jsonl", ".ndjson"}:
        if path.suffix.lower() in {".jsonl", ".ndjson"}:
            with path.open("r", encoding=dataset.get("encoding", "utf-8-sig")) as handle:
                for index, line in enumerate(handle, 1):
                    if line.strip():
                        yield index, json.loads(line)
            return
        payload = json.loads(path.read_text(encoding=dataset.get("encoding", "utf-8-sig")))
        values = payload.get(dataset.get("records_key", "records"), []) if isinstance(payload, dict) else payload
        for index, row in enumerate(values, 1):
            yield index, dict(row)
        return
    with path.open("r", encoding=dataset.get("encoding", "utf-8-sig"), newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle, delimiter=dataset.get("delimiter", ",")), 2):
            yield index, dict(row)


def _value(row: dict[str, Any], field: str | None) -> Any:
    if not field:
        return None
    value = row.get(field)
    if isinstance(value, str):
        value = value.strip()
    return value if value is not None and value != "" else None


def _typed(value: Any, value_type: str | None) -> Any:
    if value is None or not value_type or value_type == "string":
        return value
    if value_type == "number":
        return parse_number(str(value))
    if value_type == "integer":
        number = parse_number(str(value))
        return int(number) if number is not None else None
    if value_type == "boolean":
        return normalize_text(str(value)) in {"1", "TRUE", "YES", "Y"}
    if value_type == "json":
        return json.loads(value) if isinstance(value, str) else value
    if value_type == "date":
        text = str(value).strip()
        parts = text.split("/")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            month, day, year = map(int, parts)
            return f"{year:04d}-{month:02d}-{day:02d}"
        return text
    raise ValueError(f"Unsupported linked-record value_type {value_type!r}")


def _resolve_subjects(store: EvidenceStore, scope_id: str, row: dict[str, Any],
                      match: dict[str, Any]) -> tuple[str, list[str], str]:
    parcel_value = _value(row, match.get("parcel_id_field"))
    address_value = _value(row, match.get("address_field"))
    property_ids: set[str] = set()
    parcel_ids: set[str] = set()
    basis = ""
    if parcel_value is not None:
        key = normalize_text(str(parcel_value))
        for result in store.rows(
            "SELECT DISTINCT p.property_id,e.entity_id FROM property_index p "
            "JOIN property_entity_links l ON l.property_id=p.property_id "
            "JOIN entities e ON e.entity_id=l.entity_id AND e.entity_type='parcel' "
            "LEFT JOIN entity_aliases a ON a.entity_id=e.entity_id AND a.alias_type='parcel_identifier' "
            "WHERE p.scope_id=? AND (a.normalized_value=? OR UPPER(COALESCE(e.external_id,''))=?)",
            (scope_id, key, str(parcel_value).upper()),
        ):
            property_ids.add(result["property_id"])
            parcel_ids.add(result["entity_id"])
        basis = "parcel_identifier"
    if not property_ids and address_value is not None:
        key = normalize_address(str(address_value))
        suffix = match.get("address_suffix", "")
        keys = {key, normalize_address(f"{address_value}{suffix}")}
        marks = ",".join("?" for _ in keys)
        for result in store.rows(
            f"SELECT property_id FROM property_index WHERE scope_id=? AND normalized_address IN ({marks})",
            (scope_id, *sorted(keys)),
        ):
            property_ids.add(result["property_id"])
        basis = "normalized_address"
    if not property_ids:
        raise LookupError("unmatched")
    if len(property_ids) != 1:
        raise LookupError(f"ambiguous:{sorted(property_ids)}")
    property_id = next(iter(property_ids))
    if not parcel_ids:
        parcel_ids = {row[0] for row in store.rows(
            "SELECT l.entity_id FROM property_entity_links l JOIN entities e ON e.entity_id=l.entity_id "
            "WHERE l.property_id=? AND e.entity_type='parcel'", (property_id,))}
    return property_id, sorted(parcel_ids), basis


def _profile() -> dict[str, Counter[str] | int]:
    return {
        "entity_types": Counter(), "fact_categories": Counter(),
        "fact_predicates": Counter(), "relationship_types": Counter(),
        "property_roles": Counter(), "event_types": Counter(),
        "alias_types": Counter(), "memberships": 0,
    }


def collect(store: EvidenceStore, scope: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Ingest deed, lien, permit, zoning, hazard, and infrastructure rows.

    Records are joined only through an exact jurisdiction-scoped parcel alias or
    exact normalized property address. Unmatched and ambiguous rows are counted,
    never guessed onto a property.
    """
    totals: dict[str, Any] = {
        "datasets": 0, "rows": 0, "matched": 0, "unmatched": 0, "ambiguous": 0,
        "entities": 0, "facts": 0, "relationships": 0, "events": 0,
        "output_profile": _profile(), "unresolved_examples": [],
    }
    for dataset in config.get("datasets", []):
        source_payload = {
            "kind": "linked_jurisdiction_dataset",
            "file": file_fingerprint(Path(dataset["path"])) if dataset.get("path") else None,
            "mapping": {key: value for key, value in dataset.items() if key not in {"rows"}},
        }
        source_raw = store.put_raw(source_payload)
        source_config = dataset.get("source", {})
        source_id = store.source(
            name=source_config.get("name", dataset.get("name", "Linked jurisdiction records")),
            url=source_config.get("url"),
            authority=source_config.get("authority", "configured public source"),
            parser_version=PARSER_VERSION, raw_sha256=source_raw,
            source_date=source_config.get("source_date"),
            retrieved_at=source_config.get("retrieved_at"),
            access_note=source_config.get("access_note"),
        )
        entity_config = dataset.get("entity") or {}
        match = dataset.get("match") or {}
        for row_number, row in _rows(dataset):
            totals["rows"] += 1
            try:
                property_id, parcel_ids, match_basis = _resolve_subjects(
                    store, scope["id"], row, match)
            except LookupError as exc:
                kind = "ambiguous" if str(exc).startswith("ambiguous:") else "unmatched"
                totals[kind] += 1
                if len(totals["unresolved_examples"]) < 20:
                    totals["unresolved_examples"].append({
                        "dataset": dataset.get("name"), "row": row_number,
                        "status": kind, "parcel": _value(row, match.get("parcel_id_field")),
                        "address": _value(row, match.get("address_field")),
                    })
                if match.get("required", True) and kind == "ambiguous":
                    raise ValueError(
                        f"Ambiguous linked record in {dataset.get('name', 'dataset')} row {row_number}"
                    )
                continue
            totals["matched"] += 1
            row_raw = store.put_raw({"dataset": dataset.get("name"), "row_number": row_number, "record": row})
            subject_map: dict[str, Any] = {"property": property_id, "parcels": parcel_ids}

            entity_id = None
            if entity_config:
                entity_type = entity_config["type"]
                external = _value(row, entity_config.get("id_field"))
                name = _value(row, entity_config.get("name_field")) or external
                if not name:
                    raise ValueError(
                        f"Linked record entity lacks name/id in {dataset.get('name')} row {row_number}"
                    )
                attributes = {
                    output: _typed(_value(row, field), None)
                    for output, field in (entity_config.get("attributes") or {}).items()
                    if _value(row, field) is not None
                }
                entity_id = stable_id(
                    entity_type[:4], scope["jurisdiction_id"],
                    external or normalize_text(str(name)),
                )
                store.entity(entity_type, str(name), external_id=(
                    f"{scope['jurisdiction_id']}:{external}" if external else None),
                    attributes=attributes, entity_id=entity_id)
                role = entity_config.get("property_role", entity_type)
                store.link_property_entity(
                    property_id=property_id, entity_id=entity_id, role=role,
                    confidence=float(entity_config.get("confidence", 1.0)),
                    source_id=source_id,
                    evidence={"match_basis": match_basis, "source_row": row_number},
                )
                totals["entities"] += 1
                totals["output_profile"]["entity_types"][entity_type] += 1
                totals["output_profile"]["property_roles"][role] += 1
                for alias in entity_config.get("aliases", []):
                    raw_value = _value(row, alias.get("field"))
                    if raw_value is None:
                        continue
                    alias_type = alias["type"]
                    normalized = (normalize_address(str(raw_value)) if alias_type == "situs_address"
                                  else normalize_text(str(raw_value)))
                    store.alias(entity_id, alias_type, str(raw_value), normalized,
                                source_id=source_id,
                                confidence=float(alias.get("confidence", 1.0)))
                    totals["output_profile"]["alias_types"][alias_type] += 1
            subject_map["entity"] = entity_id

            for relationship in dataset.get("relationships", []):
                source_subject = relationship.get("from", "entity")
                target_subject = relationship.get("to", "parcel")
                from_ids = (parcel_ids if source_subject == "parcel" else
                            [subject_map.get(source_subject)])
                to_ids = (parcel_ids if target_subject == "parcel" else
                          [subject_map.get(target_subject)])
                for from_id in [value for value in from_ids if value]:
                    for to_id in [value for value in to_ids if value]:
                        store.relationship(
                            from_id=from_id, relationship_type=relationship["type"],
                            to_id=to_id, fact_class=relationship.get("fact_class", "confirmed_official"),
                            confidence=float(relationship.get("confidence", 1.0)),
                            source_id=source_id, parser_version=PARSER_VERSION,
                            raw_sha256=row_raw,
                            effective_date=_typed(_value(row, relationship.get("effective_date_field")), "date"),
                            explanation={"match_basis": match_basis, "source_row": row_number},
                        )
                        totals["relationships"] += 1
                        totals["output_profile"]["relationship_types"][relationship["type"]] += 1

            for fact in dataset.get("facts", []):
                value = _typed(_value(row, fact.get("field")), fact.get("value_type"))
                if value is None:
                    continue
                targets = (parcel_ids if fact.get("target", "entity") == "parcel" else
                           [subject_map.get(fact.get("target", "entity"))])
                for target_id in [item for item in targets if item]:
                    store.fact(
                        subject_id=target_id, category=fact["category"],
                        predicate=fact["predicate"], value=value, unit=fact.get("unit"),
                        fact_class=fact.get("fact_class", "confirmed_official"),
                        confidence=float(fact.get("confidence", 1.0)),
                        source_id=source_id, parser_version=PARSER_VERSION,
                        raw_sha256=row_raw,
                        effective_date=_typed(_value(row, fact.get("effective_date_field")), "date"),
                        freshness_days=fact.get("freshness_days"),
                        evidence_locator=f"{dataset.get('name', 'dataset')}:row:{row_number}:{fact.get('field')}",
                    )
                    totals["facts"] += 1
                    totals["output_profile"]["fact_categories"][fact["category"]] += 1
                    totals["output_profile"]["fact_predicates"][fact["predicate"]] += 1

            event = dataset.get("event")
            if event:
                event_date = _typed(_value(row, event.get("date_field")), "date")
                summary_value = _value(row, event.get("summary_field"))
                event_subject = subject_map.get(event.get("subject", "entity")) or property_id
                store.event(
                    target_id=property_id, event_type=event["type"],
                    event_date=event_date, date_precision=event.get("date_precision", "day"),
                    subject_id=event_subject,
                    summary=str(summary_value or event.get("summary", event["type"])),
                    fact_class=event.get("fact_class", "confirmed_official"),
                    confidence=float(event.get("confidence", 1.0)),
                    source_ids=[source_id],
                    evidence={"match_basis": match_basis, "source_row": row_number},
                )
                totals["events"] += 1
                totals["output_profile"]["event_types"][event["type"]] += 1
        totals["datasets"] += 1

    maximum_unmatched = int(config.get("maximum_unmatched", 0))
    if totals["unmatched"] > maximum_unmatched:
        raise ValueError(
            f"Linked-record join left {totals['unmatched']} unmatched rows; "
            f"configured maximum is {maximum_unmatched}"
        )
    totals["output_profile"] = {
        key: dict(value) if isinstance(value, Counter) else value
        for key, value in totals["output_profile"].items()
    }
    return totals
