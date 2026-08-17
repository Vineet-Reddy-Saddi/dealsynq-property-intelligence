from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests

from .store import EvidenceStore
from .util import canonical_json, stable_id, utcnow


@dataclass(frozen=True)
class FetchResult:
    url: str
    status_code: int
    content_type: str
    content: bytes
    raw_sha256: str
    retrieved_at: str
    from_cache: bool

    def json(self) -> Any:
        return json.loads(self.content)


class PublicHttpClient:
    """Small evidence-aware client for public endpoints.

    It performs ordinary HTTP GET/POST requests only. It does not automate login,
    challenge, CAPTCHA, robots circumvention, or paywall behavior.
    """

    def __init__(self, store: EvidenceStore, *, timeout: int = 60,
                 user_agent: str = "DealSynqPropertyIntelligence/1.0 public-record research"):
        self.store = store
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json, application/geo+json, text/html;q=0.9, */*;q=0.5"})

    def fetch(self, url: str, *, method: str = "GET", params: dict[str, Any] | None = None,
              json_body: dict[str, Any] | None = None, cache_days: int = 1) -> FetchResult:
        method = method.upper()
        request_payload = {"params": params or {}, "json": json_body or {}}
        request_id = stable_id("request", method, url, request_payload)
        cached = self.store.db.execute(
            "SELECT * FROM fetch_cache WHERE request_id=? AND error IS NULL", (request_id,)
        ).fetchone()
        now = datetime.now(timezone.utc)
        if cached and cached["raw_sha256"] and cached["expires_at"]:
            expiry = datetime.fromisoformat(cached["expires_at"].replace("Z", "+00:00"))
            if expiry > now:
                raw = self.store.db.execute(
                    "SELECT content FROM raw_evidence WHERE raw_sha256=?", (cached["raw_sha256"],)
                ).fetchone()
                if raw:
                    return FetchResult(cached["url"], cached["status_code"],
                                       cached["content_type"] or "application/octet-stream",
                                       bytes(raw[0]), cached["raw_sha256"], cached["retrieved_at"], True)
        headers: dict[str, str] = {}
        if cached and cached["etag"]:
            headers["If-None-Match"] = cached["etag"]
        if cached and cached["last_modified"]:
            headers["If-Modified-Since"] = cached["last_modified"]
        try:
            response = self.session.request(method, url, params=params, json=json_body,
                                            headers=headers, timeout=self.timeout)
            retrieved = utcnow()
            expires = (now + timedelta(days=max(0, cache_days))).isoformat()
            if response.status_code == 304 and cached and cached["raw_sha256"]:
                raw = self.store.db.execute(
                    "SELECT content FROM raw_evidence WHERE raw_sha256=?", (cached["raw_sha256"],)
                ).fetchone()
                if raw:
                    self.store.db.execute(
                        "UPDATE fetch_cache SET retrieved_at=?,expires_at=? WHERE request_id=?",
                        (retrieved, expires, request_id))
                    return FetchResult(cached["url"], cached["status_code"],
                                       cached["content_type"] or "application/octet-stream",
                                       bytes(raw[0]), cached["raw_sha256"], retrieved, True)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "application/octet-stream").split(";")[0]
            raw_sha = self.store.put_raw(response.content, content_type)
            final_url = response.url if method == "GET" else url
            self.store.db.execute(
                "INSERT OR REPLACE INTO fetch_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, method, final_url, canonical_json(request_payload), response.status_code,
                 raw_sha, content_type, response.headers.get("ETag"), response.headers.get("Last-Modified"),
                 retrieved, expires, None),
            )
            return FetchResult(final_url, response.status_code, content_type, response.content,
                               raw_sha, retrieved, False)
        except Exception as exc:
            self.store.db.execute(
                "INSERT OR REPLACE INTO fetch_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, method, url + (("?" + urlencode(params)) if params and method == "GET" else ""),
                 canonical_json(request_payload), 0, None, None, None, None, utcnow(), None, str(exc)),
            )
            raise

