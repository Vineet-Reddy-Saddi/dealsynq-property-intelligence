from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable, Protocol

from .store import EvidenceStore


@dataclass(frozen=True)
class AdapterContext:
    store: EvidenceStore
    target_id: str
    target: dict[str, Any]


class CollectionAdapter(Protocol):
    """Stable boundary between orchestration and any collection/parser tool."""

    key: str

    def fingerprint(self, context: AdapterContext, config: dict[str, Any]) -> str: ...

    def collect(self, context: AdapterContext, config: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class FunctionAdapter:
    key: str
    fingerprint_fn: Callable[[AdapterContext, dict[str, Any]], str]
    collect_fn: Callable[[AdapterContext, dict[str, Any]], dict[str, Any]]

    def fingerprint(self, context: AdapterContext, config: dict[str, Any]) -> str:
        return self.fingerprint_fn(context, config)

    def collect(self, context: AdapterContext, config: dict[str, Any]) -> dict[str, Any]:
        return self.collect_fn(context, config)


class AdapterRegistry:
    """Registry for collection tools that obey the common evidence contract.

    An adapter can use Python, public HTTP, a database export, a local file, or a
    separate service. The core is therefore independent of search, GIS, browser,
    scraping, and AI vendors.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, CollectionAdapter] = {}

    def register(self, adapter: CollectionAdapter) -> None:
        if adapter.key in self._adapters:
            raise ValueError(f"Adapter already registered: {adapter.key}")
        self._adapters[adapter.key] = adapter

    def get(self, key: str) -> CollectionAdapter:
        try:
            return self._adapters[key]
        except KeyError as exc:
            raise KeyError(f"Unknown collection adapter {key!r}; registered={sorted(self._adapters)}") from exc

    def keys(self) -> list[str]:
        return sorted(self._adapters)


def builtin_adapters() -> AdapterRegistry:
    registry = AdapterRegistry()
    module = lambda name: import_module(f"{__package__}.adapters.{name}")
    bindings = [
        FunctionAdapter("web_intelligence", lambda c, cfg: module("web_intelligence").fingerprint(c.store, c.target_id, cfg),
                        lambda c, cfg: module("web_intelligence").collect(c.store, c.target_id, c.target, cfg)),
        FunctionAdapter("documents", lambda c, cfg: module("documents").fingerprint(cfg),
                        lambda c, cfg: module("documents").collect(c.store, c.target_id, c.target, cfg)),
        FunctionAdapter("listings_csv", lambda c, cfg: module("listings_csv").fingerprint(cfg),
                        lambda c, cfg: module("listings_csv").collect(c.store, c.target_id, c.target, cfg)),
        FunctionAdapter("canonical_bundle", lambda c, cfg: module("canonical_bundle").fingerprint(cfg),
                        lambda c, cfg: module("canonical_bundle").collect(c.store, c.target_id, cfg)),
        FunctionAdapter("national_public", lambda c, cfg: module("national").fingerprint(cfg),
                        lambda c, cfg: module("national").collect(c.store, c.target_id, c.target, cfg)),
        FunctionAdapter("arcgis_context", lambda c, cfg: module("arcgis_context").fingerprint(c.store, c.target_id, cfg),
                        lambda c, cfg: module("arcgis_context").collect(c.store, c.target_id, c.target, cfg)),
        FunctionAdapter("ct_registry", lambda c, cfg: module("ct_registry").fingerprint(c.store, c.target_id, cfg),
                        lambda c, cfg: module("ct_registry").collect(c.store, c.target_id, c.target, cfg)),
    ]
    for adapter in bindings:
        registry.register(adapter)
    return registry
