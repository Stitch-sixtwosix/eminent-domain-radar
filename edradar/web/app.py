from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .. import outreach
from ..db import DB

STATIC = Path(__file__).parent / "static"
STATUSES = ["new", "reviewing", "contacted", "retained", "rejected"]

LEAD_SQL = """
SELECT l.id, l.score, l.match_reason, l.distance_m, l.status, l.notes, l.created_at,
       pa.apn, pa.county AS parcel_county, pa.address, pa.owner, pa.mailing_address, pa.market_value, pa.acres,
       pa.layer, p.id AS project_id, p.name AS project, p.project_type, p.stage, p.score AS project_score,
       p.est_cost, s.source, s.url, s.agency,
       (SELECT MAX(o.created_at) FROM outreach o WHERE o.lead_id = l.id AND o.channel = 'postcard') AS postcard_at,
       (SELECT MAX(o.created_at) FROM outreach o WHERE o.lead_id = l.id AND o.channel = 'email') AS email_at
FROM leads l JOIN parcels pa ON pa.id = l.parcel_id JOIN projects p ON p.id = l.project_id
JOIN signals s ON s.id = p.signal_id
"""


class Enroll(BaseModel):
    person_id: str


class LeadUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None


def _lead_filters(project_id, status, county, min_score, owner_only, q):
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
    return " WHERE " + " AND ".join(where), params


def create_app(cfg: Dict[str, Any]) -> FastAPI:
    db = DB(cfg["database"])
    app = FastAPI(title="edradar")

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
                      s.source, s.url, s.agency, s.county, s.published,
                      (SELECT COUNT(*) FROM leads l WHERE l.project_id = p.id) AS lead_count
               FROM projects p JOIN signals s ON s.id = p.signal_id
               WHERE %s ORDER BY p.score DESC, p.est_cost DESC LIMIT ?""" % " AND ".join(where),
            params + [min(limit, 2000)],
        )
        for r in rows:
            r["geometry"] = json.loads(r["geometry"]) if r["geometry"] else None
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

    @app.get("/api/outreach/status")
    def outreach_status() -> Dict[str, Any]:
        return outreach.status(cfg)

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
        return guard(lambda: outreach.Apollo(cfg).find_contacts(lead))

    @app.post("/api/leads/{lead_id}/email")
    def email_enroll(lead_id: int, body: Enroll) -> Dict[str, Any]:
        lead = one_lead(lead_id)
        if lead["email_at"]:
            raise HTTPException(409, "This lead is already in the email sequence.")
        if not outreach.status(cfg)["email"]["enabled"]:
            raise HTTPException(409, outreach.status(cfg)["email"]["why"])
        result = guard(lambda: outreach.Apollo(cfg).enroll(lead, body.person_id))
        db.log_outreach(lead_id, "email", "sent", result["external_id"], result)
        return result

    @app.get("/api/leads")
    def leads(project_id: Optional[int] = None, status: Optional[str] = None, county: Optional[str] = None,
              min_score: int = 0, owner_only: bool = False, q: Optional[str] = None,
              limit: int = 300) -> List[Dict[str, Any]]:
        where, params = _lead_filters(project_id, status, county, min_score, owner_only, q)
        return db.rows(LEAD_SQL + where + " ORDER BY l.score DESC, pa.market_value DESC LIMIT ?",
                       params + [min(limit, 5000)])

    @app.get("/api/leads.csv")
    def leads_csv(project_id: Optional[int] = None, status: Optional[str] = None, county: Optional[str] = None,
                  min_score: int = 0, owner_only: bool = False, q: Optional[str] = None) -> StreamingResponse:
        where, params = _lead_filters(project_id, status, county, min_score, owner_only, q)
        rows = db.rows(LEAD_SQL + where + " ORDER BY l.score DESC LIMIT 50000", params)
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=edradar-leads.csv"})

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
    keep = ("id", "project_id", "score", "match_reason", "distance_m", "status", "apn", "parcel_county", "address",
            "owner", "mailing_address", "market_value", "acres")
    leads = [{k: r[k] for k in keep} for r in client.get("/api/leads", params={"limit": 5000}).json()]
    db = DB(cfg["database"])
    if len(leads) == 5000:  # the API caps a page; a snapshot wants every lead
        leads = [{k: r[k] for k in keep} for r in db.rows(LEAD_SQL + " ORDER BY l.score DESC, pa.market_value DESC")]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    html = (STATIC / "index.html").read_text()
    html = html.replace("<script>\nconst $", "<script>window.EDRADAR_STATIC = true;</script>\n<script>\nconst $", 1)
    assert "EDRADAR_STATIC = true" in html
    (out / "index.html").write_text(html)
    (out / "data.json").write_text(json.dumps({"stats": stats, "projects": projects, "leads": leads},
                                              separators=(",", ":")))
    (out / ".nojekyll").write_text("")
    return {"projects": len(projects), "leads": len(leads)}
