"""Owner outreach: postcards through Lob and email sequences through Apollo.

Nothing here sends on its own. Every send is one explicit request from the dashboard, and
both services stay switched off until their API key is present in the environment:

    LOB_API_KEY     test_... keys render proofs without mailing; live_... keys mail and bill
    APOLLO_API_KEY  an Apollo API key with access to people search, contacts and sequences
"""
from __future__ import annotations

import html
import os
import re
from typing import Any, Dict, List, Optional

import httpx

LOB_URL = "https://api.lob.com/v1/postcards"
APOLLO_URL = "https://api.apollo.io/api/v1"

GOVERNMENT = re.compile(
    r"\b(COMMONWEALTH|CITY OF|COUNTY OF|COUNTY|BOROUGH|TOWNSHIP|TWP|SCHOOL DIST|AUTHORITY|AUTH\b|UNITED STATES|"
    r"U ?S ?A\b|PENNDOT|DEPT\b|DEPARTMENT|TURNPIKE|REDEVELOPMENT|MUNICIPAL|PHILA AUTH|DCNR|GAME COMMISSION|"
    r"HOUSING AUTH|SEPTA|PORT AUTH)", re.I)
COMPANY = re.compile(
    r"\b(LLC|L L C|INC|CORP|CORPORATION|COMPANY|CO\b|LP\b|L P\b|LLP|LTD|PARTNERS|PARTNERSHIP|ASSOC|ASSN|"
    r"ENTERPRISES|PROPERTIES|HOLDINGS|REALTY|BANK|CHURCH|TRUSTEES?|FOUNDATION|GROUP|INVESTMENTS?|RAILROAD|"
    r"DEVELOPMENT|MANAGEMENT|ASSOCIATES|CLUB|FUND|VENTURES|CAPITAL)\b", re.I)
ADDRESS_TAIL = re.compile(r"^(?P<head>.*?)[,\s]+(?P<state>[A-Z]{2})[,\s]+(?P<zip>\d{5})(?:-?\d{4})?[,\s\d-]*$")
STREET_START = re.compile(r"^(\d|P\.? ?O\.? ?BOX|PO BOX|RR |HC |ONE |TWO )", re.I)


def owner_kind(owner: Optional[str]) -> str:
    """government | company | individual | unknown. Public bodies are never solicited."""
    if not owner:
        return "unknown"
    if GOVERNMENT.search(owner):
        return "government"
    return "company" if COMPANY.search(owner) else "individual"


def parse_address(text: Optional[str]) -> Optional[Dict[str, str]]:
    """Split a one-line mailing address into the parts a mail service needs.

    County files publish addresses as loose text ("C/O X, PO BOX 1, KANE, PA, 16735"), so this
    is deliberately strict: it returns None rather than guess, and the lead is flagged for review.
    """
    if not text:
        return None
    m = ADDRESS_TAIL.match(" ".join(text.upper().split()))
    if not m:
        return None
    head = [p.strip() for p in m.group("head").split(",") if p.strip()]
    if not head:
        return None
    if len(head) == 1:  # "221 HOLYOKE RD BUTLER": street and city share one segment
        words = head[0].split()
        if len(words) < 3:
            return None
        suffixes = {"RD", "ROAD", "ST", "STREET", "AVE", "AVENUE", "DR", "DRIVE", "LN", "LANE", "BLVD", "HWY",
                    "PIKE", "WAY", "CT", "COURT", "PL", "CIR", "TER", "PKWY", "N", "S", "E", "W"}
        cut = max((i for i, w in enumerate(words) if w.strip(".") in suffixes or w.isdigit()), default=-1)
        if cut < 1 or cut >= len(words) - 1:
            return None
        head = [" ".join(words[:cut + 1]), " ".join(words[cut + 1:])]
    city = head[-1]
    lines = head[:-1]
    care_of = [x for x in lines if x.startswith(("C/O", "ATTN", "%"))]
    streets = [x for x in lines if x not in care_of]
    if not streets or not STREET_START.match(streets[-1]) and not any(STREET_START.match(s) for s in streets):
        return None
    line1 = next(s for s in reversed(streets) if STREET_START.match(s))
    extra = [s for s in streets if s != line1]
    if any(ch.isdigit() for ch in city):
        return None
    return {"line1": line1[:64], "line2": ", ".join(care_of + extra)[:64], "city": city[:40],
            "state": m.group("state"), "zip": m.group("zip")}


def status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """What the dashboard may offer right now, and why not when it may not."""
    o = cfg.get("outreach") or {}
    firm = o.get("firm") or {}
    firm_ready = all(firm.get(k) for k in ("name", "address_line1", "city", "state", "zip"))
    lob_key = os.environ.get("LOB_API_KEY", "")
    apollo = o.get("apollo") or {}
    return {
        "firm_ready": firm_ready,
        "postcard": {
            "enabled": bool(lob_key) and firm_ready,
            "mode": "live" if lob_key.startswith("live_") else "test" if lob_key else "off",
            "why": None if lob_key and firm_ready else
            "Add the firm's return address under outreach.firm in config.yaml." if lob_key else
            "Set LOB_API_KEY to turn on postcards.",
        },
        "email": {
            "enabled": bool(os.environ.get("APOLLO_API_KEY")) and bool(apollo.get("sequence_id")),
            "from": apollo.get("from_email"),
            "why": None if os.environ.get("APOLLO_API_KEY") and apollo.get("sequence_id") else
            "Set outreach.apollo.sequence_id in config.yaml." if os.environ.get("APOLLO_API_KEY") else
            "Set APOLLO_API_KEY to turn on email.",
        },
    }


# ---------------------------------------------------------------- postcards (Lob)
CARD_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{width:6.25in;height:4.25in;font-family:Georgia,'Times New Roman',serif;color:#14202b}
.safe{position:absolute;inset:0.25in;display:flex;flex-direction:column}
"""

FRONT = """<html><head><style>%(css)s
body{background:#f6f3ec}
h1{font-size:25pt;line-height:1.12;font-weight:700;letter-spacing:-0.01em;max-width:4.9in}
p{font-family:Helvetica,Arial,sans-serif;font-size:11.5pt;line-height:1.4;margin-top:0.16in;max-width:4.7in}
.firm{margin-top:auto;font-family:Helvetica,Arial,sans-serif;font-size:10pt;letter-spacing:0.02em;
border-top:1.5pt solid #14202b;padding-top:0.09in;display:flex;justify-content:space-between}
</style></head><body><div class="safe">
<h1>A public project is planned near your property%(place)s.</h1>
<p>%(project)s is in %(stage)s. Projects like this can require the government to acquire private land or easements. Owners are entitled to just compensation, and the first offer is not always the full amount.</p>
<div class="firm"><span>%(firm)s</span><span>%(phone)s</span></div>
</div></body></html>"""

BACK = """<html><head><style>%(css)s
body{background:#fff}
.msg{width:2.75in;font-family:Helvetica,Arial,sans-serif;font-size:9.5pt;line-height:1.42}
.msg b{font-family:Georgia,serif;font-size:11.5pt;display:block;margin-bottom:0.08in}
.msg p{margin-top:0.08in}
.fine{margin-top:auto;width:2.75in;font-family:Helvetica,Arial,sans-serif;font-size:6.6pt;line-height:1.3;color:#3d4a57}
</style></head><body><div class="safe">
<div class="msg"><b>Know your rights before you sign.</b>
<p>Public records show your parcel (%(apn)s) lies along the planned route. We represent Pennsylvania property owners, not the agencies taking the land.</p>
<p>A first conversation costs nothing: %(phone)s%(email)s.</p></div>
<div class="fine">%(label)s%(firm)s, %(firm_addr)s. This notice is based on public planning records. No taking of your property has been announced, and receiving this card does not mean one will occur.</div>
</div></body></html>"""


def postcard_html(cfg: Dict[str, Any], lead: Dict[str, Any]) -> Dict[str, str]:
    firm = (cfg.get("outreach") or {}).get("firm") or {}
    pc = (cfg.get("outreach") or {}).get("postcard") or {}
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    place = lead.get("address") or ""
    project = re.sub(r"\s*\(MPMS \d+\)", "", lead.get("project") or "A public project")
    vals = {
        "css": CARD_CSS,
        "place": " at " + e(place.title()) if place else "",
        "project": e(project),
        "stage": e((lead.get("stage") or "planning").replace("_", " ")),
        "firm": e(firm.get("name") or "Your firm name"),
        "phone": e(firm.get("phone") or ""),
        "email": " or " + e(firm["email"]) if firm.get("email") else "",
        "apn": e(lead.get("apn")),
        "label": "ADVERTISEMENT. " if pc.get("advertising_label", True) else "",
        "firm_addr": e(", ".join(str(firm.get(k)) for k in ("address_line1", "city", "state", "zip") if firm.get(k))),
    }
    return {"front": FRONT % vals, "back": BACK % vals}


def postcard_check(cfg: Dict[str, Any], lead: Dict[str, Any]) -> Dict[str, Any]:
    """Everything the confirmation step shows: who it goes to, the artwork, and any blocker."""
    kind = owner_kind(lead.get("owner"))
    to = parse_address(lead.get("mailing_address"))
    blocker = None
    if kind == "government":
        blocker = "This parcel is owned by a public body."
    elif kind == "unknown":
        blocker = "No owner of record for this parcel."
    elif not to:
        blocker = "The mailing address could not be read reliably. Confirm it with the county first."
    return {"owner_kind": kind, "to_name": (lead.get("owner") or "")[:40], "to": to, "blocker": blocker,
            **postcard_html(cfg, lead)}


def send_postcard(cfg: Dict[str, Any], lead: Dict[str, Any], client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    check = postcard_check(cfg, lead)
    if check["blocker"]:
        raise ValueError(check["blocker"])
    st = status(cfg)["postcard"]
    if not st["enabled"]:
        raise ValueError(st["why"])
    firm = cfg["outreach"]["firm"]
    to = check["to"]
    data = {
        "description": "Eminent Tracker lead %s" % lead["id"],
        "to[name]": check["to_name"], "to[address_line1]": to["line1"], "to[address_line2]": to["line2"],
        "to[address_city]": to["city"], "to[address_state]": to["state"], "to[address_zip]": to["zip"],
        "from[name]": firm["name"][:40], "from[address_line1]": firm["address_line1"],
        "from[address_city]": firm["city"], "from[address_state]": firm["state"], "from[address_zip]": str(firm["zip"]),
        "front": check["front"], "back": check["back"], "size": "4x6", "use_type": "marketing",
        "metadata[lead_id]": str(lead["id"]),
    }
    client = client or httpx.Client(timeout=60)
    r = client.post(LOB_URL, data={k: v for k, v in data.items() if v}, auth=(os.environ["LOB_API_KEY"], ""),
                    headers={"Idempotency-Key": "edradar-lead-%s" % lead["id"]})
    if r.status_code >= 400:
        msg = (r.json().get("error") or {}).get("message") if "json" in r.headers.get("content-type", "") else r.text
        raise RuntimeError("Lob declined the postcard: %s" % (msg or r.status_code))
    body = r.json()
    return {"external_id": body.get("id"), "mode": st["mode"], "proof_url": body.get("url"),
            "expected_delivery": body.get("expected_delivery_date")}


# ---------------------------------------------------------------- email (Apollo)
OWNER_TITLES = ["owner", "president", "managing member", "principal", "partner", "ceo", "manager", "founder"]


def clean_company(owner: str) -> str:
    name = re.sub(r"\b(C/O|ATTN|%).*$", "", owner, flags=re.I)
    return " ".join(name.replace(",", " ").split()).title()


class Apollo:
    def __init__(self, cfg: Dict[str, Any], client: Optional[httpx.Client] = None):
        self.cfg = (cfg.get("outreach") or {}).get("apollo") or {}
        self.client = client or httpx.Client(timeout=60)
        self.headers = {"X-Api-Key": os.environ.get("APOLLO_API_KEY", ""), "Content-Type": "application/json",
                        "Cache-Control": "no-cache"}

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        r = self.client.post(APOLLO_URL + path, json=body, headers=self.headers)
        if r.status_code >= 400:
            raise RuntimeError("Apollo %s returned %s: %s" % (path, r.status_code, r.text[:200]))
        return r.json()

    def find_contacts(self, lead: Dict[str, Any]) -> List[Dict[str, Any]]:
        """People at the owning company who could decide about the property. Search spends no credits."""
        if owner_kind(lead.get("owner")) != "company":
            raise ValueError("Email lookup works for company-owned parcels. Use a postcard for individual owners.")
        body = {"q_organization_name": clean_company(lead["owner"]), "person_titles": OWNER_TITLES,
                "organization_locations": ["Pennsylvania, US"], "per_page": 5, "page": 1}
        people = self._post("/mixed_people/api_search", body).get("people") or []
        if not people:  # the owning entity is often registered out of state
            body.pop("organization_locations")
            people = self._post("/mixed_people/api_search", body).get("people") or []
        return [{"person_id": p.get("id"),
                 "name": " ".join(x for x in (p.get("first_name"), p.get("last_name") or p.get("last_name_obfuscated")) if x),
                 "title": p.get("title"), "organization": (p.get("organization") or {}).get("name"),
                 "has_email": bool(p.get("has_email", True))} for p in people]

    def enroll(self, lead: Dict[str, Any], person_id: str) -> Dict[str, Any]:
        """Reveal the person's work email (1 Apollo credit), save them as a contact, add them to the sequence."""
        person = (self._post("/people/match", {"id": person_id, "reveal_personal_emails": False}).get("person")) or {}
        email = person.get("email")
        if not email or "not_unlocked" in email:
            raise RuntimeError("Apollo has no deliverable email for this person.")
        contact = self._post("/contacts", {
            "first_name": person.get("first_name"), "last_name": person.get("last_name"), "email": email,
            "title": person.get("title"), "organization_name": (person.get("organization") or {}).get("name"),
            "label_names": ["Eminent Tracker"], "run_dedupe": True,
        }).get("contact") or {}
        seq = str(self.cfg["sequence_id"])
        self._post("/emailer_campaigns/%s/add_contact_ids" % seq, {
            "emailer_campaign_id": seq, "contact_ids": [contact["id"]],
            "send_email_from_email_account_id": self.cfg["email_account_id"],
        })
        return {"external_id": contact["id"], "email": email, "name": person.get("name"), "sequence_id": seq}


# ---------------------------------------------------------------- postcard campaigns
POSTCARD_ELIGIBLE = """
    pa.mailable = 1 AND pa.owner_kind IN ('individual', 'company') AND l.status NOT IN ('rejected', 'retained')
    AND NOT EXISTS (SELECT 1 FROM outreach o JOIN leads l2 ON l2.id = o.lead_id JOIN parcels p2 ON p2.id = l2.parcel_id
                    WHERE o.channel = 'postcard' AND p2.owner = pa.owner AND p2.mailing_address = pa.mailing_address)
"""


def campaign_leads(db: Any, min_score: int = 80, project_id: Optional[int] = None, county: Optional[str] = None,
                   limit: int = 25) -> List[Dict[str, Any]]:
    """The next owners a campaign would mail: highest score first, one card per owner and address."""
    from .web.app import LEAD_SQL

    where, params = [POSTCARD_ELIGIBLE, "l.score >= ?"], [min_score]
    if project_id is not None:
        where.append("l.project_id = ?"); params.append(project_id)
    if county:
        where.append("UPPER(pa.county) = ?"); params.append(county.upper())
    rows = db.rows(LEAD_SQL + " WHERE " + " AND ".join(where) + " ORDER BY l.score DESC, pa.market_value DESC", params)
    seen, out = set(), []
    for r in rows:
        key = (r["owner"], r["mailing_address"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out[:max(0, limit)] if limit is not None else out


def run_campaign(cfg: Dict[str, Any], db: Any, min_score: int = 80, project_id: Optional[int] = None,
                 county: Optional[str] = None, limit: int = 25, dry_run: bool = True,
                 client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    """Mail one postcard to each eligible owner, up to `limit`. A dry run only reports who would be mailed."""
    leads = campaign_leads(db, min_score, project_id, county, limit)
    st = status(cfg)["postcard"]
    out: Dict[str, Any] = {"mode": st["mode"], "dry_run": dry_run, "eligible": len(leads), "sent": [], "failed": [],
                           "recipients": [{"lead_id": r["id"], "owner": r["owner"], "mailing_address": r["mailing_address"],
                                           "score": r["score"], "project": r["project"]} for r in leads]}
    if dry_run:
        return out
    if not st["enabled"]:
        raise ValueError(st["why"])
    for r in leads:
        try:
            res = send_postcard(cfg, r, client)
            db.log_outreach(r["id"], "postcard", "sent" if res["mode"] == "live" else "test", res["external_id"], res)
            out["sent"].append({"lead_id": r["id"], "owner": r["owner"], "id": res["external_id"]})
        except (ValueError, RuntimeError, httpx.HTTPError) as e:
            out["failed"].append({"lead_id": r["id"], "owner": r["owner"], "why": str(e)[:160]})
            if len(out["failed"]) >= 3 and not out["sent"]:
                break        # the account or key is the problem, not the addresses
    return out


def scan_email_leads(cfg: Dict[str, Any], db: Any, limit: int = 50, client: Optional[httpx.Client] = None) -> Dict[str, int]:
    """Ask Apollo which company owners have reachable contacts. Uses search only, which spends no credits."""
    owners = db.rows(
        """SELECT pa.owner, MAX(l.score) score FROM parcels pa JOIN leads l ON l.parcel_id = pa.id
           WHERE pa.owner_kind = 'company' AND pa.email_contacts IS NULL GROUP BY pa.owner ORDER BY score DESC LIMIT ?""",
        (limit,))
    ap = Apollo(cfg, client)
    found = 0
    for o in owners:
        n = len(ap.find_contacts({"owner": o["owner"]}))
        found += n > 0
        db.conn.execute("UPDATE parcels SET email_contacts=? WHERE owner=?", (n, o["owner"]))
        db.conn.commit()
    return {"owners_checked": len(owners), "owners_with_contacts": found}
