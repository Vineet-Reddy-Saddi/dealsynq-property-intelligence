from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..http_client import PublicHttpClient
from ..providers import SearchRequest, build_search_provider, canonical_url
from ..store import EvidenceStore
from ..util import file_fingerprint, stable_id, utcnow

PARSER_VERSION = "tool-agnostic-web-intelligence/1.0.0"


def fingerprint(store: EvidenceStore, target_id: str, config: dict[str, Any]) -> str:
    provider_inputs = []
    for provider in config.get("providers", []):
        item = dict(provider)
        if item.get("path"):
            item["file"] = file_fingerprint(Path(item["path"]))
        provider_inputs.append(item)
    queries = [tuple(row) for row in store.rows(
        "SELECT query_id,query_text,query_type,status FROM search_queries "
        "WHERE target_id=? ORDER BY query_id", (target_id,))]
    return stable_id("input", PARSER_VERSION, provider_inputs, queries)


def collect(store: EvidenceStore, target_id: str, target: dict[str, Any],
            config: dict[str, Any]) -> dict[str, Any]:
    providers_config = [p for p in config.get("providers", []) if p.get("enabled", True)]
    if not providers_config:
        store.gap(target_id, "web_intelligence", "missing",
                  "Open-web search execution provider",
                  reason="The query plan exists, but no approved provider is configured.")
        return {"providers": 0, "queries_run": 0, "discoveries": 0}
    client = PublicHttpClient(store, timeout=int(config.get("timeout_seconds", 45)))
    providers = [build_search_provider(item, client) for item in providers_config]
    queries = store.rows(
        "SELECT * FROM search_queries WHERE target_id=? ORDER BY priority,query_type,query_text",
        (target_id,),
    )
    seen: set[tuple[str, str]] = set()
    total = 0
    executed = 0
    by_provider: dict[str, int] = {}
    for query in queries:
        request = SearchRequest(
            query_id=query["query_id"], query_text=query["query_text"],
            query_type=query["query_type"], identifiers=json.loads(query["identifiers_json"]),
            target=target,
        )
        query_count = 0
        provider_names = []
        for provider in providers:
            provider_names.append(provider.provider_id)
            results = list(provider.search(request))
            for result in results:
                normalized_url = canonical_url(result.url)
                dedupe_key = (query["query_id"], normalized_url)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                raw_payload = {
                    "query_id": query["query_id"], "provider": provider.provider_id,
                    "url": result.url, "canonical_url": normalized_url,
                    "title": result.title, "snippet": result.snippet,
                    "published_date": result.published_date,
                    "document_type": result.document_type,
                    "attributes": result.attributes,
                }
                raw = store.put_raw(raw_payload)
                source_id = store.source(
                    name=result.source_name or f"{provider.provider_id} search result",
                    url=result.url, authority=result.authority,
                    parser_version=PARSER_VERSION, raw_sha256=raw,
                    source_date=result.published_date, retrieved_at=utcnow(),
                    access_note="Discovery candidate only; assertions require document/source parsing.",
                )
                store.web_discovery(
                    target_id=target_id, query_id=query["query_id"],
                    provider=provider.provider_id, url=result.url,
                    canonical_url=normalized_url, source_id=source_id,
                    title=result.title, snippet=result.snippet,
                    published_date=result.published_date, raw_sha256=raw,
                    status="discovered", document_type=result.document_type,
                    attributes=result.attributes,
                )
                query_count += 1
                total += 1
                by_provider[provider.provider_id] = by_provider.get(provider.provider_id, 0) + 1
        store.db.execute(
            "UPDATE search_queries SET status=?,provider=?,last_run_at=?,result_count=?,updated_at=? "
            "WHERE query_id=?",
            ("completed" if provider_names else "planned", ",".join(provider_names) or None,
             utcnow(), query_count, utcnow(), query["query_id"]),
        )
        executed += 1
    if total:
        store.resolve_gap(target_id, "Open-web search execution provider",
                          reason=f"{total} approved discovery candidates stored")
    return {"providers": len(providers), "queries_run": executed,
            "discoveries": total, "by_provider": by_provider}
