# DealSynq property intelligence

This is the active batch-first pipeline. It precomputes structured evidence at
the source's real publication scope, then activates one isolated property graph
when a user searches by address, parcel ID, or property name.

The former address-first runner, property-specific pilot artifacts, and
cross-jurisdiction validation remnants are stored separately at
`F:\DealSynq\property-intelligence-legacy`.

## Runtime model

### Batch precomputation

The following engines run for a municipality, county, recording district, or
state before user search:

- assessor
- parcel/GIS
- zoning
- owners/entities
- deeds/land records
- mortgages/liens
- property/site assembly
- permits/planning
- environmental/hazards
- optional infrastructure

Each engine records `complete`, `partial`, `blocked`, `missing`, or
`not_applicable`. A successful parser defaults to `partial`; `complete` requires
a documented `completion_basis`.

### Property activation

After lookup, the system copies only the selected property's linked parcels,
buildings, owners, documents, deeds, loans, permits, hazards, facts, events, and
provenance into an isolated activation database. It then runs the configured
current web, document, tenant, listing, and market adapters before producing
the timeline, resolved claims, current state, and report.

## Install and run

```powershell
cd F:\DealSynq\property-intelligence
python -m pip install -e .

# Populate or refresh a jurisdiction cache.
python -m property_intel precompute configs\municipality.example.json

# Inspect honest batch coverage.
python -m property_intel scope-status configs\municipality.example.json

# Activate one property from the cache.
python -m property_intel activate-property configs\municipality.example.json `
  --address "10 Main Street, Example, MA 01000"

# Display the 25 semantic stage contracts and execution modes.
python -m property_intel stage-status

# Reproducibly select and activate a geometry-backed property from a scope.
python -m property_intel validate-sample configs\cumberland_ri_validation.json `
  --seed 20260815

# Run validation tests.
python -m unittest discover -s tests -v
```

`--skip-live` omits network-backed activation adapters. `--force` reruns stages
whose fingerprints have not changed.

## Configuration contracts

- `configs/municipality.example.json` defines the collection scope, batch
  engines, coverage assertions, database, and on-demand adapters.
- `configs/municipality_bundle.example.json` demonstrates the neutral batch
  evidence interchange format.
- `configs/canonical_bundle.example.json` demonstrates the property-level
  on-demand evidence interchange format.
- `configs/stamford_ct_pilot.json` is the first real municipality-scale pilot.
  It consumes the verified Connecticut delivery read-only, materializes
  qualified assessor transfer observations, and activates official City zoning,
  planning, environmental, footprint, transit, parking, commercial-lease, and
  commercial-inventory GIS alongside generic national public screens. It still
  reports incomplete source domains explicitly.
- `configs/cumberland_ri_validation.json` is the non-Stamford portability
  validation. It consumes the verified Rhode Island delivery read-only and
  activates generic national spatial collectors against a reproducibly chosen
  property. See `pilots/cumberland_ri/CUMBERLAND_VALIDATION_AUDIT.md` for the
  measured results and source limitations.

A collector may use a lawful public API, bulk download, GIS service, database
export, browser-assisted workflow, or vendor integration. It must emit the
neutral evidence contract with sources and provenance. The orchestrator contains
no Five Town Plaza, Springfield, SEC, source-vendor, or final-parcel hardcoding.

The generic `tabular_municipality` adapter maps a jurisdiction-wide CSV through
configuration. See `pilots/stamford_ct/STAMFORD_PILOT_AUDIT.md` for scale,
validation results, performance, and limitations.

The generic `arcgis_context` adapter performs parcel or buffered spatial
queries against configured public ArcGIS layers. Configuration controls the
fact predicate, fields, limitations, optional entities/events/temporal states,
and source-capability evidence; no Stamford address or layer is hardcoded in
the adapter.

The generic `arcgis_municipality` adapter downloads configured public feature
layers once, joins official parcel identifiers locally, and materializes an
explicit intersection or proximity result for every indexed property. The
generic `ct_registry_municipality` adapter batches exact organization-name and
business-ID queries against Connecticut's nightly public business and active
UCC extracts. Zero-result screens remain recorded results; neither adapter
promotes a screen into a legal, title, environmental, or zoning determination.

## Evidence policy

Every fact has one class: `confirmed_official`, `reported`, `calculation`,
`inference`, or `prediction`. Each observation retains source URL, source and
retrieval dates, raw hash, parser version, locator, confidence, and freshness.
Alternatives and contradictions remain stored; claim resolution does not delete
competing evidence.

Predicates also preserve measurement meaning. A publisher-supplied parcel area
remains `parcel_geometry_union_area` in its stated unit; an equal-area geometry
calculation is `projected_parcel_geometry_union_area`. Equivalent values in
acres and square feet are therefore not reported as conflicting observations.

No adapter bypasses logins, CAPTCHAs, paywalls, or prohibited access controls.
Listing platforms requiring restricted access are not collection sources.

## Documentation

- `ARCHITECTURE.md` — two-layer execution and evidence design
- `SCHEMA.md` — SQLite storage model
- `MIGRATION.md` — what was retained, what was archived, and rollout sequence
