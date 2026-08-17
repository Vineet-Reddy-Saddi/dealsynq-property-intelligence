from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..store import EvidenceStore
from ..util import (file_fingerprint, normalize_address, normalize_text,
                    parse_number, stable_id)

PARSER_VERSION = "mapped-tabular-municipality/1.3.0"


def fingerprint(config: dict[str, Any]) -> str:
    manifest = Path(config["manifest_path"]) if config.get("manifest_path") else None
    return stable_id(
        "input", PARSER_VERSION, file_fingerprint(config["path"]),
        file_fingerprint(manifest) if manifest and manifest.exists() else None,
        config,
    )


def _field(row: dict[str, str], fields: dict[str, str], key: str) -> str | None:
    column = fields.get(key)
    value = row.get(column) if column else None
    return value.strip() if value and value.strip() else None


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.strip().split("/")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        month, day, year = map(int, parts)
        return f"{year:04d}-{month:02d}-{day:02d}"
    return value.strip()


def _typed_value(value: str | None, value_type: str | None) -> Any:
    if value is None or not value_type or value_type == "string":
        return value
    if value_type == "number":
        return parse_number(value)
    if value_type == "integer":
        number = parse_number(value)
        return int(number) if number is not None else None
    if value_type == "boolean":
        return normalize_text(value) in {"1", "TRUE", "YES", "Y"}
    if value_type == "json":
        return json.loads(value)
    if value_type == "date":
        return _iso_date(value)
    raise ValueError(f"Unsupported mapped fact value_type {value_type!r}")


def _source(store: EvidenceStore, item: dict[str, Any], raw_sha: str,
            fallback_name: str) -> str:
    return store.source(
        name=item.get("name", fallback_name), url=item.get("url"),
        authority=item.get("authority", "configured public source"),
        parser_version=PARSER_VERSION, raw_sha256=raw_sha,
        source_date=item.get("source_date"), retrieved_at=item.get("retrieved_at"),
        access_note=item.get("access_note"),
    )


def collect(store: EvidenceStore, scope: dict[str, Any],
            config: dict[str, Any]) -> dict[str, Any]:
    """Ingest any municipality-wide CSV through an explicit field map."""
    path = Path(config["path"])
    fields = config["fields"]
    scope_id = scope["id"]
    address_suffix = config.get("address_suffix", "")
    accepted_statuses = set(config.get("accepted_statuses", []))
    manifest = None
    if config.get("manifest_path"):
        manifest = json.loads(Path(config["manifest_path"]).read_text(encoding="utf-8"))
    raw_sha = store.put_raw({
        "kind": "external_tabular_source", "file": file_fingerprint(path),
        "manifest": manifest, "field_map": fields,
    })
    assessor_source = _source(
        store, config.get("assessor_source", {}), raw_sha,
        "Mapped official assessor source")
    parcel_source = _source(
        store, config.get("parcel_source", config.get("assessor_source", {})),
        raw_sha, "Mapped official parcel source")

    stats: dict[str, Any] = {
        "rows": 0, "accepted_rows": 0, "properties": 0, "parcels": 0,
        "owners": 0, "facts": 0, "relationships": 0, "memberships": 0,
        "geometry_rows": 0, "skipped_without_address": 0,
        "included_without_address": 0,
        "join_status_counts": defaultdict(int),
        "output_profile": {
            "entity_types": {"property_site": 0, "parcel": 0, "organization": 0},
            "fact_categories": defaultdict(int),
            "fact_predicates": defaultdict(int),
            "relationship_types": defaultdict(int),
            "property_roles": defaultdict(int),
            "event_types": {},
            "alias_types": defaultdict(int),
            "memberships": 0,
        },
    }
    properties: set[str] = set()
    owners: set[str] = set()
    aggregates: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"land_sqft": 0.0, "building_sqft": 0.0, "assessed": 0.0,
                 "land_n": 0, "building_n": 0, "assessed_n": 0,
                 "geometries": [], "geometry_area": 0.0, "geometry_area_n": 0})

    with path.open("r", encoding=config.get("encoding", "utf-8-sig"), newline="") as handle:
        reader = csv.DictReader(handle, delimiter=config.get("delimiter", ","))
        missing = sorted({column for column in fields.values()
                          if column and column not in (reader.fieldnames or [])})
        if missing:
            raise ValueError(f"Mapped municipality CSV columns are missing: {missing}")

        for row_number, row in enumerate(reader, start=2):
            stats["rows"] += 1
            join_status = _field(row, fields, "join_status") or "unknown"
            stats["join_status_counts"][join_status] += 1
            if accepted_statuses and join_status not in accepted_statuses:
                continue
            parcel_raw = (_field(row, fields, "parcel_id") or
                          _field(row, fields, "assessor_id") or f"row-{row_number}")
            situs = _field(row, fields, "address")
            if not situs and not config.get("include_without_address", False):
                stats["skipped_without_address"] += 1
                continue
            has_situs = bool(situs)
            if not situs:
                situs = config.get(
                    "unaddressed_label_template", "Unaddressed parcel {parcel_id}"
                ).format(parcel_id=parcel_raw, municipality=scope.get("name", ""))
                stats["included_without_address"] += 1
            stats["accepted_rows"] += 1
            record_url = _field(row, fields, "record_url")
            record_retrieved_at = _field(row, fields, "record_retrieved_at")
            full_address = f"{situs}{address_suffix}"
            normalized_address = normalize_address(full_address)
            property_id = stable_id(
                "property", scope_id,
                normalized_address if has_situs else normalize_text(parcel_raw),
            )
            property_name = config.get("property_name_template", "{address} Property").format(
                address=situs, municipality=scope.get("name", ""))
            if property_id not in properties:
                store.upsert_indexed_property(
                    property_id=property_id, scope_id=scope_id, name=property_name,
                    address=full_address, normalized_address=normalized_address,
                    external_id=f"{scope_id}:{normalize_address(situs) if has_situs else normalize_text(parcel_raw)}",
                    attributes={"assembly_basis": ("exact normalized situs address" if has_situs
                                                   else "official parcel without published situs")},
                )
                store.entity(
                    "property_site", property_name,
                    external_id=f"{scope_id}:{normalize_address(situs) if has_situs else normalize_text(parcel_raw)}",
                    attributes={"address": full_address,
                                "assembly_basis": ("exact normalized situs address" if has_situs
                                                   else "official parcel without published situs")},
                    entity_id=property_id,
                )
                store.link_property_entity(
                    property_id=property_id, entity_id=property_id,
                    role="property_site", source_id=assessor_source,
                    evidence={"basis": ("exact normalized situs address" if has_situs
                                        else "official parcel without published situs")},
                )
                properties.add(property_id)
                stats["output_profile"]["entity_types"]["property_site"] += 1
                stats["output_profile"]["property_roles"]["property_site"] += 1

            parcel_id = stable_id("parc", scope["jurisdiction_id"], parcel_raw)
            store.entity(
                "parcel", parcel_raw,
                external_id=f"{scope['jurisdiction_id']}:{parcel_raw}",
                attributes={"join_status": join_status, "source_row": row_number},
                entity_id=parcel_id,
            )
            store.alias(parcel_id, "parcel_identifier", parcel_raw,
                        normalize_text(parcel_raw), source_id=assessor_source)
            stats["output_profile"]["alias_types"]["parcel_identifier"] += 1
            for alias_key in ("secondary_parcel_id", "geometry_parcel_id"):
                alias_value = _field(row, fields, alias_key)
                if alias_value and normalize_text(alias_value) != normalize_text(parcel_raw):
                    store.alias(parcel_id, "parcel_identifier", alias_value,
                                normalize_text(alias_value), source_id=assessor_source)
                    stats["output_profile"]["alias_types"]["parcel_identifier"] += 1
            if has_situs:
                store.alias(parcel_id, "situs_address", situs,
                            normalize_address(situs), source_id=assessor_source)
                stats["output_profile"]["alias_types"]["situs_address"] += 1
            membership_evidence = {
                "classification": ("confirmed_address_membership" if has_situs
                                   else "confirmed_unaddressed_parcel_membership"),
                "same_address": {"matched": has_situs, "weight": 1.0 if has_situs else 0.0},
                "source_row": row_number, "join_status": join_status,
            }
            store.link_property_entity(
                property_id=property_id, entity_id=parcel_id, role="parcel",
                source_id=assessor_source, evidence=membership_evidence,
            )
            store.record_decision(property_id, parcel_id, True, 1.0, 0.6,
                                  membership_evidence, PARSER_VERSION)
            stats["parcels"] += 1
            stats["memberships"] += 1
            stats["output_profile"]["memberships"] += 1
            stats["output_profile"]["entity_types"]["parcel"] += 1
            stats["output_profile"]["property_roles"]["parcel"] += 1

            owner = _field(row, fields, "owner")
            if owner:
                owner_key = normalize_text(owner)
                owner_id = stable_id("org", scope["jurisdiction_id"], owner_key)
                store.entity("organization", owner,
                             external_id=f"{scope_id}:owner:{owner_key}", entity_id=owner_id)
                store.alias(owner_id, "organization_name", owner, owner_key,
                            source_id=assessor_source)
                store.link_property_entity(
                    property_id=property_id, entity_id=owner_id,
                    role="assessor_owner", source_id=assessor_source,
                    evidence={"source_row": row_number},
                )
                store.relationship(
                    from_id=owner_id, relationship_type="assessor_owner_of",
                    to_id=parcel_id, fact_class="confirmed_official", confidence=1.0,
                    source_id=assessor_source, parser_version=PARSER_VERSION,
                    raw_sha256=raw_sha, explanation={"source_row": row_number},
                )
                owners.add(owner_id)
                stats["relationships"] += 1
                stats["output_profile"]["entity_types"]["organization"] += 1
                stats["output_profile"]["alias_types"]["organization_name"] += 1
                stats["output_profile"]["property_roles"]["assessor_owner"] += 1
                stats["output_profile"]["relationship_types"]["assessor_owner_of"] += 1

            locator = f"{path.name}:row:{row_number}"
            specs = [
                ("use_code", _field(row, fields, "use_code"), None),
                ("use_description", _field(row, fields, "use_description"), None),
                ("assessor_zoning_code", _field(row, fields, "zoning"), None),
                ("assessment_year", _field(row, fields, "assessment_year"), None),
                ("year_built", parse_number(_field(row, fields, "year_built")), "year"),
                ("building_area", parse_number(_field(row, fields, "building_area")), "sq_ft"),
                ("land_area", parse_number(_field(row, fields, "land_area")), "acres"),
                ("assessed_land", parse_number(_field(row, fields, "assessed_land")), "usd"),
                ("assessed_improvement", parse_number(_field(row, fields, "assessed_improvement")), "usd"),
                ("assessed_value", parse_number(_field(row, fields, "assessed_total")), "usd"),
                ("last_sale_date", _iso_date(_field(row, fields, "last_sale_date")), None),
                ("last_sale_price", parse_number(_field(row, fields, "last_sale_price")), "usd"),
                ("assessor_record_url", record_url, None),
                ("assessor_record_retrieved_at", record_retrieved_at, None),
            ]
            for predicate, value, unit in specs:
                if value is None or value == "":
                    continue
                category = "transaction" if predicate.startswith("last_sale") else "assessor"
                store.fact(
                    subject_id=parcel_id, category=category, predicate=predicate,
                    value=value, unit=unit, fact_class="confirmed_official",
                    confidence=1.0, source_id=assessor_source,
                    parser_version=PARSER_VERSION, raw_sha256=raw_sha,
                    evidence_locator=locator,
                )
                stats["facts"] += 1
                stats["output_profile"]["fact_categories"][category] += 1
                stats["output_profile"]["fact_predicates"][predicate] += 1

            for mapping in config.get("extra_facts", []):
                raw_value = row.get(mapping["field"])
                raw_value = raw_value.strip() if isinstance(raw_value, str) else raw_value
                if raw_value is None or raw_value == "":
                    continue
                value = _typed_value(str(raw_value), mapping.get("value_type"))
                if value is None:
                    continue
                subject_id = property_id if mapping.get("target") == "property" else parcel_id
                mapped_source = parcel_source if mapping.get("source", "parcel") == "parcel" else assessor_source
                store.fact(
                    subject_id=subject_id, category=mapping["category"],
                    predicate=mapping["predicate"], value=value, unit=mapping.get("unit"),
                    fact_class=mapping.get("fact_class", "confirmed_official"),
                    confidence=float(mapping.get("confidence", 1.0)),
                    source_id=mapped_source, parser_version=PARSER_VERSION,
                    raw_sha256=raw_sha, freshness_days=mapping.get("freshness_days"),
                    effective_date=_iso_date(row.get(mapping.get("effective_date_field", ""))),
                    evidence_locator=f"{locator}:{mapping['field']}",
                )
                stats["facts"] += 1
                stats["output_profile"]["fact_categories"][mapping["category"]] += 1
                stats["output_profile"]["fact_predicates"][mapping["predicate"]] += 1

            transaction_cfg = config.get("transaction_document")
            if transaction_cfg:
                book_page = row.get(transaction_cfg.get("book_page_field", ""))
                book_page = book_page.strip() if isinstance(book_page, str) else book_page
                transaction_date = _iso_date(row.get(transaction_cfg.get("date_field", "")))
                instrument = row.get(transaction_cfg.get("instrument_field", ""))
                instrument = instrument.strip() if isinstance(instrument, str) else instrument
                party = row.get(transaction_cfg.get("party_field", ""))
                party = party.strip() if isinstance(party, str) else party
                price = parse_number(row.get(transaction_cfg.get("price_field", "")))
                if book_page or transaction_date or instrument:
                    document_id = stable_id(
                        "doc", scope_id, "assessor_transfer_observation", parcel_id,
                        book_page, transaction_date, instrument,
                    )
                    title_parts = ["Assessor transfer observation"]
                    if book_page:
                        title_parts.append(f"book/page {book_page}")
                    store.entity(
                        "recorded_document", " - ".join(title_parts),
                        external_id=f"{scope_id}:assessor-transfer:{document_id}",
                        attributes={
                            "document_type": "assessor_transfer_observation",
                            "book_page": book_page, "instrument_code": instrument,
                            "reported_party": party, "reported_price": price,
                            "qualification": (
                                "Assessor transfer/index observation only; the recorded "
                                "instrument and complete land-record index are not connected."
                            ),
                        }, entity_id=document_id,
                    )
                    store.link_property_entity(
                        property_id=property_id, entity_id=document_id,
                        role="assessor_transfer_observation", source_id=assessor_source,
                        evidence={"source_row": row_number, "qualification": "not deed proof"},
                    )
                    store.relationship(
                        from_id=document_id, relationship_type="affects", to_id=parcel_id,
                        fact_class="confirmed_official", confidence=0.9,
                        source_id=assessor_source, parser_version=PARSER_VERSION,
                        raw_sha256=raw_sha, effective_date=transaction_date,
                        explanation={"source_row": row_number,
                                     "qualification": "assessor index observation; instrument not collected"},
                    )
                    store.fact(
                        subject_id=document_id, category="deeds_liens",
                        predicate="document_type", value="assessor_transfer_observation",
                        fact_class="confirmed_official", confidence=0.9,
                        source_id=assessor_source, parser_version=PARSER_VERSION,
                        raw_sha256=raw_sha, effective_date=transaction_date,
                        evidence_locator=f"{locator}:assessor transfer fields; not deed image",
                    )
                    if book_page:
                        store.fact(
                            subject_id=document_id, category="deeds_liens",
                            predicate="book_page", value=book_page,
                            fact_class="confirmed_official", confidence=0.9,
                            source_id=assessor_source, parser_version=PARSER_VERSION,
                            raw_sha256=raw_sha, effective_date=transaction_date,
                            evidence_locator=f"{locator}:{transaction_cfg.get('book_page_field')}",
                        )
                    stats["facts"] += 1 + int(bool(book_page))
                    stats["relationships"] += 1
                    stats["output_profile"]["entity_types"].setdefault("recorded_document", 0)
                    stats["output_profile"]["entity_types"]["recorded_document"] += 1
                    stats["output_profile"]["property_roles"].setdefault("assessor_transfer_observation", 0)
                    stats["output_profile"]["property_roles"]["assessor_transfer_observation"] += 1
                    stats["output_profile"]["relationship_types"].setdefault("affects", 0)
                    stats["output_profile"]["relationship_types"]["affects"] += 1
                    stats["output_profile"]["fact_categories"]["deeds_liens"] += 1 + int(bool(book_page))
                    stats["output_profile"]["fact_predicates"]["document_type"] += 1
                    if book_page:
                        stats["output_profile"]["fact_predicates"]["book_page"] += 1

            aggregate = aggregates[property_id]
            land_acres = parse_number(_field(row, fields, "land_area"))
            building_sqft = parse_number(_field(row, fields, "building_area"))
            assessed = parse_number(_field(row, fields, "assessed_total"))
            if land_acres is not None:
                aggregate["land_sqft"] += land_acres * 43560.0
                aggregate["land_n"] += 1
            if building_sqft is not None:
                aggregate["building_sqft"] += building_sqft
                aggregate["building_n"] += 1
            if assessed is not None:
                aggregate["assessed"] += assessed
                aggregate["assessed_n"] += 1

            geometry_text = _field(row, fields, "geometry")
            if geometry_text:
                try:
                    geometry_value = json.loads(geometry_text)
                    store.fact(
                        subject_id=parcel_id, category="spatial",
                        predicate="parcel_geometry", value=geometry_value,
                        fact_class="confirmed_official", confidence=1.0,
                        source_id=parcel_source, parser_version=PARSER_VERSION,
                        raw_sha256=raw_sha, evidence_locator=locator,
                    )
                    aggregate["geometries"].append(geometry_value)
                    source_area = parse_number(_field(row, fields, "geometry_area"))
                    if source_area is not None:
                        aggregate["geometry_area"] += source_area
                        aggregate["geometry_area_n"] += 1
                    stats["geometry_rows"] += 1
                    stats["facts"] += 1
                    stats["output_profile"]["fact_categories"]["spatial"] += 1
                    stats["output_profile"]["fact_predicates"]["parcel_geometry"] += 1
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

    for property_id, aggregate in aggregates.items():
        for predicate, key, count_key, unit in (
            ("site_land_area", "land_sqft", "land_n", "sq_ft"),
            ("site_building_area", "building_sqft", "building_n", "sq_ft"),
            ("site_assessed_value", "assessed", "assessed_n", "usd"),
        ):
            if aggregate[count_key]:
                store.fact(
                    subject_id=property_id, category="calculation", predicate=predicate,
                    value=round(aggregate[key], 4), unit=unit,
                    fact_class="calculation", confidence=1.0,
                    source_id=assessor_source, parser_version=PARSER_VERSION,
                    raw_sha256=raw_sha,
                    evidence_locator=f"sum of {aggregate[count_key]} parcel observations",
                )
                stats["facts"] += 1
                stats["output_profile"]["fact_categories"]["calculation"] += 1
                stats["output_profile"]["fact_predicates"][predicate] += 1
        if aggregate["geometries"]:
            geometry_value = (aggregate["geometries"][0]
                              if len(aggregate["geometries"]) == 1 else
                              {"type": "GeometryCollection",
                               "geometries": aggregate["geometries"]})
            geometry_facts = [("analysis_geometry", geometry_value, None)]
            if aggregate["geometry_area_n"]:
                geometry_facts.append((
                    "parcel_geometry_union_area",
                    round(aggregate["geometry_area"], 4),
                    config.get("geometry_area_unit", "sq_ft"),
                ))
            for predicate, value, unit in geometry_facts:
                store.fact(
                    subject_id=property_id, category="spatial", predicate=predicate,
                    value=value, unit=unit, fact_class="calculation", confidence=1.0,
                    source_id=parcel_source, parser_version=PARSER_VERSION,
                    raw_sha256=raw_sha,
                    evidence_locator=("single linked parcel geometry" if len(aggregate["geometries"]) == 1
                                      else "geometry collection of exact-address parcels; not dissolved"),
                )
                stats["facts"] += 1
                stats["output_profile"]["fact_categories"]["spatial"] += 1
                stats["output_profile"]["fact_predicates"][predicate] += 1

    stats["properties"] = len(properties)
    stats["owners"] = len(owners)
    stats["join_status_counts"] = dict(stats["join_status_counts"])
    stats["output_profile"] = {
        key: dict(value) if hasattr(value, "items") else value
        for key, value in stats["output_profile"].items()
    }
    return stats
