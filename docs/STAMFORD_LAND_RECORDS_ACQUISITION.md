# Stamford Land-Records Acquisition and Ingestion

## What is available now

Stamford's Town Clerk uses SearchIQS for online land-record access. Its public
availability statement lists index data from July 3, 1967 and images from
December 31, 1997 to present. The Clerk offers a two-year electronic access
subscription for $750, but that subscription is an interactive user account,
not a bulk-download or API license.

SearchIQS terms prohibit bots and scraping. Do not use browser automation,
credential sharing, CAPTCHA circumvention, or page harvesting for the land
record portal.

## Required acquisition path for complete deeds/title

Obtain one of the following in writing before loading data:

1. Stamford Town Clerk / IQS licensed bulk index and image export, with a
   permitted refresh cadence and image URI/download rights;
2. a Clerk-approved records-production export; or
3. a title-data vendor feed that grants programmatic use and redisplay rights.

The request should require a data dictionary and coverage reconciliation for
recording ID, book/page, document type, recording date, grantor, grantee,
parcel identifier or situs address, consideration, and image/official-copy
status. Include deeds, mortgages, assignments, satisfactions/releases, liens,
easements, maps, lis pendens, and foreclosure-related instruments.

## Implementation already prepared

`property_intel.adapters.linked_records` is the production batch adapter for
licensed jurisdiction exports. It matches only exact parcel aliases or exact
normalized situs addresses; unmatched and ambiguous records are never guessed
onto a property.

Use `configs/stamford_ct_land_records_licensed.example.json` as the mapping
contract. Copy its `config` into the `deeds` batch-engine definition after the
licensed export is placed outside the repository (for example,
`F:/DealSynq/private-data/stamford_land_records/index.csv`).

Before declaring complete coverage, run a reconciliation of source row count,
document-type counts, image coverage by date, matched/unmatched rows, and
parcel/address matching exceptions. Retain raw source payload hashes and the
data-use agreement reference with the collection run.

## Boundaries

An online subscription alone does not authorize automated collection. A title
report or recorded-image feed may be valuable evidence but must be labelled
with its contractual scope and must not be represented as a legal title opinion.
