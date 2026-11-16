"""Federal Register notices (NEPA scoping, FERC pipeline certificates, Corps permits) mentioning Pennsylvania."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Iterator

from ..http import Http

API = "https://www.federalregister.gov/api/v1/documents.json"
FIELDS = ["document_number", "title", "abstract", "html_url", "publication_date", "agencies", "type"]


def fetch(cfg: Dict[str, Any], http: Http) -> Iterator[Dict[str, Any]]:
    since = (date.today() - timedelta(days=int(cfg.get("lookback_days", 365)))).isoformat()
    seen = set()
    for term in cfg.get("terms", []):
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
                seen.add(num)
                agencies = ", ".join(a.get("name") or a.get("raw_name") or "" for a in r.get("agencies") or [])
                yield {
                    "source": "federal_register",
                    "external_id": num,
                    "title": r.get("title") or "",
                    "body": r.get("abstract"),
                    "url": r.get("html_url"),
                    "agency": agencies or None,
                    "county": None,
                    "municipality": None,
                    "published": r.get("publication_date"),
                    "geometry": None,
                    "raw": r,
                }
            if page >= int(data.get("total_pages") or 1):
                break
            page += 1
