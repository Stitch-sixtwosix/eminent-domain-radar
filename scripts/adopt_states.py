#!/usr/bin/env python3
"""Copy researched state files into the package, applying the reviewed adjustments below.

research/states/XX.json is what the research found; edradar/data/states/XX.json is what the tool uses.
Only layers verified with a live query are kept, and each adjustment here records a decision made on review.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, DST = ROOT / "research" / "states", ROOT / "edradar" / "data" / "states"

# code -> {"drop": [substrings of layer names to leave out], "where": {substring: SQL filter}, "stage": default stage,
#          "copy_fields": {to_substring: from_substring}}
REVIEW = {
    "TX": {"where": {"DCIS": "PROJ_STAT = 'Active' AND PROJ_STG IN ('Planning','Initiate','PE','PS&E','Ready to Let','Pre Procurement','Procurement')"}},
    "FL": {"where": {"Work Program": "PHASE IN ('PE','PDE','ENV','ROW')"}},      # only the pre-construction phases
    "KS": {"where": {"WinCPMS": "proj_status_cd = 'PLAN'"}},
    "MI": {"where": {"Five-Year": "ACTL_LET_DATE IS NULL"}, "copy_fields": {"(P": "(L"}},
    "IL": {"drop": ["older edition"]},
    "WI": {"drop": ["STIP Projects (4 Year)"]},                                   # the 6-year layer already holds these
}


# Statewide parcel layers that answered but cannot be used as they are, and why.
NO_STATEWIDE = {"TX": "the layer refuses area queries; only single-point lookups return owners",
                "IA": "a 2017 snapshot with the deed holder's name only"}


# Statewide owner layers that do not hold every county: how many they do hold.
PARTIAL = {"NY": 38, "MN": 60}


def adopt(code: str) -> str:
    d = json.loads((SRC / ("%s.json" % code)).read_text())
    rev = REVIEW.get(code, {})
    kept = []
    for p in d.get("projects") or []:
        if not (p and p.get("verified") and p.get("url")):
            continue
        name = p.get("name") or ""
        if any(x in name for x in rev.get("drop", [])):
            continue
        for sub, where in rev.get("where", {}).items():
            if sub in name:
                p["where"] = where
        for to, frm in rev.get("copy_fields", {}).items():
            if to in name:
                donor = next((q for q in d["projects"] if frm in (q.get("name") or "") and len(q.get("fields") or {}) > 2), None)
                if donor:
                    p["fields"] = donor["fields"]
        if len(p.get("fields") or {}) < 2:
            continue
        p.setdefault("stage", rev.get("stage", "planning"))
        kept.append(p)
    d["projects"] = kept
    if code in NO_STATEWIDE and d.get("parcels_statewide"):
        d["parcels_statewide"] = dict(d["parcels_statewide"], verified=False, held=NO_STATEWIDE[code])
    if code in PARTIAL and d.get("parcels_statewide"):
        d["parcels_statewide"]["counties_covered"] = PARTIAL[code]
    DST.mkdir(parents=True, exist_ok=True)
    (DST / ("%s.json" % code)).write_text(json.dumps(d, indent=1))
    return "%s: %d project layer(s), statewide owners %s, %d county layer(s)" % (
        code, len(kept), "yes" if (d.get("parcels_statewide") or {}).get("verified") else "no",
        len([c for c in d.get("parcels_counties") or [] if c and c.get("verified")]))


if __name__ == "__main__":
    codes = [a.upper() for a in sys.argv[1:]] or sorted(f.stem for f in SRC.glob("*.json"))
    for c in codes:
        print(adopt(c))
