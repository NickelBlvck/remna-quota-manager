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

    @staticmethod
    def _total(payload) -> Optional[int]:
        if isinstance(payload, dict):
            for key in ("total", "total_count", "count"):
                v = payload.get(key)
                if isinstance(v, int):
                    return v
        return None

    def _paginate(self, path: str, page_size: int) -> Iterator[Dict[str, Any]]:
        """Устойчиво к обоим стилям пагинации: шлём и limit/offset, и page/per_page."""
        offset = 0
        seen = 0
        while True:
            params = {
                "limit": page_size, "per_page": page_size,
                "offset": offset, "page": offset // page_size + 1,
            }
            payload = self._get(path, params)
            items = self._items(payload)
            if not items:
                return
            for it in items:
                yield it
            seen += len(items)
            total = self._total(payload)
            if len(items) < page_size:
                return
            if total is not None and seen >= total:
                return
            offset += page_size

    def iter_users(self, page_size: int = 200) -> Iterator[Dict[str, Any]]:
        """GET /users — каждый юзер несёт telegram_id + вложенную subscription."""
        return self._paginate("/users", page_size)

    def iter_subscriptions(self, page_size: int = 200) -> Iterator[Dict[str, Any]]:
        return self._paginate("/subscriptions", page_size)
