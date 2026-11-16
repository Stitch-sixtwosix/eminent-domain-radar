from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .. import outreach, parceldetail
from .. import outreach as outreach_mod
from ..classify import score_breakdown, timeline
from ..db import DB

STATIC = Path(__file__).parent / "static"
STATUSES = ["new", "reviewing", "contacted", "retained", "rejected"]

LEAD_SQL = """
SELECT l.id, l.score, l.match_reason, l.distance_m, l.status, l.notes, l.created_at,
       pa.apn, pa.county AS parcel_county, pa.address, pa.owner, pa.mailing_address, pa.market_value, pa.acres,
       pa.layer, p.id AS project_id, p.name AS project, p.project_type, p.stage, p.score AS project_score,
       p.est_cost, s.source, s.url, s.agency, pa.owner_kind, pa.mailable, pa.email_contacts,
       (SELECT MAX(o.created_at) FROM outreach o WHERE o.lead_id = l.id AND o.channel = 'postcard') AS postcard_at,
       (SELECT MAX(o.created_at) FROM outreach o WHERE o.lead_id = l.id AND o.channel = 'email') AS email_at
FROM leads l JOIN parcels pa ON pa.id = l.parcel_id JOIN projects p ON p.id = l.project_id
JOIN signals s ON s.id = p.signal_id
"""


class Enroll(BaseModel):
    person_id: str


class Campaign(BaseModel):
    min_score: int = 90
    project_id: Optional[int] = None
    county: Optional[str] = None
    limit: int = 25
    dry_run: bool = True


class LeadUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None


OUTREACH_FILTERS = {
    # company owners Apollo can search; hidden once a scan found nobody there
    "email": "pa.owner_kind = 'company' AND COALESCE(pa.email_contacts, 1) > 0 "
             "AND NOT EXISTS (SELECT 1 FROM outreach o WHERE o.lead_id = l.id AND o.channel = 'email')",
    "postcard": outreach.POSTCARD_ELIGIBLE,
    "contacted": "EXISTS (SELECT 1 FROM outreach o WHERE o.lead_id = l.id)",
    "none": "NOT EXISTS (SELECT 1 FROM outreach o WHERE o.lead_id = l.id)",
}


def _lead_filters(project_id, status, county, min_score, owner_only, q, outreach_filter=None):
    where, params = ["l.score >= ?"], [min_score]
    if project_id is not None:
        where.append("l.project_id = ?"); params.append(project_id)
    if status:
        where.append("l.status = ?"); params.append(status)
    if county:
        where.append("UPPER(pa.county) = ?"); params.append(county.upper())
    if owner_only:
        where.append("pa.owner IS NOT NULL")
    if q:
        where.append("(pa.owner LIKE ? OR pa.address LIKE ? OR pa.apn LIKE ?)"); params += ["%%%s%%" % q] * 3
    if outreach_filter in OUTREACH_FILTERS:
        where.append(OUTREACH_FILTERS[outreach_filter])
    return " WHERE " + " AND ".join(where), params


def create_app(cfg: Dict[str, Any]) -> FastAPI:
    db = DB(cfg["database"])
    outreach.apply_saved(cfg, db)
    app = FastAPI(title="Eminent Tracker")

    @app.get("/")
    def index() -> FileResponse:
        # no-cache: the browser revalidates, so an updated dashboard shows up without a hard refresh
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/stats")
    def stats() -> Dict[str, Any]:
        one = lambda sql: db.rows(sql)[0]["n"]
        return {
            "signals": one("SELECT COUNT(*) n FROM signals"),
            "updated": one("SELECT MAX(fetched_at) n FROM signals"),
            "projects": one("SELECT COUNT(*) n FROM projects WHERE score >= 60"),
            "leads": one("SELECT COUNT(*) n FROM leads"),
            "leads_with_owner": one("SELECT COUNT(*) n FROM leads l JOIN parcels p ON p.id=l.parcel_id "
                                    "WHERE p.owner IS NOT NULL"),
            "counties": [r["county"] for r in db.rows(
                "SELECT DISTINCT county FROM signals WHERE county IS NOT NULL ORDER BY county")],
            "types": [r["project_type"] for r in db.rows(
                "SELECT DISTINCT project_type FROM projects ORDER BY project_type")],
            "statuses": STATUSES,
        }

    @app.get("/api/projects")
    def projects(min_score: int = 60, county: Optional[str] = None, project_type: Optional[str] = None,
                 source: Optional[str] = None, q: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        where, params = ["p.score >= ?"], [min_score]
        if county:
            where.append("s.county = ?"); params.append(county.upper())
        if project_type:
            where.append("p.project_type = ?"); params.append(project_type)
        if source:
            where.append("s.source LIKE ?"); params.append(source + "%")
        if q:
            where.append("(p.name LIKE ? OR p.summary LIKE ?)"); params += ["%%%s%%" % q] * 2
        rows = db.rows(
            """SELECT p.id, p.name, p.project_type, p.stage, p.acquisition_likelihood, p.score, p.summary,
                      p.est_cost, p.contact, p.geometry, p.lat, p.lon, p.matched_at, p.engine,
                      s.source, s.url, s.agency, s.county, s.published, s.raw,
                      (SELECT COUNT(*) FROM leads l WHERE l.project_id = p.id) AS lead_count
               FROM projects p JOIN signals s ON s.id = p.signal_id
               WHERE %s ORDER BY p.score DESC, p.est_cost DESC LIMIT ?""" % " AND ".join(where),
            params + [min(limit, 2000)],
        )
        for r in rows:
            r["geometry"] = json.loads(r["geometry"]) if r["geometry"] else None
            raw = json.loads(r.pop("raw") or "{}")
            r["timeline"] = timeline(r["stage"], raw if isinstance(raw, dict) else {})
            r["score_parts"] = score_breakdown(r["acquisition_likelihood"], r["stage"], r["est_cost"], r["score"])
        return rows

    def one_lead(lead_id: int) -> Dict[str, Any]:
        rows = db.rows(LEAD_SQL + " WHERE l.id = ?", (lead_id,))
        if not rows:
            raise HTTPException(404, "lead not found")
        return rows[0]

    def guard(fn):
        """Provider and validation failures become readable 4xx/502 answers, not stack traces."""
        try:
            return fn()
        except ValueError as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(502, str(e))

    @app.get("/api/leads/{lead_id}/detail")
    def lead_detail(lead_id: int) -> Dict[str, Any]:
        lead = one_lead(lead_id)
        parcel = db.rows("SELECT pa.* FROM parcels pa JOIN leads l ON l.parcel_id = pa.id WHERE l.id = ?", (lead_id,))[0]
        parcel["attributes"] = json.loads(parcel["attributes"] or "{}")
        bg, others = parceldetail.background(parcel), parceldetail.related(db, lead)
        return {"lead": lead, "layer": parcel["layer"], "boundary": parceldetail.boundary(cfg, db, parcel),
                "background": bg, "related": others, "owner": parceldetail.owner_profile(lead, bg["facts"], others),
                "contacts": db.contacts_for(lead["owner"])}

    @app.get("/api/leads/{lead_id}/cases")
    def lead_cases(lead_id: int) -> Dict[str, Any]:
        return parceldetail.cases(one_lead(lead_id))

    @app.get("/api/outreach/template")
    def get_template() -> Dict[str, Any]:
        return outreach.template(cfg)

    @app.put("/api/outreach/template")
    def put_template(body: Dict[str, Any]) -> Dict[str, Any]:
        return outreach.save_template(cfg, db, body)

    @app.get("/api/outreach/status")
    def outreach_status() -> Dict[str, Any]:
        return outreach.status(cfg)

    @app.post("/api/postcards/campaign")
    def postcard_campaign(body: Campaign) -> Dict[str, Any]:
        """Dry run by default: lists who would be mailed. With dry_run false it mails up to `limit` owners."""
        limit = max(1, min(body.limit, 200))
        return guard(lambda: outreach.run_campaign(cfg, db, body.min_score, body.project_id, body.county, limit,
                                                   body.dry_run))

    @app.get("/api/leads/{lead_id}/postcard")
    def postcard_preview(lead_id: int) -> Dict[str, Any]:
        lead = one_lead(lead_id)
        return dict(outreach.postcard_check(cfg, lead), already_sent=lead["postcard_at"],
                    owner=lead["owner"], project=lead["project"])

    @app.post("/api/leads/{lead_id}/postcard")
    def postcard_send(lead_id: int) -> Dict[str, Any]:
        lead = one_lead(lead_id)
        if lead["postcard_at"]:
            raise HTTPException(409, "A postcard was already sent for this lead on %s." % lead["postcard_at"][:10])
        result = guard(lambda: outreach.send_postcard(cfg, lead))
        db.log_outreach(lead_id, "postcard", "sent" if result["mode"] == "live" else "test",
                        result["external_id"], result)
        return result

    @app.get("/api/leads/{lead_id}/email/contacts")
    def email_contacts(lead_id: int) -> List[Dict[str, Any]]:
        lead = one_lead(lead_id)
        if not outreach.status(cfg)["email"]["enabled"]:
            raise HTTPException(409, outreach.status(cfg)["email"]["why"])
        people = guard(lambda: outreach.Apollo(cfg).find_contacts(lead))
        db.save_contacts(lead["owner"], people)
        return people

    @app.post("/api/leads/{lead_id}/email")
    def email_enroll(lead_id: int, body: Enroll) -> Dict[str, Any]:
        lead = one_lead(lead_id)
        if lead["email_at"]:
            raise HTTPException(409, "This lead is already in the email sequence.")
        if not outreach.status(cfg)["email"]["enabled"]:
            raise HTTPException(409, outreach.status(cfg)["email"]["why"])
        result = guard(lambda: outreach.Apollo(cfg).enroll(lead, body.person_id))
        db.log_outreach(lead_id, "email", "sent", result["external_id"], result)
        db.save_contacts(lead["owner"], [{"person_id": result["person_id"], "name": result["name"], "title": result["title"],
                                          "email": result["email"]}] + [c for c in db.contacts_for(lead["owner"])
                                                                        if c["person_id"] != result["person_id"]])
        return result

    @app.get("/api/leads")
    def leads(project_id: Optional[int] = None, status: Optional[str] = None, county: Optional[str] = None,
              min_score: int = 0, owner_only: bool = False, q: Optional[str] = None,
              outreach: Optional[str] = None, limit: int = 300) -> List[Dict[str, Any]]:
        where, params = _lead_filters(project_id, status, county, min_score, owner_only, q, outreach)
        return db.rows(LEAD_SQL + where + " ORDER BY l.score DESC, pa.market_value DESC LIMIT ?",
                       params + [min(limit, 5000)])

    @app.get("/api/leads.csv")
    def leads_csv(project_id: Optional[int] = None, status: Optional[str] = None, county: Optional[str] = None,
                  min_score: int = 0, owner_only: bool = False, q: Optional[str] = None,
                  outreach: Optional[str] = None) -> StreamingResponse:
        where, params = _lead_filters(project_id, status, county, min_score, owner_only, q, outreach)
        rows = db.rows(LEAD_SQL + where + " ORDER BY l.score DESC LIMIT 50000", params)
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=eminent-tracker-leads.csv"})

    @app.patch("/api/leads/{lead_id}")
    def update_lead(lead_id: int, body: LeadUpdate) -> Dict[str, Any]:
        if body.status is not None and body.status not in STATUSES:
            raise HTTPException(400, "status must be one of %s" % STATUSES)
        if not db.rows("SELECT 1 FROM leads WHERE id=?", (lead_id,)):
            raise HTTPException(404, "lead not found")
        db.update_lead(lead_id, body.status, body.notes)
        return {"ok": True}

    return app


def export_static(cfg: Dict[str, Any], out_dir: str, min_score: int = 40) -> Dict[str, int]:
    """Write a read-only copy of the dashboard (index.html + data.json) that any static host can serve."""
    from datetime import date

    from fastapi.testclient import TestClient

    client = TestClient(create_app(cfg))
    stats = client.get("/api/stats").json()
    stats["generated"] = date.today().isoformat()
    projects = client.get("/api/projects", params={"min_score": min_score, "limit": 2000}).json()
    keep = ("id", "project_id", "project_score", "score", "match_reason", "distance_m", "status", "apn", "parcel_county", "address",
            "owner", "mailing_address", "market_value", "acres", "owner_kind", "mailable", "email_contacts")
    db = DB(cfg["database"])
    layer_cfg = {x.get("name"): x for x in cfg.get("parcel_layers") or []}
    layers: List[Dict[str, Any]] = []      # each lead points at one of these so the page can fetch its own boundary
    layer_index: Dict[str, int] = {}
    leads = []
    extra = {r["lead_id"]: r for r in db.rows(
        "SELECT l.id AS lead_id, pa.layer, pa.attributes, pa.apn FROM leads l JOIN parcels pa ON pa.id = l.parcel_id")}
    for r in db.rows(LEAD_SQL + " ORDER BY l.score DESC, pa.market_value DESC"):
        row = {k: r[k] for k in keep}
        x = extra[r["id"]]
        parcel = {"apn": x["apn"], "attributes": json.loads(x["attributes"] or "{}")}
        facts = parceldetail.background(parcel)["facts"]
        prof = parceldetail.owner_profile(r, facts, [])
        row["facts"] = [[f["label"], f["value"]] for f in facts]
        row["own"] = [prof["location"], prof["occupancy"]]
        layer = layer_cfg.get(x["layer"])
        q = parceldetail.boundary_query(layer, parcel) if layer else None
        if q:
            if x["layer"] not in layer_index:
                layer_index[x["layer"]] = len(layers)
                layers.append({"name": x["layer"], "url": q.pop("url"), "provider": q.pop("provider"), "field": q.pop("field")})
            row["bq"] = dict({k: v for k, v in q.items() if k not in ("url", "provider", "field")}, l=layer_index[x["layer"]])
        leads.append(row)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    html = (STATIC / "index.html").read_text()
    html = html.replace("<script>\nconst $", "<script>window.EDRADAR_STATIC = true;</script>\n<script>\nconst $", 1)
    assert "EDRADAR_STATIC = true" in html
    (out / "index.html").write_text(html)
    (out / "data.json").write_text(json.dumps({"stats": stats, "projects": projects, "leads": leads, "layers": layers, "template": outreach.template(cfg),
                                               "contacts": {o["owner"]: db.contacts_for(o["owner"]) for o in
                                                            db.rows("SELECT DISTINCT owner FROM contacts")}},
                                              separators=(",", ":")))
    (out / ".nojekyll").write_text("")
    return {"projects": len(projects), "leads": len(leads)}
