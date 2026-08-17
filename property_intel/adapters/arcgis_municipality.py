from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Iterable

import requests
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

from ..geometry import SQM_TO_ACRES, SQM_TO_SQFT, buffered_point
from ..store import EvidenceStore
from ..util import normalize_address, normalize_text, stable_id, utcnow

PARSER_VERSION = "arcgis-municipality/1.1.0"
WGS84_TO_UTM18 = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True)


def fingerprint(config: dict[str, Any]) -> str:
    days = max(1, int(config.get("refresh_days", 7)))
    bucket = int(datetime.now(timezone.utc).timestamp() // (days * 86400))
    return stable_id("input", PARSER_VERSION, config, bucket)


def _profile() -> dict[str, Counter[str] | int]:
    return {
        "entity_types": Counter(), "fact_categories": Counter(),
        "fact_predicates": Counter(), "relationship_types": Counter(),
        "property_roles": Counter(), "event_types": Counter(),
        "alias_types": Counter(), "memberships": 0,
    }


def _clean_properties(properties: dict[str, Any], fields: Iterable[str] | None) -> dict[str, Any]:
    if not fields or "*" in fields:
        return {key: value for key, value in properties.items() if value not in (None, "")}
    return {key: properties.get(key) for key in fields if properties.get(key) not in (None, "")}


def _request_json(session: requests.Session, url: str, *, data: dict[str, Any],
                  timeout: int, attempts: int = 4) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.post(url, data=data, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                raise RuntimeError(f"ArcGIS error: {payload['error']}")
            return payload
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(8, 2 ** attempt))
    raise RuntimeError(f"ArcGIS request failed for {url}: {last}")


def _download_features(store: EvidenceStore, session: requests.Session,
                       layer: dict[str, Any], timeout: int) -> tuple[list[dict[str, Any]], str]:
    url = layer["url"].rstrip("/")
    query_url = f"{url}/query"
    metadata = _request_json(session, url, data={"f": "json"}, timeout=timeout)
    metadata_raw = store.put_raw({"url": url, "metadata": metadata})
    object_ids = _request_json(
        session, query_url,
        data={"where": layer.get("where", "1=1"), "returnIdsOnly": "true", "f": "json"},
        timeout=timeout,
    ).get("objectIds") or []
    id_field = metadata.get("objectIdField") or metadata.get("objectIdFieldName") or "OBJECTID"
    out_fields = layer.get("out_fields") or ["*"]
    features: list[dict[str, Any]] = []
    batch_size = min(int(layer.get("batch_size", 1000)), int(metadata.get("maxRecordCount") or 2000))
    for offset in range(0, len(object_ids), max(1, batch_size)):
        chunk = object_ids[offset:offset + batch_size]
        payload = _request_json(
            session, query_url,
            data={
                "objectIds": ",".join(str(value) for value in chunk),
                "outFields": ",".join(out_fields), "returnGeometry": "true",
                "outSR": "4326", "f": "geojson",
            }, timeout=timeout,
        )
        page_raw = store.put_raw({"url": query_url, "object_ids": chunk, "payload": payload})
        for feature in payload.get("features") or []:
            if not feature.get("geometry"):
                continue
            properties = feature.get("properties") or {}
            features.append({
                "geometry": feature["geometry"], "properties": properties,
                "object_id": properties.get(id_field) or properties.get("OBJECTID") or feature.get("id"),
                "raw_sha256": page_raw,
            })
    return features, metadata_raw


def _source(store: EvidenceStore, layer: dict[str, Any], raw_sha: str) -> str:
    source = layer.get("source") or {}
    return store.source(
        name=source.get("name", layer.get("label", layer["key"])),
        url=source.get("url", layer["url"]),
        authority=source.get("authority", "Official ArcGIS publisher"),
        parser_version=PARSER_VERSION, raw_sha256=raw_sha,
        source_date=source.get("source_date"),
        access_note=source.get("access_note", "Public ArcGIS REST feature service."),
    )


def _supersede_scope_snapshot(store: EvidenceStore, scope_id: str, source_id: str,
                              predicates: list[str]) -> None:
    """Retire one publisher snapshot in bulk before its replacement is inserted.

    Municipality layers emit one bounded fact per property. Doing the source-
    equivalent supersession once per layer avoids hundreds of thousands of
    repeated source joins while preserving prior observations as superseded.
    """
    if not predicates:
        return
    placeholders = ",".join("?" for _ in predicates)
    store.db.execute(
        f"UPDATE facts SET status='superseded' WHERE status='current' "
        f"AND predicate IN ({placeholders}) "
        "AND subject_id IN (SELECT property_id FROM property_index WHERE scope_id=? AND status!='legacy_unmappable') "
        "AND source_id IN (SELECT old.source_id FROM sources old JOIN sources new "
        "ON old.source_name=new.source_name AND old.authority=new.authority WHERE new.source_id=?)",
        (*predicates, scope_id, source_id),
    )


def _supersede_scope_entity_snapshot(store: EvidenceStore, scope_id: str, source_id: str,
                                     predicates: list[str]) -> None:
    if not predicates:
        return
    placeholders = ",".join("?" for _ in predicates)
    store.db.execute(
        f"UPDATE facts SET status='superseded' WHERE status='current' "
        f"AND predicate IN ({placeholders}) "
        "AND subject_id IN (SELECT g.parcel_id FROM grouping_decisions g "
        "JOIN property_index p ON p.property_id=g.target_id WHERE p.scope_id=? AND p.status!='legacy_unmappable') "
        "AND source_id IN (SELECT old.source_id FROM sources old JOIN sources new "
        "ON old.source_name=new.source_name AND old.authority=new.authority WHERE new.source_id=?)",
        (*predicates, scope_id, source_id),
    )


def _project(geometry: Any) -> Any:
    return transform(WGS84_TO_UTM18.transform, geometry)


def _parcel_lookup(store: EvidenceStore, scope_id: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    aliases: dict[str, set[str]] = defaultdict(set)
    properties: dict[str, set[str]] = defaultdict(set)
    for row in store.rows(
        "SELECT DISTINCT e.entity_id,a.normalized_value FROM property_index p "
        "JOIN property_entity_links l ON l.property_id=p.property_id "
        "JOIN entities e ON e.entity_id=l.entity_id AND e.entity_type='parcel' "
        "JOIN entity_aliases a ON a.entity_id=e.entity_id AND a.alias_type='parcel_identifier' "
        "WHERE p.scope_id=? AND p.status!='legacy_unmappable'", (scope_id,),
    ):
        aliases[str(row["normalized_value"])].add(row["entity_id"])
    for row in store.rows(
        "SELECT l.entity_id,l.property_id FROM property_index p "
        "JOIN property_entity_links l ON l.property_id=p.property_id "
        "JOIN entities e ON e.entity_id=l.entity_id AND e.entity_type='parcel' "
        "WHERE p.scope_id=? AND p.status!='legacy_unmappable'", (scope_id,),
    ):
        properties[row["entity_id"]].add(row["property_id"])
    return aliases, properties


def _collect_parcels(store: EvidenceStore, scope: dict[str, Any], session: requests.Session,
                     layer: dict[str, Any], timeout: int,
                     totals: dict[str, Any]) -> None:
    features, metadata_raw = _download_features(store, session, layer, timeout)
    source_id = _source(store, layer, metadata_raw)
    attribute_predicate = layer.get("attribute_predicate", "official_city_parcel_record")
    _supersede_scope_entity_snapshot(
        store, scope["id"], source_id, ["parcel_geometry", attribute_predicate]
    )
    aliases, parcel_properties = _parcel_lookup(store, scope["id"])
    identifier_fields = layer.get("identifier_fields") or ["ParcelID"]
    address_field = layer.get("address_field")
    address_suffix = layer.get("address_suffix", "")
    property_by_address = {
        row["normalized_address"]: row["property_id"] for row in store.rows(
            "SELECT property_id,normalized_address FROM property_index WHERE scope_id=? AND status!='legacy_unmappable'", (scope["id"],)
        )
    }
    matched_entities: set[str] = set()
    matched_properties: set[str] = set()
    unmatched = 0
    ambiguous = 0
    for feature in features:
        props = feature["properties"]
        entity_ids: set[str] = set()
        for field in identifier_fields:
            value = props.get(field)
            if value not in (None, ""):
                candidates = aliases.get(normalize_text(str(value)), set())
                # Identifier fields are ordered from strongest to weakest. Do
                # not union independent identifiers: a secondary GIS tag may
                # legitimately be shared by multiple assessor cards even when
                # the publisher's ParcelID is an exact one-card match.
                if len(candidates) == 1:
                    entity_ids = set(candidates)
                    break
                if candidates and not entity_ids:
                    entity_ids = set(candidates)
        if not entity_ids and address_field and props.get(address_field):
            key = normalize_address(f"{props[address_field]}{address_suffix}")
            property_id = property_by_address.get(key)
            if property_id:
                entity_ids.update(row[0] for row in store.rows(
                    "SELECT l.entity_id FROM property_entity_links l JOIN entities e ON e.entity_id=l.entity_id "
                    "WHERE l.property_id=? AND e.entity_type='parcel'", (property_id,)
                ))
        if not entity_ids:
            unmatched += 1
            continue
        if len(entity_ids) > 1:
            ambiguous += 1
            continue
        parcel_id = next(iter(entity_ids))
        geometry = feature["geometry"]
        store.fact(
            subject_id=parcel_id, category="spatial", predicate="parcel_geometry",
            value=geometry, fact_class="confirmed_official", confidence=0.99,
            source_id=source_id, parser_version=PARSER_VERSION,
            raw_sha256=feature["raw_sha256"],
            evidence_locator=f"{layer['key']}:{feature['object_id']}",
            supersede_current=False,
        )
        attrs = _clean_properties(props, layer.get("attribute_fields"))
        if attrs:
            store.fact(
                subject_id=parcel_id, category="spatial",
                predicate=layer.get("attribute_predicate", "official_city_parcel_record"),
                value=attrs, fact_class="confirmed_official", confidence=0.98,
                source_id=source_id, parser_version=PARSER_VERSION,
                raw_sha256=feature["raw_sha256"],
                evidence_locator=f"{layer['key']}:{feature['object_id']}:attributes",
                supersede_current=False,
            )
            totals["facts"] += 1
            totals["output_profile"]["fact_categories"]["spatial"] += 1
            totals["output_profile"]["fact_predicates"][layer.get("attribute_predicate", "official_city_parcel_record")] += 1
        matched_entities.add(parcel_id)
        matched_properties.update(parcel_properties.get(parcel_id, set()))
        totals["facts"] += 1
    totals["parcel_features"] = len(features)
    totals["parcel_entities_matched"] = len(matched_entities)
    totals["parcel_properties_matched"] = len(matched_properties)
    totals["parcel_features_unmatched"] = unmatched
    totals["parcel_features_ambiguous"] = ambiguous
    totals["output_profile"]["entity_types"]["parcel"] += len(matched_entities)
    totals["output_profile"]["alias_types"]["parcel_identifier"] += len(matched_entities)
    totals["output_profile"]["fact_categories"]["spatial"] += len(matched_entities)
    totals["output_profile"]["fact_predicates"]["parcel_geometry"] += len(matched_entities)


def _site_geometries(store: EvidenceStore, scope_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    by_property: dict[str, dict[str, Any]] = defaultdict(dict)
    for row in store.rows(
        "SELECT p.property_id,l.entity_id,f.value_json,f.observed_at FROM property_index p "
        "JOIN property_entity_links l ON l.property_id=p.property_id AND l.role='parcel' "
        "JOIN facts f ON f.subject_id=l.entity_id AND f.predicate='parcel_geometry' AND f.status='current' "
        "WHERE p.scope_id=? AND p.status!='legacy_unmappable' ORDER BY f.observed_at DESC", (scope_id,),
    ):
        by_property[row["property_id"]].setdefault(row["entity_id"], json.loads(row["value_json"]))
    geographic: dict[str, Any] = {}
    projected: dict[str, Any] = {}
    for property_id, values in by_property.items():
        geometries = []
        for value in values.values():
            try:
                geometry = shape(value)
                if not geometry.is_empty:
                    geometries.append(geometry if geometry.is_valid else geometry.buffer(0))
            except (TypeError, ValueError):
                continue
        if not geometries:
            continue
        union = unary_union(geometries)
        geographic[property_id] = union
        projected[property_id] = _project(union)
    return geographic, projected


def _geocode_missing(store: EvidenceStore, scope: dict[str, Any], session: requests.Session,
                     geographic: dict[str, Any], projected: dict[str, Any],
                     config: dict[str, Any], timeout: int, totals: dict[str, Any]) -> None:
    if not config.get("geocode_unmatched", True):
        return
    url = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
    source_raw = store.put_raw({"url": url, "benchmark": "Public_AR_Current"})
    source_id = store.source(
        name="U.S. Census Geocoder", url=url, authority="United States Census Bureau",
        parser_version=PARSER_VERSION, raw_sha256=source_raw,
        access_note="Official public geocoding API; point match is not parcel geometry.",
    )
    rows = store.rows(
        "SELECT property_id,address FROM property_index WHERE scope_id=? AND status!='legacy_unmappable' ORDER BY property_id",
        (scope["id"],),
    )
    pending = [dict(row) for row in rows if row["property_id"] not in geographic]
    geocode_timeout = min(timeout, int(config.get("geocode_timeout_seconds", 12)))

    def fetch(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        try:
            response = requests.get(url, params={
                "address": row["address"], "benchmark": "Public_AR_Current",
                "vintage": "Current_Current", "format": "json",
            }, timeout=geocode_timeout)
            response.raise_for_status()
            return row, response.json()
        except (requests.RequestException, ValueError):
            return row, None

    geocoded = 0
    workers = max(1, min(int(config.get("geocode_workers", 8)), 16))
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch, row) for row in pending]
        for future in as_completed(futures):
            results.append(future.result())
    for row, payload in sorted(results, key=lambda item: item[0]["property_id"]):
        property_id = row["property_id"]
        if payload is None:
            continue
        try:
            raw = store.put_raw(payload)
            matches = ((payload.get("result") or {}).get("addressMatches") or [])
            if not matches:
                continue
            coordinates = matches[0].get("coordinates") or {}
            lon, lat = float(coordinates["x"]), float(coordinates["y"])
            site = buffered_point(lon, lat, radius_m=float(config.get("geocode_buffer_m", 2)))
            geographic[property_id] = site.geometry
            projected[property_id] = _project(site.geometry)
            store.fact(
                subject_id=property_id, category="spatial", predicate="geocoded_centroid",
                value={"lat": lat, "lon": lon, "matched_address": matches[0].get("matchedAddress"),
                       "analysis_basis": "census_geocoded_point"},
                fact_class="confirmed_official", confidence=0.9,
                source_id=source_id, parser_version=PARSER_VERSION, raw_sha256=raw,
                evidence_locator=f"Census one-line address query: {row['address']}",
            )
            geocoded += 1
        except (ValueError, KeyError):
            continue
    totals["properties_geocoded_without_parcel"] = geocoded


def _materialize_site_facts(store: EvidenceStore, scope_id: str, geographic: dict[str, Any],
                            projected: dict[str, Any], source_id: str,
                            raw_sha: str, totals: dict[str, Any]) -> None:
    _supersede_scope_snapshot(
        store, scope_id, source_id, ["analysis_geometry", "analysis_geometry_summary"]
    )
    for property_id, geometry in geographic.items():
        projected_geometry = projected[property_id]
        centroid = geometry.centroid
        basis = "parcel_union" if projected_geometry.area > 20 else "census_geocoded_point_buffer"
        store.fact(
            subject_id=property_id, category="spatial", predicate="analysis_geometry",
            value=mapping(geometry), fact_class="calculation", confidence=0.96 if basis == "parcel_union" else 0.72,
            source_id=source_id, parser_version=PARSER_VERSION, raw_sha256=raw_sha,
            evidence_locator=f"municipality-wide {basis}",
            supersede_current=False,
        )
        store.fact(
            subject_id=property_id, category="spatial", predicate="analysis_geometry_summary",
            value={
                "basis": basis, "centroid": {"lat": centroid.y, "lon": centroid.x},
                "area_sqft": round(projected_geometry.area * SQM_TO_SQFT),
                "area_acres": round(projected_geometry.area * SQM_TO_ACRES, 4),
            }, fact_class="calculation", confidence=0.96 if basis == "parcel_union" else 0.72,
            source_id=source_id, parser_version=PARSER_VERSION, raw_sha256=raw_sha,
            evidence_locator=f"municipality-wide {basis} summary",
            supersede_current=False,
        )
        totals["facts"] += 2
        totals["output_profile"]["fact_categories"]["spatial"] += 2
        totals["output_profile"]["fact_predicates"]["analysis_geometry"] += 1
        totals["output_profile"]["fact_predicates"]["analysis_geometry_summary"] += 1


def _record_for_feature(feature: dict[str, Any], layer: dict[str, Any],
                        distance_m: float | None = None,
                        intersection_sqft: float | None = None) -> dict[str, Any]:
    record = _clean_properties(feature["properties"], layer.get("record_fields") or layer.get("out_fields"))
    record["feature_id"] = feature.get("object_id")
    if distance_m is not None:
        record["distance_m"] = round(distance_m, 1)
        record["distance_ft"] = round(distance_m * 3.280839895, 1)
    if intersection_sqft is not None:
        record["intersection_sqft"] = round(intersection_sqft)
    return record


def _entity_for_match(store: EvidenceStore, property_id: str, layer: dict[str, Any],
                      feature: dict[str, Any], source_id: str,
                      distance_m: float | None, totals: dict[str, Any]) -> None:
    config = layer.get("entity")
    if not config:
        return
    props = feature["properties"]
    entity_type = config["type"]
    external = props.get(config.get("id_field", "OBJECTID")) or feature.get("object_id")
    name = props.get(config.get("name_field")) or f"{layer.get('label', layer['key'])} {external}"
    entity_id = stable_id(entity_type[:4], layer["key"], external)
    attributes = _clean_properties(props, config.get("attribute_fields") or layer.get("record_fields"))
    if distance_m is not None:
        attributes["distance_m"] = round(distance_m, 1)
    store.entity(entity_type, str(name), external_id=f"{layer['key']}:{external}",
                 attributes={"source_adapter": "arcgis_municipality", **attributes}, entity_id=entity_id)
    role = config.get("property_role", entity_type)
    store.link_property_entity(
        property_id=property_id, entity_id=entity_id, role=role,
        confidence=float(config.get("confidence", layer.get("confidence", 0.85))),
        source_id=source_id,
        evidence={"layer": layer["key"], "distance_m": distance_m,
                  "basis": layer.get("mode", "intersection")},
    )
    totals["entities"] += 1
    totals["property_links"] += 1
    totals["output_profile"]["entity_types"][entity_type] += 1
    totals["output_profile"]["property_roles"][role] += 1

    relationship_type = config.get("relationship_type")
    if relationship_type:
        store.relationship(
            from_id=entity_id, relationship_type=relationship_type, to_id=property_id,
            fact_class=layer.get("fact_class", "confirmed_official"),
            confidence=float(config.get("confidence", layer.get("confidence", 0.85))),
            source_id=source_id, parser_version=PARSER_VERSION,
            raw_sha256=feature.get("raw_sha256"),
            explanation={"layer": layer["key"], "distance_m": distance_m,
                         "basis": layer.get("mode", "intersection")},
        )
        totals["relationships"] = totals.get("relationships", 0) + 1
        totals["output_profile"]["relationship_types"][relationship_type] += 1

    temporal = config.get("temporal_state")
    if temporal:
        date_value = next((props.get(field) for field in temporal.get("valid_from_fields", [])
                           if props.get(field) not in (None, "")), None)
        state_value = _clean_properties(props, temporal.get("value_fields"))
        store.temporal_state(
            target_id=property_id, subject_id=entity_id, state_type=temporal["type"],
            value=state_value, source_id=source_id,
            confidence=float(config.get("confidence", layer.get("confidence", 0.85))),
            fact_class=layer.get("fact_class", "reported"), parser_version=PARSER_VERSION,
            valid_from=str(date_value)[:10] if date_value else None,
            first_seen=utcnow(),
        )
        totals["temporal_states"] += 1


def _collect_context_layer(store: EvidenceStore, scope: dict[str, Any], session: requests.Session,
                           layer: dict[str, Any], timeout: int,
                           geographic: dict[str, Any], projected: dict[str, Any],
                           totals: dict[str, Any]) -> None:
    features, metadata_raw = _download_features(store, session, layer, timeout)
    source_id = _source(store, layer, metadata_raw)
    _supersede_scope_snapshot(store, scope["id"], source_id, [layer["predicate"]])
    usable: list[dict[str, Any]] = []
    shapes: list[Any] = []
    for feature in features:
        try:
            geometry = shape(feature["geometry"])
            if geometry.is_empty:
                continue
            projected_geometry = _project(geometry if geometry.is_valid else geometry.buffer(0))
        except (TypeError, ValueError):
            continue
        feature["projected"] = projected_geometry
        usable.append(feature)
        shapes.append(projected_geometry)
    tree = STRtree(shapes) if shapes else None
    mode = layer.get("mode", "intersection")
    radius_m = float(layer.get("radius_m", 0))
    max_records = max(0, int(layer.get("max_records", 20)))
    category = layer["category"]
    predicate = layer["predicate"]
    evaluated = 0
    matched_properties = 0
    match_count = 0
    property_rows = store.rows(
        "SELECT property_id FROM property_index WHERE scope_id=? AND status!='legacy_unmappable' ORDER BY property_id", (scope["id"],)
    )
    for row in property_rows:
        property_id = row["property_id"]
        site = projected.get(property_id)
        records: list[tuple[float, dict[str, Any], float | None]] = []
        if site is not None and tree is not None:
            query_geometry = site.buffer(radius_m) if mode == "proximity" else site
            indices = tree.query(query_geometry)
            for raw_index in indices:
                index = int(raw_index)
                feature = usable[index]
                feature_geometry = shapes[index]
                if mode == "proximity":
                    distance_m = float(site.distance(feature_geometry))
                    if distance_m > radius_m:
                        continue
                    records.append((distance_m, feature, None))
                else:
                    if not site.intersects(feature_geometry):
                        continue
                    intersection = site.intersection(feature_geometry)
                    area_sqft = float(intersection.area * SQM_TO_SQFT)
                    records.append((0.0, feature, area_sqft))
            records.sort(key=lambda item: (item[0], str(item[1].get("object_id"))))
        limited = records[:max_records] if max_records else []
        value: dict[str, Any] = {
            "analysis_basis": ("parcel_union_or_official_geocode" if site is not None else "unavailable"),
            "evaluated": site is not None,
            "feature_count": len(records),
            "records": [_record_for_feature(feature, layer, distance, area)
                        for distance, feature, area in limited],
            "source_feature_count": len(usable),
            "screening_note": layer.get("limitation"),
        }
        if mode == "proximity":
            value["radius_m"] = radius_m
            value["nearest_distance_m"] = round(records[0][0], 1) if records else None
        else:
            total_area = sum(area or 0.0 for _, _, area in records)
            value["intersection_sqft"] = round(total_area)
            if site is not None and site.area > 0:
                value["site_percent"] = round(total_area / (site.area * SQM_TO_SQFT) * 100, 2)
        if len(records) > len(limited):
            value["records_truncated"] = len(records) - len(limited)
        store.fact(
            subject_id=property_id, category=category, predicate=predicate,
            value=value, fact_class=layer.get("fact_class", "confirmed_official"),
            confidence=float(layer.get("confidence", 0.9)),
            source_id=source_id, parser_version=PARSER_VERSION,
            raw_sha256=metadata_raw,
            evidence_locator=f"{layer['key']}:municipality-wide-{mode}",
            freshness_days=layer.get("freshness_days"),
            supersede_current=False,
        )
        totals["facts"] += 1
        totals["output_profile"]["fact_categories"][category] += 1
        totals["output_profile"]["fact_predicates"][predicate] += 1
        evaluated += int(site is not None)
        if records:
            matched_properties += 1
            match_count += len(records)
            for distance, feature, _ in limited:
                _entity_for_match(store, property_id, layer, feature, source_id,
                                  distance if mode == "proximity" else None, totals)
    totals["layers"][layer["key"]] = {
        "source_features": len(usable), "properties": len(property_rows),
        "evaluated": evaluated, "matched_properties": matched_properties,
        "matches": match_count, "predicate": predicate,
    }


def collect(store: EvidenceStore, scope: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Precompute official ArcGIS parcel and context evidence for every property.

    The adapter downloads each configured public layer once, performs local spatial
    joins, and writes an explicit result for every indexed property. A zero-result
    screen remains evidence of a completed screen; it is never promoted to proof
    that a legal, environmental, market, or infrastructure condition is absent.
    """
    timeout = int(config.get("timeout_seconds", 90))
    session = requests.Session()
    session.headers.update({"User-Agent": config.get("user_agent", "DealSynq property intelligence/1.0")})
    totals: dict[str, Any] = {
        "facts": 0, "entities": 0, "property_links": 0,
        "temporal_states": 0, "layers": {}, "output_profile": _profile(),
    }
    parcel_layer = config.get("parcel_layer")
    if not parcel_layer:
        raise ValueError("arcgis_municipality requires parcel_layer")
    _collect_parcels(store, scope, session, parcel_layer, timeout, totals)
    geographic, projected = _site_geometries(store, scope["id"])
    _geocode_missing(store, scope, session, geographic, projected, config, timeout, totals)
    parcel_source_raw = store.put_raw({
        "kind": "municipality_analysis_geometry", "parcel_layer": parcel_layer["url"],
        "properties_with_geometry": len(geographic),
    })
    parcel_source_id = _source(store, parcel_layer, parcel_source_raw)
    _materialize_site_facts(store, scope["id"], geographic, projected, parcel_source_id,
                            parcel_source_raw, totals)
    totals["properties_with_analysis_geometry"] = len(geographic)
    totals["indexed_properties"] = store.db.execute(
        "SELECT COUNT(*) FROM property_index WHERE scope_id=? AND status!='legacy_unmappable'", (scope["id"],)
    ).fetchone()[0]
    for layer in config.get("layers", []):
        _collect_context_layer(store, scope, session, layer, timeout,
                               geographic, projected, totals)
    for item in config.get("constant_facts", []):
        raw = store.put_raw({"kind": "municipality_constant_fact", "item": item})
        source_id = store.source(
            name=item["source"]["name"], url=item["source"].get("url"),
            authority=item["source"]["authority"], parser_version=PARSER_VERSION,
            raw_sha256=raw, source_date=item["source"].get("source_date"),
            access_note=item["source"].get("access_note"),
        )
        _supersede_scope_snapshot(store, scope["id"], source_id, [item["predicate"]])
        for row in store.rows(
            "SELECT property_id FROM property_index WHERE scope_id=? AND status!='legacy_unmappable' ORDER BY property_id",
            (scope["id"],),
        ):
            store.fact(
                subject_id=row["property_id"], category=item["category"],
                predicate=item["predicate"], value=item["value"],
                fact_class=item.get("fact_class", "confirmed_official"),
                confidence=float(item.get("confidence", 1.0)), source_id=source_id,
                parser_version=PARSER_VERSION, raw_sha256=raw,
                evidence_locator=item.get("evidence_locator"),
                supersede_current=False,
            )
            totals["facts"] += 1
            totals["output_profile"]["fact_categories"][item["category"]] += 1
            totals["output_profile"]["fact_predicates"][item["predicate"]] += 1
    totals["output_profile"] = {
        key: dict(value) if isinstance(value, Counter) else value
        for key, value in totals["output_profile"].items()
    }
    return totals
