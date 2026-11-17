#!/usr/bin/env python3
"""Turn researched project sources (research/sources/*.json) into layers the tool reads.

Kept: verified ArcGIS layers with geometry that belong to exactly one state. Each source's "planned only"
filter is tried against the live service; if the service rejects it, the layer is kept without a filter and
marked unfiltered so its results can be reviewed. National layers need a state per record and are listed
as skipped for now.
"""
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, DST = ROOT / "research" / "sources", ROOT / "edradar" / "data" / "sources"
STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD "
             "TN TX UT VT VA WA WV WI WY".split())


# Reviewed by hand: the filter that keeps only work still ahead, taken from each source's own status values.
# "drop" leaves a layer out; "likelihood" marks layers that list parcels the agency is actually acquiring.
REVIEW = {
    "TxDOT Right of Way - Proposed Parcels": {"likelihood": 100, "where": "PARCEL_STATUS = 'Active' AND PARCEL_STAGE IN ('New','Created','Appraised',"
        "'Appraisal Reviewed','Appraisal Approved','Initial Offer','Final Offer','Pre-Litigation','Special Commissioner Phase','Trial Phase')"},
    "UDOT Right of Way parcels": {"likelihood": 100, "where": "PROPERTY_STATUS IN ('Acquisition','Condemnation','Hold For Project','Relocation','Right of Entry')"},
    "ODOT (Ohio) STIP Projects": {"where": "CURRENT_STIP_PERIOD_IND = 'Y'"},
    "GDOT GeoPI Project Hub": {"where": "STATUS IN ('Construction Work Program','Long Range Program') AND GEOMETRY_SOURCE = 'TPRO Web'"},
    "PennDOT Transportation Projects": {"drop": "Pennsylvania already has its own PennDOT reader; loading this too would duplicate every project"},
    "NCDOT Right of Way GIS": {"drop": "no status or date; needs joining to the STIP before it can be filtered to current projects"},
    "LA Metro": {"likelihood": 100},
    "Charlotte Right-of-Way Tracking": {"likelihood": 90},
    "Proposed ROW": {"likelihood": 95}, "Proposed Right of Way": {"likelihood": 95},
    "Land Acquisition": {"likelihood": 100},
}


def review_for(name: str) -> dict:
    return next((v for k, v in REVIEW.items() if k.lower() in name.lower()), {})


def count(url: str, where: str) -> int:
    q = urllib.parse.urlencode({"where": where, "returnCountOnly": "true", "f": "json"})
    req = urllib.request.Request(url.rstrip("/") + "/query?" + q, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=40) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    if "error" in data:
        raise RuntimeError(str(data["error"])[:120])
    return int(data.get("count", 0))


def looks_like_sql(text: str) -> bool:
    return bool(text) and len(text) < 300 and bool(re.search(r"\b\w+\s*(=|<>|>=|<=|>|<|\bIN\b|\bLIKE\b|\bIS\b)", text, re.I)) \
        and not re.search(r"\b(use|filter|select only|records where|means|e\.g\.)\b", text, re.I)


def main() -> None:
    layers, skipped = [], []
    for f in sorted(SRC.glob("*.json")):
        try:
            doc = json.loads(f.read_text())
        except ValueError as e:
            print("unreadable %s: %s" % (f.name, e)); continue
        for i, s in enumerate(doc.get("sources") or []):
            name = s.get("name") or "source %d" % i
            states = [x for x in (s.get("states") or []) if x in STATES]
            if not s.get("verified") or s.get("kind") != "arcgis" or (s.get("geometry") or "none") == "none" or not s.get("fields"):
                skipped.append((name, "not a verified map layer")); continue
            if len(states) != 1 or len(s.get("states") or []) != 1:
                skipped.append((name, "covers several states; needs a state per record")); continue
            if s.get("restrictions"):
                skipped.append((name, "has a recorded use restriction; needs approval")); continue
            rev = review_for(name)
            if rev.get("drop"):
                skipped.append((name, rev["drop"])); continue
            where, unfiltered = None, True
            try:
                total = count(s["url"], "1=1")
                cand = rev.get("where") or (s.get("planned_filter") or "").strip()
                if looks_like_sql(cand):
                    try:
                        n = count(s["url"], cand)
                        if 0 < n <= total:
                            where, unfiltered = cand, False
                    except Exception:
                        pass
            except Exception as e:
                skipped.append((name, "did not answer just now: %s" % str(e)[:60])); continue
            layers.append({"state": states[0], "name": name, "key": "g" + hashlib.sha1((s["url"] + "|" + name).encode()).hexdigest()[:9],   # permanent: never the position in a file "url": s["url"], "fields": s["fields"],
                           "where": where, "unfiltered": unfiltered, "agency": s.get("authority") or name, "stage": "planning",
                           "likelihood": rev.get("likelihood"), "source_prefix": "gis", "category": doc.get("category") or f.stem, "coverage": s.get("coverage"),
                           "records": total})
    DST.mkdir(parents=True, exist_ok=True)
    (DST / "layers.json").write_text(json.dumps(layers, indent=1))
    print("%d layers kept (%d with a planned-only filter, %d unfiltered); %d skipped" % (
        len(layers), sum(1 for x in layers if not x["unfiltered"]), sum(1 for x in layers if x["unfiltered"]), len(skipped)))
    by = {}
    for x in layers:
        by[x["state"]] = by.get(x["state"], 0) + 1
    print("states:", dict(sorted(by.items())))
    for n, why in skipped:
        print("  skipped: %s  (%s)" % (n[:70], why))


if __name__ == "__main__":
    main()
