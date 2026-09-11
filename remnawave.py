import requests
import time
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

class RemnawaveAPI:
    def __init__(self, base_url: str, token: str, timeout: int = 15, secret_key: Optional[str] = None):
        self.base_url = base_url.rstrip('/')
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(self.headers)

        # Панели за камуфляж-прокси (eGamesAPI/remnawave-reverse-proxy) отдают
        # decoy-страницу всем, у кого нет секретной куки NAME<sep>VALUE — тот же
        # секрет, что REMNAWAVE_SECRET_KEY у Bedolaga. Разделитель встречается и
        # как ":" (так его называют в .env-комментарии Bedolaga), и как "="
        # (буквальный формат из доки самого eGames-прокси, ?NAME=VALUE) — берём
        # что первым попадётся, вставлять можно как есть, без конвертации.
        # Кука привязана к этой сессии и уходит на все запросы к base_url,
        # включая /api/*.
        if secret_key:
            sep = next((c for c in (":", "=") if c in secret_key), None)
            cookie_name, cookie_value = "", ""
            if sep:
                name_part, _, value_part = secret_key.partition(sep)
                cookie_name, cookie_value = name_part.strip(), value_part.strip()
            if cookie_name and cookie_value:
                self.session.cookies.set(cookie_name, cookie_value)
                logger.info("Panel secret cookie configured (name=%s)", cookie_name)
            else:
                logger.warning("panel.secret_key set but malformed (expected NAME:VALUE or NAME=VALUE)")

    _RETRYABLE = (429, 500, 502, 503, 504)

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        for attempt in range(3):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    logger.error(f"Request failed: {method} {path} - {e}")
                    return None
                time.sleep(2 ** attempt)
                continue

            if response.status_code >= 400:
                logger.error(f"API {response.status_code} {method} {path}: {response.text[:300]}")

            if response.status_code in self._RETRYABLE:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                return None

            if response.status_code >= 400:
                # Non-retryable client error (400/401/403/404/...): retrying is pointless.
                return None

            try:
                data = response.json()
            except ValueError as e:
                logger.error(f"JSON parse failed: {e}")
                return None
            if isinstance(data, dict):
                return data.get('response', data)
            return data
        return None

    @staticmethod
    def _normalize_user(u: Dict) -> Dict:
        """Current Remnawave versions dropped `uuid` from the user model —
        the identifier is the numeric `id` (REST paths and the update body
        both use it: ``GET/PATCH /api/users/:userId``). Every place in this
        codebase keys off ``user["uuid"]``, so inject it once here
        (``str(id)``) rather than touch every call site.
        """
        if isinstance(u, dict) and not u.get('uuid') and u.get('id') is not None:
            u = dict(u)
            u['uuid'] = str(u['id'])
        return u

    def get_users(self, limit: int = 5000, page_size: int = 1000) -> List[Dict]:
        """Fetch every user, paginating properly and verifying we actually got
        everyone.

        ``GET /api/users/stream`` (cursor pagination — ``size`` up to 1000,
        response carries ``hasMore``/``nextCursor``) is what Remnawave's own
        ecosystem (e.g. the Bedolaga bot) uses for full enumeration, and is
        usually complete in one request. But it's been observed to come back
        short on a live panel — a page mid-walk silently returning fewer users
        (``hasMore``/``nextCursor`` still looked "done") — with no error, so a
        naive "non-empty means it worked" check isn't safe. We cross-check the
        stream result against the authoritative ``total`` from a cheap
        ``GET /api/users`` call and, if short, fall back to walking that
        endpoint's own ``start``/``size`` pagination (slower, but every page
        observed against this panel has been consistent and deterministic).
        """
        total = self._get_users_total()
        stream_users = self._get_users_stream(limit, page_size)
        if stream_users and (total is None or len(stream_users) >= min(total, limit)):
            return [self._normalize_user(u) for u in stream_users]
        if stream_users:
            logger.warning(
                "GET /api/users/stream returned %s of %s users; falling back to /api/users pagination",
                len(stream_users), total,
            )
        users = self._get_users_legacy_paginated(limit, page_size)
        return [self._normalize_user(u) for u in users]

    def _get_users_total(self) -> Optional[int]:
        raw = self._request('GET', '/api/users', params={"start": 0, "size": 1})
        if isinstance(raw, dict) and isinstance(raw.get('total'), int):
            return raw['total']
        return None

    def _get_users_stream(self, limit: int, page_size: int) -> List[Dict]:
        users: List[Dict] = []
        cursor = None
        for _ in range(200):  # safety net against a server that never stops hasMore
            params: Dict[str, Any] = {"size": min(page_size, 1000)}
            if cursor is not None:
                params["cursor"] = cursor
            raw = self._request('GET', '/api/users/stream', params=params)
            if not isinstance(raw, dict):
                break
            page = raw.get('users')
            if not isinstance(page, list) or not page:
                break
            users.extend(page)
            if len(users) >= limit:
                break
            if not raw.get('hasMore') or raw.get('nextCursor') is None:
                break
            cursor = raw['nextCursor']
        return users[:limit]

    def _get_users_legacy_paginated(self, limit: int, page_size: int) -> List[Dict]:
        users: List[Dict] = []
        start = 0
        total: Optional[int] = None
        for _ in range(200):
            raw = self._request('GET', '/api/users', params={"start": start, "size": page_size})
            if not isinstance(raw, dict):
                break
            page = raw.get('users')
            if page is None and isinstance(raw.get('data'), list):
                page = raw['data']
            if not isinstance(page, list) or not page:
                break
            users.extend(page)
            if isinstance(raw.get('total'), int):
                total = raw['total']
            start += len(page)
            if len(users) >= limit:
                break
            if total is not None and start >= total:
                break
        return users[:limit]

    def get_nodes(self) -> List[Dict]:
        """Return the current node list from the panel."""
        raw = self._request("GET", "/api/nodes")
        if isinstance(raw, dict) and isinstance(raw.get("nodes"), list):
            return raw["nodes"]
        return raw if isinstance(raw, list) else []

    def get_user(self, user_uuid: str) -> Optional[Dict]:
        """``user_uuid`` is our normalized id (``str(user["id"])``) — REST
        paths take the numeric id directly: ``GET /api/users/{id}``."""
        result = self._request('GET', f'/api/users/{user_uuid}')
        if isinstance(result, dict):
            if 'users' in result and isinstance(result['users'], list) and result['users']:
                return self._normalize_user(result['users'][0])
            if 'id' in result or 'uuid' in result:
                return self._normalize_user(result)
        return None

    def get_node_bandwidth_stats(
        self, node_uuid: str, start_date: str, end_date: str, top_limit: int = 100
    ) -> Dict:
        """Top users on one node for a date range.

        Documented endpoint: POST /api/bandwidth-stats/nodes/users with the node
        list in the body. Response is
        ``{categories, sparklineData, topUsers:[{username,total}]}`` — ``total``
        in bytes, no node-total field, no per-node split. Falls back to the
        legacy GET route for older panels.
        """
        params = {"start": start_date, "end": end_date, "topUsersLimit": str(top_limit)}
        result = self._request(
            "POST", "/api/bandwidth-stats/nodes/users",
            params=params, json={"nodesUuids": [node_uuid]},
        )
        if not isinstance(result, dict):
            result = self._request(
                "GET", f"/api/bandwidth-stats/nodes/{node_uuid}/users", params=params
            )
        if not isinstance(result, dict):
            return {"topUsers": [], "total_bytes": None}

        top_users = result.get("topUsers", []) or []

        total_bytes = None
        for key in ("total", "totalBytes", "totalUsage", "totalTraffic", "bandwidthTotal"):
            if result.get(key) is not None:
                total_bytes = int(result[key])
                break
        if total_bytes is None:
            spark = result.get("sparklineData")
            if isinstance(spark, list) and spark:
                try:
                    total_bytes = int(sum(spark))
                except (TypeError, ValueError):
                    total_bytes = None
        if total_bytes is None and top_users:
            # last resort: sum the returned top users (understates the tail)
            try:
                total_bytes = int(sum(int(u.get("total", 0)) for u in top_users))
            except (TypeError, ValueError):
                total_bytes = None

        return {"topUsers": top_users, "total_bytes": total_bytes}

    def get_node_bandwidth(self, node_uuid: str, start_date: str, end_date: str, top_limit: int = 100) -> List[Dict]:
        return self.get_node_bandwidth_stats(node_uuid, start_date, end_date, top_limit)["topUsers"]

    def get_user_node_traffic_bytes(
        self, user_uuid: str, node_uuid: str, start_date: str, end_date: str,
        username: Optional[str] = None,
    ) -> Optional[int]:
        """Трафик пользователя: только через рабочий bulk-эндпоинт.

        topUsers entries carry no `uuid` either — just `userId` (numeric,
        matches our normalized uuid=str(id)) and `username`.
        """
        top_users = self.get_node_bandwidth(node_uuid, start_date, end_date, top_limit=500)

        if username:
            for u in top_users:
                if u.get("username") == username:
                    return int(u.get("total", 0))

        for u in top_users:
            stat_id = u.get("uuid") or u.get("userId")
            if stat_id is not None and str(stat_id) == str(user_uuid):
                return int(u.get("total", 0))

        logger.debug(f"User {username or user_uuid[:8]}... not found in topUsers for node {node_uuid[:8]}")
        return None

    def get_user_period_traffic_gb(
        self, user: Dict[str, Any], node_uuid: str, start_date: str, end_date: str,
    ) -> Optional[float]:
        uuid = user.get("uuid")
        if not uuid:
            return None
        # Must be per-node traffic: the user object's usedTrafficBytes is the
        # global total across every node and would over-count against a single
        # node's limit_gb.
        raw = self.get_user_node_traffic_bytes(uuid, node_uuid, start_date, end_date, username=user.get("username"))
        if raw is None:
            return None
        return raw / (1024**3)

    def get_user_current_squads(self, user_uuid: str) -> Dict[str, List[str]]:
        user = self.get_user(user_uuid)
        if not user:
            return {'internal': [], 'external': []}
        internal = [s.get('uuid') for s in (user.get('activeInternalSquads') or []) if isinstance(s, dict) and s.get('uuid')]
        # Current Remnawave: one external squad slot per user (`externalSquadUuid`,
        # nullable string), not a list — confirmed against a live panel and
        # matches how Bedolaga's own client reads it.
        ext = user.get('externalSquadUuid')
        external = [ext] if ext else []
        return {'internal': internal, 'external': external}

    def set_user_external_squad(
        self, user_uuid: str, target_external_uuid: Optional[str], remove_from_all: bool = True
    ) -> bool:
        """Set the user's external squad.

        Current Remnawave versions give each user exactly one external-squad
        slot (`externalSquadUuid` on the user record) instead of a
        add/remove-from-list model — confirmed against a live panel (a decoy
        page was the tell) and cross-checked against Bedolaga's own client,
        which moves users the same way: ``PATCH /api/users`` with
        ``{"id": <numeric id>, "externalSquadUuid": <target or null>}``.
        ``remove_from_all`` is accepted for signature compatibility with
        callers written for the old model; setting the single slot always
        implicitly replaces whatever was there, so it has no separate effect.
        """
        try:
            numeric_id = int(user_uuid)
        except (TypeError, ValueError):
            logger.error("set_user_external_squad: %r is not a numeric user id", user_uuid)
            return False

        target = (target_external_uuid or "").strip() or None
        result = self._request('PATCH', '/api/users', json={"id": numeric_id, "externalSquadUuid": target})
        if result is None:
            logger.error("PATCH /api/users failed for id=%s -> externalSquadUuid=%s", numeric_id, target)
            return False

        updated = self.get_user(str(numeric_id))
        current = (updated or {}).get('externalSquadUuid')
        if current != target:
            logger.error(
                "Panel did not confirm externalSquadUuid for id=%s (got %r, wanted %r)",
                numeric_id, current, target,
            )
            return False

        logger.info("External squad for id=%s -> %s", numeric_id, target or "(cleared)")
        return True

    def set_user_external_squads(
        self, user_uuid: str, target_uuids, remove_from_all: bool = True
    ) -> bool:
        """Compat shim for callers built around "several full squads at once"
        (a user limited on more than one node simultaneously). The panel only
        has one external-squad slot per user, so all of those can't be held
        at the same time — uses the first (order-preserving de-duplicated)
        target and logs a warning if more than one was actually requested,
        rather than silently picking one with no trace.
        """
        targets = list(dict.fromkeys(str(u).strip() for u in (target_uuids or []) if u and str(u).strip()))
        if len(targets) > 1:
            logger.warning(
                "set_user_external_squads: panel supports one external squad per user; "
                "%s targets requested for id=%s, using %s — ignoring %s",
                len(targets), user_uuid, targets[0], targets[1:],
            )
        target = targets[0] if targets else None
        return self.set_user_external_squad(user_uuid, target, remove_from_all=remove_from_all)

    def move_user_to_squads(self, user_uuid: str, target_squads: Dict[str, str], remove_from_all: bool = True) -> bool:
        external = target_squads.get("external")
        if target_squads.get("internal"):
            logger.warning("internal squad ignored — quota uses external squads only")
        return self.set_user_external_squad(user_uuid, external, remove_from_all=remove_from_all)

    def reset_user_traffic(self, user_uuid: str) -> bool:
        for ep in [f'/api/users/{user_uuid}/actions/reset-traffic', f'/api/users/{user_uuid}/reset-traffic']:
            if self._request('POST', ep):
                return True
        return False

    def get_internal_squads(self) -> List[Dict]:
        result = self._request('GET', '/api/internal-squads')
        return result if isinstance(result, list) else []
