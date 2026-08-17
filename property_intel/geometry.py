from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union

from .store import EvidenceStore

SQM_TO_SQFT = 10.76391041671
SQM_TO_ACRES = 0.000247105381467
M_TO_FT = 3.280839895


@dataclass(frozen=True)
class SiteGeometry:
    geometry: Any
    projected: Any
    crs: CRS

    @property
    def centroid(self) -> tuple[float, float]:
        point = self.geometry.centroid
        return point.x, point.y

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.geometry.bounds

    def snapshot(self) -> dict[str, Any]:
        lon, lat = self.centroid
        return {
            "geometry": mapping(self.geometry),
            "centroid": {"lat": lat, "lon": lon},
            "bbox": list(self.bounds),
            "area_sqft": round(self.projected.area * SQM_TO_SQFT),
            "area_acres": round(self.projected.area * SQM_TO_ACRES, 4),
            "perimeter_ft": round(self.projected.length * M_TO_FT, 1),
            "analysis_crs": self.crs.to_string(),
        }


def _projector(geometry: Any) -> tuple[CRS, Transformer]:
    point = geometry.centroid
    zone = max(1, min(60, int(math.floor((point.x + 180) / 6) + 1)))
    epsg = (32600 if point.y >= 0 else 32700) + zone
    crs = CRS.from_epsg(epsg)
    return crs, Transformer.from_crs("EPSG:4326", crs, always_xy=True)


def from_geometries(values: Iterable[dict[str, Any]]) -> SiteGeometry | None:
    geometries = []
    for value in values:
        try:
            geom = shape(value)
            if not geom.is_empty:
                geometries.append(geom if geom.is_valid else geom.buffer(0))
        except (TypeError, ValueError):
            continue
    if not geometries:
        return None
    union = unary_union(geometries)
    crs, transformer = _projector(union)
    return SiteGeometry(union, transform(transformer.transform, union), crs)


def site_geometry(store: EvidenceStore, target_id: str) -> SiteGeometry | None:
    rows = store.rows(
        "SELECT e.entity_id,e.attributes_json FROM entities e JOIN grouping_decisions g ON g.parcel_id=e.entity_id "
        "WHERE g.target_id=? AND g.included=1 AND e.entity_type='parcel'", (target_id,)
    )
    values = []
    for row in rows:
        attrs = json.loads(row["attributes_json"])
        if isinstance(attrs.get("geometry"), dict):
            values.append(attrs["geometry"])
            continue
        # Collector-neutral municipality adapters may store geometry as a
        # sourced fact rather than mutating entity attributes. Both are valid
        # evidence representations and must drive the same spatial engines.
        fact = store.db.execute(
            "SELECT value_json FROM facts WHERE subject_id=? AND predicate='parcel_geometry' "
            "AND status='current' ORDER BY observed_at DESC LIMIT 1",
            (row["entity_id"],),
        ).fetchone()
        if fact:
            value = json.loads(fact["value_json"])
            if isinstance(value, dict):
                values.append(value)
    if not values:
        fact = store.db.execute(
            "SELECT value_json FROM facts WHERE subject_id=? AND predicate='analysis_geometry' "
            "AND status='current' ORDER BY observed_at DESC LIMIT 1",
            (target_id,),
        ).fetchone()
        if fact:
            value = json.loads(fact["value_json"])
            if isinstance(value, dict):
                if value.get("type") == "GeometryCollection":
                    values.extend(value.get("geometries") or [])
                else:
                    values.append(value)
    return from_geometries(values)


def buffered_point(lon: float, lat: float, radius_m: float = 25.0) -> SiteGeometry:
    point = shape({"type": "Point", "coordinates": [lon, lat]})
    crs, forward = _projector(point)
    inverse = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    projected = transform(forward.transform, point).buffer(radius_m)
    geographic = transform(inverse.transform, projected)
    return SiteGeometry(geographic, projected, crs)


def project_to_site(site: SiteGeometry, geometry: Any) -> Any:
    transformer = Transformer.from_crs("EPSG:4326", site.crs, always_xy=True)
    return transform(transformer.transform, geometry)


def overlay_summary(site: SiteGeometry, features: Iterable[dict[str, Any]],
                    group_fields: Iterable[str]) -> dict[str, Any]:
    by_group: dict[str, float] = {}
    feature_count = 0
    for feature in features:
        geometry = feature.get("geometry")
        if not geometry:
            continue
        try:
            projected = project_to_site(site, shape(geometry))
            intersection = site.projected.intersection(projected)
        except (TypeError, ValueError):
            continue
        if intersection.is_empty or intersection.area <= 0:
            continue
        feature_count += 1
        props = feature.get("properties") or {}
        label = next((str(props.get(field)) for field in group_fields
                      if props.get(field) not in (None, "")), "unclassified")
        by_group[label] = by_group.get(label, 0.0) + intersection.area
    area = site.projected.area or 1.0
    return {
        "feature_count": feature_count,
        "intersection_sqft": round(sum(by_group.values()) * SQM_TO_SQFT),
        "intersection_acres": round(sum(by_group.values()) * SQM_TO_ACRES, 4),
        "site_percent": round(sum(by_group.values()) / area * 100, 2),
        "classes": [
            {"class": key, "intersection_sqft": round(value * SQM_TO_SQFT),
             "intersection_acres": round(value * SQM_TO_ACRES, 4),
             "site_percent": round(value / area * 100, 2)}
            for key, value in sorted(by_group.items())
        ],
    }
