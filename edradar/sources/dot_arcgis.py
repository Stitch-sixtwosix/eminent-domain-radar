"""Any state DOT's planned-project map service, described by configuration instead of code.

Each layer in the config names its state, its ArcGIS REST layer URL and which of its fields hold the
project's id, name, description, type of work, cost, status and letting date. Records arrive with
their geometry, so they can be matched to parcels the same way PennDOT's are.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

from ..http import Http

PAGE = 1000
MAX_RECORDS = 20000


def _first(attrs: Dict[str, Any], names: Any) -> Any:
    """A field mapping may be one name or a list to try in order."""
    for n in ([names] if isinstance(names, str) else list(names or [])):
        v = attrs.get(n)
        if v not in (None, "", " "):
            return v
    return None


def _money(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        n = float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return n if n > 0 else None


def _date(v: Any) -> Optional[str]:
    """ArcGIS dates are epoch milliseconds; some services publish text. Returns YYYY-MM-DD or the text."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and abs(v) > 1e11:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return str(v)[:40]


def to_signal(layer: Dict[str, Any], attrs: Dict[str, Any], geometry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    f = layer.get("fields") or {}
    pid = _first(attrs, f.get("id")) or attrs.get("OBJECTID") or attrs.get("objectid") or attrs.get("FID")
    if pid is None:
        return None
    name, desc, work = _first(attrs, f.get("name")), _first(attrs, f.get("description")), _first(attrs, f.get("work_type"))
    status, let = _first(attrs, f.get("status")), _date(_first(attrs, f.get("let_date")))
    title = str(name or desc or work or "Project %s" % pid).strip()
    body = ". ".join(str(x).strip() for x in (work, desc if desc != name else None,
                                              "Status: %s" % status if status else None, "Let: %s" % let if let else None) if x)
    county = _first(attrs, f.get("county"))
    return {
        "source": "%s:%s" % (layer.get("source_prefix") or "dot", layer["state"].upper()), "external_id": "%s:%s" % (layer.get("key") or layer["name"][:24], pid),
        "title": title[:160], "body": body[:4000], "url": layer.get("info_url") or layer["url"],
        "agency": layer.get("agency") or layer["name"], "state": layer["state"].upper(),
        "county": str(county).upper().replace(" COUNTY", "").strip() if county else None,
        "published": None, "geometry": geometry or None,
        "raw": dict({k: v for k, v in attrs.items() if v is not None},
                    _work_type=str(work) if work else None, _status=str(status) if status else None,
                    _cost=_money(_first(attrs, f.get("cost"))), _let=let, _stage=layer.get("stage"), _likelihood=layer.get("likelihood")),
    }


def fetch_layer(layer: Dict[str, Any], http: Http) -> Iterator[Dict[str, Any]]:
    url, offset, seen = layer["url"].rstrip("/"), 0, 0
    while seen < int(layer.get("max_records") or MAX_RECORDS):
        data = http.get_json(url + "/query", {
            "where": layer.get("where") or "1=1", "outFields": "*", "returnGeometry": "true", "outSR": 4326,
            "geometryPrecision": 6, "resultOffset": offset, "resultRecordCount": PAGE, "f": "json"})
        if "error" in data:
            raise RuntimeError("%s: %s" % (layer["name"], data["error"]))
        feats = data.get("features") or []
        for feat in feats:
            s = to_signal(layer, feat.get("attributes") or {}, feat.get("geometry"))
            if s:
                seen += 1
                yield s
        if not feats or not (data.get("exceededTransferLimit") or len(feats) >= PAGE):
            return
        offset += len(feats)


READS: List[Dict[str, Any]] = []        # what each list answered on the latest run: the pipeline stores it for the status screen


def fetch(cfg: Dict[str, Any], http: Http) -> Iterator[Dict[str, Any]]:
    only: List[str] = [s.upper() for s in cfg.get("states") or []]
    del READS[:]
    for layer in cfg.get("layers") or []:
        if only and layer["state"].upper() not in only:
            continue
        read = {"source": "%s:%s" % (layer.get("source_prefix") or "dot", layer["state"].upper()), "key": layer.get("key") or layer["name"][:24],
                "records": 0, "error": None}
        try:
            for s in fetch_layer(layer, http):
                read["records"] += 1
                yield s
        except Exception as e:                      # one state's server being down must not stop the others
            read["error"] = str(e)[:300]
            print("  %s (%s) failed: %s" % (layer["name"], layer["state"], str(e)[:140]))
        READS.append(read)
