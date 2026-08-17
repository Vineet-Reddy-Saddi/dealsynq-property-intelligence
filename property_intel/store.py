from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .util import canonical_json, sha256_bytes, stable_id, utcnow

FACT_CLASSES = {"confirmed_official", "reported", "calculation", "inference", "prediction"}

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS targets (
  target_id TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL,
  config_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
  entity_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  external_id TEXT, attributes_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE TABLE IF NOT EXISTS raw_evidence (
  raw_sha256 TEXT PRIMARY KEY, media_type TEXT NOT NULL, byte_length INTEGER NOT NULL,
  content BLOB NOT NULL, stored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY, source_name TEXT NOT NULL, source_url TEXT,
  authority TEXT NOT NULL, source_date TEXT, retrieved_at TEXT NOT NULL,
  raw_sha256 TEXT, parser_version TEXT NOT NULL, access_note TEXT,
  FOREIGN KEY(raw_sha256) REFERENCES raw_evidence(raw_sha256)
);
CREATE TABLE IF NOT EXISTS facts (
  fact_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, category TEXT NOT NULL,
  predicate TEXT NOT NULL, value_json TEXT NOT NULL, unit TEXT,
  fact_class TEXT NOT NULL CHECK(fact_class IN ('confirmed_official','reported','calculation','inference','prediction')),
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  freshness_days INTEGER, observed_at TEXT NOT NULL, effective_date TEXT,
  source_id TEXT NOT NULL, raw_sha256 TEXT, evidence_locator TEXT,
  parser_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'current',
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(raw_sha256) REFERENCES raw_evidence(raw_sha256)
);
CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject_id, category, predicate);
CREATE TABLE IF NOT EXISTS relationships (
  relationship_id TEXT PRIMARY KEY, from_entity_id TEXT NOT NULL, relationship_type TEXT NOT NULL,
  to_entity_id TEXT NOT NULL, fact_class TEXT NOT NULL, confidence REAL NOT NULL,
  source_id TEXT NOT NULL, raw_sha256 TEXT, effective_date TEXT,
  explanation_json TEXT NOT NULL DEFAULT '{}', parser_version TEXT NOT NULL,
  FOREIGN KEY(source_id) REFERENCES sources(source_id)
);
CREATE INDEX IF NOT EXISTS idx_relationships_from ON relationships(from_entity_id,relationship_type);
CREATE INDEX IF NOT EXISTS idx_relationships_to ON relationships(to_entity_id,relationship_type);
CREATE TABLE IF NOT EXISTS grouping_decisions (
  decision_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, parcel_id TEXT NOT NULL,
  included INTEGER NOT NULL, score REAL NOT NULL, threshold REAL NOT NULL,
  evidence_json TEXT NOT NULL, algorithm_version TEXT NOT NULL, decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grouping_target_parcel ON grouping_decisions(target_id,parcel_id);
CREATE TABLE IF NOT EXISTS contradictions (
  contradiction_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, predicate TEXT NOT NULL,
  fact_ids_json TEXT NOT NULL, severity TEXT NOT NULL, explanation TEXT NOT NULL,
  detected_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS stage_runs (
  run_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, stage_key TEXT NOT NULL,
  input_hash TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT, stats_json TEXT NOT NULL DEFAULT '{}', error TEXT
);
CREATE INDEX IF NOT EXISTS idx_stage_latest ON stage_runs(target_id, stage_key, status, finished_at);
CREATE TABLE IF NOT EXISTS gaps (
  gap_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, category TEXT NOT NULL,
  status TEXT NOT NULL, description TEXT NOT NULL, reason TEXT,
  source_url TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_capabilities (
  capability_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, jurisdiction_id TEXT NOT NULL,
  capability TEXT NOT NULL, status TEXT NOT NULL, source_name TEXT, source_url TEXT,
  adapter TEXT, reason TEXT, registry_version TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_aliases (
  alias_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, alias_type TEXT NOT NULL,
  raw_value TEXT NOT NULL, normalized_value TEXT NOT NULL, source_id TEXT,
  confidence REAL NOT NULL, observed_at TEXT NOT NULL,
  FOREIGN KEY(entity_id) REFERENCES entities(entity_id), FOREIGN KEY(source_id) REFERENCES sources(source_id)
);
CREATE INDEX IF NOT EXISTS idx_alias_normalized ON entity_aliases(alias_type,normalized_value);
CREATE TABLE IF NOT EXISTS fact_changes (
  change_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, predicate TEXT NOT NULL,
  old_fact_id TEXT NOT NULL, new_fact_id TEXT NOT NULL, source_name TEXT NOT NULL,
  old_value_json TEXT NOT NULL, new_value_json TEXT NOT NULL, detected_at TEXT NOT NULL,
  change_type TEXT NOT NULL DEFAULT 'unclassified', old_parser_version TEXT,
  new_parser_version TEXT
);
CREATE TABLE IF NOT EXISTS documents (
  document_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, title TEXT NOT NULL,
  document_type TEXT NOT NULL, source_id TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
  content_type TEXT NOT NULL, text_sha256 TEXT, page_count INTEGER,
  published_date TEXT, retrieved_at TEXT NOT NULL, parser_version TEXT NOT NULL,
  attributes_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(raw_sha256) REFERENCES raw_evidence(raw_sha256)
);
CREATE INDEX IF NOT EXISTS idx_documents_target ON documents(target_id,document_type);
CREATE TABLE IF NOT EXISTS document_mentions (
  mention_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, mention_type TEXT NOT NULL,
  raw_value TEXT NOT NULL, normalized_value TEXT NOT NULL, page_number INTEGER,
  character_start INTEGER, character_end INTEGER, context TEXT,
  confidence REAL NOT NULL, parser_version TEXT NOT NULL,
  FOREIGN KEY(document_id) REFERENCES documents(document_id)
);
CREATE INDEX IF NOT EXISTS idx_mentions_value ON document_mentions(mention_type,normalized_value);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, event_type TEXT NOT NULL,
  event_date TEXT, date_precision TEXT NOT NULL, subject_id TEXT NOT NULL,
  summary TEXT NOT NULL, fact_class TEXT NOT NULL, confidence REAL NOT NULL,
  source_ids_json TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'current', generated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_target_date ON events(target_id,event_date,event_type);
CREATE TABLE IF NOT EXISTS resolved_claims (
  claim_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, subject_id TEXT NOT NULL,
  predicate TEXT NOT NULL, preferred_fact_id TEXT, resolution_status TEXT NOT NULL,
  score REAL, competing_fact_ids_json TEXT NOT NULL, rationale_json TEXT NOT NULL,
  resolved_at TEXT NOT NULL,
  FOREIGN KEY(preferred_fact_id) REFERENCES facts(fact_id)
);
CREATE INDEX IF NOT EXISTS idx_claims_target ON resolved_claims(target_id,subject_id,predicate);
CREATE TABLE IF NOT EXISTS fetch_cache (
  request_id TEXT PRIMARY KEY, method TEXT NOT NULL, url TEXT NOT NULL,
  request_json TEXT NOT NULL, status_code INTEGER NOT NULL, raw_sha256 TEXT,
  content_type TEXT, etag TEXT, last_modified TEXT, retrieved_at TEXT NOT NULL,
  expires_at TEXT, error TEXT,
  FOREIGN KEY(raw_sha256) REFERENCES raw_evidence(raw_sha256)
);
CREATE TABLE IF NOT EXISTS refresh_policies (
  policy_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, stage_key TEXT NOT NULL,
  cadence_days INTEGER NOT NULL, priority TEXT NOT NULL, enabled INTEGER NOT NULL,
  rationale TEXT, last_success_at TEXT, next_due_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_queries (
  query_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, query_text TEXT NOT NULL,
  query_type TEXT NOT NULL, identifiers_json TEXT NOT NULL, priority TEXT NOT NULL,
  status TEXT NOT NULL, provider TEXT, last_run_at TEXT, result_count INTEGER,
  rationale TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_queries_target ON search_queries(target_id,status,priority);
CREATE TABLE IF NOT EXISTS temporal_states (
  state_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, subject_id TEXT NOT NULL,
  state_type TEXT NOT NULL, state_value_json TEXT NOT NULL,
  valid_from TEXT, valid_to TEXT, first_seen TEXT, last_seen TEXT,
  source_id TEXT NOT NULL, confidence REAL NOT NULL, fact_class TEXT NOT NULL,
  parser_version TEXT NOT NULL, observed_at TEXT NOT NULL,
  FOREIGN KEY(source_id) REFERENCES sources(source_id)
);
CREATE INDEX IF NOT EXISTS idx_temporal_subject ON temporal_states(target_id,subject_id,state_type,valid_from);
CREATE TABLE IF NOT EXISTS web_discoveries (
  discovery_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, query_id TEXT,
  provider TEXT NOT NULL, url TEXT NOT NULL, canonical_url TEXT NOT NULL,
  title TEXT, snippet TEXT, published_date TEXT, retrieved_at TEXT NOT NULL,
  source_id TEXT NOT NULL, raw_sha256 TEXT, status TEXT NOT NULL,
  document_type TEXT, attributes_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(raw_sha256) REFERENCES raw_evidence(raw_sha256)
);
CREATE INDEX IF NOT EXISTS idx_web_discoveries_target ON web_discoveries(target_id,status,provider);
CREATE TABLE IF NOT EXISTS pipeline_stage_states (
  stage_state_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, stage_key TEXT NOT NULL,
  stage_order INTEGER NOT NULL, label TEXT NOT NULL,
  implementation_status TEXT NOT NULL, coverage_status TEXT NOT NULL,
  dependency_keys_json TEXT NOT NULL, metrics_json TEXT NOT NULL,
  evidence_refs_json TEXT NOT NULL, missing_requirements_json TEXT NOT NULL,
  output_hash TEXT NOT NULL, contract_version TEXT NOT NULL, evaluated_at TEXT NOT NULL,
  UNIQUE(target_id,stage_key)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_stages_target ON pipeline_stage_states(target_id,stage_order);
CREATE TABLE IF NOT EXISTS property_state_snapshots (
  snapshot_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, as_of TEXT NOT NULL,
  state_hash TEXT NOT NULL, state_json TEXT NOT NULL,
  source_claim_ids_json TEXT NOT NULL, contradiction_ids_json TEXT NOT NULL,
  stage_coverage_json TEXT NOT NULL, resolver_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_property_states_target ON property_state_snapshots(target_id,as_of);
CREATE TABLE IF NOT EXISTS collection_scopes (
  scope_id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, jurisdiction_id TEXT NOT NULL,
  name TEXT NOT NULL, state_code TEXT, parent_scope_id TEXT,
  config_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS property_index (
  property_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, canonical_name TEXT NOT NULL,
  address TEXT NOT NULL, normalized_address TEXT NOT NULL, external_id TEXT,
  status TEXT NOT NULL, attributes_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(scope_id) REFERENCES collection_scopes(scope_id)
);
CREATE INDEX IF NOT EXISTS idx_property_index_scope ON property_index(scope_id,status);
CREATE INDEX IF NOT EXISTS idx_property_index_address ON property_index(scope_id,normalized_address);
CREATE TABLE IF NOT EXISTS property_entity_links (
  property_id TEXT NOT NULL, entity_id TEXT NOT NULL, role TEXT NOT NULL,
  confidence REAL NOT NULL, source_id TEXT, evidence_json TEXT NOT NULL DEFAULT '{}',
  linked_at TEXT NOT NULL, PRIMARY KEY(property_id,entity_id,role),
  FOREIGN KEY(property_id) REFERENCES property_index(property_id),
  FOREIGN KEY(entity_id) REFERENCES entities(entity_id),
  FOREIGN KEY(source_id) REFERENCES sources(source_id)
);
CREATE INDEX IF NOT EXISTS idx_property_entity_entity ON property_entity_links(entity_id,property_id);
CREATE TABLE IF NOT EXISTS scope_engine_runs (
  run_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, engine_key TEXT NOT NULL,
  input_hash TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT, stats_json TEXT NOT NULL DEFAULT '{}', error TEXT,
  FOREIGN KEY(scope_id) REFERENCES collection_scopes(scope_id)
);
CREATE INDEX IF NOT EXISTS idx_scope_runs_latest ON scope_engine_runs(scope_id,engine_key,status,finished_at);
CREATE TABLE IF NOT EXISTS scope_engine_states (
  state_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, engine_key TEXT NOT NULL,
  execution_mode TEXT NOT NULL, coverage_status TEXT NOT NULL,
  required INTEGER NOT NULL, adapter TEXT, dependencies_json TEXT NOT NULL,
  metrics_json TEXT NOT NULL, reason TEXT, updated_at TEXT NOT NULL,
  UNIQUE(scope_id,engine_key),
  FOREIGN KEY(scope_id) REFERENCES collection_scopes(scope_id)
);
CREATE INDEX IF NOT EXISTS idx_scope_engine_states ON scope_engine_states(scope_id,coverage_status);
"""


class EvidenceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def _migrate(self) -> None:
        """Small additive migrations for evidence databases created by earlier builds."""
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(fact_changes)")}
        for name, definition in [
            ("change_type", "TEXT NOT NULL DEFAULT 'unclassified'"),
            ("old_parser_version", "TEXT"), ("new_parser_version", "TEXT"),
        ]:
            if name not in columns:
                self.db.execute(f"ALTER TABLE fact_changes ADD COLUMN {name} {definition}")
        self.db.execute("UPDATE fact_changes SET change_type='legacy_unclassified' WHERE change_type='unclassified'")
        for column in ("valid_from", "valid_to", "first_seen", "last_seen"):
            self.db.execute(f"UPDATE temporal_states SET {column}=NULL WHERE TRIM(COALESCE({column},''))='' ")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def upsert_target(self, target_id: str, name: str, address: str, config: dict[str, Any]) -> None:
        now = utcnow()
        self.db.execute(
            "INSERT INTO targets VALUES(?,?,?,?,?,?) ON CONFLICT(target_id) DO UPDATE SET "
            "name=excluded.name,address=excluded.address,config_json=excluded.config_json,updated_at=excluded.updated_at",
            (target_id, name, address, canonical_json(config), now, now),
        )

    def upsert_collection_scope(self, scope_id: str, scope_type: str,
                                jurisdiction_id: str, name: str,
                                config: dict[str, Any], *, state_code: str | None = None,
                                parent_scope_id: str | None = None) -> None:
        now = utcnow()
        self.db.execute(
            "INSERT INTO collection_scopes VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope_id) DO UPDATE SET scope_type=excluded.scope_type,"
            "jurisdiction_id=excluded.jurisdiction_id,name=excluded.name,"
            "state_code=excluded.state_code,parent_scope_id=excluded.parent_scope_id,"
            "config_json=excluded.config_json,updated_at=excluded.updated_at",
            (scope_id, scope_type, jurisdiction_id, name, state_code,
             parent_scope_id, canonical_json(config), now, now),
        )

    def upsert_indexed_property(self, *, property_id: str, scope_id: str,
                                name: str, address: str, normalized_address: str,
                                external_id: str | None = None,
                                status: str = "precomputed",
                                attributes: dict[str, Any] | None = None) -> None:
        now = utcnow()
        self.db.execute(
            "INSERT INTO property_index VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(property_id) DO UPDATE SET scope_id=excluded.scope_id,"
            "canonical_name=excluded.canonical_name,address=excluded.address,"
            "normalized_address=excluded.normalized_address,external_id=excluded.external_id,"
            "status=excluded.status,attributes_json=excluded.attributes_json,"
            "updated_at=excluded.updated_at",
            (property_id, scope_id, name, address, normalized_address, external_id,
             status, canonical_json(attributes or {}), now, now),
        )

    def link_property_entity(self, *, property_id: str, entity_id: str,
                             role: str, confidence: float = 1.0,
                             source_id: str | None = None,
                             evidence: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO property_entity_links VALUES(?,?,?,?,?,?,?)",
            (property_id, entity_id, role, float(confidence), source_id,
             canonical_json(evidence or {}), utcnow()),
        )

    def scope_engine_state(self, *, scope_id: str, engine_key: str,
                           execution_mode: str, coverage_status: str,
                           required: bool, adapter: str | None,
                           dependencies: list[str], metrics: dict[str, Any] | None = None,
                           reason: str | None = None) -> None:
        state_id = stable_id("scope-state", scope_id, engine_key)
        self.db.execute(
            "INSERT OR REPLACE INTO scope_engine_states VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (state_id, scope_id, engine_key, execution_mode, coverage_status,
             int(required), adapter, canonical_json(dependencies),
             canonical_json(metrics or {}), reason, utcnow()),
        )

    def latest_scope_success_hash(self, scope_id: str, engine_key: str) -> str | None:
        row = self.db.execute(
            "SELECT input_hash FROM scope_engine_runs WHERE scope_id=? AND engine_key=? "
            "AND status='success' ORDER BY finished_at DESC LIMIT 1",
            (scope_id, engine_key),
        ).fetchone()
        return row[0] if row else None

    def begin_scope_run(self, scope_id: str, engine_key: str, input_hash: str) -> str:
        now = utcnow()
        self.db.execute(
            "UPDATE scope_engine_runs SET status='failed',finished_at=?,"
            "error='Interrupted before a later municipality run began' "
            "WHERE scope_id=? AND engine_key=? AND status='running'",
            (now, scope_id, engine_key),
        )
        run_id = stable_id("scope-run", scope_id, engine_key, input_hash, utcnow())
        self.db.execute(
            "INSERT INTO scope_engine_runs(run_id,scope_id,engine_key,input_hash,status,started_at) "
            "VALUES(?,?,?,?,?,?)",
            (run_id, scope_id, engine_key, input_hash, "running", now),
        )
        self.db.commit()
        return run_id

    def finish_scope_run(self, run_id: str, status: str,
                         stats: dict[str, Any] | None = None,
                         error: str | None = None) -> None:
        self.db.execute(
            "UPDATE scope_engine_runs SET status=?,finished_at=?,stats_json=?,error=? WHERE run_id=?",
            (status, utcnow(), canonical_json(stats or {}), error, run_id),
        )
        self.db.commit()

    def put_raw(self, content: bytes | str | dict[str, Any] | list[Any], media_type: str = "application/json") -> str:
        if isinstance(content, (dict, list)):
            payload = canonical_json(content).encode("utf-8")
        elif isinstance(content, str):
            payload = content.encode("utf-8")
        else:
            payload = content
        digest = sha256_bytes(payload)
        self.db.execute("INSERT OR IGNORE INTO raw_evidence VALUES(?,?,?,?,?)",
                        (digest, media_type, len(payload), payload, utcnow()))
        return digest

    def source(self, *, name: str, url: str | None, authority: str, parser_version: str,
               raw_sha256: str | None = None, source_date: str | None = None,
               retrieved_at: str | None = None, access_note: str | None = None) -> str:
        retrieved_at = retrieved_at or utcnow()
        source_id = stable_id("src", name, url, raw_sha256, source_date, parser_version)
        self.db.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?,?,?)",
                        (source_id, name, url, authority, source_date, retrieved_at,
                         raw_sha256, parser_version, access_note))
        return source_id

    def entity(self, entity_type: str, canonical_name: str, *, external_id: str | None = None,
               attributes: dict[str, Any] | None = None, entity_id: str | None = None) -> str:
        entity_id = entity_id or stable_id(entity_type[:4], entity_type, external_id or canonical_name)
        self.db.execute(
            "INSERT INTO entities VALUES(?,?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET "
            "canonical_name=excluded.canonical_name,external_id=excluded.external_id,"
            "attributes_json=excluded.attributes_json,updated_at=excluded.updated_at",
            (entity_id, entity_type, canonical_name, external_id, canonical_json(attributes or {}), utcnow()),
        )
        return entity_id

    def alias(self, entity_id: str, alias_type: str, raw_value: str, normalized_value: str,
              *, source_id: str | None = None, confidence: float = 1.0) -> str:
        if source_id:
            self.db.execute(
                "DELETE FROM entity_aliases WHERE entity_id=? AND alias_type=? AND raw_value=? "
                "AND normalized_value=? AND source_id IN (SELECT old.source_id FROM sources old JOIN sources new "
                "ON old.source_name=new.source_name AND old.authority=new.authority WHERE new.source_id=?)",
                (entity_id, alias_type, raw_value, normalized_value, source_id),
            )
        alias_id = stable_id("alias", entity_id, alias_type, raw_value, source_id)
        self.db.execute("INSERT OR REPLACE INTO entity_aliases VALUES(?,?,?,?,?,?,?,?)",
                        (alias_id, entity_id, alias_type, raw_value, normalized_value,
                         source_id, confidence, utcnow()))
        return alias_id

    def register_capability(self, target_id: str, jurisdiction_id: str, item: dict[str, Any],
                            registry_version: str) -> None:
        capability_id = stable_id("cap", target_id, jurisdiction_id, item["capability"])
        self.db.execute("INSERT OR REPLACE INTO source_capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (capability_id, target_id, jurisdiction_id, item["capability"], item["status"],
                         item.get("source_name"), item.get("source_url"), item.get("adapter"),
                         item.get("reason"), registry_version, utcnow()))

    def document(self, *, target_id: str, title: str, document_type: str,
                 source_id: str, raw_sha256: str, content_type: str,
                 parser_version: str, text_sha256: str | None = None,
                 page_count: int | None = None, published_date: str | None = None,
                 attributes: dict[str, Any] | None = None) -> str:
        document_id = stable_id("doc", target_id, source_id, raw_sha256, document_type)
        self.db.execute(
            "INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, target_id, title, document_type, source_id, raw_sha256,
             content_type, text_sha256, page_count, published_date, utcnow(),
             parser_version, canonical_json(attributes or {})),
        )
        return document_id

    def mention(self, *, document_id: str, mention_type: str, raw_value: str,
                normalized_value: str, parser_version: str, confidence: float,
                page_number: int | None = None, character_start: int | None = None,
                character_end: int | None = None, context: str | None = None) -> str:
        mention_id = stable_id("mention", document_id, mention_type, raw_value,
                               page_number, character_start)
        self.db.execute(
            "INSERT OR REPLACE INTO document_mentions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (mention_id, document_id, mention_type, raw_value, normalized_value,
             page_number, character_start, character_end, context, confidence, parser_version),
        )
        return mention_id

    def event(self, *, target_id: str, event_type: str, event_date: str | None,
              date_precision: str, subject_id: str, summary: str, fact_class: str,
              confidence: float, source_ids: list[str], evidence: dict[str, Any] | None = None) -> str:
        event_id = stable_id("event", target_id, event_type, event_date, subject_id, summary, source_ids)
        # Events are materialized timeline projections. Parser/source-row upgrades can
        # change source IDs without changing the underlying occurrence, so replace
        # the prior semantic event instead of accumulating duplicate timeline rows.
        self.db.execute(
            "DELETE FROM events WHERE target_id=? AND event_type=? AND event_date IS ? "
            "AND subject_id=? AND summary=? AND event_id!=?",
            (target_id, event_type, event_date, subject_id, summary, event_id),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, target_id, event_type, event_date, date_precision, subject_id,
             summary, fact_class, confidence, canonical_json(sorted(set(source_ids))),
             canonical_json(evidence or {}), "current", utcnow()),
        )
        return event_id

    def refresh_policy(self, *, target_id: str, stage_key: str, cadence_days: int,
                       priority: str, enabled: bool = True, rationale: str | None = None,
                       last_success_at: str | None = None, next_due_at: str | None = None) -> str:
        policy_id = stable_id("refresh", target_id, stage_key)
        self.db.execute(
            "INSERT OR REPLACE INTO refresh_policies VALUES(?,?,?,?,?,?,?,?,?,?)",
            (policy_id, target_id, stage_key, cadence_days, priority, int(enabled),
             rationale, last_success_at, next_due_at, utcnow()),
        )
        return policy_id

    def temporal_state(self, *, target_id: str, subject_id: str, state_type: str,
                       value: Any, source_id: str, confidence: float, fact_class: str,
                       parser_version: str, valid_from: str | None = None,
                       valid_to: str | None = None, first_seen: str | None = None,
                       last_seen: str | None = None) -> str:
        valid_from = valid_from or None
        valid_to = valid_to or None
        first_seen = first_seen or None
        last_seen = last_seen or None
        state_id = stable_id("state", target_id, subject_id, state_type, value,
                             valid_from, valid_to, source_id)
        value_json = canonical_json(value)
        self.db.execute(
            "DELETE FROM temporal_states WHERE target_id=? AND subject_id=? AND state_type=? "
            "AND state_value_json=? AND valid_from IS ? AND valid_to IS ? "
            "AND source_id IN (SELECT old.source_id FROM sources old JOIN sources new "
            "ON old.source_name=new.source_name AND old.authority=new.authority WHERE new.source_id=?)",
            (target_id, subject_id, state_type, value_json, valid_from, valid_to, source_id),
        )
        self.db.execute("INSERT OR REPLACE INTO temporal_states VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (state_id, target_id, subject_id, state_type, value_json,
                         valid_from, valid_to, first_seen, last_seen, source_id, confidence,
                         fact_class, parser_version, utcnow()))
        return state_id

    def web_discovery(self, *, target_id: str, query_id: str | None, provider: str,
                      url: str, canonical_url: str, source_id: str,
                      title: str | None = None, snippet: str | None = None,
                      published_date: str | None = None, raw_sha256: str | None = None,
                      status: str = "discovered", document_type: str | None = None,
                      attributes: dict[str, Any] | None = None) -> str:
        discovery_id = stable_id("web", target_id, provider, canonical_url, query_id)
        self.db.execute(
            "INSERT OR REPLACE INTO web_discoveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (discovery_id, target_id, query_id, provider, url, canonical_url, title,
             snippet, published_date, utcnow(), source_id, raw_sha256, status,
             document_type, canonical_json(attributes or {})),
        )
        return discovery_id

    def pipeline_stage_state(self, *, target_id: str, stage_key: str, stage_order: int,
                             label: str, implementation_status: str,
                             coverage_status: str, dependencies: list[str],
                             metrics: dict[str, Any], evidence_refs: list[str],
                             missing_requirements: list[str], contract_version: str) -> str:
        payload = {
            "stage_key": stage_key, "implementation_status": implementation_status,
            "coverage_status": coverage_status, "metrics": metrics,
            "evidence_refs": sorted(set(evidence_refs)),
            "missing_requirements": missing_requirements,
        }
        output_hash = sha256_bytes(canonical_json(payload).encode("utf-8"))
        state_id = stable_id("stage-state", target_id, stage_key)
        self.db.execute(
            "INSERT OR REPLACE INTO pipeline_stage_states VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (state_id, target_id, stage_key, stage_order, label, implementation_status,
             coverage_status, canonical_json(dependencies), canonical_json(metrics),
             canonical_json(sorted(set(evidence_refs))), canonical_json(missing_requirements),
             output_hash, contract_version, utcnow()),
        )
        return state_id

    def property_state_snapshot(self, *, target_id: str, state: dict[str, Any],
                                source_claim_ids: list[str], contradiction_ids: list[str],
                                stage_coverage: dict[str, str], resolver_version: str) -> str:
        state_json = canonical_json(state)
        state_hash = sha256_bytes(state_json.encode("utf-8"))
        snapshot_id = stable_id("property-state", target_id, state_hash)
        self.db.execute(
            "INSERT OR REPLACE INTO property_state_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
            (snapshot_id, target_id, utcnow(), state_hash, state_json,
             canonical_json(sorted(set(source_claim_ids))),
             canonical_json(sorted(set(contradiction_ids))),
             canonical_json(stage_coverage), resolver_version),
        )
        return snapshot_id

    def fact(self, *, subject_id: str, category: str, predicate: str, value: Any,
             fact_class: str, confidence: float, source_id: str, parser_version: str,
             unit: str | None = None, freshness_days: int | None = None,
             effective_date: str | None = None, observed_at: str | None = None,
             raw_sha256: str | None = None, evidence_locator: str | None = None,
             supersede_current: bool = True) -> str:
        if fact_class not in FACT_CLASSES:
            raise ValueError(f"invalid fact class: {fact_class}")
        value_json = canonical_json(value)
        fact_id = stable_id("fact", subject_id, predicate, value_json, source_id, effective_date)
        source_row = None
        prior = []
        if supersede_current:
            source_row = self.db.execute(
                "SELECT source_name,authority,parser_version FROM sources WHERE source_id=?",
                (source_id,),
            ).fetchone()
            prior = self.rows(
                "SELECT f.fact_id,f.value_json,f.parser_version,f.raw_sha256 FROM facts f JOIN sources old ON old.source_id=f.source_id "
                "WHERE f.subject_id=? AND f.predicate=? AND f.status='current' AND old.source_name=? AND old.authority=?",
                (subject_id, predicate, source_row["source_name"], source_row["authority"]),
            ) if source_row else []
        # A changed observation from the same named authority supersedes its earlier
        # current observation. The old row remains queryable for change history.
        if supersede_current:
            self.db.execute(
                "UPDATE facts SET status='superseded' WHERE subject_id=? AND predicate=? AND status='current' "
                "AND source_id IN (SELECT old.source_id FROM sources old JOIN sources new "
                "ON old.source_name=new.source_name AND old.authority=new.authority WHERE new.source_id=?)",
                (subject_id, predicate, source_id),
            )
        self.db.execute(
            "INSERT OR REPLACE INTO facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fact_id, subject_id, category, predicate, value_json, unit, fact_class,
             float(confidence), freshness_days, observed_at or utcnow(), effective_date,
             source_id, raw_sha256, evidence_locator, parser_version, "current"),
        )
        for old in prior:
            if old["value_json"] != value_json:
                change_id = stable_id("chg", old["fact_id"], fact_id)
                parser_changed = old["parser_version"] != parser_version
                raw_changed = old["raw_sha256"] != raw_sha256
                change_type = ("source_and_parser_change" if parser_changed and raw_changed else
                               "parser_change" if parser_changed else
                               "source_observation_change" if raw_changed else
                               "derived_recalculation")
                self.db.execute(
                    "INSERT OR IGNORE INTO fact_changes(change_id,subject_id,predicate,old_fact_id,new_fact_id,"
                    "source_name,old_value_json,new_value_json,detected_at,change_type,old_parser_version,new_parser_version) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (change_id, subject_id, predicate, old["fact_id"], fact_id,
                     source_row["source_name"], old["value_json"], value_json, utcnow(),
                     change_type, old["parser_version"], parser_version))
        return fact_id

    def relationship(self, *, from_id: str, relationship_type: str, to_id: str,
                     fact_class: str, confidence: float, source_id: str, parser_version: str,
                     raw_sha256: str | None = None, effective_date: str | None = None,
                     explanation: dict[str, Any] | None = None) -> str:
        rel_id = stable_id("rel", from_id, relationship_type, to_id, source_id, effective_date)
        self.db.execute(
            "DELETE FROM relationships WHERE from_entity_id=? AND relationship_type=? AND to_entity_id=? "
            "AND effective_date IS ? AND source_id IN (SELECT old.source_id FROM sources old JOIN sources new "
            "ON old.source_name=new.source_name AND old.authority=new.authority WHERE new.source_id=?)",
            (from_id, relationship_type, to_id, effective_date, source_id),
        )
        self.db.execute("INSERT OR REPLACE INTO relationships VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (rel_id, from_id, relationship_type, to_id, fact_class, confidence,
                         source_id, raw_sha256, effective_date, canonical_json(explanation or {}), parser_version))
        return rel_id

    def record_decision(self, target_id: str, parcel_id: str, included: bool, score: float,
                        threshold: float, evidence: dict[str, Any], version: str) -> None:
        # grouping_decisions is the current site-membership materialization.
        # Parser/algorithm upgrades replace the logical target-parcel decision
        # instead of leaving two simultaneously current memberships.
        self.db.execute(
            "DELETE FROM grouping_decisions WHERE target_id=? AND parcel_id=?",
            (target_id, parcel_id),
        )
        did = stable_id("grp", target_id, parcel_id, version)
        self.db.execute("INSERT OR REPLACE INTO grouping_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                        (did, target_id, parcel_id, int(included), score, threshold,
                         canonical_json(evidence), version, utcnow()))

    def gap(self, target_id: str, category: str, status: str, description: str,
            reason: str | None = None, source_url: str | None = None) -> None:
        gap_id = stable_id("gap", target_id, category, description)
        self.db.execute("INSERT OR REPLACE INTO gaps VALUES(?,?,?,?,?,?,?,?)",
                        (gap_id, target_id, category, status, description, reason, source_url, utcnow()))

    def resolve_gap(self, target_id: str, description: str, reason: str | None = None) -> None:
        self.db.execute("UPDATE gaps SET status='resolved',reason=?,updated_at=? WHERE target_id=? AND description=?",
                        (reason, utcnow(), target_id, description))

    def latest_success_hash(self, target_id: str, stage_key: str) -> str | None:
        row = self.db.execute(
            "SELECT input_hash FROM stage_runs WHERE target_id=? AND stage_key=? AND status='success' "
            "ORDER BY finished_at DESC LIMIT 1", (target_id, stage_key)).fetchone()
        return row[0] if row else None

    def begin_run(self, target_id: str, stage_key: str, input_hash: str) -> str:
        run_id = stable_id("run", target_id, stage_key, input_hash, utcnow())
        self.db.execute("INSERT INTO stage_runs(run_id,target_id,stage_key,input_hash,status,started_at) VALUES(?,?,?,?,?,?)",
                        (run_id, target_id, stage_key, input_hash, "running", utcnow()))
        self.db.commit()
        return run_id

    def finish_run(self, run_id: str, status: str, stats: dict[str, Any] | None = None,
                   error: str | None = None) -> None:
        self.db.execute("UPDATE stage_runs SET status=?,finished_at=?,stats_json=?,error=? WHERE run_id=?",
                        (status, utcnow(), canonical_json(stats or {}), error, run_id))
        self.db.commit()

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.db.execute(sql, params))

    def detect_contradictions(self) -> int:
        self.db.execute("DELETE FROM contradictions")
        groups = self.rows(
            "SELECT subject_id,predicate,COUNT(DISTINCT value_json) n FROM facts "
            "WHERE status='current' AND fact_class IN ('confirmed_official','reported','calculation') "
            "GROUP BY subject_id,predicate HAVING n>1"
        )
        count = 0
        for group in groups:
            facts = self.rows(
                "SELECT fact_id,value_json,fact_class,confidence FROM facts WHERE subject_id=? AND predicate=? AND status='current'",
                (group["subject_id"], group["predicate"]),
            )
            ids = [f["fact_id"] for f in facts]
            cid = stable_id("con", group["subject_id"], group["predicate"], ids)
            severity = "high" if all(f["fact_class"] == "confirmed_official" for f in facts) else "review"
            self.db.execute("INSERT INTO contradictions VALUES(?,?,?,?,?,?,?,?)",
                            (cid, group["subject_id"], group["predicate"], canonical_json(ids), severity,
                             "Multiple current observations disagree; preserve each value and adjudicate explicitly.",
                             utcnow(), "open"))
            count += 1
        self.db.commit()
        return count
