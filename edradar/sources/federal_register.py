"""Federal Register notices: NEPA scoping, FERC pipeline certificates, Corps permits, transmission lines.

The configured terms are searched as before (they name Pennsylvania). Then a nationwide set of searches runs,
and each notice is filed under the first state its title or abstract names; notices that name no state are
left out, because a lead has to be somewhere.
"""
from __future__ import annotations

from datetime import date, timedelta
import re
from typing import Any, Dict, Iterator, Optional

from ..http import Http

API = "https://www.federalregister.gov/api/v1/documents.json"
FIELDS = ["document_number", "title", "abstract", "html_url", "publication_date", "agencies", "type"]


NATIONWIDE_TERMS = [
    '"environmental impact statement" "right-of-way"',
    '"notice of intent" highway "environmental impact statement"',
    'pipeline "certificate of public convenience and necessity"',
    '"transmission line" "environmental impact statement"',
    '"eminent domain" OR condemnation "acquisition of"',
    '"land acquisition" "environmental assessment" project',
]
STATES = {"Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO", "Connecticut": "CT",
          "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
          "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
          "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
          "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
          "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
          "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
          "Wisconsin": "WI", "Wyoming": "WY"}
_STATE_RE = re.compile(r"\b(%s)\b" % "|".join(sorted(STATES, key=len, reverse=True)))


def state_of(text: str) -> Optional[str]:
    """The first state named in the text. "Washington, DC" and "West Virginia" are not Washington or Virginia."""
    for m in _STATE_RE.finditer(text or ""):
        name = m.group(1)
        if name == "Washington" and re.match(r",?\s*D\.?C", (text or "")[m.end():m.end() + 8]):
            continue
        return STATES[name]
    return None


def fetch(cfg: Dict[str, Any], http: Http) -> Iterator[Dict[str, Any]]:
    since = (date.today() - timedelta(days=int(cfg.get("lookback_days", 365)))).isoformat()
    seen = set()
    searches = [(t, "PA") for t in cfg.get("terms", [])]
    if cfg.get("nationwide", True):
        searches += [(t, None) for t in NATIONWIDE_TERMS]
    for term, fixed_state in searches:
        page = 1
        while page <= 5:
            data = http.get_json(
                API,
                {
                    "conditions[term]": term,
                    "conditions[type][]": "NOTICE",
                    "conditions[publication_date][gte]": since,
                    "fields[]": FIELDS,
                    "per_page": 100,
                    "page": page,
                    "order": "newest",
                },
            )
            for r in data.get("results") or []:
                num = r.get("document_number")
                if not num or num in seen:
                    continue
                state = fixed_state or state_of("%s %s" % (r.get("title") or "", r.get("abstract") or ""))
                if not state:
                    continue
                seen.add(num)
                agencies = ", ".join(a.get("name") or a.get("raw_name") or "" for a in r.get("agencies") or [])
                yield {
                    "source": "federal_register",
                    "external_id": num,
                    "title": r.get("title") or "",
                    "body": r.get("abstract"),
                    "url": r.get("html_url"),
                    "agency": agencies or None,
                    "state": state,
                    "county": None,
                    "municipality": None,
                    "published": r.get("publication_date"),
                    "geometry": None,
                    "raw": r,
                }
            if page >= int(data.get("total_pages") or 1):
                break
            page += 1
