from __future__ import annotations

import csv
import io
import json
import time
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
# Laws that limit what can be offered in a state. Shown to a firm before it subscribes. Each line is something read from
# the statute or from the state's own data during research; a state not listed has no such limit that we have found.
STATE_LAW = {
    "California": "California removes owner names from public parcel records (Government Code 7928.205). Projects are listed, but no parcels or owners can be shown from public sources.",
    "New Jersey": "New Jersey's public parcel file has owner names removed (Daniel's Law). Projects are listed, but no owners can be shown from public sources.",
    "Idaho": "Idaho Code 74-120 bars using public records as a mailing list. Owner records are not offered here, and paid owner lookups are not sold.",
    "South Carolina": "S.C. Code 30-2-50 bars using public records for commercial solicitation. Owner records are not offered here, and paid owner lookups are not sold.",
    "Washington": "RCW 42.56.070(8) restricts commercial use of public-record lists of individuals, and the statewide parcel map carries no owner names. Owner records are not offered here, and paid owner lookups are not sold.",
    "Montana": "Montana law restricts using public records as mailing lists. Owner records are not offered here, and paid owner lookups are not sold.",
    "Kansas": "K.S.A. 45-230 bars using names and addresses from public records to sell a service to the people listed. Owner records are not offered here.",
    "Virginia": "Virginia's statewide parcel map has no owner names, so owners are available only in the counties and cities that publish their own.",
    "New Hampshire": "New Hampshire's statewide parcel map has no owner names, and no county source has been found.",
    "Maine": "Maine publishes parcels without owner names. Parcels in a project's path are shown, but not who owns them.",
    "North Dakota": "North Dakota publishes parcels without owner names. Parcels in a project's path are shown, but not who owns them.",
}

STATUSES = ["new", "reviewing", "contacted", "retained", "rejected", "do not contact"]

LEAD_SQL = """
SELECT l.id, l.score, l.match_reason, l.distance_m, l.status, l.notes, l.created_at,
       pa.apn, pa.county AS parcel_county, pa.address, pa.owner, pa.mailing_address, pa.market_value, pa.acres,
       pa.layer, p.id AS project_id, p.name AS project, p.project_type, p.stage, p.score AS project_score,
       p.est_cost, s.source, s.url, s.agency, s.state, pa.owner_kind, pa.mailable, pa.email_contacts,
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


def _stage_filter(stage: str, where: List[str], params: List[Any]) -> None:
    """One stage, or a timing group: "@mail" is every stage in which owners can be written to, "@now" one group."""
    from ..classify import MAIL_WINDOWS, WINDOW_OF

    if stage.startswith("@"):
        keys = MAIL_WINDOWS if stage == "@mail" else (stage[1:],)
        stages = [s for s, w in WINDOW_OF.items() if w in keys] or ["none"]
        where.append("COALESCE(p.stage, 'unknown') IN (%s)" % ",".join("?" * len(stages))); params.extend(stages)
    else:
        where.append("p.stage = ?"); params.append(stage)


def _lead_filters(project_id, status, county, min_score, owner_only, q, outreach_filter=None, stage=None, list_id=None,
                  state=None):
    where, params = ["l.score >= ?"], [min_score]
    if state:
        where.append("s.state = ?"); params.append(state.upper())
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
        _stage_filter(stage, where, params)
    return " WHERE " + " AND ".join(where), params


def create_app(cfg: Dict[str, Any]) -> FastAPI:
    db = DB(cfg["database"])
    outreach.apply_saved(cfg, db)
    app = FastAPI(title="Eminent Tracker")

    @app.get("/")
    def index() -> FileResponse:
        # no-cache: the browser revalidates, so an updated dashboard shows up without a hard refresh
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    _stats_cache: Dict[Any, Any] = {}

    @app.get("/api/stats")
    def stats(state: Optional[str] = None) -> Dict[str, Any]:
        st = state.upper() if state else None
        w = " AND s.state = '%s'" % st if st and st.isalpha() and len(st) == 2 else ""
        hit = _stats_cache.get(st)
        if hit and time.time() - hit[0] < 60:        # a dozen counts over the whole database: once a minute is plenty
            return hit[1]
        one = lambda sql: db.rows(sql)[0]["n"]
        by_stage = {r["stage"]: r["n"] for r in db.rows("SELECT p.stage, COUNT(*) AS n " + (
            "FROM projects p JOIN signals s ON s.id = p.signal_id WHERE p.score >= 60 AND p.duplicate_of IS NULL" + w) + " GROUP BY p.stage")}
        proj = "FROM projects p JOIN signals s ON s.id = p.signal_id WHERE p.score >= 60 AND p.duplicate_of IS NULL" + w
        lead = "FROM leads l JOIN parcels pa ON pa.id = l.parcel_id JOIN projects p ON p.id = l.project_id " \
               "JOIN signals s ON s.id = p.signal_id WHERE 1=1" + w
        out = {
            "state": st,
            "states": [dict(r, name=STATE_NAMES.get(r["code"], r["code"])) for r in db.rows(
                "SELECT s.state AS code, COUNT(*) AS projects FROM projects p JOIN signals s ON s.id = p.signal_id "
                "WHERE p.score >= 60 AND p.duplicate_of IS NULL GROUP BY s.state ORDER BY s.state")],
            "signals": one("SELECT COUNT(*) n FROM signals s WHERE 1=1" + w),
            "updated": one("SELECT MAX(fetched_at) n FROM signals s WHERE 1=1" + w),
            "projects": one("SELECT COUNT(*) n " + proj),
            "leads": one("SELECT COUNT(*) n " + lead),
            "leads_with_owner": one("SELECT COUNT(*) n " + lead + " AND pa.owner IS NOT NULL"),
            "counties": [r["county"] for r in db.rows(
                "SELECT DISTINCT s.county FROM signals s WHERE s.county IS NOT NULL" + w + " ORDER BY s.county")],
            "types": [r["project_type"] for r in db.rows(
                ("SELECT DISTINCT p.project_type FROM projects p JOIN signals s ON s.id = p.signal_id WHERE 1=1" + w
                 if w else "SELECT DISTINCT p.project_type FROM projects p") + " ORDER BY p.project_type")],
            "statuses": STATUSES,
            "stages": [{"key": k, "label": label, "projects": by_stage.get(k, 0)} for k, label, _ in STAGE_ORDER],
        }
        _stats_cache[st] = (time.time(), out)
        return out

    @app.get("/api/projects")
    def projects(min_score: int = 60, county: Optional[str] = None, project_type: Optional[str] = None,
                 source: Optional[str] = None, q: Optional[str] = None, stage: Optional[str] = None,
                 state: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        where, params = ["p.score >= ?", "p.duplicate_of IS NULL"], [min_score]
        if state:
            where.append("s.state = ?"); params.append(state.upper())
        if county:
            where.append("s.county = ?"); params.append(county.upper())
        if project_type:
            where.append("p.project_type = ?"); params.append(project_type)
        if source:
            where.append("s.source LIKE ?"); params.append(source + "%")
        if stage:
            _stage_filter(stage, where, params)
        if q:
            where.append("(p.name LIKE ? OR p.summary LIKE ?)"); params += ["%%%s%%" % q] * 2
        rows = db.rows(
            """SELECT p.id, p.name, p.project_type, p.stage, p.acquisition_likelihood, p.score, p.summary,
                      p.est_cost, p.contact, p.geometry, p.lat, p.lon, p.matched_at, p.engine,
                      s.source, s.url, s.agency, s.county, s.state, s.published, s.raw,
                      (SELECT COUNT(*) FROM leads l WHERE l.project_id = p.id) AS lead_count,
                      (SELECT COALESCE(o.note, 'stage set by hand') FROM stage_overrides o
                       WHERE o.source = s.source AND o.external_id = s.external_id) AS confirmed
               FROM projects p JOIN signals s ON s.id = p.signal_id
               WHERE %s ORDER BY p.score DESC, p.est_cost DESC LIMIT ?""" % " AND ".join(where),
            params + [min(limit, 20000)],
        )
        for r in rows:
            r["geometry"] = json.loads(r["geometry"]) if r["geometry"] else None
            raw = json.loads(r.pop("raw") or "{}")
            confirmed = r.pop("confirmed", None)
            r["timeline"] = timeline(r["stage"], dict(raw if isinstance(raw, dict) else {}, _confirmed=confirmed))
            r["score_parts"] = score_breakdown(r["acquisition_likelihood"], r["stage"], r["est_cost"], r["score"])
        return rows[:limit]

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
              state: Optional[str] = None, limit: int = 300) -> List[Dict[str, Any]]:
        where, params = _lead_filters(project_id, status, county, min_score, owner_only, q, outreach, stage, list_id, state)
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


US_STATES = ("Alabama,Alaska,Arizona,Arkansas,California,Colorado,Connecticut,Delaware,Florida,Georgia,Hawaii,Idaho,Illinois,"
             "Indiana,Iowa,Kansas,Kentucky,Louisiana,Maine,Maryland,Massachusetts,Michigan,Minnesota,Mississippi,Missouri,Montana,"
             "Nebraska,Nevada,New Hampshire,New Jersey,New Mexico,New York,North Carolina,North Dakota,Ohio,Oklahoma,Oregon,"
             "Pennsylvania,Rhode Island,South Carolina,South Dakota,Tennessee,Texas,Utah,Vermont,Virginia,Washington,West Virginia,"
             "Wisconsin,Wyoming").split(",")
STATE_CODES = dict(zip(US_STATES, "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
                                  "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()))
STATE_NAMES = {v: k for k, v in STATE_CODES.items()}
PRIVATE_BUCKET = "app-data"


def state_slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _upload_private(cfg: Dict[str, Any], payload: bytes, client: Any = None, state: str = "pennsylvania") -> bool:
    """Put the snapshot's data, gzipped, in the private Supabase bucket. True when it is safely there."""
    import gzip
    import os
    import sys

    import httpx

    url, key = (cfg.get("site") or {}).get("supabase_url"), os.environ.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        return False
    # Once private publishing is set up, a failed upload stops the publish. It used to fall back to writing every lead to
    # the open site, which only GitHub's file-size limit prevented on the one day the network dropped.
    import time

    last: Any = None
    for attempt in range(4):
        try:
            r = (client or httpx.Client(timeout=180)).post(
                "%s/storage/v1/object/%s/states/%s.json.gz" % (url.rstrip("/"), PRIVATE_BUCKET, state), content=gzip.compress(payload, 6),
                headers={"Authorization": "Bearer " + key, "apikey": key, "x-upsert": "true",
                         "Content-Type": "application/gzip", "Cache-Control": "max-age=300"})
            if r.status_code >= 400:
                raise RuntimeError("%s %s" % (r.status_code, r.text[:200]))
            return True
        except Exception as e:
            last = e
            print("  upload of %s failed (%s), try %d of 4" % (state, str(e)[:120], attempt + 1), file=sys.stderr)
            if client is None:
                time.sleep(20 * (attempt + 1))
    raise RuntimeError("The private data upload for %s failed four times (%s). Nothing was published; the live site is unchanged." % (state, last))


def _md(text: str) -> str:
    """The small part of Markdown the blog uses: ## headings, paragraphs, - lists, **bold**, *italic*, [links](url)."""
    import html as _html
    import re

    def inline(t: str) -> str:
        t = _html.escape(t, quote=False)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*", r"<em>\1</em>", t)
        return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+|/[^)\s]*)\)", r'<a href="\2">\1</a>', t)

    out: List[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if lines[0].startswith("## "):
            out.append("<h2>%s</h2>" % inline(lines[0][3:].strip()))
        elif all(ln.lstrip().startswith("- ") for ln in lines):
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % inline(ln.lstrip()[2:]) for ln in lines))
        else:
            out.append("<p>%s</p>" % inline(" ".join(ln.strip() for ln in lines)))
    return "\n".join(out)


def _build_blog(landing: Path, site: Path, fill: Dict[str, str], domain: str) -> None:
    """Render landing/blog/*.md to /blog/, and write sitemap.xml for every public page."""
    import html as _html
    from datetime import date as _date

    shell = (landing / "index.html").read_text()
    head = shell[:shell.index('<a class="skip"')]
    header = shell[shell.index('<a class="skip"'):shell.index("<main")].replace('href="#', 'href="/#')
    footer = shell[shell.index("<footer>"):shell.index("</footer>") + 9]
    for k, v in fill.items():
        head, header, footer = (x.replace("{{%s}}" % k, v) for x in (head, header, footer))
    base = "https://%s" % domain if domain else ""

    def page(path: str, title: str, desc: str, body: str) -> None:
        import re
        h = head.replace("<title>EminentTracker: eminent domain leads, years before the notice</title>",
                         "<title>%s · EminentTracker</title>" % _html.escape(title))
        h = re.sub(r'<meta name="description" content="[^"]*">',
                   '<meta name="description" content="%s">\n<base href="/">\n<link rel="canonical" href="%s/%s">'
                   % (_html.escape(desc, quote=True), base, path), h, 1)
        h = re.sub(r'<meta property="og:title" content="[^"]*">', '<meta property="og:title" content="%s">' % _html.escape(title, quote=True), h, 1)
        (site / path).parent.mkdir(parents=True, exist_ok=True)
        (site / (path if path.endswith(".html") else path + "index.html")).write_text(
            h + header + '<main id="main" class="doc in">\n' + body + "\n</main>\n\n" + footer + "\n</body>\n</html>\n")

    posts = []
    for f in sorted((landing / "blog").glob("*.md"), reverse=True):
        raw = f.read_text()
        meta: Dict[str, str] = {}
        if raw.startswith("---"):
            front, raw = raw[3:].split("\n---", 1)
            meta = dict((k.strip(), v.strip()) for k, v in (ln.split(":", 1) for ln in front.strip().splitlines() if ":" in ln))
        when = _date.fromisoformat(meta.get("date") or f.name[:10])
        posts.append({"slug": f.stem[11:] if f.stem[:4].isdigit() else f.stem, "title": meta.get("title") or f.stem,
                      "desc": meta.get("description") or "", "date": when, "html": _md(raw)})
    posts.sort(key=lambda x: x["date"], reverse=True)
    nice = lambda d: d.strftime("%B %-d, %Y")
    for x in posts:
        page("blog/%s.html" % x["slug"], x["title"], x["desc"],
             '<p class="updated"><a href="blog/">Blog</a> · <time datetime="%s">%s</time></p>\n<h1>%s</h1>\n%s\n'
             '<p class="post-cta"><a class="btn" href="signup.html">See the leads in your state</a></p>'
             % (x["date"].isoformat(), nice(x["date"]), _html.escape(x["title"]), x["html"]))
    listing = "".join('<li><a href="blog/%s.html">%s</a><time datetime="%s">%s</time><p>%s</p></li>'
                      % (x["slug"], _html.escape(x["title"]), x["date"].isoformat(), nice(x["date"]), _html.escape(x["desc"])) for x in posts)
    page("blog/", "Blog", "Notes on eminent domain practice, public project planning and reaching property owners early.",
         '<h1>Blog</h1>\n<p class="updated">Eminent domain practice, public project planning and reaching owners early.</p>\n'
         '<ul class="posts">%s</ul>' % listing)
    if base:
        urls = [("", None), ("blog/", posts[0]["date"] if posts else None)] + [("blog/%s.html" % x["slug"], x["date"]) for x in posts] \
            + [(n, None) for n in ("signup.html", "terms.html", "privacy.html", "compliance.html")]
        (site / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "".join("  <url><loc>%s/%s</loc>%s</url>\n" % (base, u, "<lastmod>%s</lastmod>" % d.isoformat() if d else "") for u, d in urls)
            + "</urlset>\n")


def export_static(cfg: Dict[str, Any], out_dir: str, min_score: int = 40, upload_client: Any = None) -> Dict[str, int]:
    """Write a read-only copy of the dashboard (index.html + data.json) that any static host can serve."""
    from datetime import date

    from fastapi.testclient import TestClient

    client = TestClient(create_app(cfg))
    db = DB(cfg["database"])
    # Parcels added by a matching run that was started by hand have not been sorted into owner types and usable
    # mailing addresses yet. Without this they publish with no Postcard button and count as having no address.
    try:
        db.refresh_outreach_flags()
    except Exception as e:           # another run holds the database: publish with what is already classified
        print("  owner types were not refreshed: %s" % str(e)[:100])
    layer_cfg = {x.get("name"): x for x in cfg.get("parcel_layers") or []}
    keep = ("id", "project_id", "project_score", "score", "match_reason", "distance_m", "status", "apn", "parcel_county", "address",
            "owner", "mailing_address", "market_value", "acres", "owner_kind", "mailable", "email_contacts")
    extra = {r["lead_id"]: r for r in db.rows(
        "SELECT l.id AS lead_id, pa.layer, pa.attributes, pa.apn FROM leads l JOIN parcels pa ON pa.id = l.parcel_id")}

    def state_data(code: Optional[str], with_leads: bool = True) -> Dict[str, Any]:
        """Everything the app shows for one state (or for all of them when code is None)."""
        q = {"state": code} if code else {}
        st = client.get("/api/stats", params=q).json()
        st["generated"] = date.today().isoformat()
        projs = client.get("/api/projects", params=dict(q, min_score=min_score, limit=20000)).json()
        layers: List[Dict[str, Any]] = []      # each lead points at one of these so the page can fetch its own boundary
        layer_index: Dict[str, int] = {}
        rows_out = []
        where, params = (" WHERE s.state = ?", (code,)) if code else ("", ())
        for r in (db.rows(LEAD_SQL + where + " ORDER BY l.score DESC, pa.market_value DESC", params) if with_leads else []):
            x = extra.get(r["id"])
            if x is None:        # added by a matching run after this export began; it goes out with the next one
                continue
            row = {k: r[k] for k in keep}
            parcel = {"apn": x["apn"], "attributes": json.loads(x["attributes"] or "{}")}
            facts = parceldetail.background(parcel)["facts"]
            prof = parceldetail.owner_profile(r, facts, [])
            row["facts"] = [[f["label"], f["value"]] for f in facts]
            row["own"] = [prof["location"], prof["occupancy"]]
            layer = layer_cfg.get(x["layer"])
            bq = parceldetail.boundary_query(layer, parcel) if layer else None
            if bq:
                if x["layer"] not in layer_index:
                    layer_index[x["layer"]] = len(layers)
                    layers.append({"name": x["layer"], "url": bq.pop("url"), "provider": bq.pop("provider"), "field": bq.pop("field")})
                row["bq"] = dict({k: v for k, v in bq.items() if k not in ("url", "provider", "field")}, l=layer_index[x["layer"]])
            rows_out.append(row)
        return {"stats": st, "projects": projs, "leads": rows_out, "layers": layers, "state": code}

    # With several states, every lead is written once, in its own state's file. Building them all a second time
    # for a combined file took most of an hour, so that is done only if the private upload fails.
    whole = state_data(None, with_leads=False)
    stats, projects = whole["stats"], whole["projects"]
    many = len(stats.get("states") or []) > 1
    if not many:
        whole = state_data(None)
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
    # Subscribers see their own usage, fetched live with their sign-in. What the operator mailed, paid and earned
    # in the running app is never published.
    hub["activity"] = []
    hub["usage"] = {"this_month": [], "all_time": []}
    hub["rates"] = {k: {"price": v["price"]} for k, v in hub["rates"].items()}
    hub["sent"] = {k: 0 for k in hub["sent"]}
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
    fill["photo_class"] = "" if (landing / "img" / "william-duerr.jpg").exists() else " no-photo"
    # Subscribe buttons lead to sign-up with the plan preselected; payment follows as soon as the account exists.
    fill.update(pay_state_monthly="signup.html", pay_state_annual="signup.html?bill=year",
                pay_all_monthly="signup.html?plan=all", pay_all_annual="signup.html?plan=all&amp;bill=year")
    # The owner's portrait appears once a photo is saved as landing/img/william-duerr.jpg.
    fill["photo"] = ('<figure class="portrait"><img src="img/william-duerr.jpg" width="520" height="520" loading="lazy" '
                     'alt="William Christopher Duerr"></figure>') if (landing / "img" / "william-duerr.jpg").exists() else ""
    fill["contact_email"] = (cfg.get("site") or {}).get("contact_email") or "info@eminenttracker.com"
    live = (cfg.get("site") or {}).get("live_states") or [
        STATE_NAMES.get(x["code"], x["code"]) for x in stats.get("states") or []] or ["Pennsylvania"]
    fill["live_states"] = ", ".join(live)
    fill["state_options"] = "".join('<option value="%s">%s%s</option>' % (n, n, "" if n in live else " (coming)") for n in US_STATES)
    fill["state_names"] = "|".join(US_STATES)
    # What each state holds today, shown before anyone pays: projects, parcels in their path, and how many of those
    # parcels come with an owner and a usable mailing address.
    per = {r["code"]: r for r in db.rows(
        """SELECT s.state AS code, COUNT(DISTINCT l.parcel_id) AS parcels,
                  COUNT(DISTINCT CASE WHEN pa.owner IS NOT NULL THEN l.parcel_id END) AS owners,
                  COUNT(DISTINCT CASE WHEN pa.mailable = 1 AND pa.owner_kind IN ('individual', 'company') THEN l.parcel_id END) AS mailable
           FROM leads l JOIN parcels pa ON pa.id = l.parcel_id JOIN projects p ON p.id = l.project_id
           JOIN signals s ON s.id = p.signal_id GROUP BY s.state""")}
    by_state = []
    for x in stats.get("states") or []:
        name, c = STATE_NAMES.get(x["code"], x["code"]), per.get(x["code"]) or {}
        if name in US_STATES:
            by_state.append({"code": x["code"], "state": name, "projects": x["projects"], "parcels": c["parcels"] if c else 0,
                             "owners": c["owners"] if c else 0, "mailable": c["mailable"] if c else 0})
    by_state.sort(key=lambda r: r["state"])
    num = "{:,}".format
    fill["coverage_rows"] = "".join(
        '<tr><th scope="row">%s</th><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            r["state"], num(r["projects"]), num(r["parcels"]), num(r["owners"]), num(r["mailable"])) for r in by_state)
    fill["coverage_total"] = '<tr><th scope="row">All %d states</th><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
        len(by_state), *(num(sum(r[k] for r in by_state)) for k in ("projects", "parcels", "owners", "mailable")))
    fill["state_counts"] = json.dumps({r["state"]: [r["projects"], r["parcels"], r["owners"], r["mailable"]] for r in by_state})
    # What a firm is shown about its state before it pays: how many counties have owner records, and what the law there
    # keeps out of reach. Said plainly, so nobody subscribes expecting data that does not exist.
    from ..config import coverage as _cov

    reach = {r["state"]: r for r in _cov(cfg)}
    fill["state_limits"] = json.dumps({name: {"counties": (reach.get(name) or {}).get("counties"), "with_owners": (reach.get(name) or {}).get("counties_owners"),
                                              "law": STATE_LAW.get(name)} for name in US_STATES})
    (site / "states.json").write_text(json.dumps(by_state))
    for name in ("index.html", "signup.html", "login.html", "account.html", "terms.html", "privacy.html", "compliance.html", "admin.html"):
        page = (landing / name).read_text()
        for k, v in fill.items():
            page = page.replace("{{%s}}" % k, v)
        (site / name).write_text(page)
    _build_blog(landing, site, fill, (cfg.get("site") or {}).get("domain") or "")
    shutil.copyfile(landing / "site.css", site / "site.css")
    shutil.copyfile(landing / "auth.js", site / "auth.js")
    shutil.copyfile(landing / "track.js", site / "track.js")
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
    from datetime import datetime, timedelta

    build = datetime.now().strftime("%Y%m%d%H%M%S")
    html = html.replace("<script>\nconst $", "<script>window.EDRADAR_STATIC = true; window.EDRADAR_BUILD = \"%s\";</script>"
                        "\n<script>\nconst $" % build, 1)
    assert "EDRADAR_STATIC = true" in html
    if (cfg.get("site") or {}).get("supabase_url"):
        # Signed-in firms only: the page stays blank until a session is confirmed, otherwise it goes to sign-in.
        # This keeps visitors out of the screen; the data file itself is still a public file on the host.
        gate = ('<meta name="robots" content="noindex">\n<style>html.gate body { visibility: hidden; }</style>\n'
                '<script>document.documentElement.classList.add("gate"); window.ET_TOKEN = new Promise(r => window.__etToken = r);</script>\n'
                '<script src="../auth-config.js"></script>\n<script src="../track.js" defer></script>\n'
                '<script type="module">import {supa} from "../auth.js";\n'
                'const s = await supa(), got = await s.auth.getSession().catch(() => ({data: {}}));\n'
                'const session = got.data && got.data.session;\n'
                'const meta = (session && session.user.app_metadata) || {};\n'
                'if (!session) location.replace("../login.html?next=app");\n'
                'else if (meta.access !== "active") location.replace("../account.html?subscribe=1");\n'
                'else { document.documentElement.classList.remove("gate");\n'
                '  // The sign-in is renewed in the background, so a page left open still has a valid one when it orders something.\n'
                '  window.ET_FRESH = async () => { const g = await s.auth.getSession(); return g.data.session ? g.data.session.access_token : session.access_token; };\n'
                '  window.__etToken({token: session.access_token, plan: meta.plan, state: meta.state, admin: meta.role === "admin"}); }</script>\n')
        html = html.replace("</head>", gate + "</head>", 1)
        assert "login.html?next=app" in html
    common = {"template": outreach.template(cfg), "hub": hub, "contacts": {}}
    payload = None if many else json.dumps(dict(whole, **common), separators=(",", ":")).encode()
    # With a Supabase secret key each state's data goes to a private bucket that only subscribers to that state
    # can read, and no copy is published on the open site. Only a setup with no secret key at all (a local trial) writes
    # the data file to the site; a failed upload stops the publish instead.
    codes = [x["code"] for x in stats.get("states") or []] or ["PA"]
    live_slugs, private = [], True
    for code in codes:
        slug = state_slug(STATE_NAMES.get(code, code))
        one = payload if len(codes) == 1 else json.dumps(dict(state_data(code), **common), separators=(",", ":")).encode()
        if _upload_private(cfg, one, upload_client, slug):
            live_slugs.append(slug)
        else:
            private = False
            break
    if private:
        html = html.replace('window.EDRADAR_STATIC = true;', 'window.EDRADAR_STATIC = true; window.EDRADAR_PRIVATE = true; '
                            'window.EDRADAR_STATES = %s;' % json.dumps(live_slugs), 1)
        (out / "data.json").unlink(missing_ok=True)
    else:
        (out / "data.json").write_bytes(payload or json.dumps(dict(state_data(None), **common), separators=(",", ":")).encode())
    (out / "index.html").write_text(html)
    from ..config import coverage as _coverage

    counts = {x["code"]: x["projects"] for x in stats.get("states") or []}
    (site / "coverage.json").write_text(json.dumps(
        [dict(r, live=r["code"] in counts, scored_projects=counts.get(r["code"], 0)) for r in _coverage(cfg)]))
    from ..config import registry as _registry

    reg = _registry(cfg, db)
    reg["generated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    (site / "sources.json").write_text(json.dumps(reg, separators=(",", ":")))
    # What the daily run last did, for the admin console's system status screen. Counts and times only.
    one = lambda sql: (db.rows(sql) or [{"v": None}])[0]["v"]
    on = [x for x in reg["sources"] if x["status"] == "on"]
    cutoff = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
    # A source is in trouble when its last attempt failed, or it has not answered in four days. Only sources whose
    # reads have been recorded are judged: the newest record's date says nothing about whether a source still answers.
    judged = [x for x in on if x["kind"] == "projects" and x.get("measured")]
    quiet = [x for x in judged if x.get("error") or (x.get("updated") or "") < cutoff]
    (site / "status.json").write_text(json.dumps({
        "published": datetime.now().astimezone().isoformat(timespec="seconds"),
        "last_collection": one("SELECT MAX(updated_at) v FROM signals"), "last_new_record": one("SELECT MAX(fetched_at) v FROM signals"),
        "last_match": one("SELECT MAX(matched_at) v FROM projects"),
        "new_records_24h": one("SELECT COUNT(*) v FROM signals WHERE fetched_at >= datetime('now', '-1 day')"),
        "matched_24h": one("SELECT COUNT(*) v FROM projects WHERE matched_at >= datetime('now', '-1 day')"),
        "waiting_to_match": one("SELECT COUNT(*) v FROM projects WHERE score >= 60 AND duplicate_of IS NULL AND matched_at IS NULL AND geometry IS NOT NULL"),
        "projects": one("SELECT COUNT(*) v FROM projects WHERE score >= 60 AND duplicate_of IS NULL"), "leads": one("SELECT COUNT(*) v FROM leads"),
        "stages": {r["stage"] or "unknown": r["n"] for r in db.rows(
            "SELECT stage, COUNT(*) n FROM projects WHERE score >= 60 AND duplicate_of IS NULL GROUP BY stage")},
        "stages_confirmed": one("SELECT COUNT(*) v FROM stage_overrides"),
        "sources_on": len(on), "project_sources_on": sum(1 for x in on if x["kind"] == "projects"),
        "owner_files_on": sum(1 for x in on if x["kind"] == "owners"),
        "owner_files_held": sum(1 for x in reg["sources"] if x["status"] == "held"),
        "quiet_sources": [{"state": x["state"], "name": x["name"], "updated": x.get("updated"), "error": x.get("error")} for x in quiet[:80]],
        "quiet_count": len(quiet), "sources_judged": len(judged),
    }))
    (site / ".nojekyll").write_text("")
    domain = (cfg.get("site") or {}).get("domain")
    (site / "robots.txt").write_text("User-agent: *\nDisallow: /app/\nDisallow: /account.html\nDisallow: /admin.html\n"
                                     + ("Sitemap: https://%s/sitemap.xml\n" % domain if domain else ""))
    domain = (cfg.get("site") or {}).get("domain")
    if domain:                       # GitHub Pages serves the snapshot at this address while the file is present
        (site / "CNAME").write_text(domain.strip() + "\n")
    return {"projects": len(projects), "leads": stats["leads"]}
