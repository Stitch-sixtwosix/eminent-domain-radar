from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .. import outreach, parceldetail, skiptrace
from .. import outreach as outreach_mod
from ..classify import STAGE_ORDER, score_breakdown, timeline
from ..db import DB

STATIC = Path(__file__).parent / "static"
STATUSES = ["new", "reviewing", "contacted", "retained", "rejected"]

LEAD_SQL = """
SELECT l.id, l.score, l.match_reason, l.distance_m, l.status, l.notes, l.created_at,
       pa.apn, pa.county AS parcel_county, pa.address, pa.owner, pa.mailing_address, pa.market_value, pa.acres,
       pa.layer, p.id AS project_id, p.name AS project, p.project_type, p.stage, p.score AS project_score,
       p.est_cost, s.source, s.url, s.agency, pa.owner_kind, pa.mailable, pa.email_contacts,
       (SELECT MAX(o.created_at) FROM outreach o WHERE o.lead_id = l.id AND o.channel = 'postcard'
          AND o.status = 'sent') AS postcard_at,
       (SELECT MAX(o.created_at) FROM outreach o WHERE o.lead_id = l.id AND o.channel = 'postcard'
          AND o.status = 'test') AS postcard_test_at,
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


class ListSave(BaseModel):
    name: str
    lead_ids: List[int] = []


class ListMembers(BaseModel):
    lead_ids: List[int]


class ListRun(BaseModel):
    postcards: bool = True
    email: bool = True
    dry_run: bool = True


class LeadUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None


OUTREACH_FILTERS = {
    "email": outreach.EMAIL_ELIGIBLE,
    "postcard": outreach.POSTCARD_ELIGIBLE,
    "contacted": "EXISTS (SELECT 1 FROM outreach o WHERE o.lead_id = l.id AND o.status = 'sent')",
    "none": "NOT EXISTS (SELECT 1 FROM outreach o WHERE o.lead_id = l.id AND o.status = 'sent')",
    "traced": "EXISTS (SELECT 1 FROM skiptraces t WHERE t.owner = pa.owner AND t.mailing_address IS pa.mailing_address "
              "AND t.matched = 1)",
}


def _lead_filters(project_id, status, county, min_score, owner_only, q, outreach_filter=None, stage=None, list_id=None):
    where, params = ["l.score >= ?"], [min_score]
    if list_id is not None:
        where.append(outreach.IN_LIST); params.append(list_id)
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
    if stage:
        where.append("p.stage = ?"); params.append(stage)
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
            "stages": [{"key": k, "label": label, "projects": n} for (k, label, _), n in
                       ((st, one("SELECT COUNT(*) n FROM projects WHERE score >= 60 AND stage = '%s'" % st[0]))
                        for st in STAGE_ORDER)],
        }

    @app.get("/api/projects")
    def projects(min_score: int = 60, county: Optional[str] = None, project_type: Optional[str] = None,
                 source: Optional[str] = None, q: Optional[str] = None, stage: Optional[str] = None,
                 limit: int = 500) -> List[Dict[str, Any]]:
        where, params = ["p.score >= ?"], [min_score]
        if county:
            where.append("s.county = ?"); params.append(county.upper())
        if project_type:
            where.append("p.project_type = ?"); params.append(project_type)
        if source:
            where.append("s.source LIKE ?"); params.append(source + "%")
        if stage:
            where.append("p.stage = ?"); params.append(stage)
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
                "contacts": db.contacts_for(lead["owner"]), "company": db.company_for(lead["owner"]),
                "skiptrace": skiptrace.cached(db, lead)}

    @app.get("/api/leads/{lead_id}/cases")
    def lead_cases(lead_id: int) -> Dict[str, Any]:
        return parceldetail.cases(one_lead(lead_id))

    @app.get("/api/outreach/hub")
    def outreach_hub(min_score: int = 60) -> Dict[str, Any]:
        """Everything the Outreach hub shows: what has gone out, who is ready to contact, and recent activity."""
        one = lambda sql, *a: db.rows(sql, a)[0]["n"]
        owners = lambda where: one(
            "SELECT COUNT(*) n FROM (SELECT DISTINCT pa.owner, pa.mailing_address FROM leads l JOIN parcels pa "
            "ON pa.id = l.parcel_id WHERE l.score >= ? AND " + where + ")", min_score)
        activity = db.rows(
            """SELECT o.id, o.created_at, o.channel, o.status, o.external_id, o.detail, l.id AS lead_id, l.score,
                      pa.owner, pa.mailing_address, pa.address, p.name AS project
               FROM outreach o JOIN leads l ON l.id = o.lead_id JOIN parcels pa ON pa.id = l.parcel_id
               JOIN projects p ON p.id = l.project_id ORDER BY o.created_at DESC, o.id DESC LIMIT 100""")
        for a in activity:
            d = json.loads(a.pop("detail") or "{}")
            a["proof_url"], a["email"], a["expected_delivery"] = d.get("proof_url"), d.get("email"), d.get("expected_delivery")
        return {
            "status": dict(outreach.status(cfg), skiptrace=skiptrace.status(cfg)),
            "sent": {
                "postcards": one("SELECT COUNT(*) n FROM outreach WHERE channel='postcard' AND status='sent'"),
                "test_proofs": one("SELECT COUNT(*) n FROM outreach WHERE channel='postcard' AND status='test'"),
                "emails": one("SELECT COUNT(*) n FROM outreach WHERE channel='email' AND status='sent'"),
                "skip_traces": one("SELECT COUNT(*) n FROM skiptraces WHERE matched=1"),
            },
            "ready": {
                "min_score": min_score,
                "postcard_owners": owners(outreach.POSTCARD_ELIGIBLE),
                "email_companies": owners(OUTREACH_FILTERS["email"]),
                "companies_with_contacts": one("SELECT COUNT(DISTINCT owner) n FROM contacts"),
                "contacts_with_email": one("SELECT COUNT(*) n FROM contacts WHERE email IS NOT NULL"),
                "individuals_untraced": owners(
                    "pa.owner_kind = 'individual' AND NOT EXISTS (SELECT 1 FROM skiptraces t WHERE t.owner = pa.owner "
                    "AND t.mailing_address IS pa.mailing_address)"),
            },
            "contacts": db.rows("""SELECT c.owner, c.name, c.title, c.email, c.linkedin, co.website, co.phone
                                   FROM contacts c LEFT JOIN companies co ON co.owner = c.owner
                                   ORDER BY c.email IS NULL, c.owner, c.name LIMIT 100"""),
            "activity": activity,
            "usage": skiptrace.usage_summary(db),
            "rates": skiptrace.rates(cfg),
            "template": outreach.template(cfg),
            "lists": outreach.list_summaries(db),
        }

    def need_list(list_id: int) -> None:
        if not db.rows("SELECT 1 FROM lists WHERE id=?", (list_id,)):
            raise HTTPException(404, "That list no longer exists.")

    @app.get("/api/lists")
    def lists() -> List[Dict[str, Any]]:
        return outreach.list_summaries(db)

    @app.post("/api/lists")
    def list_save(body: ListSave) -> Dict[str, Any]:
        """Create a list, or add to the list that already has this name."""
        if not body.name.strip():
            raise HTTPException(422, "Give the list a name.")
        list_id = db.save_list(body.name, body.lead_ids)
        return next(x for x in outreach.list_summaries(db) if x["id"] == list_id)

    @app.post("/api/lists/{list_id}/remove")
    def list_remove(list_id: int, body: ListMembers) -> Dict[str, Any]:
        need_list(list_id)
        db.remove_from_list(list_id, body.lead_ids)
        return next(x for x in outreach.list_summaries(db) if x["id"] == list_id)

    @app.delete("/api/lists/{list_id}")
    def list_delete(list_id: int) -> Dict[str, Any]:
        need_list(list_id)
        db.delete_list(list_id)
        return {"deleted": list_id}

    @app.post("/api/lists/{list_id}/campaign")
    def list_campaign(list_id: int, body: ListRun) -> Dict[str, Any]:
        """Dry run by default: reports who would get a postcard and who would get an email. With dry_run false it sends."""
        need_list(list_id)
        return guard(lambda: outreach.run_list(cfg, db, list_id, body.postcards, body.email, body.dry_run))

    @app.get("/api/outreach/template")
    def get_template() -> Dict[str, Any]:
        return outreach.template(cfg)

    @app.put("/api/outreach/template")
    def put_template(body: Dict[str, Any]) -> Dict[str, Any]:
        return outreach.save_template(cfg, db, body)

    @app.get("/api/outreach/status")
    def outreach_status() -> Dict[str, Any]:
        return dict(outreach.status(cfg), skiptrace=skiptrace.status(cfg), rates=skiptrace.rates(cfg))

    @app.post("/api/leads/{lead_id}/skip-trace")
    def skip_trace(lead_id: int) -> Dict[str, Any]:
        lead = one_lead(lead_id)
        return guard(lambda: skiptrace.trace(cfg, db, lead))

    @app.get("/api/usage")
    def usage() -> Dict[str, Any]:
        return dict(skiptrace.usage_summary(db), rates=skiptrace.rates(cfg))

    @app.get("/api/usage.csv")
    def usage_csv() -> StreamingResponse:
        rows = db.rows("SELECT created_at, kind, owner, lead_id, cost, price, ROUND(price - cost, 2) AS margin FROM usage "
                       "ORDER BY created_at DESC")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "item", "owner", "lead_id", "cost", "billed", "margin"])
        w.writerows([list(r.values()) for r in rows])
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=eminent-tracker-usage.csv"})

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
        if result["mode"] == "live":
            rate = skiptrace.rates(cfg)["postcard"]
            skiptrace.record_usage(db, "postcard", lead_id, lead["owner"], rate["cost"], rate["price"])
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
              outreach: Optional[str] = None, stage: Optional[str] = None, list_id: Optional[int] = None,
              limit: int = 300) -> List[Dict[str, Any]]:
        where, params = _lead_filters(project_id, status, county, min_score, owner_only, q, outreach, stage, list_id)
        return db.rows(LEAD_SQL + where + " ORDER BY l.score DESC, pa.market_value DESC LIMIT ?",
                       params + [min(limit, 5000)])

    @app.get("/api/leads.csv")
    def leads_csv(project_id: Optional[int] = None, status: Optional[str] = None, county: Optional[str] = None,
                  min_score: int = 0, owner_only: bool = False, q: Optional[str] = None,
                  outreach: Optional[str] = None, stage: Optional[str] = None,
                  list_id: Optional[int] = None) -> StreamingResponse:
        where, params = _lead_filters(project_id, status, county, min_score, owner_only, q, outreach, stage, list_id)
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
    # The hub is published read-only: totals, the template and the activity log, with sending switched off and
    # Apollo-sourced contacts left out.
    hub = client.get("/api/outreach/hub").json()
    hub["contacts"] = []
    hub["lists"] = []                # a firm's client lists are private to the running app
    off = "Sending is only available in the running app."
    hub["status"] = {"firm_ready": hub["status"]["firm_ready"],
                     "postcard": {"enabled": False, "mode": "off", "why": off},
                     "email": {"enabled": False, "from": hub["status"]["email"].get("from"), "why": off},
                     "skiptrace": dict(hub["status"]["skiptrace"], enabled=False, why=off)}
    for a in hub["activity"]:
        a["proof_url"] = None        # proof links are signed URLs for the account holder
    site = Path(out_dir)
    site.mkdir(parents=True, exist_ok=True)
    # The public site: a landing page and a request-access page at the root, the read-only app under /app.
    import shutil

    landing = STATIC.parent / "landing"
    owner_counties = len({c for layer in cfg.get("parcel_layers") or [] for c in layer.get("counties") or []
                          if (layer.get("fields") or {}).get("owner") or layer.get("provider") == "carto_phl"})
    fill = {"leads": "{:,}".format(stats["leads"]), "projects": "{:,}".format(stats["projects"]),
            "owners": "{:,}".format(stats["leads_with_owner"]), "counties": str(len(stats["counties"])),
            "owner_counties": str(owner_counties), "date": date.today().strftime("%B %-d, %Y")}
    # Subscribe buttons go to Stripe payment links once they exist; until then they lead to sign-up.
    pay = cfg.get("site") or {}
    fill["pay_monthly"] = pay.get("stripe_monthly_link") or "signup.html"
    fill["pay_annual"] = pay.get("stripe_annual_link") or "signup.html"
    # The owner's portrait appears once a photo is saved as landing/img/william-duerr.jpg.
    fill["photo"] = ('<figure class="portrait"><img src="img/william-duerr.jpg" width="680" height="850" loading="lazy" '
                     'alt="William Christopher Duerr"></figure>') if (landing / "img" / "william-duerr.jpg").exists() else ""
    for name in ("index.html", "signup.html", "login.html", "account.html"):
        page = (landing / name).read_text()
        for k, v in fill.items():
            page = page.replace("{{%s}}" % k, v)
        (site / name).write_text(page)
    shutil.copyfile(landing / "site.css", site / "site.css")
    shutil.copyfile(landing / "auth.js", site / "auth.js")
    # Accounts switch on when a Supabase project is configured. Its URL and "anon" key are public by design;
    # the service key must never be put here.
    web = cfg.get("site") or {}
    (site / "auth-config.js").write_text("window.ET_AUTH = %s;\n" % json.dumps(
        {"url": web.get("supabase_url") or "", "key": web.get("supabase_anon_key") or "",
         "google": bool(web.get("supabase_google"))}))
    shutil.copytree(landing / "img", site / "img", dirs_exist_ok=True)
    for stale in ("data.json",):                 # the app used to live at the root
        (site / stale).unlink(missing_ok=True)
    out = site / "app"
    out.mkdir(exist_ok=True)
    html = (STATIC / "index.html").read_text()
    from datetime import datetime

    build = datetime.now().strftime("%Y%m%d%H%M%S")
    html = html.replace("<script>\nconst $", "<script>window.EDRADAR_STATIC = true; window.EDRADAR_BUILD = \"%s\";</script>"
                        "\n<script>\nconst $" % build, 1)
    assert "EDRADAR_STATIC = true" in html
    (out / "index.html").write_text(html)
    (out / "data.json").write_text(json.dumps({"stats": stats, "projects": projects, "leads": leads, "layers": layers, "template": outreach.template(cfg), "hub": hub,
                                               "contacts": {}},
                                              separators=(",", ":")))
    (site / ".nojekyll").write_text("")
    domain = (cfg.get("site") or {}).get("domain")
    if domain:                       # GitHub Pages serves the snapshot at this address while the file is present
        (site / "CNAME").write_text(domain.strip() + "\n")
    return {"projects": len(projects), "leads": len(leads)}
