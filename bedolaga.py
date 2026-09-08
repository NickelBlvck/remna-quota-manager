import logging
import time
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger("bedolaga")


def short_uuid_from_sub_url(url: Optional[str]) -> Optional[str]:
    """Remnawave shortUuid is the last path segment of the subscription URL."""
    if not url or not isinstance(url, str):
        return None
    try:
        path = urlparse(url).path
    except ValueError:
        return None
    seg = path.strip("/").split("/")[-1] if path else ""
    return seg or None


class BedolagaAPI:
    """Thin read client for the Bedolaga bot Web API (``X-API-Key`` auth).

    Only ``GET /subscriptions`` is used — it carries the per-user billing period
    (``start_date`` / ``end_date``), the tariff traffic cap (``traffic_limit_gb``)
    and the subscription URL we parse the Remnawave shortUuid out of.
    """

    _RETRYABLE = (429, 500, 502, 503, 504)

    def __init__(self, base_url: str, token: str, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": token, "Accept": "application/json"})

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None):
        url = f"{self.base_url}{path}"
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    logger.error("GET %s failed: %s", path, e)
                    return None
                time.sleep(2 ** attempt)
                continue

            if r.status_code in self._RETRYABLE:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                logger.error("Bedolaga %s %s (giving up)", r.status_code, path)
                return None
            if r.status_code >= 400:
                logger.error("Bedolaga %s %s: %s", r.status_code, path, r.text[:300])
                return None
            try:
                return r.json()
            except ValueError:
                logger.error("Bedolaga %s: bad JSON", path)
                return None
        return None

    @staticmethod
    def _items(payload) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("items", "users", "subscriptions", "data", "results"):
                val = payload.get(key)
                if isinstance(val, list):
                    return val
        return []

    def iter_users(self, page_size: int = 200) -> Iterator[Dict[str, Any]]:
        """GET /users — каждый юзер несёт telegram_id + вложенную subscription."""
        offset = 0
        while True:
            payload = self._get("/users", {"limit": page_size, "offset": offset})
            items = self._items(payload)
            if not items:
                return
            for it in items:
                yield it
            if len(items) < page_size:
                return
            offset += page_size

    def iter_subscriptions(
        self, status: Optional[str] = None, page_size: int = 200
    ) -> Iterator[Dict[str, Any]]:
        offset = 0
        while True:
            params: Dict[str, Any] = {"limit": page_size, "offset": offset}
            if status:
                params["status"] = status
            payload = self._get("/subscriptions", params)
            items = self._items(payload)
            if not items:
                return
            for it in items:
                yield it
            if len(items) < page_size:
                return
            offset += page_size

    def active_subscriptions(self, statuses=("active", "trial")) -> List[Dict[str, Any]]:
        seen: Dict[Any, Dict[str, Any]] = {}
        for st in statuses:
            for sub in self.iter_subscriptions(status=st):
                sid = sub.get("id", id(sub))
                seen.setdefault(sid, sub)
        return list(seen.values())
