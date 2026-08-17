from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import urlsplit, urlunsplit

from .http_client import PublicHttpClient


@dataclass(frozen=True)
class SearchRequest:
    query_id: str
    query_text: str
    query_type: str
    identifiers: list[str]
    target: dict[str, Any]


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str | None = None
    snippet: str | None = None
    published_date: str | None = None
    document_type: str | None = None
    authority: str = "public web source"
    source_name: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


class SearchProvider(Protocol):
    """Replaceable search boundary; implementations return evidence candidates only."""

    provider_id: str

    def search(self, request: SearchRequest) -> Iterable[SearchResult]: ...


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port and not (
        (scheme == "https" and parts.port == 443) or (scheme == "http" and parts.port == 80)
    ) else ""
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, host + port, path, parts.query, ""))


class ManifestSearchProvider:
    """Deterministic provider for approved exports or human-reviewed seed results.

    The provider supports JSON, CSV, inline rows, and a query_type filter. It is
    intentionally not tied to a search vendor and is useful for tests, exports,
    and providers whose results must be reviewed before ingestion.
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.provider_id = config.get("id", "manifest")
        self._rows = self._load_rows(config)

    @staticmethod
    def _load_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
        rows = [dict(row) for row in config.get("results", [])]
        path_value = config.get("path")
        if not path_value:
            return rows
        path = Path(path_value)
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows.extend(dict(row) for row in csv.DictReader(handle))
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value = value.get("results", [])
            rows.extend(dict(row) for row in value)
        return rows

    def search(self, request: SearchRequest) -> Iterable[SearchResult]:
        for row in self._rows:
            allowed_type = row.get("query_type")
            query_id = row.get("query_id")
            if allowed_type and allowed_type != request.query_type:
                continue
            if query_id and query_id != request.query_id:
                continue
            url = row.get("url") or row.get("source_url")
            if not url:
                continue
            yield SearchResult(
                url=url,
                title=row.get("title"),
                snippet=row.get("snippet") or row.get("description"),
                published_date=row.get("published_date") or row.get("source_date"),
                document_type=row.get("document_type"),
                authority=row.get("authority", self.config.get("authority", "approved manifest result")),
                source_name=row.get("source_name"),
                attributes={k: v for k, v in row.items() if k not in {
                    "url", "source_url", "title", "snippet", "description",
                    "published_date", "source_date", "document_type", "authority",
                    "source_name", "query_type", "query_id",
                }},
            )


class HttpJsonSearchProvider:
    """Vendor-neutral JSON search adapter configured entirely through field maps.

    No provider credentials or endpoint assumptions are embedded in DealSynq.
    Authentication headers can be supplied by the caller's runtime configuration;
    this class never circumvents login, CAPTCHA, paywall, or access controls.
    """

    def __init__(self, config: dict[str, Any], client: PublicHttpClient):
        self.config = config
        self.client = client
        self.provider_id = config.get("id", "http_json")

    @staticmethod
    def _path(value: Any, dotted: str) -> Any:
        current = value
        for part in dotted.split(".") if dotted else []:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current

    def search(self, request: SearchRequest) -> Iterable[SearchResult]:
        params = dict(self.config.get("params", {}))
        params[self.config.get("query_parameter", "q")] = request.query_text
        response = self.client.fetch(
            self.config["endpoint"], params=params,
            cache_days=int(self.config.get("cache_days", 7)),
        )
        payload = response.json()
        rows = self._path(payload, self.config.get("results_path", "results")) or []
        fields = self.config.get("fields", {})
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = self._path(row, fields.get("url", "url"))
            if not url:
                continue
            yield SearchResult(
                url=str(url),
                title=self._path(row, fields.get("title", "title")),
                snippet=self._path(row, fields.get("snippet", "snippet")),
                published_date=self._path(row, fields.get("published_date", "published_date")),
                document_type=self._path(row, fields.get("document_type", "document_type")),
                authority=self.config.get("authority", "configured search provider"),
                source_name=self.config.get("source_name", self.provider_id),
                attributes={"provider_result": row},
            )


def build_search_provider(config: dict[str, Any], client: PublicHttpClient) -> SearchProvider:
    provider_type = config.get("type", "manifest")
    if provider_type == "manifest":
        return ManifestSearchProvider(config)
    if provider_type == "http_json":
        return HttpJsonSearchProvider(config, client)
    raise ValueError(f"Unsupported search provider type: {provider_type}")
