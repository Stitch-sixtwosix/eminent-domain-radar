"""Legistar legislation feeds (city/county councils). Finds acquisition and condemnation ordinances."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Iterator

from ..http import Http

API = "https://webapi.legistar.com/v1/%s/matters"


def fetch(cfg: Dict[str, Any], http: Http) -> Iterator[Dict[str, Any]]:
    since = (date.today() - timedelta(days=int(cfg.get("lookback_days", 365)))).isoformat()
    for client in cfg.get("clients", []):
        slug = client["slug"]
        seen = set()
        for phrase in cfg.get("phrases", []):
            flt = "substringof('%s',MatterTitle) and MatterIntroDate ge datetime'%s'" % (
                phrase.replace("'", "''"), since)
            try:
                matters = http.get_json(API % slug, {"$filter": flt, "$top": 200,
                                                     "$orderby": "MatterIntroDate desc"})
            except Exception as e:  # one unavailable council should not stop the rest
                print("  legistar %s (%r): %s" % (slug, phrase, e))
                break
            for m in matters:
                mid = m.get("MatterId")
                if mid in seen:
                    continue
                seen.add(mid)
                title = (m.get("MatterTitle") or m.get("MatterName") or "").strip()
                yield {
                    "source": "legistar:%s" % slug,
                    "external_id": str(mid),
                    "title": "%s: %s" % (m.get("MatterFile") or "Matter", title[:300]),
                    "body": title,
                    "url": "https://%s.legistar.com/LegislationDetail.aspx?ID=%s&GUID=%s" % (
                        slug, mid, m.get("MatterGuid")),
                    "agency": client.get("name") or slug,
                    "state": (client.get("state") or "PA").upper(),
                    "county": (client.get("county") or "").upper() or None,
                    "municipality": client.get("place") or client.get("name"),
                    "published": (m.get("MatterIntroDate") or "")[:10] or None,
                    "geometry": None,
                    "raw": {k: m.get(k) for k in ("MatterFile", "MatterTypeName", "MatterStatusName",
                                                   "MatterBodyName", "MatterIntroDate", "MatterPassedDate")},
                }
