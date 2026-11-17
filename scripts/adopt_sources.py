#!/usr/bin/env python3
"""Turn researched project sources (research/sources/*.json) into layers the tool reads.

Kept: verified ArcGIS layers with geometry that belong to exactly one state. Each source's "planned only"
filter is tried against the live service; if the service rejects it, the layer is kept without a filter and
marked unfiltered so its results can be reviewed. National layers need a state per record and are listed
as skipped for now.
"""
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
            where, unfiltered = None, True
            try:
                total = count(s["url"], "1=1")
                cand = (s.get("planned_filter") or "").strip()
                if looks_like_sql(cand):
                    try:
                        n = count(s["url"], cand)
                        if 0 < n <= total:
                            where, unfiltered = cand, False
                    except Exception:
                        pass
            except Exception as e:
                skipped.append((name, "did not answer just now: %s" % str(e)[:60])); continue
            layers.append({"state": states[0], "name": name, "key": "%s%d" % (f.stem[:6], i), "url": s["url"], "fields": s["fields"],
                           "where": where, "unfiltered": unfiltered, "agency": s.get("authority") or name, "stage": "planning",
                           "source_prefix": "gis", "category": doc.get("category") or f.stem, "coverage": s.get("coverage"),
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
