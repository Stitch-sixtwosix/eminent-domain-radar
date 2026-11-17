"""Find the same project listed more than once and keep one copy.

A state DOT list, a regional plan and a city map often carry the same project, and some sources list one
project once per road segment. A copy is only marked when the evidence is strong, because hiding a real
project costs more than showing one twice:

  * same state, mapped within 300 m of each other, and the estimated costs do not disagree, and either
  * the names are the same, or
  * the names share most of their words, are at least three words long, and carry the same numbers
    (route numbers, years, project numbers), so "FY27 D5 planning" never merges with "FY31 D5 planning".

Lists of individual parcels an agency is acquiring are left alone: neighbouring parcels are not duplicates.
The copy kept is the one with leads, then the highest score, then a cost, then the oldest.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

NEAR_M = 300.0
CELL = 0.004                     # degrees; a little over 300 m at any US latitude
STOP = set("the of and to from at in on a an for road rd street st ave avenue project projects "
           "improvement improvements county city phase".split())


def words(name: Optional[str]) -> Set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in STOP and len(w) > 1}


def same_project(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if a["state"] != b["state"] or a["parcel_list"] or b["parcel_list"]:
        return False
    if math.hypot((a["lat"] - b["lat"]) * 111000.0,
                  (a["lon"] - b["lon"]) * 111000.0 * math.cos(math.radians(a["lat"]))) > NEAR_M:
        return False
    ca, cb = a["est_cost"], b["est_cost"]
    if ca and cb and abs(ca - cb) > 0.2 * max(ca, cb):
        return False
    wa, wb = a["words"], b["words"]
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    numbers = lambda ws: {w for w in ws if any(c.isdigit() for c in w)}
    if min(len(wa), len(wb)) < 3 or numbers(wa) != numbers(wb):
        return False
    return len(wa & wb) / float(len(wa | wb)) >= 0.6


def find(projects: List[Dict[str, Any]]) -> Dict[int, int]:
    """{id of a copy: id of the project kept}. Each project needs id, name, state, lat, lon, est_cost, score,
    leads and parcel_list."""
    rows = [dict(p, words=words(p.get("name"))) for p in projects if p.get("lat") is not None and p.get("lon") is not None]
    grid: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        grid[(r["state"], int(math.floor(r["lat"] / CELL)), int(math.floor(r["lon"] / CELL)))].append(r)
    parent = {r["id"]: r["id"] for r in rows}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for (state, gx, gy), cell in grid.items():
        near = [o for dx in (-1, 0, 1) for dy in (-1, 0, 1) for o in grid.get((state, gx + dx, gy + dy), ())]
        for a in cell:
            for b in near:
                if a["id"] < b["id"] and same_project(a, b):
                    parent[root(b["id"])] = root(a["id"])
    groups: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[root(r["id"])].append(r)
    out: Dict[int, int] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (-(r.get("leads") or 0), -(r.get("score") or 0), 0 if r.get("est_cost") else 1, r["id"]))
        for r in members[1:]:
            if not r.get("leads"):              # a copy that already has leads stays visible
                out[r["id"]] = members[0]["id"]
    return out


def run(db: Any) -> Dict[str, int]:
    rows = [dict(r) for r in db.rows(
        """SELECT p.id, p.name, p.lat, p.lon, p.est_cost, p.score, s.state, s.raw,
                  (SELECT COUNT(*) FROM leads l WHERE l.project_id = p.id) AS leads
           FROM projects p JOIN signals s ON s.id = p.signal_id WHERE p.lat IS NOT NULL""")]
    for r in rows:
        raw = r.pop("raw") or ""
        r["parcel_list"] = '"_likelihood"' in raw and bool((json.loads(raw) or {}).get("_likelihood"))
    copies = find(rows)
    db.conn.execute("UPDATE projects SET duplicate_of = NULL WHERE duplicate_of IS NOT NULL")
    db.conn.executemany("UPDATE projects SET duplicate_of = ? WHERE id = ?", [(keep, dup) for dup, keep in copies.items()])
    db.conn.commit()
    return {"checked": len(rows), "copies": len(copies)}
