"""PennDOT project map service (MPMS). Structured, statewide, and delivered with alignment geometry."""
from __future__ import annotations

from typing import Any, Dict, Iterator, List

from ..http import Http

# Improvement types that can require new right-of-way. Everything else (resurfacing, painting,
# signs, guiderail...) is skipped at the source.
ROW_IMPROVEMENTS = [
    "New Roadway", "New Interchange", "Relocation/Realignment", "Add Lane", "Widen", "New Bridge",
    "Interchange Improvement", "Park and Ride Lot", "Intersection Improvement", "Add Turning Lane",
    "Reconstruct", "Bridge Replacement", "Corridor Safety Improvement", "Safety Improvement",
    "New Traffic Signal", "Transit System Improvement", "Intermodal Services", "Slides Correction",
    "Pedestrian Facilities", "Bicycle Facilities/Services", "Drainage Improvement",
]

PAGE = 1000


def _where(counties: List[str]) -> str:
    codes = ",".join("'%s'" % c.replace("'", "''") for c in ROW_IMPROVEMENTS)
    w = "IMPROVEMENT_CODE_DESC IN (%s)" % codes
    if counties:
        w += " AND COUNTY_NAME IN (%s)" % ",".join("'%s'" % c.upper().replace("'", "''") for c in counties)
    return w


def fetch(cfg: Dict[str, Any], http: Http) -> Iterator[Dict[str, Any]]:
    base = cfg["url"].rstrip("/")
    merged: Dict[str, Dict[str, Any]] = {}
    for layer in cfg.get("layers", []):
        offset = 0
        while True:
            data = http.get_json(
                "%s/%s/query" % (base, layer["id"]),
                {
                    "where": _where(cfg.get("counties") or []),
                    "outFields": "*",
                    "returnGeometry": "true",
                    "outSR": 4326,
                    "geometryPrecision": 6,
                    "orderByFields": "OBJECTID",
                    "resultOffset": offset,
                    "resultRecordCount": PAGE,
                    "f": "json",
                },
            )
            if "error" in data:
                raise RuntimeError("PennDOT layer %s: %s" % (layer["id"], data["error"]))
            feats = data.get("features") or []
            for f in feats:
                a = f.get("attributes") or {}
                pid = a.get("PROJ_ID")
                if pid is None:
                    continue
                key = str(pid)
                geom = f.get("geometry") or {}
                cur = merged.get(key)
                if cur is None:
                    cur = merged[key] = {"attrs": a, "program": layer["program"], "paths": [], "points": []}
                # One project can span several route segments: merge them into one geometry.
                cur["paths"].extend(geom.get("paths") or [])
                if "x" in geom and "y" in geom:
                    cur["points"].append([geom["x"], geom["y"]])
            if len(feats) < PAGE and not data.get("exceededTransferLimit"):
                break
            offset += len(feats)
    for key, m in merged.items():
        yield _to_signal(key, m)


def _to_signal(key: str, m: Dict[str, Any]) -> Dict[str, Any]:
    a = m["attrs"]
    if m["paths"]:
        geometry: Any = {"paths": m["paths"]}
    elif m["points"]:
        geometry = {"points": m["points"]}
    else:
        geometry = None
    keep = ("PROJ_ID", "IMPROVEMENT_CODE_DESC", "PROJECT_TITLE", "PROJ_STATUS_DESC", "PROJ_CLASS", "DISTRICT_NO",
            "PLANNING_PARTNER_NAME", "LET_DATE_DISPLAY", "TYP_APPR_IND", "TIP_APPR_IND", "STIP_APPR_IND",
            "ST_RT_NO", "COUNTY_NAME", "MUNICIPAL_CODE", "PRJCT_GRPHY_DC", "PRJCT_IMPRV_DC", "PHS_EST_COST_AM",
            "PRJCT_MNGR_CONTACT_NM", "PRJCT_MNGR_NO", "PRJCT_MNGR_EMAIL", "SEG_LNGTH_FEET")
    raw = {k: a.get(k) for k in keep}
    raw["program"] = m["program"]
    body = " | ".join(str(x) for x in (a.get("PRJCT_GRPHY_DC"), a.get("PRJCT_IMPRV_DC")) if x)
    return {
        "source": "penndot",
        "external_id": key,
        "title": "%s (MPMS %s)" % ((a.get("PROJECT_TITLE") or "Untitled project").strip(), key),
        "body": body or None,
        "url": "https://gis.penndot.gov/paprojects/",
        "agency": "PennDOT District %s" % (a.get("DISTRICT_NO") or "?"),
        "county": (a.get("COUNTY_NAME") or "").upper() or None,
        "municipality": None,
        "published": None,
        "geometry": geometry,
        "raw": raw,
    }
