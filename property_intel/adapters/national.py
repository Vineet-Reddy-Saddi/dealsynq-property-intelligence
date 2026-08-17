from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from typing import Any

from shapely.geometry import shape
from shapely.ops import nearest_points

from ..geometry import (M_TO_FT, SQM_TO_ACRES, SQM_TO_SQFT, SiteGeometry,
                        buffered_point, overlay_summary, project_to_site,
                        site_geometry)
from ..http_client import PublicHttpClient
from ..store import EvidenceStore
from ..util import normalize_address, normalize_text, stable_id

PARSER_VERSION = "national-public-sources/1.0.5"

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
EPQS_URL = "https://epqs.nationalmap.gov/v1/json"
NFHL_LAYER = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28"
NWI_LAYER = "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0"
SOIL_WFS = "https://sdmdataaccess.sc.egov.usda.gov/Spatial/SDMWGS84Geographic.wfs"
SOIL_SDA = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"
TIGER_ROADS = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/8"
ECHO_URL = "https://echodata.epa.gov/echo/echo_rest_services.get_facilities"
NRI_LAYER = "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/National_Risk_Index_Census_Tracts/FeatureServer/0"
USA_STRUCTURES = "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/USA_Structures_View/FeatureServer/0"


def fingerprint(config: dict[str, Any]) -> str:
    # A refresh bucket is deliberate: reruns within the bucket use cached raw bytes.
    from datetime import datetime, timezone
    days = max(1, int(config.get("refresh_days", 30)))
    bucket = int(datetime.now(timezone.utc).timestamp() // (days * 86400))
    return stable_id("input", PARSER_VERSION, config, bucket)


def _source(store: EvidenceStore, result: Any, name: str, authority: str,
            *, source_date: str | None = None, note: str | None = None) -> str:
    return store.source(name=name, url=result.url, authority=authority,
                        parser_version=PARSER_VERSION, raw_sha256=result.raw_sha256,
                        source_date=source_date, retrieved_at=result.retrieved_at,
                        access_note=note or "Public government endpoint; ordinary API request without authentication or access-control bypass.")


def _fact(store: EvidenceStore, target_id: str, source_id: str, raw_sha: str,
          category: str, predicate: str, value: Any, *, unit: str | None = None,
          confidence: float = 0.95, fact_class: str = "confirmed_official",
          freshness: int = 30, locator: str | None = None) -> None:
    store.fact(subject_id=target_id, category=category, predicate=predicate, value=value,
               unit=unit, fact_class=fact_class, confidence=confidence,
               source_id=source_id, parser_version=PARSER_VERSION, raw_sha256=raw_sha,
               freshness_days=freshness, evidence_locator=locator)


def _census(client: PublicHttpClient, store: EvidenceStore, target_id: str,
            target: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    result = client.fetch(CENSUS_URL, params={
        "address": target["address"], "benchmark": "Public_AR_Current",
        "vintage": "Current_Current", "format": "json",
    }, cache_days=config.get("refresh_days", 30))
    payload = result.json().get("result", {})
    matches = payload.get("addressMatches") or []
    source_id = _source(store, result, "U.S. Census Geocoder",
                        "official federal address and geography service")
    if not matches:
        store.gap(target_id, "identity", "missing", "Census address/geography match",
                  reason="The current Census geocoder returned no address match.", source_url=CENSUS_URL)
        return {"matched": False}
    match = matches[0]
    coordinates = match.get("coordinates") or {}
    geographies = match.get("geographies") or {}
    locator = "result.addressMatches[0]"
    _fact(store, target_id, source_id, result.raw_sha256, "identity", "geocoder_match",
          {"matched_address": match.get("matchedAddress"), "match_type": match.get("addressComponents", {}).get("matchType"),
           "tiger_line_id": (match.get("tigerLine") or {}).get("tigerLineId")}, locator=locator)
    input_zip = next(iter(re.findall(r"\b\d{5}(?:-\d{4})?\b", target["address"])), None)
    matched_zip = next(iter(re.findall(r"\b\d{5}(?:-\d{4})?\b", str(match.get("matchedAddress") or ""))), None)
    comparison = {"input_address": target["address"], "matched_address": match.get("matchedAddress"),
                  "input_street_normalized": normalize_address(target["address"].split(",")[0]),
                  "matched_street_normalized": normalize_address(str(match.get("matchedAddress") or "").split(",")[0]),
                  "input_postal_code": input_zip, "matched_postal_code": matched_zip,
                  "postal_code_agrees": not input_zip or not matched_zip or input_zip == matched_zip}
    _fact(store, target_id, source_id, result.raw_sha256, "identity", "geocoder_input_match_comparison",
          comparison, fact_class="calculation", confidence=1.0, locator=locator)
    discrepancy = "Census geocoder postal-code discrepancy"
    if input_zip and matched_zip and input_zip != matched_zip:
        store.gap(target_id, "identity", "partial", discrepancy,
                  reason=f"Input postal code {input_zip} differs from Census matched postal code {matched_zip}; street match is retained but the target address should be reviewed.",
                  source_url=result.url)
    else:
        store.resolve_gap(target_id, discrepancy, "Input and matched postal codes agree or one is unavailable.")
    _fact(store, target_id, source_id, result.raw_sha256, "identity", "geocoded_centroid",
          {"lat": coordinates.get("y"), "lon": coordinates.get("x")}, locator=f"{locator}.coordinates")
    selected: dict[str, Any] = {}
    for key in ("States", "Counties", "Census Tracts", "2020 Census Blocks", "Unified School Districts"):
        if geographies.get(key):
            row = geographies[key][0]
            selected[key] = {k: row.get(k) for k in ("GEOID", "NAME", "BASENAME", "CENTLAT", "CENTLON") if row.get(k) is not None}
    if selected:
        _fact(store, target_id, source_id, result.raw_sha256, "identity", "census_geographies",
              selected, locator=f"{locator}.geographies")
    store.resolve_gap(target_id, "Census address/geography match", "Resolved by current Census geocoder.")
    return {"matched": True, "lon": float(coordinates["x"]), "lat": float(coordinates["y"]),
            "geographies": selected}


def _analysis_site(store: EvidenceStore, target_id: str, geocode: dict[str, Any]) -> tuple[SiteGeometry | None, str]:
    site = site_geometry(store, target_id)
    if site:
        return site, "parcel_union"
    if geocode.get("matched"):
        return buffered_point(geocode["lon"], geocode["lat"], 25.0), "geocode_25m_screening_buffer"
    return None, "unavailable"


def _arcgis_query(client: PublicHttpClient, layer: str, site: SiteGeometry,
                  out_fields: str, cache_days: int, *, return_geometry: bool = True) -> Any:
    xmin, ymin, xmax, ymax = site.bounds
    geometry = {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
                "spatialReference": {"wkid": 4326}}
    return client.fetch(f"{layer}/query", params={
        "where": "1=1", "geometry": json.dumps(geometry, separators=(",", ":")),
        "geometryType": "esriGeometryEnvelope", "inSR": 4326, "outSR": 4326,
        "spatialRel": "esriSpatialRelIntersects", "outFields": out_fields,
        "returnGeometry": str(return_geometry).lower(), "f": "geojson" if return_geometry else "json",
    }, cache_days=cache_days)


def _flood(client: PublicHttpClient, store: EvidenceStore, target_id: str,
           site: SiteGeometry, basis: str, config: dict[str, Any]) -> dict[str, Any]:
    result = _arcgis_query(client, NFHL_LAYER, site, "FLD_ZONE,ZONE_SUBTY,SFHA_TF,STATIC_BFE,DEPTH", config.get("refresh_days", 30))
    features = result.json().get("features") or []
    summary = overlay_summary(site, features, ("FLD_ZONE", "ZONE_SUBTY", "SFHA_TF"))
    summary["analysis_basis"] = basis
    summary["screening_note"] = "NFHL zone intersection is a map screen; Zone X does not mean zero flood risk, and LOMA/LOMR or structure-elevation review may still be relevant."
    source_id = _source(store, result, "FEMA National Flood Hazard Layer",
                        "official federal flood hazard GIS")
    _fact(store, target_id, source_id, result.raw_sha256, "hazards", "nfhl_site_overlay",
          summary, confidence=0.96 if basis == "parcel_union" else 0.62,
          freshness=config.get("refresh_days", 30),
          locator="ArcGIS NFHL layer 28 features spatially intersected with analysis geometry")
    return summary


def _wetlands(client: PublicHttpClient, store: EvidenceStore, target_id: str,
              site: SiteGeometry, basis: str, config: dict[str, Any]) -> dict[str, Any]:
    result = _arcgis_query(client, NWI_LAYER, site,
                           "Wetlands.ATTRIBUTE,Wetlands.WETLAND_TYPE,Wetlands.ACRES",
                           config.get("refresh_days", 180))
    features = result.json().get("features") or []
    summary = overlay_summary(site, features, ("WETLAND_TYPE", "Wetlands.WETLAND_TYPE", "ATTRIBUTE", "Wetlands.ATTRIBUTE"))
    summary["analysis_basis"] = basis
    summary["screening_note"] = "No mapped NWI intersection is not proof of no jurisdictional wetland; field/regulatory delineation may differ."
    source_id = _source(store, result, "U.S. Fish and Wildlife Service National Wetlands Inventory",
                        "official federal wetlands GIS",
                        note="NWI mapped wetlands are screening evidence and not a regulatory wetland delineation.")
    _fact(store, target_id, source_id, result.raw_sha256, "environmental", "nwi_site_overlay",
          summary, confidence=0.88 if basis == "parcel_union" else 0.58,
          freshness=config.get("refresh_days", 180),
          locator="NWI Wetlands layer features spatially intersected with analysis geometry")
    return summary


def _parse_soil_gml(content: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(content)
    ns = {"gml": "http://www.opengis.net/gml", "ms": "http://mapserver.gis.umn.edu/mapserver"}
    features: list[dict[str, Any]] = []
    for member in root.findall(".//gml:featureMember", ns):
        item = next(iter(member), None)
        if item is None:
            continue
        props: dict[str, Any] = {}
        for child in item:
            name = child.tag.split("}")[-1]
            if name not in {"multiPolygon", "boundedBy"}:
                props[name] = child.text
        polygons = []
        for polygon in item.findall(".//gml:Polygon", ns):
            outer = polygon.find(".//gml:outerBoundaryIs//gml:coordinates", ns)
            if outer is None or not outer.text:
                continue
            # This official WFS emits latitude,longitude pairs despite EPSG:4326.
            ring = [[float(pair.split(",")[1]), float(pair.split(",")[0])]
                    for pair in outer.text.split()]
            holes = []
            for inner in polygon.findall(".//gml:innerBoundaryIs//gml:coordinates", ns):
                if inner.text:
                    holes.append([[float(pair.split(",")[1]), float(pair.split(",")[0])]
                                  for pair in inner.text.split()])
            polygons.append([ring, *holes])
        geometry = None
        if len(polygons) == 1:
            geometry = {"type": "Polygon", "coordinates": polygons[0]}
        elif polygons:
            geometry = {"type": "MultiPolygon", "coordinates": polygons}
        features.append({"type": "Feature", "geometry": geometry, "properties": props})
    return features


def _soils(client: PublicHttpClient, store: EvidenceStore, target_id: str,
           site: SiteGeometry, basis: str, config: dict[str, Any]) -> dict[str, Any]:
    xmin, ymin, xmax, ymax = site.bounds
    spatial = client.fetch(SOIL_WFS, params={
        "service": "WFS", "version": "1.1.0", "request": "GetFeature",
        "typeName": "mapunitpoly", "bbox": f"{xmin},{ymin},{xmax},{ymax}", "outputFormat": "GML2",
    }, cache_days=config.get("refresh_days", 180))
    features = _parse_soil_gml(spatial.content)
    overlay = overlay_summary(site, features, ("mukey",))
    mukeys = sorted({str(f.get("properties", {}).get("mukey")) for f in features
                     if f.get("properties", {}).get("mukey") and
                     not shape(f["geometry"]).intersection(site.geometry).is_empty})
    rows: list[dict[str, Any]] = []
    tabular_raw = spatial.raw_sha256
    tabular_url = spatial.url
    if mukeys:
        quoted = ",".join("'" + re.sub(r"[^0-9]", "", key) + "'" for key in mukeys)
        query = ("SELECT mu.mukey,mu.muname,mu.farmlndcl,co.compname,co.comppct_r,"
                 "co.hydricrating,co.drainagecl,co.slope_r,co.taxorder FROM mapunit mu "
                 "LEFT JOIN component co ON mu.mukey=co.mukey WHERE mu.mukey IN (" + quoted + ") "
                 "ORDER BY mu.mukey,co.comppct_r DESC")
        tabular = client.fetch(SOIL_SDA, method="POST",
                               json_body={"query": query, "format": "JSON+COLUMNNAME"},
                               cache_days=config.get("refresh_days", 180))
        table = tabular.json().get("Table") or []
        if table:
            rows = [dict(zip(table[0], row)) for row in table[1:]]
        tabular_raw = tabular.raw_sha256
        tabular_url = tabular.url
    summary = {**overlay, "analysis_basis": basis, "mapunit_keys": mukeys,
               "components": rows, "screening_note": "SSURGO map-unit interpretations are not a site-specific geotechnical investigation."}
    source_id = store.source(name="USDA NRCS SSURGO / Soil Data Access", url=tabular_url,
                             authority="official federal soil survey", parser_version=PARSER_VERSION,
                             raw_sha256=tabular_raw, retrieved_at=spatial.retrieved_at,
                             access_note="Official WFS polygons intersected with the analysis geometry; official SDA tabular component query.")
    _fact(store, target_id, source_id, tabular_raw, "environmental", "ssurgo_site_overlay",
          summary, confidence=0.9 if basis == "parcel_union" else 0.6,
          freshness=config.get("refresh_days", 180))
    return summary


def _elevation(client: PublicHttpClient, store: EvidenceStore, target_id: str,
               site: SiteGeometry, basis: str, config: dict[str, Any]) -> dict[str, Any]:
    lon, lat = site.centroid
    points = [(lon, lat)]
    if basis == "parcel_union":
        xmin, ymin, xmax, ymax = site.bounds
        points += [(xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)]
    samples = []
    raw_hashes = []
    last_result = None
    for x, y in points:
        result = client.fetch(EPQS_URL, params={"x": x, "y": y, "wkid": 4326,
                                               "units": "Feet", "includeDate": "false"},
                              cache_days=config.get("refresh_days", 365))
        payload = result.json()
        if payload.get("value") is not None:
            samples.append({"lon": x, "lat": y, "elevation_ft": round(float(payload["value"]), 2),
                            "resolution_m": payload.get("resolution")})
        raw_hashes.append(result.raw_sha256)
        last_result = result
    values = [s["elevation_ft"] for s in samples]
    summary = {"analysis_basis": basis, "samples": samples,
               "minimum_elevation_ft": min(values) if values else None,
               "maximum_elevation_ft": max(values) if values else None,
               "sampled_relief_ft": round(max(values) - min(values), 2) if values else None,
               "screening_note": "Point samples do not replace a topographic survey or full DEM slope analysis."}
    if last_result:
        raw = store.put_raw({"sample_raw_sha256": raw_hashes, "summary": summary})
        source_id = store.source(name="USGS National Map Elevation Point Query Service", url=last_result.url,
                                 authority="official federal elevation service", parser_version=PARSER_VERSION,
                                 raw_sha256=raw, retrieved_at=last_result.retrieved_at)
        _fact(store, target_id, source_id, raw, "terrain", "elevation_screening",
              summary, confidence=0.82 if basis == "parcel_union" else 0.6,
              freshness=config.get("refresh_days", 365))
    return summary


def _roads(client: PublicHttpClient, store: EvidenceStore, target_id: str,
           site: SiteGeometry, basis: str, config: dict[str, Any]) -> dict[str, Any]:
    result = _arcgis_query(client, TIGER_ROADS, site, "NAME,BASENAME,MTFCC,RTTYP",
                           config.get("refresh_days", 365))
    features = result.json().get("features") or []
    found = []
    for feature in features:
        if not feature.get("geometry"):
            continue
        road = project_to_site(site, shape(feature["geometry"]))
        distance = site.projected.distance(road)
        site_point, road_point = nearest_points(site.projected, road)
        found.append({"name": (feature.get("properties") or {}).get("NAME"),
                      "mtfcc": (feature.get("properties") or {}).get("MTFCC"),
                      "distance_ft": round(distance * M_TO_FT, 1),
                      "nearest_point_ft": round(site_point.distance(road_point) * M_TO_FT, 1)})
    found.sort(key=lambda row: row["distance_ft"])
    summary = {"analysis_basis": basis, "nearest_roads": found[:20],
               "screening_note": "TIGER road centerlines support proximity screening, not legal frontage or curb-cut confirmation."}
    source_id = _source(store, result, "U.S. Census TIGERweb Transportation",
                        "official federal road centerline GIS")
    _fact(store, target_id, source_id, result.raw_sha256, "access", "road_centerline_proximity",
          summary, confidence=0.78 if basis == "parcel_union" else 0.58,
          freshness=config.get("refresh_days", 365))
    return summary


def _echo(client: PublicHttpClient, store: EvidenceStore, target_id: str,
          site: SiteGeometry, basis: str, config: dict[str, Any]) -> dict[str, Any]:
    lon, lat = site.centroid
    radius = float(config.get("epa_radius_miles", 1))
    maximum = max(1, min(1000, int(config.get("epa_max_facilities", 100))))
    result = client.fetch(ECHO_URL, params={"output": "JSON", "p_lat": lat, "p_long": lon,
                                           "p_radius": radius, "responseset": maximum,
                                           "tablelist": "Y", "maplist": "Y", "summarylist": "Y"},
                          cache_days=config.get("refresh_days", 30))
    payload = result.json().get("Results", {})
    facilities = payload.get("Facilities") or []
    map_rows = ((payload.get("MapOutput") or {}).get("MapData") or [])
    coordinates = {str(row.get("PUV")): row for row in map_rows if row.get("PUV")}
    for row in facilities:
        mapped = coordinates.get(str(row.get("RegistryID"))) or {}
        row["FacLong"] = row.get("FacLong") or mapped.get("LON")
        row["FacLat"] = row.get("FacLat") or mapped.get("LAT")
        try:
            row["distance_miles"] = round(_haversine_miles(lat, lon, float(row.get("FacLat")), float(row.get("FacLong"))), 3)
        except (TypeError, ValueError):
            row["distance_miles"] = None
    facilities.sort(key=lambda row: row["distance_miles"] if row["distance_miles"] is not None else 1e9)
    facilities = facilities[:maximum]
    summary = {"analysis_basis": basis, "radius_miles": radius,
               "facility_count": int(payload.get("QueryRows") or len(facilities)),
               "facilities_returned": len(facilities),
               "facilities": [{k: row.get(k) for k in ("FacName", "FacStreet", "RegistryID", "FacComplianceStatus",
                                                          "AIRFlag", "TRIFlag", "RCRAComplianceStatus", "CWAComplianceStatus",
                                                          "FacDateLastInspection", "FacPenaltyCount", "FacLat", "FacLong", "distance_miles")}
                              for row in facilities],
               "screening_note": "ECHO absence is not proof of no environmental condition; state/local files and historic releases may differ."}
    source_id = _source(store, result, "U.S. EPA ECHO All Media Facility Search",
                        "official federal environmental compliance database")
    _fact(store, target_id, source_id, result.raw_sha256, "environmental", "echo_radius_search",
          summary, confidence=0.9, freshness=config.get("refresh_days", 30))
    return summary


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.7613 * 2 * math.asin(math.sqrt(a))


def _nri(client: PublicHttpClient, store: EvidenceStore, target_id: str,
         site: SiteGeometry, basis: str, config: dict[str, Any]) -> dict[str, Any]:
    lon, lat = site.centroid
    result = client.fetch(f"{NRI_LAYER}/query", params={
        "where": "1=1", "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
        "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false",
        "outFields": "*",
        "f": "json",
    }, cache_days=config.get("refresh_days", 90))
    payload = result.json()
    features = payload.get("features") or []
    attrs = features[0].get("attributes", {}) if features else {}
    hazards: dict[str, Any] = {}
    for key, value in attrs.items():
        if key.endswith("_AFREQ") and value not in (None, 0):
            prefix = key[:-6]
            hazards[prefix] = {"annualized_frequency": value, "risk_rating": attrs.get(prefix + "_RISKR")}
    summary = {"analysis_basis": "census_tract_at_site_centroid", "nri_id": attrs.get("NRI_ID"),
               "nri_version": attrs.get("NRI_VER"), "tract_fips": attrs.get("TRACTFIPS"),
               "county": attrs.get("COUNTY"), "risk_score": attrs.get("RISK_SCORE"),
               "risk_rating": attrs.get("RISK_RATNG"), "expected_annual_loss_score": attrs.get("EAL_SCORE"),
               "expected_annual_loss_rating": attrs.get("EAL_RATNG"),
               "social_vulnerability_rating": attrs.get("SOVI_RATNG"),
               "community_resilience_rating": attrs.get("RESL_RATNG"), "hazards": hazards,
               "screening_note": "FEMA NRI is a Census-tract community risk indicator, not a parcel-specific loss model."}
    source_id = _source(store, result, "FEMA National Risk Index Census Tracts",
                        "official federal natural-hazard risk dataset",
                        source_date=str(attrs.get("NRI_VER") or "current"))
    _fact(store, target_id, source_id, result.raw_sha256, "hazards", "fema_nri_tract_profile",
          summary, confidence=0.9, freshness=config.get("refresh_days", 90))
    return summary


def _structures(client: PublicHttpClient, store: EvidenceStore, target_id: str,
                site: SiteGeometry, basis: str, config: dict[str, Any]) -> dict[str, Any]:
    result = _arcgis_query(client, USA_STRUCTURES, site,
                           "BUILD_ID,OCC_CLS,PRIM_OCC,SEC_OCC,PROP_ADDR,HEIGHT,SQMETERS,SQFEET,H_ADJ_ELEV,L_ADJ_ELEV,PROD_DATE,SOURCE,IMAGE_DATE,VAL_METHOD",
                           config.get("refresh_days", 90))
    features = result.json().get("features") or []
    source_id = _source(store, result, "FEMA USA Structures",
                        "official federal public structure inventory",
                        note="Public USA Structures footprint service; building outlines and classifications are screening data, not a survey or assessor record.")
    old_ids = [r[0] for r in store.rows("SELECT entity_id FROM entities WHERE entity_type='building_footprint' AND external_id LIKE 'usa_structures:%'")]
    if old_ids:
        marks = ",".join("?" for _ in old_ids)
        store.db.execute(f"DELETE FROM facts WHERE subject_id IN ({marks})", tuple(old_ids))
        store.db.execute(f"DELETE FROM relationships WHERE from_entity_id IN ({marks}) OR to_entity_id IN ({marks})",
                         tuple(old_ids) + tuple(old_ids))
        store.db.execute(f"DELETE FROM entities WHERE entity_id IN ({marks})", tuple(old_ids))
    structures = []
    total_footprint = 0.0
    for feature in features:
        if not feature.get("geometry"):
            continue
        footprint = project_to_site(site, shape(feature["geometry"]))
        overlap = footprint.intersection(site.projected)
        if overlap.is_empty or overlap.area <= 0:
            continue
        props = feature.get("properties") or {}
        area_sqft = overlap.area * SQM_TO_SQFT
        total_footprint += area_sqft
        item = {"build_id": props.get("BUILD_ID"), "primary_occupancy": props.get("PRIM_OCC"),
                "secondary_occupancy": props.get("SEC_OCC"), "address": props.get("PROP_ADDR"),
                "height_m": props.get("HEIGHT"), "reported_sqft": props.get("SQFEET"),
                "site_intersection_sqft": round(area_sqft), "production_date": props.get("PROD_DATE"),
                "image_date": props.get("IMAGE_DATE"), "source": props.get("SOURCE"),
                "validation_method": props.get("VAL_METHOD")}
        structures.append(item)
        raw = store.put_raw({"properties": props, "geometry": feature["geometry"]})
        bid = store.entity("building_footprint", str(props.get("PROP_ADDR") or props.get("BUILD_ID") or "USA Structure"),
                           external_id=f"usa_structures:{props.get('BUILD_ID') or stable_id('structure', props, feature['geometry'])}",
                           attributes=item)
        store.relationship(from_id=bid, relationship_type="footprint_intersects", to_id=target_id,
                           fact_class="confirmed_official", confidence=0.88,
                           source_id=source_id, parser_version=PARSER_VERSION, raw_sha256=raw,
                           explanation={"analysis_basis": basis})
    summary = {"analysis_basis": basis, "footprint_count": len(structures),
               "total_site_intersection_sqft": round(total_footprint), "structures": structures,
               "screening_note": "Footprint area is not gross building area and may include outbuildings or imperfect classifications."}
    _fact(store, target_id, source_id, result.raw_sha256, "buildings", "usa_structures_footprints",
          summary, confidence=0.9 if basis == "parcel_union" else 0.6,
          freshness=config.get("refresh_days", 90))
    return summary


def collect(store: EvidenceStore, target_id: str, target: dict[str, Any],
            config: dict[str, Any]) -> dict[str, Any]:
    client = PublicHttpClient(store, timeout=int(config.get("timeout_seconds", 90)))
    stats: dict[str, Any] = {"successful": [], "failed": {}}
    try:
        geocode = _census(client, store, target_id, target, config)
        stats["geocoder"] = geocode
        stats["successful"].append("census_geocoder")
    except Exception as exc:
        geocode = {"matched": False}
        stats["failed"]["census_geocoder"] = str(exc)
        store.gap(target_id, "identity", "partial", "Census address/geography match", reason=str(exc), source_url=CENSUS_URL)
    site, basis = _analysis_site(store, target_id, geocode)
    stats["analysis_basis"] = basis
    if not site:
        store.gap(target_id, "national_sources", "missing", "National geospatial overlays",
                  reason="Neither parcel geometry nor a geocoded target point was available.")
        return stats
    snapshot = site.snapshot()
    raw = store.put_raw(snapshot)
    source_id = store.source(name="DealSynq geometry engine", url=None, authority="calculation",
                             parser_version=PARSER_VERSION, raw_sha256=raw,
                             access_note="Deterministic union/projection of sourced parcel geometry or documented geocode buffer.")
    store.db.execute(
        "UPDATE facts SET status='superseded' WHERE subject_id=? AND predicate='analysis_geometry' "
        "AND source_id IN (SELECT source_id FROM sources WHERE source_name='DealSynq geometry engine')",
        (target_id,),
    )
    _fact(store, target_id, source_id, raw, "parcels", "analysis_geometry_summary", snapshot,
          fact_class="calculation", confidence=0.98 if basis == "parcel_union" else 0.55,
          freshness=config.get("refresh_days", 30), locator=basis)
    collectors = {
        "fema_nfhl": _flood, "fws_nwi": _wetlands, "usda_ssurgo": _soils,
        "usgs_elevation": _elevation, "census_roads": _roads,
        "epa_echo": _echo, "fema_nri": _nri,
        "fema_usa_structures": _structures,
    }
    for name, collector in collectors.items():
        if config.get("sources", {}).get(name, True) is False:
            continue
        try:
            value = collector(client, store, target_id, site, basis, config)
            stats[name] = _compact(value)
            stats["successful"].append(name)
        except Exception as exc:
            stats["failed"][name] = str(exc)
            store.gap(target_id, "national_sources", "partial", f"National source {name} failed",
                      reason=str(exc))
    jurisdiction = store.db.execute(
        "SELECT jurisdiction_id FROM source_capabilities WHERE target_id=? LIMIT 1",
        (target_id,),
    ).fetchone()
    hazard_sources = {"fema_nfhl", "fws_nwi", "epa_echo", "fema_nri"}
    completed_hazards = hazard_sources.intersection(stats["successful"])
    hazard_status = "working" if completed_hazards == hazard_sources else "partial"
    store.register_capability(target_id, str(jurisdiction[0]) if jurisdiction else "national", {
        "capability": "environmental_hazards", "status": hazard_status,
        "source_name": "Official national public hazard sources",
        "source_url": "https://www.fema.gov/flood-maps/national-flood-hazard-layer",
        "adapter": "national_public",
        "reason": (
            "FEMA NFHL/NRI, FWS wetlands, and EPA facility screens completed."
            if hazard_status == "working" else
            f"Only {sorted(completed_hazards)} of the configured core hazard screens completed."
        ),
    }, PARSER_VERSION)
    if not stats["failed"]:
        store.resolve_gap(target_id, "National geospatial overlays", "All enabled national adapters completed.")
    return stats


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    """Keep run ledgers/CLI output useful while full evidence stays in facts/raw bytes."""
    hidden = {"facilities", "structures", "components", "nearest_roads", "samples", "hazards", "geometry"}
    result = {key: item for key, item in value.items() if key not in hidden}
    for key in hidden:
        item = value.get(key)
        if isinstance(item, list):
            result[f"{key}_count"] = len(item)
        elif isinstance(item, dict):
            result[f"{key}_count"] = len(item)
    if isinstance(value.get("nearest_roads"), list):
        result["nearest_named_roads"] = [r for r in value["nearest_roads"] if r.get("name")][:5]
    if isinstance(value.get("facilities"), list):
        result["nearest_facilities"] = value["facilities"][:5]
    return result
