"""Skip tracing for individual owners (BatchData), plus the usage ledger used to bill clients.

Request and response shapes follow BatchData's V1 Property Skip Trace endpoint as used in their own
integrations: POST /api/v1/property/skip-trace with {"requests": [{propertyAddress | apn, name, mailingAddress}],
"options": {"includeTCPABlacklistedPhones": false}} and persons under results.persons.

Nothing runs without BATCHDATA_API_KEY. Each owner and mailing address is looked up at most once, and the
answer (including "no match") is kept so it is never paid for twice.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from .outreach import parse_address

URL = "https://api.batchdata.com/api/v1/property/skip-trace"
DEFAULT_RATES = {"skip_trace": {"cost": 0.20, "price": 0.75}, "postcard": {"cost": 0.91, "price": 1.45}}


def rates(cfg: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """What each billable action costs you and what the client is charged. Edit under `billing:` in config.yaml."""
    saved = cfg.get("billing") or {}
    return {k: {"cost": float((saved.get(k) or {}).get("cost", v["cost"])),
                "price": float((saved.get(k) or {}).get("price", v["price"]))} for k, v in DEFAULT_RATES.items()}


def status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    key = os.environ.get("BATCHDATA_API_KEY")
    return {"enabled": bool(key), "why": None if key else "Set BATCHDATA_API_KEY to turn on skip tracing.",
            "price": rates(cfg)["skip_trace"]["price"], "cost": rates(cfg)["skip_trace"]["cost"]}


def build_request(lead: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find the owner where they receive mail; fall back to the parcel ID when that address cannot be read."""
    mail = parse_address(lead.get("mailing_address"))
    req: Dict[str, Any] = {}
    if mail:
        req["propertyAddress"] = {"street": mail["line1"], "city": mail["city"], "state": mail["state"], "zip": mail["zip"]}
    elif lead.get("apn") and lead.get("parcel_county"):
        req.update(apn=str(lead["apn"]), county=str(lead["parcel_county"]).title(), state="PA")
    else:
        return None
    owner = lead.get("owner") or ""
    if "," in owner and "&" not in owner:      # "GARNER, BEVERLY ANN": the only form whose order is certain
        last, first = [x.strip() for x in owner.split(",", 1)]
        if last and first:
            req["name"] = {"first": first.split()[0].title(), "last": last.title()}
    return {"requests": [req], "options": {"includeTCPABlacklistedPhones": False}}


def _name_matches(owner: Optional[str], person: Dict[str, Any]) -> bool:
    """A lookup by address can return a tenant. Trust it only when the surname appears in the owner of record."""
    last = ((person.get("name") or {}).get("last") or "").upper().strip()
    return bool(last) and bool(re.search(r"\b%s\b" % re.escape(last), (owner or "").upper()))


def parse_response(owner: Optional[str], body: Dict[str, Any]) -> Dict[str, Any]:
    persons = ((body.get("results") or {}).get("persons")) or []
    person = next((p for p in persons if (p.get("meta") or {}).get("matched", True) and
                   (p.get("phoneNumbers") or p.get("emails"))), None)
    if not person:
        return {"matched": False, "name": None, "name_match": False, "phones": [], "emails": [], "litigator": False}
    n = person.get("name") or {}
    phones = [{"number": p.get("number"), "type": p.get("type"), "carrier": p.get("carrier"),
               "reachable": p.get("reachable"), "score": p.get("score"), "dnc": bool(p.get("dnc")), "tcpa": bool(p.get("tcpa"))}
              for p in person.get("phoneNumbers") or [] if p.get("number")]
    phones.sort(key=lambda p: -(p.get("score") or 0))
    return {"matched": True, "name": " ".join(x.title() for x in (n.get("first"), n.get("last")) if x) or None,
            "name_match": _name_matches(owner, person), "phones": phones[:5],
            "emails": [e["email"] for e in person.get("emails") or [] if e.get("email")][:5],
            "litigator": bool(person.get("litigator"))}


def cached(db: Any, lead: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rows = db.rows("SELECT result, fetched_at FROM skiptraces WHERE owner=? AND mailing_address IS ?",
                   (lead.get("owner"), lead.get("mailing_address")))
    return dict(json.loads(rows[0]["result"]), fetched_at=rows[0]["fetched_at"]) if rows else None


def trace(cfg: Dict[str, Any], db: Any, lead: Dict[str, Any], client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    """Look one owner up, store the answer and record the billable usage. Raises ValueError when it should not run."""
    if lead.get("owner_kind") != "individual":
        raise ValueError("Skip tracing is for individual owners. Company owners are looked up in Apollo.")
    hit = cached(db, lead)
    if hit:
        return dict(hit, cached=True)
    st = status(cfg)
    if not st["enabled"]:
        raise ValueError(st["why"])
    body = build_request(lead)
    if body is None:
        raise ValueError("There is no readable mailing address or parcel ID to search with.")
    client = client or httpx.Client(timeout=45)
    r = client.post(URL, json=body, headers={"Authorization": "Bearer " + os.environ["BATCHDATA_API_KEY"],
                                             "Accept": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError("BatchData returned %s: %s" % (r.status_code, r.text[:160]))
    result = parse_response(lead.get("owner"), r.json())
    db.conn.execute("INSERT OR REPLACE INTO skiptraces (owner, mailing_address, matched, result) VALUES (?,?,?,?)",
                    (lead.get("owner"), lead.get("mailing_address"), int(result["matched"]), json.dumps(result)))
    db.conn.commit()
    rate = rates(cfg)["skip_trace"]
    if result["matched"]:                       # BatchData bills per matched result; so does the client ledger
        record_usage(db, "skip_trace", lead.get("id"), lead.get("owner"), rate["cost"], rate["price"])
    return dict(result, cached=False)


def trace_batch(cfg: Dict[str, Any], db: Any, min_score: int = 90, limit: int = 25, dry_run: bool = True,
                client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    from .web.app import LEAD_SQL

    rows = db.rows(LEAD_SQL + """ WHERE pa.owner_kind = 'individual' AND l.score >= ? AND l.status != 'rejected'
        AND NOT EXISTS (SELECT 1 FROM skiptraces t WHERE t.owner = pa.owner AND t.mailing_address IS pa.mailing_address)
        ORDER BY l.score DESC, pa.market_value DESC""", (min_score,))
    seen, todo = set(), []
    for r in rows:
        key = (r["owner"], r["mailing_address"])
        if key not in seen and build_request(r):
            seen.add(key)
            todo.append(r)
    todo = todo[:max(0, limit)]
    rate = rates(cfg)["skip_trace"]
    out: Dict[str, Any] = {"dry_run": dry_run, "owners": len(todo), "matched": 0, "failed": 0,
                           "max_cost": round(len(todo) * rate["cost"], 2), "max_billed": round(len(todo) * rate["price"], 2)}
    if dry_run:
        return out
    if not status(cfg)["enabled"]:
        raise ValueError(status(cfg)["why"])
    for r in todo:
        try:
            out["matched"] += trace(cfg, db, r, client)["matched"]
        except (RuntimeError, httpx.HTTPError):
            out["failed"] += 1
            if out["failed"] >= 3 and not out["matched"]:
                break
    return out


# ---------------------------------------------------------------- usage ledger
def record_usage(db: Any, kind: str, lead_id: Optional[int], owner: Optional[str], cost: float, price: float) -> None:
    db.conn.execute("INSERT INTO usage (kind, lead_id, owner, cost, price) VALUES (?,?,?,?,?)",
                    (kind, lead_id, owner, cost, price))
    db.conn.commit()


def usage_summary(db: Any) -> Dict[str, Any]:
    def block(where: str) -> List[Dict[str, Any]]:
        return db.rows("""SELECT kind, COUNT(*) AS count, ROUND(SUM(cost), 2) AS cost, ROUND(SUM(price), 2) AS billed,
                                 ROUND(SUM(price) - SUM(cost), 2) AS margin FROM usage %s GROUP BY kind ORDER BY kind""" % where)
    return {"this_month": block("WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')"), "all_time": block("")}
