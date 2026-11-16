"""ingest -> classify -> geocode -> match. Each step is idempotent and can be re-run."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from . import classify as C
from . import parcels as P
from .db import DB
from .geocode import Geocoder
from .http import Http
from .sources import federal_register, legistar, penndot

SOURCES = {"penndot": penndot, "federal_register": federal_register, "legistar": legistar}


def ingest(cfg: Dict[str, Any], db: DB, only: Optional[str] = None) -> Dict[str, int]:
    http = Http(cfg["user_agent"])
    counts: Dict[str, int] = {}
    for name, module in SOURCES.items():
        scfg = (cfg.get("sources") or {}).get(name) or {}
        if not scfg.get("enabled") or (only and only != name):
            continue
        new = total = 0
        try:
            for signal in module.fetch(scfg, http):
                total += 1
                new += db.upsert_signal(signal)
        except Exception as e:
            print("  %s failed after %d records: %s" % (name, total, e))
        counts[name] = new
        print("  %-17s %5d fetched, %5d new" % (name, total, new))
    return counts


def classify(cfg: Dict[str, Any], db: DB) -> int:
    text_classifier = None
    n = 0
    for row in db.unclassified_signals():
        signal = dict(row)
        if signal["source"] == "penndot":
            engine, result = "rules", C.classify_penndot(signal)
        else:
            if text_classifier is None:
                text_classifier = C.make_text_classifier(cfg.get("classifier") or {})
            engine, result = text_classifier(signal)
        db.save_project(signal["id"], result, engine)
        n += 1
    print("  classified %d signals" % n)
    return n


def geocode(cfg: Dict[str, Any], db: DB) -> int:
    """Give text-sourced projects a geometry by geocoding the places the classifier extracted."""
    min_score = int(cfg["matching"]["min_project_score"])
    rows = db.projects_needing_geocode(min_score)
    if not rows:
        return 0
    g = Geocoder(cfg["geocoder"], cfg["user_agent"])
    n = 0
    for p in rows:
        pts, county = [], None
        for place in json.loads(p["locations"] or "[]"):
            hit = g.lookup(place, p["signal_municipality"])
            if hit:
                pts.append([hit["lon"], hit["lat"]])
                county = county or hit["county"]
        if pts:
            lon = sum(x[0] for x in pts) / len(pts)
            lat = sum(x[1] for x in pts) / len(pts)
            db.set_geometry(p["id"], {"points": pts}, lat, lon)
            if county and not p["signal_county"]:
                db.conn.execute("UPDATE signals SET county=? WHERE id=?", (county, p["signal_id"]))
                db.commit()
            n += 1
    print("  geocoded %d of %d text projects" % (n, len(rows)))
    return n


def lead_score(project_score: int, reason: str) -> int:
    if reason == "named_in_document":
        return min(100, project_score + 10)
    if reason == "abuts_alignment":
        return project_score
    return int(round(project_score * 0.75))


def match(cfg: Dict[str, Any], db: DB, limit: Optional[int] = None) -> int:
    m = cfg["matching"]
    layers = cfg.get("parcel_layers") or []
    http = Http(cfg["user_agent"], timeout=90)
    projects = db.projects_to_match(int(m["min_project_score"]), int(limit or m["projects_per_run"]))
    total = 0
    for p in projects:
        layer = P.pick_layer(layers, p["county"])
        if layer is None:
            continue
        found = []
        try:
            apns = [P.normalize_apn(p["county"], a) for a in json.loads(p["parcel_ids_mentioned"] or "[]")]
            if apns:
                found += [dict(r, reason="named_in_document") for r in P.by_apn(layer, http, apns)]
            if p["geometry"]:
                for r in P.near(layer, http, json.loads(p["geometry"]), m):
                    found.append(dict(r, reason="abuts_alignment" if r["abuts"] else "within_buffer"))
        except Exception as e:
            print("  project %s (%s): %s" % (p["id"], layer["name"], e))
            continue  # left unmatched so the next run retries it
        for r in found:
            r.setdefault("county", None)
            r["county"] = r["county"] or p["county"]
            parcel_id = db.upsert_parcel(dict(r, layer=layer["name"]))
            db.upsert_lead(p["id"], parcel_id, r.get("distance_m"), r["reason"],
                           lead_score(p["score"], r["reason"]))
        db.mark_matched(p["id"])
        total += len(found)
    print("  matched %d projects -> %d parcel leads" % (len(projects), total))
    return total


def run_all(cfg: Dict[str, Any], db: DB, limit: Optional[int] = None) -> None:
    print("Ingest"); ingest(cfg, db)
    print("Classify"); classify(cfg, db)
    print("Geocode"); geocode(cfg, db)
    print("Match"); match(cfg, db, limit)
    db.refresh_outreach_flags()
    auto_postcards(cfg, db)


def auto_postcards(cfg: Dict[str, Any], db: DB) -> None:
    """Mail the configured automatic batch. Off unless outreach.postcard.auto.enabled is true and a Lob key is set."""
    from . import outreach

    auto = ((cfg.get("outreach") or {}).get("postcard") or {}).get("auto") or {}
    if not auto.get("enabled"):
        return
    print("Postcards")
    counties = auto.get("counties") or [None]
    cap = int(auto.get("max_per_run", 25))
    for county in counties:
        if cap <= 0:
            break
        try:
            res = outreach.run_campaign(cfg, db, int(auto.get("min_score", 90)), None, county, cap, dry_run=False)
        except ValueError as e:
            print("  not sent: %s" % e)
            return
        cap -= len(res["sent"])
        print("  %s%d sent in %s mode, %d failed" % ("%s: " % county if county else "", len(res["sent"]), res["mode"],
                                                      len(res["failed"])))
