from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import requests

from ..store import EvidenceStore
from ..util import normalize_text, stable_id

PARSER_VERSION = "ct-public-registry-municipality/1.0.1"
SOCRATA_ROOT = "https://data.ct.gov/resource"
DATASETS = {
    "master": ("n7gp-d28j", "Connecticut Business Registry - Business Master"),
    "principals": ("ka36-64k6", "Connecticut Business Registry - Principals"),
    "filings": ("ah3s-bes7", "Connecticut Business Registry - Business Filing History"),
    "agents": ("qh2m-n44y", "Connecticut Business Registry - Agents"),
    "names": ("enwv-52we", "Connecticut Business Registry - Name Change History"),
    "ucc": ("xfev-8smz", "Connecticut active UCC and other lien filings"),
}
BUSINESS_MARKERS = re.compile(
    r"\b(LLC|L L C|INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|LP|L P|LLP|LTD|"
    r"LIMITED|PC|P C|PLLC|ASSOCIATES|PARTNERS|HOLDINGS|PROPERTIES|REALTY|TRUST|"
    r"FOUNDATION|ASSOCIATION|AUTHORITY|CHURCH|SCHOOL|UNIVERSITY|COLLEGE|BANK|"
    r"SOCIETY|CLUB|CONDOMINIUM|HOMEOWNERS|HOUSING|DEVELOPMENT)\b", re.I,
)


def fingerprint(config: dict[str, Any]) -> str:
    days = max(1, int(config.get("refresh_days", 1)))
    bucket = int(datetime.now(timezone.utc).timestamp() // (days * 86400))
    return stable_id("input", PARSER_VERSION, config, bucket)


def _profile() -> dict[str, Counter[str] | int]:
    return {
        "entity_types": Counter(), "fact_categories": Counter(),
        "fact_predicates": Counter(), "relationship_types": Counter(),
        "property_roles": Counter(), "event_types": Counter(),
        "alias_types": Counter(), "memberships": 0,
    }


def _quoted(value: str) -> str:
    return value.replace("'", "''")


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _query(dataset: str, soql: str, timeout: int, attempts: int = 4) -> list[dict[str, Any]]:
    url = f"{SOCRATA_ROOT}/{dataset}.json"
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, params={"$query": soql}, timeout=timeout,
                                    headers={"User-Agent": "DealSynq property intelligence/1.0"})
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("error"):
                raise RuntimeError(str(payload))
            return [dict(row) for row in payload]
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(8, 2 ** attempt))
    raise RuntimeError(f"Connecticut Socrata query failed for {dataset}: {last}")


def _fetch_batches(dataset: str, field: str, values: list[str], *, upper: bool,
                   batch_size: int, timeout: int, workers: int) -> list[dict[str, Any]]:
    tasks: list[tuple[int, str]] = []
    for index, batch in enumerate(_chunks(values, batch_size)):
        rendered = ",".join(f"'{_quoted(value.upper() if upper else value)}'" for value in batch)
        expression = f"upper({field})" if upper else field
        tasks.append((index, f"select * where {expression} in ({rendered}) limit 50000"))
    results: list[tuple[int, str, list[dict[str, Any]]]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as executor:
        futures = {executor.submit(_query, dataset, soql, timeout): (index, soql)
                   for index, soql in tasks}
        for future in as_completed(futures):
            index, soql = futures[future]
            results.append((index, soql, future.result()))
    return [{"query_index": index, "soql": soql, "rows": rows}
            for index, soql, rows in sorted(results)]


def _source(store: EvidenceStore, kind: str, raw: str) -> str:
    dataset, name = DATASETS[kind]
    return store.source(
        name=name, url=f"https://data.ct.gov/d/{dataset}",
        authority="Connecticut Secretary of the State, Business Services Division",
        parser_version=PARSER_VERSION, raw_sha256=raw,
        access_note="Official nightly Connecticut Open Data extract queried by exact normalized name or business identifier.",
    )


def _owners(store: EvidenceStore, scope_id: str) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    rows = [dict(row) for row in store.rows(
        "SELECT DISTINCT e.entity_id,e.canonical_name FROM property_index p "
        "JOIN property_entity_links l ON l.property_id=p.property_id "
        "JOIN entities e ON e.entity_id=l.entity_id AND e.entity_type='organization' "
        "WHERE p.scope_id=? AND l.role='assessor_owner' ORDER BY e.entity_id", (scope_id,),
    )]
    properties: dict[str, list[str]] = defaultdict(list)
    for row in store.rows(
        "SELECT DISTINCT l.entity_id,l.property_id FROM property_index p "
        "JOIN property_entity_links l ON l.property_id=p.property_id "
        "WHERE p.scope_id=? AND l.role='assessor_owner' ORDER BY l.entity_id,l.property_id",
        (scope_id,),
    ):
        properties[row["entity_id"]].append(row["property_id"])
    return rows, properties


def _link_related(store: EvidenceStore, owner_id: str, entity_id: str, role: str,
                  owner_properties: dict[str, list[str]], source_id: str,
                  evidence: dict[str, Any], totals: dict[str, Any]) -> None:
    for property_id in owner_properties.get(owner_id, []):
        store.link_property_entity(
            property_id=property_id, entity_id=entity_id, role=role,
            confidence=0.9, source_id=source_id, evidence=evidence,
        )
        totals["property_links"] += 1
        totals["output_profile"]["property_roles"][role] += 1


def collect(store: EvidenceStore, scope: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    timeout = int(config.get("timeout_seconds", 45))
    workers = int(config.get("workers", 12))
    batch_size = int(config.get("batch_size", 12))
    owners, owner_properties = _owners(store, scope["id"])
    eligible = [row for row in owners if BUSINESS_MARKERS.search(row["canonical_name"])]
    names = sorted({row["canonical_name"].upper() for row in eligible})
    master_batches = _fetch_batches(DATASETS["master"][0], "name", names, upper=True,
                                    batch_size=batch_size, timeout=timeout, workers=workers)
    ucc_batches = _fetch_batches(DATASETS["ucc"][0], "debtor_nm_bus", names, upper=True,
                                 batch_size=batch_size, timeout=timeout, workers=workers)

    master_by_name: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    ucc_by_name: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    for kind, batches, field in (("master", master_batches, "name"),
                                 ("ucc", ucc_batches, "debtor_nm_bus")):
        for batch in batches:
            raw = store.put_raw({"dataset": DATASETS[kind][0], **batch})
            source_id = _source(store, kind, raw)
            target = master_by_name if kind == "master" else ucc_by_name
            for row in batch["rows"]:
                value = row.get(field)
                if value:
                    target[normalize_text(str(value))].append((row, source_id))

    business_ids = sorted({str(record.get("id")) for values in master_by_name.values()
                           for record, _ in values if record.get("id")})
    related_specs = {
        # The Agents extract calls the Salesforce business identifier
        # `business_key`; its `business_id` field is the numeric ALEI/account.
        "principals": "business_id", "agents": "business_key",
        "filings": "account", "names": "unique_key",
    }
    related_by_kind: dict[str, dict[str, list[tuple[dict[str, Any], str]]]] = {}
    for kind, field in related_specs.items():
        batches = _fetch_batches(
            DATASETS[kind][0], field, business_ids, upper=False,
            batch_size=batch_size, timeout=timeout, workers=workers,
        )
        by_business: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
        for batch in batches:
            raw = store.put_raw({"dataset": DATASETS[kind][0], **batch})
            source_id = _source(store, kind, raw)
            for row in batch["rows"]:
                value = row.get(field)
                if value:
                    by_business[str(value)].append((row, source_id))
        related_by_kind[kind] = by_business

    totals: dict[str, Any] = {
        "owners": len(owners), "eligible_business_names": len(names),
        "registry_matches": 0, "principals": 0, "agents": 0,
        "filings": 0, "name_changes": 0, "active_ucc_records": 0,
        "facts": 0, "entities": 0, "relationships": 0, "property_links": 0,
        "output_profile": _profile(),
    }
    totals["output_profile"]["entity_types"]["organization"] = len(owners)
    totals["output_profile"]["relationship_types"]["assessor_owner_of"] = store.db.execute(
        "SELECT COUNT(*) FROM relationships WHERE relationship_type='assessor_owner_of'"
    ).fetchone()[0]
    fallback_raw = store.put_raw({"datasets": DATASETS, "eligible_names": len(names)})
    fallback_master_source = _source(store, "master", fallback_raw)
    fallback_ucc_source = _source(store, "ucc", fallback_raw)

    for owner in owners:
        owner_id = owner["entity_id"]
        owner_name = owner["canonical_name"]
        key = normalize_text(owner_name)
        eligible_name = bool(BUSINESS_MARKERS.search(owner_name))
        matches = master_by_name.get(key, []) if eligible_name else []
        ucc_matches = ucc_by_name.get(key, []) if eligible_name else []
        master_source = matches[0][1] if matches else fallback_master_source
        ucc_source = ucc_matches[0][1] if ucc_matches else fallback_ucc_source
        store.fact(
            subject_id=owner_id, category="ownership",
            predicate="ct_business_registry_match_screen",
            value={
                "queried_name": owner_name, "eligible_business_name": eligible_name,
                "match_count": len(matches),
                "records": [{key: record.get(key) for key in (
                    "id", "name", "business_type", "status", "sub_status",
                    "accountnumber", "date_registration", "citizenship",
                    "formation_place", "naics_code", "billingcity", "billingstate")
                             if record.get(key) is not None} for record, _ in matches[:20]],
                "limitation": "Exact-name registry matching does not prove beneficial ownership or identity among similarly named parties.",
            }, fact_class="confirmed_official", confidence=0.95,
            source_id=master_source, parser_version=PARSER_VERSION,
            raw_sha256=store.db.execute("SELECT raw_sha256 FROM sources WHERE source_id=?", (master_source,)).fetchone()[0],
            evidence_locator=f"Municipality-wide exact-name registry screen: {owner_name}",
        )
        store.fact(
            subject_id=owner_id, category="deeds_liens",
            predicate="ct_active_ucc_lien_screen",
            value={
                "queried_debtor_name": owner_name, "eligible_business_name": eligible_name,
                "active_record_count": len(ucc_matches),
                "limitation": "An exact-name active UCC screen is not a property-title, mortgage, judgment, release, or collateral adjudication.",
            }, fact_class="confirmed_official", confidence=0.9,
            source_id=ucc_source, parser_version=PARSER_VERSION,
            raw_sha256=store.db.execute("SELECT raw_sha256 FROM sources WHERE source_id=?", (ucc_source,)).fetchone()[0],
            evidence_locator=f"Municipality-wide exact debtor-business-name screen: {owner_name}",
        )
        totals["facts"] += 2
        totals["output_profile"]["fact_categories"]["ownership"] += 1
        totals["output_profile"]["fact_categories"]["deeds_liens"] += 1
        totals["output_profile"]["fact_predicates"]["ct_business_registry_match_screen"] += 1
        totals["output_profile"]["fact_predicates"]["ct_active_ucc_lien_screen"] += 1

        for business, source_id in matches:
            business_id = str(business.get("id"))
            totals["registry_matches"] += 1
            if business.get("accountnumber"):
                store.alias(owner_id, "ct_business_alei", str(business["accountnumber"]),
                            str(business["accountnumber"]), source_id=source_id, confidence=0.98)
                totals["output_profile"]["alias_types"]["ct_business_alei"] += 1
            for record, related_source in related_by_kind["principals"].get(business_id, []):
                name = record.get("name__c") or "Unnamed Connecticut principal"
                entity_id = stable_id("ct-principal", business_id, name, record.get("designation"))
                store.entity("ct_principal", str(name), external_id=business_id,
                             attributes={"source_adapter": "ct_registry_municipality", "record": record},
                             entity_id=entity_id)
                store.relationship(from_id=entity_id, relationship_type="principal_of", to_id=owner_id,
                                   fact_class="confirmed_official", confidence=0.9,
                                   source_id=related_source, parser_version=PARSER_VERSION,
                                   explanation={"business_id": business_id,
                                                "limitation": "Registry principal role is not a beneficial-ownership percentage."})
                _link_related(store, owner_id, entity_id, "ct_principal", owner_properties,
                              related_source, {"business_id": business_id}, totals)
                totals["principals"] += 1
                totals["entities"] += 1
                totals["relationships"] += 1
                totals["output_profile"]["entity_types"]["ct_principal"] += 1
                totals["output_profile"]["relationship_types"]["principal_of"] += 1
            for record, related_source in related_by_kind["agents"].get(business_id, []):
                name = record.get("name__c") or "Unnamed Connecticut registered agent"
                entity_id = stable_id("ct-agent", business_id, name, record.get("business_key"))
                store.entity("ct_registered_agent", str(name), external_id=record.get("business_key"),
                             attributes={"source_adapter": "ct_registry_municipality", "record": record},
                             entity_id=entity_id)
                store.relationship(from_id=entity_id, relationship_type="registered_agent_of", to_id=owner_id,
                                   fact_class="confirmed_official", confidence=0.95,
                                   source_id=related_source, parser_version=PARSER_VERSION,
                                   explanation={"business_id": business_id})
                _link_related(store, owner_id, entity_id, "ct_registered_agent", owner_properties,
                              related_source, {"business_id": business_id}, totals)
                totals["agents"] += 1
                totals["entities"] += 1
                totals["relationships"] += 1
                totals["output_profile"]["entity_types"]["ct_registered_agent"] += 1
                totals["output_profile"]["relationship_types"]["registered_agent_of"] += 1
            for index, (record, related_source) in enumerate(related_by_kind["filings"].get(business_id, [])):
                filing_number = record.get("name") or f"filing-{index + 1}"
                entity_id = stable_id("ct-filing", business_id, filing_number)
                store.entity("corporate_filing", str(filing_number), external_id=str(filing_number),
                             attributes={"source_adapter": "ct_registry_municipality", "record": record},
                             entity_id=entity_id)
                store.relationship(from_id=entity_id, relationship_type="filing_for", to_id=owner_id,
                                   fact_class="confirmed_official", confidence=0.98,
                                   source_id=related_source, parser_version=PARSER_VERSION,
                                   effective_date=str(record.get("filing_date") or "")[:10] or None,
                                   explanation={"business_id": business_id})
                _link_related(store, owner_id, entity_id, "corporate_filing", owner_properties,
                              related_source, {"business_id": business_id}, totals)
                totals["filings"] += 1
                totals["entities"] += 1
                totals["relationships"] += 1
                totals["output_profile"]["entity_types"]["corporate_filing"] += 1
                totals["output_profile"]["relationship_types"]["filing_for"] += 1
            for record, related_source in related_by_kind["names"].get(business_id, []):
                old_name = record.get("business_name_old")
                if old_name:
                    store.alias(owner_id, "former_business_name", str(old_name),
                                normalize_text(str(old_name)), source_id=related_source, confidence=0.95)
                    totals["output_profile"]["alias_types"]["former_business_name"] += 1
                totals["name_changes"] += 1

        for record, source_id in ucc_matches:
            filing_number = record.get("id_ucc_flng_nbr") or record.get("id_lien_flng_nbr")
            entity_id = stable_id("ucc", owner_id, filing_number, record)
            store.entity("ucc_lien_record", f"Connecticut UCC/lien {filing_number}",
                         external_id=str(filing_number) if filing_number else None,
                         attributes={"source_adapter": "ct_registry_municipality", "record": record,
                                     "property_collateral_status": "not_adjudicated"}, entity_id=entity_id)
            store.relationship(from_id=owner_id, relationship_type="named_debtor_in", to_id=entity_id,
                               fact_class="confirmed_official", confidence=0.9,
                               source_id=source_id, parser_version=PARSER_VERSION,
                               effective_date=str(record.get("dt_accept") or "")[:10] or None,
                               explanation={"limitation": "Debtor-name match does not establish that a subject property is collateral."})
            _link_related(store, owner_id, entity_id, "ucc_lien_record", owner_properties,
                          source_id, {"property_collateral_status": "not_adjudicated"}, totals)
            totals["active_ucc_records"] += 1
            totals["entities"] += 1
            totals["relationships"] += 1
            totals["output_profile"]["entity_types"]["ucc_lien_record"] += 1
            totals["output_profile"]["relationship_types"]["named_debtor_in"] += 1

    totals["output_profile"] = {
        key: dict(value) if isinstance(value, Counter) else value
        for key, value in totals["output_profile"].items()
    }
    return totals
