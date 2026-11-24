"""ingest -> classify -> geocode -> match. Each step is idempotent and can be re-run."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from . import classify as C
from . import counties
from . import dedupe
from . import parcels as P
from .db import DB
from .geocode import Geocoder
from .http import Http
from .sources import federal_register, legistar, penndot, dot_arcgis

SOURCES = {"penndot": penndot, "federal_register": federal_register, "legistar": legistar, "dot_arcgis": dot_arcgis}


def ingest(cfg: Dict[str, Any], db: DB, only: Optional[str] = None) -> Dict[str, int]:
    http = Http(cfg["user_agent"])
    counts: Dict[str, int] = {}
    for name, module in SOURCES.items():
        scfg = (cfg.get("sources") or {}).get(name) or {}
        if not scfg.get("enabled") or (only and only != name):
            continue
        new = total = 0
        failed = None
        try:
            for signal in module.fetch(scfg, http):
                total += 1
                new += db.upsert_signal(signal)
        except Exception as e:
            failed = str(e)[:300]
            print("  %s failed after %d records: %s" % (name, total, e))
        # what was asked and what answered, list by list where the source has many
        db.save_reads(getattr(module, "READS", None) or [{"source": name, "key": "", "records": total, "error": failed}])
        counts[name] = new
        print("  %-17s %5d fetched, %5d new" % (name, total, new))
        if name == "penndot" and hasattr(module, "statuses"):
            try:
                print("  %-17s %5d projects already bid, in construction or finished" % ("", db.save_agency_status("penndot", module.statuses(scfg, http))))
            except Exception as e:
                print("  penndot status failed: %s" % e)
    return counts


def set_stage(db: DB, project_id: int, stage: str, note: Optional[str] = None, by: Optional[str] = None) -> Dict[str, Any]:
    """Record what someone who knows the job says its stage is. It holds until it is changed, whatever the sources say."""
    if stage not in C.STAGE_RANK:
        raise ValueError("stage must be one of: " + ", ".join(C.STAGE_RANK))
    rows = db.rows("SELECT s.source, s.external_id, p.name, p.stage FROM projects p JOIN signals s ON s.id = p.signal_id WHERE p.id=?", (project_id,))
    if not rows:
        raise ValueError("no project %s" % project_id)
    r = rows[0]
    db.conn.execute("""INSERT INTO stage_overrides (source, external_id, stage, note, set_by) VALUES (?,?,?,?,?)
                       ON CONFLICT(source, external_id) DO UPDATE SET stage=excluded.stage, note=excluded.note,
                         set_by=excluded.set_by, set_at=datetime('now')""", (r["source"], r["external_id"], stage, note, by))
    db.commit()
    return {"project": r["name"], "was": r["stage"], "now": stage}


MPMS = re.compile(r"mpms\D{0,3}(\d{3,7})", re.I)


def restage(db: DB) -> Dict[str, int]:
    """Move every agency project to the stage its own schedule implies, and rescore. Runs on every update.

    A project already matched to parcels keeps its record, so its stage is never recomputed by classify; without this
    a job stays "in design" until long after it is built. PennDOT's own stages only move forward; other lists are read afresh.
    """
    status = {r["ext_id"]: r for r in db.rows("SELECT * FROM agency_status WHERE source='penndot'")}
    fixed = {(r["source"], r["external_id"]): r["stage"] for r in db.rows("SELECT source, external_id, stage FROM stage_overrides")}
    changed = 0
    for p in db.rows("""SELECT p.id, p.stage, s.source, s.external_id, s.title, s.state, s.raw FROM projects p
                        JOIN signals s ON s.id = p.signal_id WHERE s.source = 'penndot' OR s.source LIKE 'dot:%' OR s.source LIKE 'gis:%'"""):
        current = p["stage"]
        try:
            raw = json.loads(p["raw"] or "{}")
        except ValueError:
            continue
        if p["source"] == "penndot":
            let, bid, building, pid = (raw.get("LET_DATE") or raw.get("LET_DATE_DISPLAY"), "A" in (raw.get("LET_IND"), raw.get("NTP_IND")),
                                       raw.get("UNDER_CONSTRUCTION") == "Y", str(raw.get("PROJ_ID") or p["external_id"]))
        else:
            let, bid, building, pid = raw.get("_let"), False, False, None
            p = dict(p, stage=C.dot_base_stage(raw))     # worked out afresh from the record: these have no history worth keeping
            if (p["state"] or "") == "PA":       # another body's list of a PennDOT job: find PennDOT's own number
                pid = next((str(int(v)) for k, v in raw.items() if "mpms" in k.lower() and str(v).replace(".0", "").isdigit()), None)
                m = pid or MPMS.search(p["title"] or "")
                pid = pid or (m.group(1) if m else None)
        known = status.get(pid) if pid else None
        if known:
            bid, building, let = bid or bool(known["let_actual"]) or bool(known["completed"]), building or bool(known["building"]), let or known["let_date"]
        mined = None if p["source"] == "penndot" else C.mined_stage(raw)[0]
        new = fixed.get((p["source"], p["external_id"])) or C.later_stage(C.later_stage(p["stage"], C.stage_from_schedule(let, bid, building)), mined)
        if new != (current or "unknown"):
            db.conn.execute("UPDATE projects SET stage=? WHERE id=?", (new, p["id"]))
            changed += 1
    db.commit()
    return dict(rescore(db), stages_changed=changed)


def classify(cfg: Dict[str, Any], db: DB) -> int:
    text_classifier = None
    n = 0
    for row in db.unclassified_signals():
        signal = dict(row)
        if signal["source"] == "penndot":
            engine, result = "rules", C.classify_penndot(signal)
        elif signal["source"].startswith(("dot:", "gis:")):
            try:
                engine, result = "rules", C.classify_dot(signal)
            except Exception as e:                  # one malformed record must not stop a run of thousands
                print("  skipped %s %s: %s" % (signal["source"], signal["external_id"], str(e)[:100]))
                continue
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
    return C.lead_points(project_score, reason)


def rescore(db: DB) -> Dict[str, int]:
    """Recompute every project and lead score from what is already stored. Run after the scoring rules change."""
    n = 0
    for p in db.rows("SELECT id, acquisition_likelihood, stage, est_cost, score FROM projects"):
        outside = p["score"] == 0 and (p["acquisition_likelihood"] or 0) > 0     # scored 0 because it is not in PA
        new = 0 if outside else C.overall_score(p["acquisition_likelihood"] or 0, p["stage"] or "unknown", p["est_cost"])
        if new != p["score"]:
            db.conn.execute("UPDATE projects SET score=? WHERE id=?", (new, p["id"]))
            n += 1
    leads = 0
    for l in db.rows("SELECT l.id, l.score, l.match_reason, p.score AS ps FROM leads l JOIN projects p ON p.id=l.project_id"):
        new = C.lead_points(l["ps"], l["match_reason"])
        if new != l["score"]:
            db.conn.execute("UPDATE leads SET score=? WHERE id=?", (new, l["id"]))
            leads += 1
    db.commit()
    return {"projects_changed": n, "leads_changed": leads}


def match(cfg: Dict[str, Any], db: DB, limit: Optional[int] = None) -> int:
    m = cfg["matching"]
    layers = cfg.get("parcel_layers") or []
    http = Http(cfg["user_agent"], timeout=60, retries=2)
    failures: Dict[str, int] = {}      # a parcel server that is down should cost one run a few minutes, not hours
    budget = int(limit or m["projects_per_run"])
    # Only projects in places with parcel records can be matched, and every state gets a share of each run,
    # so one large state cannot use the whole budget.
    pool = [p for p in db.projects_to_match(int(m["min_project_score"]), 200000)
            if P.pick_layer(layers, p["county"], p["state"]) is not None]
    states = sorted({p["state"] for p in pool})
    share = max(1, budget // max(1, len(states)))
    taken: Dict[str, int] = {}
    projects = []
    for p in pool:                                   # highest score first within each state's share
        if taken.get(p["state"], 0) < share and len(projects) < budget:
            taken[p["state"]] = taken.get(p["state"], 0) + 1
            projects.append(p)
    for p in pool:                                   # then fill what is left with the best remaining
        if len(projects) >= budget:
            break
        if p not in projects:
            projects.append(p)
    total = 0
    for p in projects:
        layer = P.pick_layer(layers, p["county"], p["state"])
        if layer is None or failures.get(layer["name"], 0) >= 3:
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
            print("  project %s (%s): %s" % (p["id"], layer["name"], str(e)[:120]))
            failures[layer["name"]] = failures.get(layer["name"], 0) + 1
            if failures[layer["name"]] == 3:
                print("  %s is not answering; skipping it for the rest of this run" % layer["name"])
            continue  # left unmatched so the next run retries it
        for r in found:
            r.setdefault("county", None)
            r["county"] = r["county"] or p["county"]
            parcel_id = db.upsert_parcel(dict(r, layer=layer["name"], state=p["state"]))
            db.upsert_lead(p["id"], parcel_id, r.get("distance_m"), r["reason"],
                           lead_score(p["score"], r["reason"]))
        db.mark_matched(p["id"])
        total += len(found)
    print("  matched %d projects -> %d parcel leads" % (len(projects), total))
    return total


def fill_counties(cfg: Dict[str, Any], db: DB) -> int:
    """Owner files are chosen by county, so a located project with no county is given one from the Census map."""
    n = counties.fill(db, Http(cfg["user_agent"], timeout=90, retries=2))
    print("  %d projects given a county" % n)
    return n


def run_all(cfg: Dict[str, Any], db: DB, limit: Optional[int] = None) -> None:
    print("Ingest"); ingest(cfg, db)
    print("Classify"); classify(cfg, db)
    print("Stages"); print("  %(stages_changed)d projects moved to a later stage" % restage(db))
    print("Merge duplicates"); print("  %(copies)d repeats hidden of %(checked)d mapped projects" % dedupe.run(db))
    print("Geocode"); geocode(cfg, db)
    print("Counties"); fill_counties(cfg, db)
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
