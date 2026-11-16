"""Geocode place strings from text sources with OpenStreetMap Nominatim (max 1 request/second)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from .http import Http

# Rough Pennsylvania bounding box; results outside it are discarded.
PA_BBOX = (-80.53, 39.71, -74.68, 42.28)


class Geocoder:
    def __init__(self, cfg: Dict[str, Any], user_agent: str):
        self.cfg = cfg
        self.http = Http(user_agent, timeout=30, min_interval=1.1)
        self.cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def lookup(self, place: str, hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
        q = ", ".join(x for x in (place, hint, self.cfg.get("state", "Pennsylvania")) if x)
        if q in self.cache:
            return self.cache[q]
        out = None
        try:
            res = self.http.get_json(self.cfg["url"], {"q": q, "format": "jsonv2", "limit": 1,
                                                       "countrycodes": "us", "addressdetails": 1})
        except Exception as e:
            print("  geocode failed for %r: %s" % (q, e))
            res = []
        if res:
            lat, lon = float(res[0]["lat"]), float(res[0]["lon"])
            if PA_BBOX[0] <= lon <= PA_BBOX[2] and PA_BBOX[1] <= lat <= PA_BBOX[3]:
                county = (res[0].get("address") or {}).get("county") or ""
                out = {"lat": lat, "lon": lon,
                       "county": county.replace(" County", "").upper() or None}
        self.cache[q] = out
        return out
