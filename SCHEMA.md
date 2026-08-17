# Storage schema

SQLite is the system of record. `property_intel.store` creates and migrates the additive schema.

| Table | Purpose |
|---|---|
| `collection_scopes` | Municipality, county, recording-district, or state batch scope and its configuration. |
| `property_index` | Searchable precomputed property/site records within a collection scope. |
| `property_entity_links` | Property-to-parcel/building/owner/deed/loan/permit/hazard links used to materialize one connected subgraph. |
| `scope_engine_runs` | Incremental batch fingerprints, run status, timing, metrics, and errors. |
| `scope_engine_states` | Per-scope coverage contract for assessor, parcel/GIS, zoning, owner/entity, deed, mortgage/lien, site, permit/planning, hazard, and infrastructure engines. |
| `targets` | Property identity and materialized run configuration. |
| `entities` | Sites, parcels, buildings/footprints, organizations, tenants/spaces, documents, permits, transactions, listings, and portfolio candidates. |
| `entity_aliases` | Raw and normalized APNs, addresses, and entity names with sources/confidence. |
| `relationships` | Sourced typed graph edges. |
| `facts` | Typed observations/calculations with fact class, confidence, dates, freshness, provenance, locator, parser, and current/superseded status. |
| `sources` | Authority, direct URL, source/retrieval dates, access note, parser, and raw hash. |
| `raw_evidence` | Immutable content-addressed bytes; SHA-256 is the primary key. |
| `documents` | First-class document metadata, raw/text hashes, type, page count, dates, and extraction attributes. |
| `document_mentions` | Page/character-located APNs, addresses, entities, dates, money, and areas. |
| `temporal_states` | Valid/observed tenant occupancy and listing-lifecycle states. |
| `events` | Normalized property timeline events linked to facts/source IDs. |
| `grouping_decisions` | Candidate inclusion, score, threshold, algorithm, and component evidence. |
| `resolved_claims` | Non-destructive preferred current fact plus competing fact IDs and scoring rationale. |
| `contradictions` | Disagreeing current observations. |
| `fact_changes` | Same-authority before/after observations. |
| `source_capabilities` | Explicit configured/partial/missing/blocked coverage contract per property. |
| `fetch_cache` | HTTP request identity, response metadata, raw hash, expiry, conditional request fields, and errors. |
| `search_queries` | Prioritized property/APN/entity discovery plan and execution status. |
| `web_discoveries` | Provider-neutral search candidates linked to queries, URLs, raw result evidence, and optional document classification. |
| `pipeline_stage_states` | All 25 Rahul stages with implementation status, property coverage, metrics, evidence references, and missing requirements. |
| `property_state_snapshots` | Time-stamped, claim-linked current property state used by reports and downstream DealSynq integration. |
| `refresh_policies` | Cadence, priority, last success, and next due time. |
| `stage_runs` | Incremental fingerprints, timing, status, stats, and errors. |
| `gaps` | Missing, partial, blocked, or resolved research threads. |

The municipality database is a reusable jurisdiction cache. Property activation
copies only the selected property's linked subgraph into the existing
single-property schema, so report queries cannot leak facts from neighboring
properties. The model is observation-oriented. Differing assessor, owner,
footprint, listing, and calculated measurements remain separate facts.
Report-time preference never overwrites evidence.
