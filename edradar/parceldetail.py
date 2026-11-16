"""Everything behind the parcel panel: exact boundary, land background, related parcels, similar cases."""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from .http import Http

# ---------------------------------------------------------------- boundary
def _ring_metrics(rings: List[List[List[float]]]) -> Dict[str, Any]:
    """Area, perimeter and side lengths of a lon/lat polygon, using a local flat projection (fine at parcel scale)."""
    pts = [p for r in rings for p in r]
    lat0 = math.radians(sum(p[1] for p in pts) / len(pts))
    kx, ky = 111320.0 * math.cos(lat0), 110540.0
    area = perimeter = 0.0
    sides: List[Dict[str, Any]] = []
    for ring in rings:
        xy = [(p[0] * kx, p[1] * ky) for p in ring]
        a = 0.0
        for (x1, y1), (x2, y2), p1, p2 in zip(xy, xy[1:], ring, ring[1:]):
            a += x1 * y2 - x2 * y1
            d = math.hypot(x2 - x1, y2 - y1)
            perimeter += d
            sides.append({"feet": d * 3.28084, "mid": [(p1[1] + p2[1]) / 2, (p1[0] + p2[0]) / 2]})
        area += a / 2.0   # outer rings and holes wind in opposite directions, so holes subtract
    sides.sort(key=lambda s: -s["feet"])
    return {"acres": abs(area) / 4046.8564, "square_feet": abs(area) * 10.7639, "perimeter_feet": perimeter * 3.28084,
            "sides": [{"feet": round(s["feet"]), "mid": s["mid"]} for s in sides[:8] if s["feet"] >= 15]}


def _close(ring: List[List[float]]) -> List[List[float]]:
    return ring if ring and ring[0] == ring[-1] else ring + ring[:1]


def fetch_boundary(layer: Dict[str, Any], parcel: Dict[str, Any], http: Http) -> Optional[List[List[List[float]]]]:
    """The parcel polygon as lon/lat rings, straight from the county layer the parcel came from."""
    if layer.get("provider") == "carto_phl":
        if not str(parcel["apn"]).isdigit():
            return None
        data = http.post_json(layer["url"], {
            "q": "SELECT ST_AsGeoJSON(the_geom, 6) g FROM pwd_parcels WHERE brt_id='%s' LIMIT 1" % parcel["apn"]})
        rows = data.get("rows") or []
        if not rows or not rows[0].get("g"):
            return None
        geo = json.loads(rows[0]["g"])
        polys = geo["coordinates"] if geo["type"] == "MultiPolygon" else [geo["coordinates"]]
        return [_close(r) for poly in polys for r in poly]
    field = (layer.get("fields") or {}).get("apn")
    if not field or isinstance(field, list):
        return None
    attrs = parcel.get("attributes") or {}
    raw = attrs.get(field, parcel["apn"])           # the county's own spelling, padding included
    quoted = str(raw).replace("'", "''")
    where = "%s = %s" % (field, raw) if isinstance(raw, (int, float)) else "%s = '%s'" % (field, quoted)
    county_field = (layer.get("fields") or {}).get("county")
    if county_field and attrs.get(county_field):   # the statewide layer reuses IDs across counties
        where += " AND %s = '%s'" % (county_field, str(attrs[county_field]).replace("'", "''"))
    url = layer["url"].rstrip("/") + "/query"
    base = {"outFields": field, "returnGeometry": "true", "outSR": 4326, "geometryPrecision": 6, "f": "json"}

    def rings_of(data: Dict[str, Any], check: bool) -> List[List[List[float]]]:
        if "error" in data:
            raise RuntimeError(str(data["error"].get("message") or data["error"]))
        feats = data.get("features") or []
        if check:   # object IDs are reassigned when a county republishes, so confirm it is still this parcel
            feats = [f for f in feats if str((f.get("attributes") or {}).get(field)).strip() == str(raw).strip()]
        return [_close(r) for f in feats for r in (f.get("geometry") or {}).get("rings") or []]

    # Large layers answer an object-ID lookup instantly but can take a minute to scan for a text ID.
    oid = next((attrs[k] for k in ("OBJECTID", "OBJECTID_1", "FID", "objectid") if isinstance(attrs.get(k), int)), None)
    if oid is not None:
        rings = rings_of(http.post_json(url, dict(base, objectIds=oid)), check=True)   # a server that cannot answer this
        if rings:                                                                      # will not answer the scan either
            return rings
    return rings_of(http.post_json(url, dict(base, where=where)), check=False) or None


def boundary(cfg: Dict[str, Any], db: Any, parcel: Dict[str, Any]) -> Dict[str, Any]:
    """Cached after the first look, so reopening a parcel is instant and works offline."""
    if parcel.get("geometry"):
        rings = json.loads(parcel["geometry"])
    else:
        layer = next((x for x in cfg.get("parcel_layers") or [] if x.get("name") == parcel["layer"]), None)
        if layer is None:
            return {"rings": None, "why": "The parcel layer this came from is no longer configured."}
        try:
            rings = fetch_boundary(layer, parcel, Http(cfg["user_agent"], timeout=20, retries=1))
        except Exception as e:
            return {"rings": None, "why": "The parcel map server did not answer in time (%s). Close and reopen the parcel "
                                          "to try again." % str(e)[:80]}
        if not rings:
            return {"rings": None, "why": "The county layer has no boundary for this parcel ID."}
        db.conn.execute("UPDATE parcels SET geometry=? WHERE id=?", (json.dumps(rings), parcel["id"]))
        db.conn.commit()
    return dict(_ring_metrics(rings), rings=rings, why=None)


# ---------------------------------------------------------------- land background
# (label, pattern over the county's own field names). The first non-empty match wins.
FACTS: List[Tuple[str, str]] = [
    ("Land use", r"^(land_?use(?!.*c(o)?d)|landuse(?!cd)|luc_?desc|category|prop_?use|property_t|land_type|class(_desc)?|pardat_cla|bldg_desc)"),
    ("Land use code", r"^(land_?use_?c|landusecd|luc$|lu_?code|land_code|py_used_co)"),
    ("Zoning", r"zon"),
    ("Municipality", r"^(muni(?!code)|municipali|township|twp|dist_name|city_name|district_n(ame)?$|municipal_n)"),
    ("School district", r"^(school|sch_?dist|schooldesc)"),
    ("Deeded acres", r"^(deed_?ac|deeded_acr|acre|calc_?ac|land_acres|total_acre|gis_?acres|statedarea|map_acres)"),
    ("Year built", r"^(year_?built|yrblt|yr_built)"),
    ("Land value", r"^(land_?val|lndval|valuland$|cur_land|asmtland|current_land|landassess|new_mv_lan|landvalue)"),
    ("Building value", r"^(bldg_?val|building(_v)?|improv_val|valubldg|cur_improve|asmtbldg|bldgassess|blding_val|new_mv_bld)"),
    ("Last sale date", r"^(sale_?date|saledate|date_of_sa|lastsaleda|deed_date|sdate)"),
    ("Last sale price", r"^(sale_?price|saleprice|price|considerat|lastsalepr|deedamount|sale_amount|sprice)"),
    ("Deed book / page", r"^(deed_?b(oo)?k|book|deedbook|deed_ref|instrument|instruno)"),
    ("Legal description", r"^(legal|prop_desc|descr1|property_desc|land_desc)"),
    ("Clean and Green", r"^(clean|clngreen|cng$|c_g_status)"),
    ("Utilities", r"^(util|sewer|water$)"),
]
SKIP = re.compile(r"^(shape|objectid|globalid|fid|created|last_edit|gis_|x_?coord|y_?coord|xlon|ylat|longitude|latitude)", re.I)


def _show(key: str, v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (int, float)) and "date" in key.lower() and abs(v) > 1e11:   # Esri epoch milliseconds
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%b %-d, %Y")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    text = " ".join(str(v).split())
    return text if text and text not in ("0", "None", "<Null>", "N/A") else None


def background(parcel: Dict[str, Any]) -> Dict[str, Any]:
    attrs = parcel.get("attributes") or {}
    facts, used = [], set()
    for label, pattern in FACTS:
        rx = re.compile(pattern, re.I)
        for key, value in attrs.items():
            shown = _show(key, value)
            if key not in used and shown and rx.search(key):
                if "date" in label.lower():
                    if re.fullmatch(r"\d{12,13}", shown):          # epoch milliseconds under a truncated field name
                        shown = datetime.fromtimestamp(int(shown) / 1000, tz=timezone.utc).strftime("%b %-d, %Y")
                    elif re.fullmatch(r"(19|20)\d{6}", shown):      # 20260828
                        shown = datetime.strptime(shown, "%Y%m%d").strftime("%b %-d, %Y")
                if re.search(r"value|price", label, re.I) and re.fullmatch(r"\d+(\.\d+)?", shown):
                    shown = "$" + format(int(float(shown)), ",")
                facts.append({"label": label, "value": shown})
                used.add(key)
                break
    record = [{"field": k, "value": _show(k, v)} for k, v in attrs.items() if not SKIP.search(k) and _show(k, v)]
    return {"facts": facts, "record": record[:80]}


def related(db: Any, lead: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Other parcels in the lead list held by the same owner of record."""
    if not lead.get("owner"):
        return []
    return db.rows(
        """SELECT l.id, l.score, pa.apn, pa.address, pa.county, pa.market_value, p.name AS project
           FROM parcels pa JOIN leads l ON l.parcel_id = pa.id JOIN projects p ON p.id = l.project_id
           WHERE pa.owner = ? AND l.id != ? ORDER BY l.score DESC LIMIT 12""", (lead["owner"], lead["id"]))


# ---------------------------------------------------------------- similar cases (CourtListener)
PA_COURTS = "pa pacommwct pasuperct"
TYPE_TERMS = {"highway": "highway widening", "bridge": "bridge replacement", "interchange": "interchange",
              "transit": "transit", "rail": "railroad", "pipeline": "pipeline easement",
              "transmission": '"transmission line" easement', "water": "sewer OR water easement",
              "utility": "utility easement", "redevelopment": "redevelopment authority blight", "airport": "airport"}


def case_query(lead: Dict[str, Any]) -> str:
    terms = ['"eminent domain"']
    source = lead.get("source") or ""
    if source == "penndot":
        terms.append('"Department of Transportation"')
    elif "redevelopment" not in (lead.get("project_type") or "") and source.startswith("legistar"):
        terms.append("condemnation")
    terms.append(TYPE_TERMS.get(lead.get("project_type") or "", "condemnation"))
    if lead.get("match_reason") in ("abuts_alignment", "within_buffer"):
        terms.append('"partial taking" OR "just compensation"')
    return " ".join(terms)


def cases(lead: Dict[str, Any], client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    q = case_query(lead)
    out: Dict[str, Any] = {
        "query": q, "results": [],
        "search_url": "https://www.courtlistener.com/?" + urlencode({"q": q, "type": "o", "court": PA_COURTS,
                                                                    "order_by": "score desc"}),
        "why": None,
    }
    token = os.environ.get("COURTLISTENER_TOKEN")
    if not token:
        out["why"] = "Set COURTLISTENER_TOKEN (a free CourtListener account) to list matching opinions here."
        return out
    client = client or httpx.Client(timeout=30)
    try:
        r = client.get("https://www.courtlistener.com/api/rest/v4/search/",
                       params={"q": q, "type": "o", "court": PA_COURTS, "order_by": "score desc"},
                       headers={"Authorization": "Token " + token})
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        out["why"] = "CourtListener did not answer (%s)." % str(e)[:100]
        return out
    for c in (body.get("results") or [])[:6]:
        cite = c.get("citation") or []
        snippet = ((c.get("opinions") or [{}])[0].get("snippet") or "").replace("<mark>", "").replace("</mark>", "")
        out["results"].append({
            "name": c.get("caseName"), "court": c.get("court"), "date": c.get("dateFiled"),
            "citation": cite[0] if isinstance(cite, list) and cite else (cite or None),
            "url": "https://www.courtlistener.com" + (c.get("absolute_url") or ""),
            "snippet": " ".join(snippet.split())[:260],
        })
    return out
