from __future__ import annotations

import time
from typing import Any, Dict, Optional

import httpx


class Http:
    """Small wrapper: shared client, polite User-Agent, retry on transient errors, optional rate limit."""

    def __init__(self, user_agent: str, timeout: float = 60.0, min_interval: float = 0.0, retries: int = 4):
        self.retries = retries
        self.client = httpx.Client(headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True)
        self.min_interval = min_interval
        self._last = 0.0

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                self._last = time.monotonic()
                r = self.client.request(method, url, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(f"{r.status_code} from {url}", request=r.request, response=r)
                r.raise_for_status()
                return r
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_exc = e
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code not in (429, 500, 502, 503, 504):
                    raise
                if attempt + 1 < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", url, params=params).json()

    def post_json(self, url: str, data: Dict[str, Any]) -> Any:
        return self.request("POST", url, data=data).json()
