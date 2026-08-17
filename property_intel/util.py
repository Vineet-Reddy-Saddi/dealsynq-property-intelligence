from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    payload = "\x1f".join(canonical_json(p) for p in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:length]}"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    stat = p.stat()
    return {
        "path": str(p.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(p),
    }


def normalize_text(value: Any) -> str:
    text = str(value or "").upper().strip()
    text = text.replace("&", " AND ")
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


_STREET = {
    "STREET": "ST", "ROAD": "RD", "AVENUE": "AVE", "BOULEVARD": "BLVD",
    "HIGHWAY": "HWY", "DRIVE": "DR", "LANE": "LN", "COURT": "CT",
    "PLACE": "PL", "PARKWAY": "PKWY", "TERRACE": "TER",
}


def normalize_address(value: Any) -> str:
    tokens = normalize_text(value).split()
    return " ".join(_STREET.get(token, token) for token in tokens)


def parse_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def centroid(geometry: dict[str, Any] | None) -> tuple[float, float] | None:
    if not geometry:
        return None
    coords = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon" and coords:
        points = coords[0]
    elif geometry.get("type") == "MultiPolygon" and coords and coords[0]:
        points = coords[0][0]
    else:
        return None
    if not points:
        return None
    return (sum(float(p[1]) for p in points) / len(points),
            sum(float(p[0]) for p in points) / len(points))


def haversine_m(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float | None:
    if not a or not b:
        return None
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371008.8 * 2 * math.asin(math.sqrt(h))


def geometry_vertices(geometry: dict[str, Any] | None) -> list[tuple[float, float]]:
    if not geometry:
        return []
    coords = geometry.get("coordinates") or []
    rings: list[list[list[float]]] = []
    if geometry.get("type") == "Polygon":
        rings = coords[:1]
    elif geometry.get("type") == "MultiPolygon":
        rings = [polygon[0] for polygon in coords if polygon]
    return [(float(point[1]), float(point[0])) for ring in rings for point in ring]


def geometry_vertex_distance_m(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float | None:
    """Screening boundary distance using source vertices.

    Zero/shared vertices are a strong adjacency signal. Non-zero results are an
    approximation and are never represented as a surveyed/legal boundary finding.
    """
    av, bv = geometry_vertices(a), geometry_vertices(b)
    if not av or not bv:
        return None
    return min(haversine_m(x, y) or 0.0 for x in av for y in bv)


def chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
