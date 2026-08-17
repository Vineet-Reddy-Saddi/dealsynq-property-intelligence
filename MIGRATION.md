# Migration from address-first to batch-first property intelligence

## Decision

Do not delete the existing pipeline. Its evidence store, provenance, raw-byte
retention, incremental stage runner, contradiction detection, claim resolution,
validation, and report generation are the property activation layer in the new
architecture.

## Preserved components

- `EvidenceStore` and the observation-oriented graph schema
- content-addressed raw evidence and source records
- fact classes, dates, confidence, freshness, parser versions, and locators
- reusable collection adapters and canonical property bundle
- parcel/site grouping decisions and spatial calculations
- temporal states, event timeline, contradictions, and resolved claims
- 25-stage coverage evaluation, current-state snapshot, reports, and validation
- existing Five Town, Chicopee, and Rhode Island validation configurations,
  preserved in `F:\DealSynq\property-intelligence-legacy`

## New components

- collection scopes for municipality, county, recording district, or state
- jurisdiction-level property index and property-to-entity links
- ten batch engine contracts and explicit coverage states
- incremental batch run fingerprints and coverage reports
- collector-neutral municipality evidence bundle
- address, parcel-ID, and name lookup against precomputed data
- isolated property-subgraph materialization
- on-demand activation for current tenants, listings/market, web, and documents
- stage execution modes: `batch`, `on_demand`, and `materialize`

## Compatibility

The former `run`, `run-property`, `validate`, `discover-sources`,
`refresh-plan`, and `search-plan` commands were moved with the legacy runner to
`F:\DealSynq\property-intelligence-legacy`. The active CLI exposes only
`precompute`, `scope-status`, `activate-property`, and `stage-status`.

Existing property databases and reports were preserved in that sibling archive;
they were not deleted or rewritten.

## Jurisdiction migration sequence

1. Define the real source scopes and their parent relationships.
2. Connect approved bulk collectors for assessor and parcel/GIS data.
3. Add zoning, owner/entity, deed, mortgage/lien, permit/planning, hazard, and
   optional infrastructure collector outputs.
4. Reconcile publisher totals and document a `completion_basis` for each engine.
5. Build explainable parcel-to-site memberships and populate `property_index`.
6. Confirm `scope-status` has no unjustified missing or blocked required engine.
7. Activate unrelated addresses and validate graph isolation and field accuracy.
8. Switch the search frontend/API to `activate-property` for that jurisdiction.
9. Compare against the former property outputs retained in the sibling archive;
   never import their property-specific conclusions as batch coverage.

## Five Town Plaza

Five Town remains a pilot acceptance case. Its prior evidence can validate the
new activated output, but it must not be treated as Springfield-wide batch
collection or used to claim complete municipal coverage. SEC, owner websites,
and listing artifacts remain source-specific evidence discovered after entity
resolution; they are not hardcoded dependencies of the generic engine.
