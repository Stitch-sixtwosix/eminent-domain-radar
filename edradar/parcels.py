"""Find the parcels around a project geometry. Two providers:

  arcgis     any ArcGIS REST polygon layer (county assessor layers, PASDA statewide fallback)
  carto_phl  Philadelphia Office of Property Assessment (owner + mailing address + market value)

Each provider returns dicts: {apn, county, address, owner, mailing_address, market_value, acres,
distance_m, abuts, attributes}.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import geo
from .http import Http


def pick_layer(layers: List[Dict[str, Any]], county: Optional[str], state: Optional[str] = "PA") -> Optional[Dict[str, Any]]:
    """The first layer for this state that covers the county; a layer with no counties covers its whole state."""
    county, state = (county or "").upper(), (state or "PA").upper()
    for layer in layers:
        if (layer.get("state") or "PA").upper() != state:
            continue
        wanted = [c.upper() for c in layer.get("counties") or []]
        wanted += [c[:-7] for c in wanted if c.endswith(" COUNTY")]      # "ST. LOUIS COUNTY" also answers to "ST. LOUIS"
        if not wanted or county in wanted:
            return layer
    return None


def normalize_apn(county: Optional[str], apn: str) -> str:
    """Convert a parcel ID as written in legislation to the form the county's GIS layer uses.

    Allegheny County writes lot-and-block IDs as 39-F-280 (block-letter-lot[-unit[-sub]]) but
    stores them as 16 characters: 0039F00280000000.
    """
    if (county or "").upper() == "ALLEGHENY":
        parts = apn.strip().upper().split("-")
        if 3 <= len(parts) <= 5 and parts[0].isdigit() and parts[2].isdigit():
            unit = parts[3] if len(parts) > 3 else "0"
            sub = parts[4] if len(parts) > 4 else "0"
            if unit.isdigit() and sub.isdigit():
                return "%04d%s%05d%04d%02d" % (int(parts[0]), parts[1], int(parts[2]), int(unit), int(sub))
    return apn.strip()


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- ArcGIS
def _field(attrs: Dict[str, Any], spec: Any, sep: str) -> Optional[str]:
    """Read one field, or join several (a list in the config) while skipping blanks and repeats."""
    names = spec if isinstance(spec, list) else [spec]
    parts: List[str] = []
    for n in names:
        v = attrs.get(n)
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        v = " ".join(str(v).split()) if v is not None else ""
        if v and v not in ("0", "None", "<Null>") and v not in parts:
            parts.append(v)
    out = sep.join(parts)
    if "&" in sep:  # "A &" + "B" would otherwise read "A & & B"
        out = " & ".join(x.strip() for x in out.split("&") if x.strip())
    return out or None


def _arcgis_row(layer: Dict[str, Any], attrs: Dict[str, Any], abuts: bool) -> Optional[Dict[str, Any]]:
    f = layer.get("fields") or {}
    apn = _field(attrs, f["apn"], "-") if f.get("apn") else None
    if not apn:
        return None

    def get(key: str, sep: str = ", ") -> Optional[str]:
        return _field(attrs, f[key], sep) if f.get(key) else None

    acres = _num(get("acres"))
    if acres is not None:
        acres *= float(layer.get("acres_multiplier", 1))
    county = get("county") or (layer["counties"][0] if len(layer.get("counties") or []) == 1 else None)
    return {"apn": apn, "county": county.upper() if county else None,
            "address": get("address", " "), "owner": get("owner", f.get("owner_sep", " & ")),
            "mailing_address": get("mailing_address"), "market_value": _num(get("market_value")),
            "acres": acres, "distance_m": None, "abuts": abuts, "attributes": attrs}


def _arcgis_query(layer: Dict[str, Any], http: Http, extra: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    params = dict({"outFields": "*", "returnGeometry": "false", "resultRecordCount": limit, "f": "json"}, **extra)
    url = layer["url"].rstrip("/") + "/query"
    data = http.post_json(url, params)
    if "error" in data and "resultRecordCount" in params:   # older servers, and Florida's statewide file with a
        del params["resultRecordCount"]                     # spatial filter, reject a record limit
        data = http.post_json(url, params)
    if "error" in data:
        raise RuntimeError("%s: %s" % (layer["name"], data["error"]))
    return [f.get("attributes") or {} for f in data.get("features") or []]


FEET_PER_METER = 3.28084


def reach(layer: Dict[str, Any], http: Http) -> float:
    """How far a layer really searches for each meter asked.

    Layers hosted on ArcGIS Online and stored in feet search about 3.28 times too far when given a distance in
    meters (measured on Williamson and Collin, Texas, on 2026-11-17; a county's own server in feet was correct).
    The layer's units are worked out by comparing its extent in its own coordinates with the same extent in
    degrees. Checked once per layer per run; any failure leaves the distance alone.
    """
    if "_reach" in layer:
        return layer["_reach"]
    factor = 1.0
    try:
        url = layer["url"].rstrip("/")
        if ".arcgis.com/" in url.lower():
            own = (http.get_json(url, {"f": "json"}).get("extent") or {})
            deg = (http.get_json(url + "/query", {"where": "1=1", "returnExtentOnly": "true", "outSR": 4326,
                                                  "f": "json"}).get("extent") or {})
            factor = reach_factor(own, deg)
    except Exception:
        factor = 1.0
    layer["_reach"] = factor
    return factor


def reach_factor(own: Dict[str, Any], deg: Dict[str, Any]) -> float:
    """3.28 when the layer's own coordinates are feet, otherwise 1."""
    try:
        meters = (float(deg["ymax"]) - float(deg["ymin"])) * 111320.0
        units = float(own["ymax"]) - float(own["ymin"])
    except (KeyError, TypeError, ValueError):
        return 1.0
    if meters <= 0 or units <= 0:
        return 1.0
    return FEET_PER_METER if 2.6 <= units / meters <= 4.0 else 1.0


def _arcgis_spatial(layer, http, geometry, distance_m, limit):
    distance_m = round(distance_m / reach(layer, http), 2)
    g = geo.thin(geometry)
    if geo.is_linear(g):
        gtype, gjson = "esriGeometryPolyline", {"paths": g["paths"], "spatialReference": {"wkid": 4326}}
    else:
        gtype, gjson = "esriGeometryMultipoint", {"points": geo.coords(g), "spatialReference": {"wkid": 4326}}
    return _arcgis_query(layer, http, {
        "where": "1=1", "geometry": json.dumps(gjson), "geometryType": gtype, "inSR": 4326,
        # outSR is needed even though no geometry is returned: without it the Florida and Connecticut statewide
        # files reject a distance search ("The spatial reference identifier (SRID) is not valid").
        "outSR": 4326,
        "spatialRel": "esriSpatialRelIntersects", "distance": distance_m, "units": "esriSRUnit_Meter"}, limit)


def arcgis_near(layer, http, geometry, m_cfg) -> List[Dict[str, Any]]:
    limit = int(m_cfg["max_parcels_per_project"])
    out: Dict[str, Dict[str, Any]] = {}
    if geo.is_linear(geometry):
        rings = [(float(m_cfg["abut_m"]), True), (float(m_cfg["buffer_m"]), False)]
    else:
        rings = [(float(m_cfg["point_buffer_m"]), False)]
    for dist, abuts in rings:
        for attrs in _arcgis_spatial(layer, http, geometry, dist, limit):
            row = _arcgis_row(layer, attrs, abuts)
            if row and row["apn"] not in out:
                out[row["apn"]] = row
    return list(out.values())


def arcgis_by_apn(layer, http, apns: List[str]) -> List[Dict[str, Any]]:
    field = (layer.get("fields") or {}).get("apn")
    if not field or isinstance(field, list) or not apns:
        return []
    quoted = ",".join("'%s'" % a.replace("'", "''") for a in apns[:100])
    rows = _arcgis_query(layer, http, {"where": "%s IN (%s)" % (field, quoted)}, 200)
    return [r for r in (_arcgis_row(layer, a, True) for a in rows) if r]


# ---------------------------------------------------------------- Philadelphia (Carto SQL API)
PHL_COLS = ("parcel_number, location, owner_1, owner_2, mailing_care_of, mailing_street, mailing_city_state, "
            "mailing_zip, market_value, total_area, category_code_description, zoning, year_built, sale_date, sale_price, "
            "taxable_land, taxable_building, total_livable_area, building_code_description")


def _phl_row(r: Dict[str, Any], abut_m: float) -> Dict[str, Any]:
    owner = " & ".join(x.strip() for x in (r.get("owner_1"), r.get("owner_2")) if x and x.strip())
    mailing = ", ".join(str(x).strip() for x in (r.get("mailing_care_of"), r.get("mailing_street"),
                                                  r.get("mailing_city_state"), r.get("mailing_zip"))
                        if x and str(x).strip())
    sqft = _num(r.get("total_area"))
    d = _num(r.get("d"))
    return {"apn": str(r["parcel_number"]), "county": "PHILADELPHIA", "address": r.get("location"),
            "owner": owner or None, "mailing_address": mailing or None,
            "market_value": _num(r.get("market_value")), "acres": sqft / 43560.0 if sqft else None,
            "distance_m": d, "abuts": d is not None and d <= abut_m,
            "attributes": {"category": r.get("category_code_description"), "zoning": r.get("zoning"),
                           "building_type": r.get("building_code_description"), "year_built": r.get("year_built"),
                           "sale_date": (r.get("sale_date") or "")[:10] or None, "sale_price": r.get("sale_price"),
                           "land_value": r.get("taxable_land"), "building_value": r.get("taxable_building"),
                           "lot_square_feet": r.get("total_area"), "livable_square_feet": r.get("total_livable_area")}}


def phl_near(layer, http, geometry, m_cfg) -> List[Dict[str, Any]]:
    linear = geo.is_linear(geometry)
    radius = float(m_cfg["buffer_m"] if linear else m_cfg["point_buffer_m"])
    gj = json.dumps(geo.to_geojson(geo.thin(geometry))).replace("'", "")
    sql = ("WITH g AS (SELECT ST_SetSRID(ST_GeomFromGeoJSON('%s'),4326)::geography AS geog) "
           "SELECT %s, ST_Distance(p.the_geom::geography, g.geog) AS d FROM opa_properties_public p, g "
           "WHERE ST_DWithin(p.the_geom::geography, g.geog, %s) ORDER BY d LIMIT %d"
           % (gj, PHL_COLS, radius, int(m_cfg["max_parcels_per_project"])))
    data = http.post_json(layer["url"], {"q": sql})
    if "error" in data:
        raise RuntimeError("Philadelphia OPA: %s" % data["error"])
    # OPA geometries are points on the lot, so allow a lot-depth margin when deciding what abuts.
    abut = float(m_cfg["abut_m"]) + 15 if linear else 0
    return [_phl_row(r, abut) for r in data.get("rows") or []]


def phl_by_apn(layer, http, apns: List[str]) -> List[Dict[str, Any]]:
    ids = [a for a in apns if a.isdigit()][:100]
    if not ids:
        return []
    sql = "SELECT %s FROM opa_properties_public WHERE parcel_number IN (%s)" % (
        PHL_COLS, ",".join("'%s'" % a for a in ids))
    data = http.post_json(layer["url"], {"q": sql})
    return [dict(_phl_row(r, 0), abuts=True) for r in data.get("rows") or []]


def near(layer, http, geometry, m_cfg) -> List[Dict[str, Any]]:
    return (phl_near if layer.get("provider") == "carto_phl" else arcgis_near)(layer, http, geometry, m_cfg)


def by_apn(layer, http, apns: List[str]) -> List[Dict[str, Any]]:
    return (phl_by_apn if layer.get("provider") == "carto_phl" else arcgis_by_apn)(layer, http, apns)
