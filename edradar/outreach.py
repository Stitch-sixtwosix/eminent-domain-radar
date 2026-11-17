"""Owner outreach: postcards through Lob and email sequences through Apollo.

Nothing here sends on its own. Every send is one explicit request from the dashboard, and
both services stay switched off until their API key is present in the environment:

    LOB_API_KEY     test_... keys render proofs without mailing; live_... keys mail and bill
    APOLLO_API_KEY  an Apollo API key with access to people search, contacts and sequences
"""
from __future__ import annotations

import hashlib
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


# ---------------------------------------------------------------- the outreach template
# One template drives the postcard, the email and the printed letter. Text in {{braces}} is filled in per owner.
FIRM_FIELDS = ("name", "attorney", "phone", "email", "website", "address_line1", "city", "state", "zip")
MERGE_FIELDS = {
    "owner_name": "Owner of record", "property": "\"your property at 12 Main St\" (or the parcel ID when no address is published)",
    "property_address": "Street address of the parcel", "parcel_id": "County parcel ID", "county": "County",
    "project_name": "Name of the planned project", "project_stage": "Stage the project is in", "agency": "Agency behind the project",
    "firm_name": "Your firm", "attorney_name": "Attorney signing the letter", "firm_phone": "Firm phone",
    "firm_email": "Firm email", "firm_website": "Firm website",
}
DEFAULT_TEMPLATE = {
    "email_subject": "About {{property}} and the {{project_name}} project",
    "letter": (
        "Dear Property Owner,\n\n"
        "Public records list {{owner_name}} as the owner of {{property}} in {{county}} County. That parcel lies along the "
        "planned {{project_name}} project, which {{agency}} currently has in {{project_stage}}.\n\n"
        "Projects like this can require the government to acquire private land or easements. No taking of your property "
        "has been announced, and it may never be needed. If it is, Pennsylvania law entitles you to just compensation, "
        "and the first offer an owner receives is not always the full amount owed.\n\n"
        "{{firm_name}} represents Pennsylvania property owners in eminent domain matters. We do not represent the agencies "
        "that take land. If you would like to understand your rights before anyone contacts you about the project, "
        "a first conversation costs nothing.\n\n"
        "You can reach us at {{firm_phone}} or {{firm_email}}.\n\n"
        "Sincerely,\n{{attorney_name}}\n{{firm_name}}"
    ),
    "postcard_headline": "A public project is planned near {{property}}.",
    "postcard_message": ("{{project_name}} is in {{project_stage}}. Projects like this can require the government to acquire "
                         "private land or easements. Owners are entitled to just compensation, and the first offer is not "
                         "always the full amount."),
    "postcard_back": ("Know your rights before you sign. Public records show your parcel ({{parcel_id}}) lies along the planned "
                      "route. {{firm_name}} represents Pennsylvania property owners, not the agencies taking the land. "
                      "A first conversation costs nothing: {{firm_phone}}."),
}


def template(cfg: Dict[str, Any]) -> Dict[str, Any]:
    o = cfg.get("outreach") or {}
    saved = o.get("template") or {}
    return {"firm": {k: (o.get("firm") or {}).get(k) or "" for k in FIRM_FIELDS},
            **{k: saved.get(k) or v for k, v in DEFAULT_TEMPLATE.items()},
            "fields": MERGE_FIELDS}


def apply_saved(cfg: Dict[str, Any], db: Any) -> None:
    """Lay the template saved from the dashboard over config.yaml."""
    saved = db.get_setting("outreach_template")
    if not saved:
        return
    o = cfg.setdefault("outreach", {})
    o["firm"] = dict(o.get("firm") or {}, **{k: v for k, v in (saved.get("firm") or {}).items() if k in FIRM_FIELDS})
    o["template"] = {k: saved[k] for k in DEFAULT_TEMPLATE if saved.get(k)}


def save_template(cfg: Dict[str, Any], db: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    clean = {"firm": {k: str((body.get("firm") or {}).get(k) or "").strip()[:200] for k in FIRM_FIELDS}}
    for k in DEFAULT_TEMPLATE:
        clean[k] = str(body.get(k) or "").strip()[:6000]
    db.set_setting("outreach_template", clean)
    apply_saved(cfg, db)
    return template(cfg)


def _title(text: Optional[str]) -> str:
    return " ".join(w if any(c.islower() for c in w) else w.capitalize() if len(w) > 3 or w.isalpha() and len(w) > 1 else w
                    for w in (text or "").split())


def merge_values(cfg: Dict[str, Any], lead: Dict[str, Any]) -> Dict[str, str]:
    firm = (cfg.get("outreach") or {}).get("firm") or {}
    addr = _title(lead.get("address")) if lead.get("address") and re.match(r"\s*\d", lead["address"]) else ""
    county = _title(lead.get("parcel_county"))
    return {
        "owner_name": _title(lead.get("owner")) or "the owner of record",
        "property": "your property at %s" % addr if addr else "your property (parcel %s)" % lead.get("apn"),
        "property_address": addr or "parcel %s" % lead.get("apn"),
        "parcel_id": str(lead.get("apn") or ""), "county": county,
        "project_name": re.sub(r"\s*\(MPMS \d+\)", "", lead.get("project") or "a planned public project"),
        "project_stage": (lead.get("stage") or "planning").replace("_", " "),
        "agency": lead.get("agency") or "the sponsoring agency",
        "firm_name": firm.get("name") or "[Your firm name]", "attorney_name": firm.get("attorney") or firm.get("name") or "[Attorney name]",
        "firm_phone": firm.get("phone") or "[phone]", "firm_email": firm.get("email") or "[email]",
        "firm_website": firm.get("website") or "",
    }


def render(text: str, values: Dict[str, str]) -> str:
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: values.get(m.group(1), m.group(0)), text or "")


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
            "Add the firm's name and return address in the outreach template." if lob_key else
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
# Lob prints a 4x6 card from a 6.25 x 4.25 in page: 0.125 in on every side is trimmed off, and its print engine is an
# older one. So the artwork uses only absolute positioning and floats (no flexbox, no `inset`), and keeps all text
# 0.3 in from the page edge. On the back, the lower right is reserved by Lob for the address, barcode and postage.
CARD_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:6.25in;height:4.25in}
body{position:relative;overflow:hidden;font-family:Georgia,'Times New Roman',serif;color:#14202b}
.safe{position:absolute;top:0.3in;left:0.3in;width:5.65in;height:3.65in}
"""

FRONT = """<html><head><style>%(css)s
body{background:#f4f5f7}
h1{font-size:%(h1)spt;line-height:1.14;font-weight:700;width:5.3in}
p{font-family:Helvetica,Arial,sans-serif;font-size:11pt;line-height:1.4;margin-top:0.17in;width:5.1in}
.firm{position:absolute;left:0;bottom:0;width:5.65in;border-top:1.5pt solid #14202b;padding-top:0.09in;
font-family:Helvetica,Arial,sans-serif;font-size:10.5pt;font-weight:bold}
.firm .ph{float:right;font-weight:normal}
</style></head><body><div class="safe">
<h1>%(headline)s</h1>
<p>%(message)s</p>
<div class="firm"><span class="ph">%(phone)s</span>%(firm)s</div>
</div></body></html>"""

BACK = """<html><head><style>%(css)s
body{background:#fff}
.msg{position:absolute;top:0;left:0;width:5.65in;height:1.3in;overflow:hidden;
font-family:Helvetica,Arial,sans-serif;font-size:10pt;line-height:1.42}
.fine{position:absolute;left:0;bottom:0;width:2.15in;font-family:Helvetica,Arial,sans-serif;font-size:6.5pt;
line-height:1.3;color:#3d4a57}
</style></head><body><div class="safe">
<div class="msg">%(back)s</div>
<div class="fine">%(label)s%(firm)s, %(firm_addr)s. This notice is based on public planning records. No taking of your property has been announced, and receiving this card does not mean one will occur.</div>
</div></body></html>"""


def postcard_html(cfg: Dict[str, Any], lead: Dict[str, Any]) -> Dict[str, str]:
    firm = (cfg.get("outreach") or {}).get("firm") or {}
    pc = (cfg.get("outreach") or {}).get("postcard") or {}
    tpl, values = template(cfg), merge_values(cfg, lead)
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    headline = render(tpl["postcard_headline"], values)
    vals = {
        "css": CARD_CSS,
        "h1": 25 if len(headline) <= 62 else 21 if len(headline) <= 90 else 17,     # long headlines step down, never overflow
        "headline": e(headline),
        "message": e(render(tpl["postcard_message"], values)[:420]),
        "back": e(render(tpl["postcard_back"], values)[:430]),
        "firm": e(values["firm_name"]),
        "phone": e(firm.get("phone") or ""),
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
                    headers={"Idempotency-Key": "et-%s-%s-%s" % (st["mode"], lead["id"], hashlib.sha1(
                        (check["front"] + check["back"]).encode()).hexdigest()[:12])})
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
                 "has_email": bool(p.get("has_email", True)), "linkedin": p.get("linkedin_url"),
                 "phone": ((p.get("organization") or {}).get("primary_phone") or {}).get("number")
                          or (p.get("organization") or {}).get("phone"),
                 "email": p.get("email") if p.get("email") and "not_unlocked" not in p.get("email") else None}
                for p in people]

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
        return {"external_id": contact["id"], "email": email, "name": person.get("name"), "sequence_id": seq,
                "person_id": person_id, "title": person.get("title")}


# ---------------------------------------------------------------- postcard campaigns
POSTCARD_ELIGIBLE = """
    pa.mailable = 1 AND pa.owner_kind IN ('individual', 'company') AND l.status NOT IN ('rejected', 'retained')
    AND NOT EXISTS (SELECT 1 FROM outreach o JOIN leads l2 ON l2.id = o.lead_id JOIN parcels p2 ON p2.id = l2.parcel_id
                    WHERE o.channel = 'postcard' AND o.status = 'sent'
                      AND p2.owner = pa.owner AND p2.mailing_address = pa.mailing_address)
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
            if res["mode"] == "live":
                from . import skiptrace

                rate = skiptrace.rates(cfg)["postcard"]
                skiptrace.record_usage(db, "postcard", r["id"], r["owner"], rate["cost"], rate["price"])
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
        people = ap.find_contacts({"owner": o["owner"]})
        found += len(people) > 0
        db.save_contacts(o["owner"], people)
    return {"owners_checked": len(owners), "owners_with_contacts": found}
