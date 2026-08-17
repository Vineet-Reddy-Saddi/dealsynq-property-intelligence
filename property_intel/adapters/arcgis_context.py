from __future__ import annotations

import json
from typing import Any

import requests
from pyproj import Transformer
from shapely.ops import transform

from ..geometry import site_geometry
from ..store import EvidenceStore
from ..util import stable_id

PARSER_VERSION = "arcgis-property-context/1.2.0"


def fingerprint(store: EvidenceStore, target_id: str, config: dict[str, Any]) -> str:
    geometry = site_geometry(store, target_id)
    snapshot = geometry.snapshot() if geometry else None
    return stable_id("input", PARSER_VERSION, config, snapshot)


def _query_bounds(site: Any, radius_m: float) -> tuple[float, float, float, float]:
    projected = site.projected.buffer(radius_m) if radius_m else site.projected
    inverse = Transformer.from_crs(site.crs, "EPSG:4326", always_xy=True)
    geographic = transform(inverse.transform, projected)
    return tuple(float(value) for value in geographic.bounds)


def _query(session: requests.Session, layer: dict[str, Any], bounds: tuple[float, ...],
           timeout: float) -> dict[str, Any]:
    xmin, ymin, xmax, ymax = bounds
    params = {
        "f": "json",
        "where": layer.get("where", "1=1"),
        "geometry": json.dumps({
            "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
            "spatialReference": {"wkid": 4326},
        }, separators=(",", ":")),
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join(layer.get("out_fields") or ["*"]),
        "returnGeometry": "false",
        "resultRecordCount": int(layer.get("maximum_records", 1000)),
    }
    response = session.get(layer["url"].rstrip("/") + "/query", params=params,
                           timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"ArcGIS query failed for {layer['key']}: {payload['error']}")
    return payload


def _date_value(value: Any) -> str | None:
    if isinstance(value, str):
        candidate = value.strip()[:10]
        if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
            try:
                from datetime import date
                return date.fromisoformat(candidate).isoformat()
            except ValueError:
                return None
        return None
    if not isinstance(value, (int, float)):
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()


def _first_date(record: dict[str, Any], fields: list[str]) -> str | None:
    return next((_date_value(record.get(field)) for field in fields
                 if _date_value(record.get(field))), None)


def collect(store: EvidenceStore, target_id: str, target: dict[str, Any],
            config: dict[str, Any]) -> dict[str, Any]:
    site = site_geometry(store, target_id)
    if not site:
        store.gap(target_id, "municipal_arcgis", "missing",
                  "Official parcel geometry is required for configured municipal ArcGIS overlays")
        store.db.commit()
        return {"layers": 0, "features": 0, "failed": {},
                "analysis_basis": "missing_property_geometry"}

    session = requests.Session()
    session.headers.update({"User-Agent": config.get(
        "user_agent", "DealSynq-property-intelligence/0.3 public-record-client")})
    timeout = float(config.get("timeout_seconds", 45))
    successful: list[str] = []
    failed: dict[str, str] = {}
    feature_total = 0
    entity_total = 0

    for layer in config.get("layers", []):
        key = layer["key"]
        radius_m = float(layer.get("radius_m", 0))
        try:
            payload = _query(session, layer, _query_bounds(site, radius_m), timeout)
            records = [feature.get("attributes") or {} for feature in payload.get("features", [])]
            feature_total += len(records)
            raw = store.put_raw({
                "request": {"url": layer["url"], "property": target.get("address"),
                            "radius_m": radius_m},
                "response": payload,
            })
            source_cfg = layer.get("source") or {}
            source_id = store.source(
                name=source_cfg.get("name", layer.get("label", key)),
                url=source_cfg.get("url", layer["url"]),
                authority=source_cfg.get("authority", "configured official ArcGIS source"),
                source_date=source_cfg.get("source_date"),
                retrieved_at=None, parser_version=PARSER_VERSION, raw_sha256=raw,
                access_note=source_cfg.get("access_note"),
            )
            value = {
                "feature_count": len(records),
                "analysis_basis": "parcel_envelope_intersection" if radius_m == 0 else "parcel_buffer_envelope",
                "radius_m": radius_m,
                "records": records,
                "limitation": layer.get("limitation"),
            }
            store.fact(
                subject_id=target_id, category=layer["category"],
                predicate=layer["predicate"], value=value,
                fact_class=layer.get("fact_class", "confirmed_official"),
                confidence=float(layer.get("confidence", 1.0)), source_id=source_id,
                parser_version=PARSER_VERSION, raw_sha256=raw,
                freshness_days=layer.get("freshness_days"),
                evidence_locator=f"ArcGIS spatial query: {layer['url']}/query",
            )

            entity_cfg = layer.get("entity")
            if entity_cfg:
                for index, record in enumerate(records):
                    external = record.get(entity_cfg.get("id_field")) if entity_cfg.get("id_field") else None
                    name = record.get(entity_cfg.get("name_field")) or external or f"{key} feature {index + 1}"
                    entity_id = stable_id(
                        entity_cfg.get("type", "development_case")[:4], key,
                        external or name, record,
                    )
                    store.entity(
                        entity_cfg.get("type", "development_case"), str(name),
                        external_id=str(external) if external is not None else None,
                        attributes={"source_layer": key, "record": record}, entity_id=entity_id,
                    )
                    store.relationship(
                        from_id=entity_id,
                        relationship_type=entity_cfg.get("relationship_type", "nearby_to"),
                        to_id=target_id, fact_class=layer.get("fact_class", "confirmed_official"),
                        confidence=float(layer.get("confidence", 1.0)), source_id=source_id,
                        parser_version=PARSER_VERSION, raw_sha256=raw,
                        explanation={"analysis_basis": value["analysis_basis"],
                                     "radius_m": radius_m},
                    )
                    entity_total += 1
                    event_cfg = entity_cfg.get("event")
                    if event_cfg:
                        event_date = _first_date(record, event_cfg.get("date_fields", []))
                        if event_date:
                            store.event(
                                target_id=target_id, event_type=event_cfg["type"],
                                event_date=event_date, date_precision="day",
                                subject_id=entity_id,
                                summary=str(record.get(event_cfg.get("summary_field")) or name),
                                fact_class=layer.get("fact_class", "confirmed_official"),
                                confidence=float(layer.get("confidence", 1.0)),
                                source_ids=[source_id], evidence={"record": record, "radius_m": radius_m},
                            )
                    state_cfg = entity_cfg.get("temporal_state")
                    if state_cfg:
                        value_fields = state_cfg.get("value_fields") or list(record)
                        state_value = {field: record.get(field) for field in value_fields}
                        valid_from = _first_date(record, state_cfg.get("valid_from_fields", []))
                        valid_to = _first_date(record, state_cfg.get("valid_to_fields", []))
                        store.temporal_state(
                            target_id=target_id, subject_id=entity_id,
                            state_type=state_cfg.get("type", "observation"),
                            value=state_value, source_id=source_id,
                            confidence=float(layer.get("confidence", 1.0)),
                            fact_class=layer.get("fact_class", "confirmed_official"),
                            parser_version=PARSER_VERSION, valid_from=valid_from,
                            valid_to=valid_to, first_seen=valid_from,
                        )

            for capability in layer.get("capabilities", []):
                item = ({"capability": capability, "status": "working"}
                        if isinstance(capability, str) else dict(capability))
                item.setdefault("status", "working")
                item.setdefault("source_name", source_cfg.get("name", layer.get("label", key)))
                item.setdefault("source_url", source_cfg.get("url", layer["url"]))
                item.setdefault("adapter", "arcgis_context")
                jurisdiction = store.db.execute(
                    "SELECT jurisdiction_id FROM source_capabilities WHERE target_id=? LIMIT 1",
                    (target_id,),
                ).fetchone()
                store.register_capability(
                    target_id, str(jurisdiction[0]) if jurisdiction else target_id,
                    item, PARSER_VERSION,
                )
            successful.append(key)
        except Exception as exc:
            failed[key] = f"{type(exc).__name__}: {exc}"
            store.gap(target_id, key, "blocked", f"Municipal ArcGIS layer failed: {key}",
                      reason=failed[key], source_url=layer.get("url"))

    store.db.commit()
    return {
        "layers": len(config.get("layers", [])), "successful": successful,
        "failed": failed, "features": feature_total, "entities": entity_total,
        "analysis_basis": "official_parcel_geometry",
    }
