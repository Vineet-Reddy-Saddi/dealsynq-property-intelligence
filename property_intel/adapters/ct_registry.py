from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..http_client import PublicHttpClient
from ..store import EvidenceStore
from ..util import stable_id

PARSER_VERSION = "ct-public-registry/1.0.0"
SOCRATA_ROOT = "https://data.ct.gov/resource"
MASTER_DATASET = "n7gp-d28j"
PRINCIPALS_DATASET = "ka36-64k6"
FILINGS_DATASET = "ah3s-bes7"
AGENTS_DATASET = "qh2m-n44y"
NAME_CHANGES_DATASET = "enwv-52we"
UCC_DATASET = "xfev-8smz"


def fingerprint(store: EvidenceStore, target_id: str, config: dict[str, Any]) -> str:
    days = max(1, int(config.get("refresh_days", 1)))
    bucket = int(datetime.now(timezone.utc).timestamp() // (days * 86400))
    owners = [tuple(row) for row in store.rows(
        "SELECT DISTINCT e.entity_id,e.canonical_name FROM entities e "
        "JOIN relationships r ON r.from_entity_id=e.entity_id "
        "WHERE e.entity_type='organization' AND r.relationship_type='assessor_owner_of' "
        "ORDER BY e.entity_id"
    )]
    return stable_id("input", PARSER_VERSION, config, owners, bucket)


def _quoted(value: str) -> str:
    return value.replace("'", "''")


def _query(client: PublicHttpClient, dataset: str, soql: str,
           cache_days: int) -> Any:
    return client.fetch(
        f"{SOCRATA_ROOT}/{dataset}.json",
        params={"$query": soql}, cache_days=cache_days,
    )


def _source(store: EvidenceStore, fetched: Any, *, name: str,
            dataset: str) -> str:
    return store.source(
        name=name, url=f"https://data.ct.gov/d/{dataset}",
        authority="Connecticut Secretary of the State, Business Services Division",
        parser_version=PARSER_VERSION, raw_sha256=fetched.raw_sha256,
        retrieved_at=fetched.retrieved_at,
        access_note="Public-domain Connecticut Open Data API; ordinary unauthenticated query.",
    )


def _date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _jurisdiction(store: EvidenceStore, target_id: str) -> str:
    row = store.db.execute(
        "SELECT jurisdiction_id FROM source_capabilities WHERE target_id=? LIMIT 1",
        (target_id,),
    ).fetchone()
    return str(row[0]) if row else "connecticut"


def _capability(store: EvidenceStore, target_id: str, capability: str,
                status: str, reason: str, dataset: str) -> None:
    store.register_capability(target_id, _jurisdiction(store, target_id), {
        "capability": capability, "status": status,
        "source_name": "Connecticut Secretary of the State open data",
        "source_url": f"https://data.ct.gov/d/{dataset}",
        "adapter": "ct_registry", "reason": reason,
    }, PARSER_VERSION)


def _clear_prior_entities(store: EvidenceStore, target_id: str) -> None:
    rows = store.rows(
        "SELECT entity_id FROM entities WHERE entity_type IN "
        "('ct_principal','ct_registered_agent','corporate_filing','ucc_lien_record') "
        "AND attributes_json LIKE '%\"source_adapter\":\"ct_registry\"%'"
    )
    ids = [row[0] for row in rows]
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    store.db.execute(f"DELETE FROM events WHERE target_id=? AND subject_id IN ({marks})",
                     (target_id, *ids))
    store.db.execute(f"DELETE FROM facts WHERE subject_id IN ({marks})", tuple(ids))
    store.db.execute(f"DELETE FROM temporal_states WHERE subject_id IN ({marks})", tuple(ids))
    store.db.execute(f"DELETE FROM entity_aliases WHERE entity_id IN ({marks})", tuple(ids))
    store.db.execute(
        f"DELETE FROM relationships WHERE from_entity_id IN ({marks}) OR to_entity_id IN ({marks})",
        tuple(ids) + tuple(ids),
    )
    store.db.execute(f"DELETE FROM entities WHERE entity_id IN ({marks})", tuple(ids))


def collect(store: EvidenceStore, target_id: str, target: dict[str, Any],
            config: dict[str, Any]) -> dict[str, Any]:
    cache_days = max(0, int(config.get("refresh_days", 1)))
    client = PublicHttpClient(store, timeout=int(config.get("timeout_seconds", 60)))
    _clear_prior_entities(store, target_id)
    owners = store.rows(
        "SELECT DISTINCT e.entity_id,e.canonical_name FROM entities e "
        "JOIN relationships r ON r.from_entity_id=e.entity_id "
        "WHERE e.entity_type='organization' AND r.relationship_type='assessor_owner_of' "
        "ORDER BY e.entity_id"
    )
    stats: dict[str, Any] = {
        "owners_queried": len(owners), "registry_matches": 0, "principals": 0,
        "agents": 0, "filings": 0, "name_changes": 0, "active_ucc_records": 0,
        "errors": [],
    }
    for owner in owners:
        owner_id, owner_name = owner["entity_id"], owner["canonical_name"]
        exact = _quoted(owner_name.upper())
        try:
            master_result = _query(
                client, MASTER_DATASET,
                f"select * where upper(name)='{exact}' limit 20", cache_days,
            )
            master_rows = master_result.json()
            master_source = _source(
                store, master_result, name="Connecticut Business Registry - Business Master",
                dataset=MASTER_DATASET,
            )
            store.fact(
                subject_id=owner_id, category="ownership",
                predicate="ct_business_registry_match_screen",
                value={"queried_name": owner_name, "match_count": len(master_rows),
                       "records": [{k: row.get(k) for k in (
                           "id", "name", "business_type", "status", "sub_status",
                           "accountnumber", "annual_report_due_date", "date_registration",
                           "citizenship", "formation_place", "state_or_territory_formation",
                           "naics_code", "billingstreet", "billingcity", "billingstate",
                           "billingpostalcode") if row.get(k) is not None}
                                   for row in master_rows],
                       "limitation": "Exact-name registry matching does not prove beneficial ownership or that similarly named entities are identical."},
                fact_class="confirmed_official", confidence=0.95,
                source_id=master_source, parser_version=PARSER_VERSION,
                raw_sha256=master_result.raw_sha256, freshness_days=cache_days or 1,
                evidence_locator=f"Socrata exact-name query for {owner_name}",
            )
            stats["registry_matches"] += len(master_rows)

            for business in master_rows:
                business_id = business.get("id")
                if not business_id:
                    continue
                if business.get("accountnumber"):
                    store.alias(
                        owner_id, "ct_business_alei", str(business["accountnumber"]),
                        str(business["accountnumber"]), source_id=master_source,
                        confidence=0.98,
                    )
                registered = _date(business.get("date_registration"))
                if registered:
                    store.event(
                        target_id=target_id, event_type="business_registered",
                        event_date=registered, date_precision="day", subject_id=owner_id,
                        summary=f"{business.get('name') or owner_name} registered in Connecticut",
                        fact_class="confirmed_official", confidence=0.95,
                        source_ids=[master_source], evidence={"business_id": business_id},
                    )

                related = (
                    (PRINCIPALS_DATASET, "business_id", "Connecticut Business Registry - Principals"),
                    (AGENTS_DATASET, "business_id", "Connecticut Business Registry - Agents"),
                    (FILINGS_DATASET, "account", "Connecticut Business Registry - Business Filing History"),
                    (NAME_CHANGES_DATASET, "unique_key", "Connecticut Business Registry - Name Change History"),
                )
                for dataset, field, source_name in related:
                    fetched = _query(
                        client, dataset,
                        f"select * where {field}='{_quoted(str(business_id))}' limit 500",
                        cache_days,
                    )
                    records = fetched.json()
                    source_id = _source(store, fetched, name=source_name, dataset=dataset)
                    for index, record in enumerate(records):
                        if dataset == PRINCIPALS_DATASET:
                            name = record.get("name__c") or "Unnamed Connecticut principal"
                            entity_id = stable_id("ct-principal", business_id, name, record.get("designation"))
                            attributes = {k: record.get(k) for k in (
                                "designation", "type", "business_street_address_1",
                                "business_street_address_2", "business_city", "business_state",
                                "business_zip_code", "business_country") if record.get(k) is not None}
                            store.entity("ct_principal", str(name), external_id=str(business_id),
                                         attributes={"source_adapter": "ct_registry", **attributes},
                                         entity_id=entity_id)
                            store.relationship(
                                from_id=entity_id, relationship_type="principal_of", to_id=owner_id,
                                fact_class="confirmed_official", confidence=0.9,
                                source_id=source_id, parser_version=PARSER_VERSION,
                                raw_sha256=fetched.raw_sha256,
                                explanation={"business_id": business_id,
                                             "limitation": "Registry principal role is not a beneficial-ownership percentage."},
                            )
                            stats["principals"] += 1
                        elif dataset == AGENTS_DATASET:
                            name = record.get("name__c") or "Unnamed Connecticut registered agent"
                            entity_id = stable_id("ct-agent", business_id, name, record.get("business_key"))
                            attributes = {k: record.get(k) for k in (
                                "type", "business_street_address_1", "business_street_address_2",
                                "business_city", "business_state", "business_zip_code",
                                "business_country") if record.get(k) is not None}
                            store.entity("ct_registered_agent", str(name), external_id=record.get("business_key"),
                                         attributes={"source_adapter": "ct_registry", **attributes},
                                         entity_id=entity_id)
                            store.relationship(
                                from_id=entity_id, relationship_type="registered_agent_of", to_id=owner_id,
                                fact_class="confirmed_official", confidence=0.95,
                                source_id=source_id, parser_version=PARSER_VERSION,
                                raw_sha256=fetched.raw_sha256,
                                explanation={"business_id": business_id},
                            )
                            stats["agents"] += 1
                        elif dataset == FILINGS_DATASET:
                            filing_number = record.get("name") or f"filing-{index + 1}"
                            entity_id = stable_id("ct-filing", business_id, filing_number)
                            store.entity(
                                "corporate_filing", str(filing_number), external_id=str(filing_number),
                                attributes={"source_adapter": "ct_registry", "business_id": business_id,
                                            "record": record}, entity_id=entity_id,
                            )
                            filing_date = _date(record.get("filing_date"))
                            store.relationship(
                                from_id=entity_id, relationship_type="filing_for", to_id=owner_id,
                                fact_class="confirmed_official", confidence=0.98,
                                source_id=source_id, parser_version=PARSER_VERSION,
                                raw_sha256=fetched.raw_sha256, effective_date=filing_date,
                                explanation={"business_id": business_id},
                            )
                            if filing_date:
                                store.event(
                                    target_id=target_id, event_type="corporate_filing",
                                    event_date=filing_date, date_precision="day", subject_id=entity_id,
                                    summary=f"{record.get('filing_type') or record.get('type') or 'Corporate filing'}: {filing_number}",
                                    fact_class="confirmed_official", confidence=0.98,
                                    source_ids=[source_id], evidence={"record": record},
                                )
                            stats["filings"] += 1
                        else:
                            old_name = record.get("business_name_old")
                            new_name = record.get("business_name_new")
                            if old_name:
                                store.alias(owner_id, "former_business_name", str(old_name),
                                            str(old_name).upper(), source_id=source_id, confidence=0.95)
                            changed = _date(record.get("name_change_date"))
                            if changed:
                                store.event(
                                    target_id=target_id, event_type="business_name_changed",
                                    event_date=changed, date_precision="day", subject_id=owner_id,
                                    summary=f"Business name changed from {old_name} to {new_name}",
                                    fact_class="confirmed_official", confidence=0.95,
                                    source_ids=[source_id], evidence={"record": record},
                                )
                            stats["name_changes"] += 1

            ucc_result = _query(
                client, UCC_DATASET,
                f"select * where upper(debtor_nm_bus)='{exact}' limit 500", cache_days,
            )
            ucc_rows = ucc_result.json()
            ucc_source = _source(
                store, ucc_result, name="Connecticut active UCC and other lien filings",
                dataset=UCC_DATASET,
            )
            store.fact(
                subject_id=owner_id, category="deeds_liens",
                predicate="ct_active_ucc_lien_screen",
                value={"queried_debtor_name": owner_name, "active_record_count": len(ucc_rows),
                       "limitation": "An exact-name active UCC screen is not a property-title search, mortgage search, judgment search, or proof of current debt/collateral."},
                fact_class="confirmed_official", confidence=0.9,
                source_id=ucc_source, parser_version=PARSER_VERSION,
                raw_sha256=ucc_result.raw_sha256, freshness_days=cache_days or 1,
                evidence_locator=f"Socrata exact debtor-business-name query for {owner_name}",
            )
            for record in ucc_rows:
                filing_number = record.get("id_ucc_flng_nbr") or record.get("id_lien_flng_nbr")
                entity_id = stable_id("ucc", owner_id, filing_number, record)
                store.entity(
                    "ucc_lien_record", f"Connecticut UCC/lien {filing_number}",
                    external_id=str(filing_number) if filing_number else None,
                    attributes={"source_adapter": "ct_registry", "record": record,
                                "property_collateral_status": "not_adjudicated"}, entity_id=entity_id,
                )
                accepted = _date(record.get("dt_accept"))
                store.relationship(
                    from_id=owner_id, relationship_type="named_debtor_in", to_id=entity_id,
                    fact_class="confirmed_official", confidence=0.9,
                    source_id=ucc_source, parser_version=PARSER_VERSION,
                    raw_sha256=ucc_result.raw_sha256, effective_date=accepted,
                    explanation={"limitation": "Debtor-name match does not establish that the subject property is collateral."},
                )
                if accepted:
                    store.event(
                        target_id=target_id, event_type="owner_ucc_lien_filing_observed",
                        event_date=accepted, date_precision="day", subject_id=entity_id,
                        summary=f"Active Connecticut UCC/lien filing for owner name {owner_name}",
                        fact_class="confirmed_official", confidence=0.85,
                        source_ids=[ucc_source], evidence={"record": record,
                                                          "property_collateral_status": "not_adjudicated"},
                    )
                stats["active_ucc_records"] += 1
        except Exception as exc:
            stats["errors"].append({"owner": owner_name, "error": f"{type(exc).__name__}: {exc}"})

    if stats["errors"]:
        store.gap(target_id, "ct_registry", "partial",
                  "Connecticut business registry or active-UCC query failed",
                  reason=str(stats["errors"]))
    else:
        _capability(store, target_id, "entity_registry", "working",
                    "Official Connecticut business master exact-name queries completed.", MASTER_DATASET)
        _capability(store, target_id, "corporate_filings", "working",
                    "Official Connecticut filing, principal, agent, and name-history queries completed.", FILINGS_DATASET)
        _capability(store, target_id, "mortgages_liens", "partial",
                    "Official active UCC/other-lien debtor-name screen completed; property mortgages, releases, judgments, and collateral remain unadjudicated.", UCC_DATASET)
    store.db.commit()
    return stats
