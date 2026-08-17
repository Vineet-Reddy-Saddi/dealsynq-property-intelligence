# Stamford, Connecticut property-intelligence readiness audit

Audit date: 2026-08-16  
Configuration: `configs/stamford_ct_pilot.json`  
Random validation seed: `20260815`  
Random validation property: `11 GARDEN STREET, Stamford, CT`

## Readiness decision

Stamford is operationally ready for municipality-wide screening and evidence-
backed property activation, but it is not a complete due-diligence dataset. The
reusable software implements all 25 semantic stages, all 28,340 indexed
properties have explicit results from 24 official municipal/state
map/reference screens, and all
33,438 assessor owners have explicit Connecticut registry and active-UCC
screens. Validated activations pass integrity checks. The municipality coverage
contract remains `complete: false` because several required public-record
domains do not have an approved complete collector.

The system must not describe missing land-record instruments, property-specific
mortgage/lien status, complete permit history, beneficial ownership, utility
capacity, legal access/frontage, or interpreted zoning capacity as known.

## Read-only source boundary

The batch build reads the verified Connecticut delivery at
`F:\DealSynq\town-source-inventory\outputs\states\Connecticut\live_refresh_2026_08_14`
without modifying it.

The Stamford Vision delivery manifest reports 37,877 enumerated assessor
records, 37,877 output records, zero failed PIDs, and successful completion on
2026-08-13. The latest official City parcel service adds 38,656 published
features. Exact prioritized City parcel/account identifiers match 37,746
assessor parcel entities and 28,320 of 28,340 indexed properties. Six of the 20
remaining properties receive bounded Census geocodes; 14 remain explicitly not
spatially evaluable. Geometry coverage is not conflated with assessor
completeness.

## Municipality cache

| Measure | Current result |
|---|---:|
| Assessor rows accepted | 37,877 |
| Indexed address-level properties | 28,340 |
| Official City parcel features | 38,656 |
| Official City parcel entities matched | 37,746 |
| Distinct assessor owners | 33,438 |
| Qualified assessor transfer-document observations | 37,877 |
| Total entities | 204,966 |
| Property-to-entity links | 1,096,132 |
| Municipality-wide official source screens | 24 × 28,340 properties |
| Municipality-wide owner screens | 2 × 33,438 owners |
| Properties with analysis geometry | 28,326 |
| Properties with official City parcel matches | 28,320 |
| Exact-address sites containing multiple parcels | 809 |
| Largest exact-address parcel group | 262 |

The latest precompute completed successfully on 2026-08-16. It skipped
unchanged collectors by fingerprint, reused the validated same-day owner
snapshot across the UTC refresh boundary, revalidated reused output contracts, and
materialized zoning, owner/entity, mortgage/lien, permit, hazard,
infrastructure, tenant, and market screens at municipality scope. SQLite
`quick_check` is `ok`; all 23 GIS layers plus the zoning-regulations reference
cover exactly 28,340 properties, and both owner predicates cover exactly
33,438 owners. The fast readiness validator reports schema integrity `ok`;
each forced deep-profile database separately reports full SQLite integrity `ok`.

## Connected evidence

The activation stack currently queries:

- Stamford Vision assessor records and Connecticut OPM 2025 parcel geometry;
- assessor-reported book/page, instrument code, party, date, and price as
  explicitly qualified transfer/index observations, not deed proof;
- the latest official City parcel service plus municipality-wide City zoning,
  Comprehensive Plan 2035, architectural-review, aquifer-protection, coastal,
  land-use, national- and state-historic-district, master-development,
  public-parking, pedestrian-path, building-footprint, paving-status, bus-stop,
  stormwater-main, 2026 commercial-lease, 2026 commercial-inventory,
  2022–2025 sales, Connecticut brownfield, enterprise-zone, and
  opportunity-zone GIS layers (24 official source screens including the zoning
  reference);
- Connecticut Secretary of the State business master, principal, registered-
  agent, filing-history, name-history, and active UCC/other-lien datasets;
- Census geocoding and roads, FEMA NFHL/NRI/USA Structures, FWS wetlands, USDA
  soils, USGS elevation, and EPA ECHO screens.

The City zoning map is retained with its general-reference disclaimer. The City
master development list is treated as a major-project screen, not the complete
permit register. City commercial lease and inventory points are treated as
screening observations, not verified rent rolls, lease abstracts, appraisals,
or complete market coverage.

The City OpenGov permit portal was verified interactively. Its record and
location pages are protected by Cloudflare Turnstile, so the pipeline does not
bypass that control or represent the portal as an approved bulk collector.

## Coverage contract

| Domain | Status | Evidence boundary |
|---|---|---|
| Assessor | Complete | Manifest reconciles all 37,877 enumerated records with zero failures. |
| Parcel/GIS | Partial | Exact official City parcel joins cover 28,320 of 28,340 indexed properties; 6 bounded geocodes add screening geometry and 14 are explicitly unevaluable. |
| Zoning | Partial | Assessor code plus official City overlay and plan; ordinance rules/capacity are not computed. |
| Owners/entities | Partial | All 33,438 owners have registry eligibility/result facts; exact business matches link 1,743 principals, 1,159 registered agents, 10,499 filings, and 31 name changes. Beneficial ownership remains unadjudicated. |
| Deeds | Partial | Assessor transfer/index observations only; full index and instrument images are absent. |
| Mortgages/liens | Partial municipality-wide | All 33,438 owners have active-UCC eligibility/result facts; 690 exact-name records are linked. Property mortgages, releases, judgments, and collateral are not adjudicated. |
| Site assembly | Partial | Explainable exact-situs grouping; deed collateral, easements, and plans are not adjudicated. |
| Permits/planning | Partial municipality-wide | Every property has an official major-development proximity result; complete permits, COs, variances, applications, and enforcement are absent. |
| Hazards/environmental | Partial municipality-wide | Every property has explicit City aquifer/coastal results; geometry-backed activations add national screens. Local/state file review remains absent. |
| Infrastructure | Partial municipality-wide | Every property has explicit paving, bus, parking, and stormwater results; legal access, frontage, sanitary/water service, and capacity are absent. |
| Tenants/market | Partial municipality-wide | Every property has explicit City lease, inventory, sales, enterprise-zone, and opportunity-zone results; historical completeness and executed-comparable normalization are absent. |
| Web/documents | Partial, working | All planned searches run against an approved official-source catalog and five official machine-readable source documents are parsed. Property-specific permit/title documents and a broad general-web provider remain absent. |

The City's [land-records page](https://www.stamfordct.gov/government/town-clerk/land-records)
offers online records beginning in 1998 through an interactive system, but no
approved complete bulk interface is connected. The City's
[building-records guidance](https://www.stamfordct.gov/government/operations/building-department/building-records)
splits records between the municipal search system and ViewPoint Cloud based on
date and directs users to search all formats for a complete result. Those access
boundaries are why the corresponding engines remain partial or missing.

## Municipality-wide validation results

The final cache validation reports:

- SQLite schema integrity: `ok`, with full profile-database integrity `ok` on all three forced activations;
- all 23 official GIS layers plus one zoning-reference screen at exactly 28,340 properties;
- compact municipal/state results exported for every searchable property;
- 28,326 spatially evaluated properties and 28,319 mapped zoning matches;
- registry and UCC screens exported for all 28,340 property records through
  their linked assessor owners;
- 1,197 exact business-master matches, 1,743 principals, 1,159 registered
  agents, 10,499 filings, and 690 active UCC records;
- 21 automated tests passing; and
- all three forced deep-profile activations passing their semantic and SQLite
  validation on 2026-08-16.

## Property activation validation

The reproducible random validation selected `11 GARDEN STREET` from the 24,085
geometry-backed properties. Its forced activation produced:

- all 24 configured Stamford/Connecticut GIS layers successful, with zero layer failures;
- all 9 national collectors successful, with zero collector failures;
- 10 of 10 planned official-source searches executed, returning 12 jurisdiction references;
- five official machine-readable source documents parsed with two located mentions and zero errors;
- the CT business/UCC adapter completed an exact-name negative registry/UCC
  screen without treating that absence as proof of no entity or lien;
- one included parcel and one grouping decision;
- one qualified assessor transfer-document observation;
- two same-assessor-owner Stamford portfolio candidates;
- 44 nearby official master-development cases;
- 15 nearby City commercial-inventory records;
- 161 dated timeline events, with no duplicate event keys;
- 74 resolved claims and seven explicitly contested claims;
- seven retained source contradictions, zero orphan facts, and zero missing raw-evidence links;
- SQLite integrity `ok`, all 25 stages materialized, and final validation passed.

A commercial-path validation at `230 TRESSER BOULEVARD` also passed. It
materialized two City-published tenant entities, two dated tenant-occurrence
states, 15 nearby City inventory records, three exact-assessor-owner portfolio
candidates, one official CT registry match, one CT principal, eight filing-
history records, and a zero-result active-UCC screen. The asset classifier
resolved it as `retail / shopping_center / regional_center`. A second commercial
validation at `1600 SUMMER STREET` materialized a City commercial-inventory
observation, an official building-footprint intersection, and a classified
`office / general_office / multi_tenant` asset.

## Outputs

- Municipality database: `data/stamford_ct_precomputed.sqlite`
- Batch coverage: `reports/stamford_ct_batch_coverage.json`
- Random validation profile: `activations/reports/11_garden_st_property_d4c2df92.md`
- Tenant-path profile: `activations/reports/230_tresser_blvd_property_01dfe679.md`
- Commercial-inventory profile: `activations/reports/1600_summer_st_property_a5984018.md`

## Conclusion

The Stamford implementation is built and validated for the sources that are
lawfully available through the verified delivery and public APIs. All software
engines are implemented, and every indexed property now receives a real
Stamford/Connecticut batch result rather than a silent gap for every connected
municipal domain. Approved official-source discovery and document parsing are
connected; the absence of a broad general-web provider and parcel-specific
record documents remains explicit. It is ready to return honest, source-linked partial
intelligence profiles. It is not ready to claim complete Stamford due diligence
until approved complete land records, property-specific mortgages and liens,
full permits/COs/variances/enforcement, computational zoning rules, historical
tenants, executed comps, and utility/access evidence are connected.
