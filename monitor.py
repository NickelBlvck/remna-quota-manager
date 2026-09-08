import logging
import time
from datetime import datetime
from typing import Dict, Optional

import billing
from bedolaga import BedolagaAPI, short_uuid_from_sub_url
from database import QuotaDatabase
from nodes import resolve_monitored_nodes
from notify import TelegramNotifier, esc
from reports import generate_daily_summary
from remnawave import RemnawaveAPI
from settings import load_config

logger = logging.getLogger("monitor")

class TrafficMonitor:
    def __init__(self, api: RemnawaveAPI, db: QuotaDatabase, config: Optional[dict] = None):
        self.api = api
        self.db = db
        self.config = config or load_config()
        self.notifier = TelegramNotifier(self.config)
        self._user_map: Dict[str, dict] = {}
        self._uuid_map: Dict[str, dict] = {}
        self._short_map: Dict[str, dict] = {}
        self._tgid_map: Dict[int, dict] = {}
        self._bedolaga_index: Dict[str, dict] = {}  # panel uuid (lower) -> sub ctx
        self.bedolaga = self._make_bedolaga()

    def _make_bedolaga(self) -> Optional[BedolagaAPI]:
        bcfg = self.config.get("bedolaga") or {}
        if not billing.is_bedolaga_mode(self.config):
            return None
        if not bcfg.get("base_url") or not bcfg.get("token"):
            logger.error("billing.mode=bedolaga but bedolaga.base_url/token missing")
            return None
        return BedolagaAPI(bcfg["base_url"], bcfg["token"], timeout=int(bcfg.get("timeout", 15)))

    def reload_config(self):
        self.config = load_config()
        self.notifier.reload(self.config)
        self.bedolaga = self._make_bedolaga()

    def is_dry_run(self) -> bool:
        """Проверяет, включен ли режим сухого запуска"""
        if not self.config.get("actions_enabled", True):
            return True
        return self.db.is_dry_run(self.config.get("dry_run", True))

    def effective_limit_gb(self, limit_gb: float) -> float:
        """Добавляет буфер (например, +1%) к лимиту для гибкости"""
        buffer_pct = float(self.config.get("limit_buffer_percent", 0))
        return limit_gb * (1 + buffer_pct / 100.0)

    def required_checks(self) -> int:
        """Сколько раз подряд нужно превысить лимит (hysteresis)"""
        return max(1, int(self.config.get("limit_hysteresis_checks", 2)))

    def evaluate(self) -> dict:
        """Read-only повтор enforcement-решения по текущему состоянию.

        Возвращает ``{"rows": [...], "dry_run": bool, "need": int}``. Каждая
        строка: node_name, username, uuid, traffic_gb (за личный цикл), limit_gb,
        checks, need, verdict ∈ {limited, over, whitelist, ok}, cycle_start,
        cycle_end. В выдаче — только те, кто на лимите/выше по своему циклу, плюс
        уже ограниченные и те, кто на верификации. Ничего не меняет.
        """
        self._build_user_map()
        nodes = self._get_monitored_nodes()
        need = self.required_checks()
        sub = billing.is_subscription_mode(self.config)
        pending = {(p["uuid"], p["node_uuid"]): p["checks_count"] for p in self.db.list_pending()}
        limited = {(r["uuid"], r["node_uuid"]) for r in self.db.list_enforced_limited()}

        rows = []
        for node_cfg in nodes:
            node_uuid = node_cfg["uuid"]
            try:
                limit_gb = float(node_cfg["limit_gb"])
            except (KeyError, TypeError, ValueError):
                continue
            if limit_gb <= 0:
                continue
            node_name = node_cfg.get("name", node_uuid[:8])
            eff = self.effective_limit_gb(limit_gb)
            scan_start, scan_end = billing.scan_window_dates(self.config)
            top = self.api.get_node_bandwidth(
                node_uuid, scan_start, scan_end,
                top_limit=int(self.config.get("bandwidth_page_size", 5000)),
            )

            # top-user статистика → реальные юзеры; оставляем только «около лимита»
            # и тех, кто уже отслеживается
            cand: dict = {}  # lower_uuid -> (user_obj, approx_gb | None, eff, limit_gb)
            for u_stat in top:
                uo = (
                    self._uuid_map.get((u_stat.get("uuid") or "").lower())
                    or self._user_map.get(u_stat.get("username"))
                )
                if not uo or not uo.get("uuid"):
                    continue
                approx = int(u_stat.get("total", 0)) / (1024 ** 3)
                lu = uo["uuid"].lower()
                tracked = (lu, node_uuid) in pending or (lu, node_uuid) in limited
                uo, u_lim, ok = self._user_quota(uo, limit_gb)
                if not ok and not tracked:
                    continue
                u_eff = self.effective_limit_gb(u_lim)
                if approx >= u_eff or tracked:
                    cand[lu] = (uo, approx, u_eff, u_lim)

            # отслеживаемые юзеры, выпавшие из топа ноды
            for (u_uuid, n_uuid) in (limited | set(pending)):
                if n_uuid != node_uuid or u_uuid.lower() in cand:
                    continue
                uo = self._uuid_map.get(u_uuid) or self.api.get_user(u_uuid) or {"uuid": u_uuid}
                uo, u_lim, _ok = self._user_quota(uo, limit_gb)
                cand[(uo.get("uuid") or u_uuid).lower()] = (uo, None, self.effective_limit_gb(u_lim), u_lim)

            if not cand:
                continue

            # точный трафик за цикл, сгруппировано по окну — экономим запросы
            precise: dict = {}
            groups: dict = {}
            for uo, _a, _e, _l in cand.values():
                groups.setdefault(billing.user_period_dates(uo, self.config), []).append(uo)
            for (w_start, w_end), users in groups.items():
                bw = self.api.get_node_bandwidth(node_uuid, w_start, w_end, top_limit=500)
                by_uuid = {(b.get("uuid") or "").lower(): b.get("total", 0) for b in bw}
                by_name = {b.get("username"): b.get("total", 0) for b in bw}
                for uo in users:
                    v = by_uuid.get((uo.get("uuid") or "").lower())
                    if v is None:
                        v = by_name.get(uo.get("username"))
                    if v is not None:
                        precise[(uo.get("uuid") or "").lower()] = int(v) / (1024 ** 3)

            for lu, (uo, approx, u_eff, u_lim) in cand.items():
                traffic = precise.get(lu, approx)
                over = (traffic or 0) >= u_eff
                if (lu, node_uuid) in limited:
                    verdict = "limited"
                elif self.db.is_whitelisted(lu):
                    verdict = "whitelist" if over else "ok"
                else:
                    verdict = "over" if over else "ok"
                cyc = billing.user_period_dates(uo, self.config)
                rows.append({
                    "node_name": node_name,
                    "username": uo.get("username") or lu[:8],
                    "uuid": uo.get("uuid") or lu,
                    "traffic_gb": traffic,
                    "limit_gb": u_lim,
                    "checks": pending.get((lu, node_uuid), 0),
                    "need": need,
                    "verdict": verdict,
                    "cycle_start": cyc[0],
                    "cycle_end": cyc[1],
                    "mode": "subscription" if sub else "calendar",
                })

        rows.sort(key=lambda r: (r["node_name"], -(r["traffic_gb"] or 0)))
        return {"rows": rows, "dry_run": self.is_dry_run(), "need": need}

    def _build_user_map(self):
        """Кэширует пользователей для быстрого поиска по username/uuid/shortUuid/telegramId"""
        users = self.api.get_users(limit=int(self.config.get("users_page_size", 5000)))
        self._user_map = {}
        self._uuid_map = {}
        self._short_map = {}
        self._tgid_map = {}
        for u in users:
            if u.get("username"):
                self._user_map[u["username"]] = u
            if u.get("uuid"):
                self._uuid_map[u["uuid"]] = u
            su = u.get("shortUuid") or u.get("subscriptionUuid")
            if su:
                self._short_map[su] = u
            tg = u.get("telegramId")
            if tg not in (None, ""):
                try:
                    self._tgid_map[int(tg)] = u
                except (TypeError, ValueError):
                    pass
        logger.info("Cached %s users", len(self._user_map))
        self._load_bedolaga_index()

    def _load_bedolaga_index(self):
        """bedolaga mode: подтянуть подписки и связать с юзерами панели."""
        self._bedolaga_index = {}
        if not self.bedolaga:
            return
        bcfg = self.config.get("bedolaga") or {}
        tmpl = bcfg.get("username_template", "user_{telegram_id}")
        dead = {s.lower() for s in (bcfg.get("inactive_statuses") or ["expired", "disabled"])}

        try:
            rows = list(self.bedolaga.iter_users())
        except Exception as e:
            logger.error("Bedolaga fetch failed: %s", e)
            return

        matched = 0
        for bu in rows:
            sub = bu.get("subscription") or {}
            if not sub:
                continue
            status = (sub.get("actual_status") or sub.get("status") or "").lower()
            if status in dead:
                continue

            ruser = None
            su = short_uuid_from_sub_url(sub.get("subscription_url"))
            if su:
                ruser = self._short_map.get(su)
            if not ruser and bu.get("telegram_id") not in (None, ""):
                try:
                    tid = int(bu["telegram_id"])
                    ruser = self._tgid_map.get(tid) or self._user_map.get(tmpl.format(telegram_id=tid))
                except (TypeError, ValueError):
                    ruser = None
            if not ruser or not ruser.get("uuid"):
                continue

            try:
                limit_gb = float(sub.get("traffic_limit_gb") or 0)
            except (TypeError, ValueError):
                limit_gb = 0.0

            self._bedolaga_index[ruser["uuid"].lower()] = {
                "start_date": sub.get("start_date"),
                "end_date": sub.get("end_date"),
                "limit_gb": limit_gb,
                "status": status,
                "squads": sub.get("connected_squads") or [],
            }
            matched += 1
        logger.info("Bedolaga: %s users fetched, %s subs linked to panel", len(rows), matched)

    def _bedolaga_ctx(self, user_obj: dict):
        """(augmented_user, sub_ctx | None) для bedolaga mode; иначе (user, None)."""
        if not self.bedolaga:
            return user_obj, None
        ctx = self._bedolaga_index.get((user_obj.get("uuid") or "").lower())
        if not ctx:
            return user_obj, None
        aug = dict(user_obj)
        if ctx.get("start_date"):
            aug["subscription_start_date"] = ctx["start_date"]
        return aug, ctx

    def _user_quota(self, user_obj: dict, node_limit_gb: float):
        """(augmented_user, limit_gb, monitor?) с учётом billing.mode.

        bedolaga: связанный юзер → лимит тарифа; не связанный → понодовый
        limit_gb, если ``bedolaga.fallback_unlinked`` (по умолчанию true).
        """
        if not billing.is_bedolaga_mode(self.config):
            return user_obj, node_limit_gb, node_limit_gb > 0
        aug, ctx = self._bedolaga_ctx(user_obj)
        if ctx is not None:
            lim = ctx["limit_gb"] if ctx["limit_gb"] > 0 else node_limit_gb
            return aug, lim, lim > 0
        if (self.config.get("bedolaga") or {}).get("fallback_unlinked", True):
            return user_obj, node_limit_gb, node_limit_gb > 0
        return user_obj, node_limit_gb, False

    def _get_monitored_nodes(self):
        """Discover nodes from the panel and merge local quota policies."""
        return resolve_monitored_nodes(self.api, self.config)

    def _send_alert_sync(self, text: str):
        self.notifier.send_limit_event(text)

    def check_daily_summary(self):
        """Проверка, пора ли слать ежедневный отчет"""
        if not self.notifier.should_send_daily_summary(self.db):
            return
        try:
            report_config = dict(self.config)
            report_config["monitored_nodes"] = self._get_monitored_nodes()
            text = generate_daily_summary(self.api, report_config)
            if self.notifier.send_daily_summary(text):
                self.notifier.mark_daily_summary_sent(self.db)
                logger.info("Daily summary sent")
            else:
                logger.error("Daily summary was not delivered; it will be retried")
        except Exception as e:
            logger.error("Daily summary failed: %s", e, exc_info=True)

    def _notify_verification_pending(self, username, node_name, traffic_gb, limit_gb, checks, need, dry):
        mode = "dry-run" if dry else "⚡ боевой"
        self._send_alert_sync(
            f"🔁 <b>Верификация лимита</b> ({checks}/{need})\n\n"
            f"👤 <code>{esc(username)}</code>\n"
            f"🌐 <code>{esc(node_name)}</code>\n"
            f"📊 <code>{traffic_gb:.1f}</code> / <code>{esc(limit_gb)}</code> GB\n"
            f"⏳ Следующая проверка через ~{self.config.get('check_interval_minutes', 10)} мин\n"
            f"<i>{mode}: лимит применится только после {need} подтверждений подряд</i>"
        )

    def _notify_verification_passed(self, username, node_name, traffic_gb, limit_gb, dry):
        if dry:
            self._send_alert_sync(
                f"✅ <b>Верификация пройдена</b> (dry-run)\n\n"
                f"👤 <code>{esc(username)}</code>\n"
                f"🌐 <code>{esc(node_name)}</code>\n"
                f"📊 <code>{traffic_gb:.1f}</code> / <code>{esc(limit_gb)}</code> GB\n"
                f"🧪 <i>В dry-run сквад не менялся.</i>"
            )
        else:
            self._send_alert_sync(
                f"🚫 <b>Лимит применён</b>\n\n"
                f"👤 <code>{esc(username)}</code>\n"
                f"🌐 <code>{esc(node_name)}</code>\n"
                f"📊 <code>{traffic_gb:.1f}</code> / <code>{esc(limit_gb)}</code> GB\n"
                f"🔒 Переведён в limited external-сквад"
            )

    def check_cycle_unblocks(self):
        """Разблокировка пользователей, у которых истёк цикл подписки"""
        if billing.is_subscription_mode(self.config):
            candidates = self.db.list_enforced_limited()
        else:
            candidates = self.db.list_for_monthly_unblock(self.db.current_period_key())

        if not candidates:
            return

        nodes = self._get_monitored_nodes()
        if not nodes:
            return

        dry = self.is_dry_run()
        mode = billing.billing_mode(self.config)

        # Group the limited rows per user so one panel update returns the user to
        # every full squad they need — updating node A must not evict them from
        # node B's squad.
        grouped: Dict[str, list] = {}
        for u_info in candidates:
            uuid = u_info["uuid"]
            user_obj = self._uuid_map.get(uuid) or self.api.get_user(uuid)
            if not user_obj:
                continue
            user_obj, _bctx = self._bedolaga_ctx(user_obj)
            if not billing.should_unblock_user(u_info, user_obj, self.config):
                continue
            node = next((n for n in nodes if n.get("uuid") == u_info.get("node_uuid")), nodes[0])
            grouped.setdefault(uuid, []).append({**u_info, "user_obj": user_obj, "node_cfg": node})

        if not grouped:
            return

        logger.info("Cycle unblock (%s): %s users, dry_run=%s", mode, len(grouped), dry)

        for uuid, infos in grouped.items():
            username = infos[0].get("username") or uuid[:8]
            user_obj = infos[0]["user_obj"]
            reset = billing.billing_reset_iso(user_obj) or "?"

            full_targets = []
            missing = False
            for info in infos:
                full_external = self._full_external_from_node(info["node_cfg"])
                if not full_external:
                    logger.error("full_external_squad_uuid not configured for %s", info.get("node_name"))
                    missing = True
                    break
                full_targets.append(full_external)
            if missing:
                continue

            self.db.audit(
                "cycle_unblock",
                user_uuid=uuid,
                username=username,
                node_name=",".join(i.get("node_name") or "" for i in infos),
                details={"billing_reset_at": reset, "mode": mode},
                dry_run=dry,
            )

            if dry:
                logger.info("[DRY-RUN] Would unblock %s (new cycle %s)", username, reset[:10])
                continue

            if self.api.set_user_external_squads(uuid, full_targets, remove_from_all=True):
                for info in infos:
                    self.db.remove_limited(uuid, info["node_uuid"])
                self._send_alert_sync(
                    f"✅ <b>Новый цикл — разблокировка</b>\n\n"
                    f"👤 <code>{esc(username)}</code>\n"
                    f"📅 Сброс трафика: <code>{esc(reset[:10])}</code>\n"
                    f"🔓 Возврат в full external-сквад"
                )
            else:
                logger.error("Failed to unblock %s", username)

    def run_monthly_reset_job(self):
        """CLI/cron: ручная разблокировка + очистка pending"""
        self.reload_config()
        self._build_user_map()
        self.check_cycle_unblocks()
        current = self.db.current_period_key()
        removed = self.db.purge_stale_pending(current)
        logger.info("Monthly reset job done, purged %s stale pending rows", removed)

    def run_loop(self):
        interval = self.config.get("check_interval_minutes", 10) * 60
        logger.info("Monitor loop started (interval %ss)", interval)
        while True:
            try:
                self.reload_config()
                self._build_user_map()
                self.check_cycle_unblocks()
                self.check_limits()
                self.check_daily_summary()
            except Exception as e:
                logger.error("Loop error: %s", e, exc_info=True)
            time.sleep(interval)

    def check_limits(self):
        dry = self.is_dry_run()
        nodes = self._get_monitored_nodes()
        if not nodes:
            logger.warning("No monitored nodes configured")
            return

        scan_start, scan_end = billing.scan_window_dates(self.config)
        mode = billing.billing_mode(self.config)
        logger.info(
            "Limit check: mode=%s, scan %s→%s, dry_run=%s, hysteresis=%s",
            mode, scan_start, scan_end, dry, self.required_checks(),
        )

        for node_cfg in nodes:
            self._check_node(node_cfg, scan_start, scan_end, dry)

    def _check_node(self, node_cfg: dict, scan_start: str, scan_end: str, dry: bool):
        node_uuid = node_cfg["uuid"]
        limit_gb = float(node_cfg["limit_gb"])
        node_name = node_cfg.get("name", node_uuid[:8])
        effective_limit = self.effective_limit_gb(limit_gb)

        if limit_gb <= 0:
            logger.warning("Skipping %s: limit_gb=%s", node_name, limit_gb)
            return

        limited_external = self._limited_external_from_node(node_cfg)
        if not limited_external:
            logger.warning("Skipping %s: limited_external_squad_uuid not set", node_name)
            return

        # Получаем топ пользователей по трафику
        top_users = self.api.get_node_bandwidth(
            node_uuid, scan_start, scan_end,
            top_limit=int(self.config.get("bandwidth_page_size", 5000)),
        )
        logger.info("%s: %s users in top, limit %.2f GB (effective %.2f)", node_name, len(top_users), limit_gb, effective_limit)

        for u_stat in top_users:
            username = u_stat.get("username")
            stat_uuid = (u_stat.get("uuid") or "").lower()
            if not username and not stat_uuid:
                continue

            user_obj = self._uuid_map.get(stat_uuid) or self._user_map.get(username)
            if not user_obj:
                continue

            user_uuid = user_obj.get("uuid")
            if not user_uuid:
                continue

            if self.db.is_whitelisted(user_uuid):
                continue

            # цикл и лимит: bedolaga → из подписки; иначе → понодовый limit_gb
            user_obj, user_limit_gb, monitor_ok = self._user_quota(user_obj, limit_gb)
            if not monitor_ok:
                continue
            user_eff = self.effective_limit_gb(user_limit_gb)

            period_key = billing.user_period_key(user_obj, self.config)
            billing_reset = billing.billing_reset_iso(user_obj)

            # Если уже ограничен — пропускаем
            if self.db.is_limited(user_uuid, node_uuid, period_key, billing_reset):
                continue

            # Получаем точный трафик
            if billing.is_subscription_mode(self.config):
                u_start, u_end = billing.user_period_dates(user_obj, self.config)
                traffic_gb = self.api.get_user_period_traffic_gb(user_obj, node_uuid, u_start, u_end)
                if traffic_gb is None:
                    traffic_gb = u_stat.get("total", 0) / (1024**3)
            else:
                traffic_gb = u_stat.get("total", 0) / (1024**3)

            if traffic_gb < user_eff:
                self.db.clear_pending(user_uuid, node_uuid, period_key)
                continue

            # Hysteresis: увеличиваем счетчик проверок
            checks = self.db.bump_pending(
                user_uuid, username, node_uuid, period_key,
                traffic_gb, user_limit_gb, node_name=node_name,
            )
            need = self.required_checks()

            if checks < need:
                logger.info(
                    "PENDING %s/%s for %s: %.2f GB >= %.2f GB",
                    checks, need, username, traffic_gb, user_eff
                )
                self.db.audit(
                    "limit_pending", user_uuid=user_uuid, username=username, node_name=node_name,
                    details={"traffic_gb": traffic_gb, "limit_gb": user_limit_gb, "checks": checks, "need": need},
                    dry_run=dry,
                )
                self._notify_verification_pending(username, node_name, traffic_gb, user_limit_gb, checks, need, dry)
                continue

            # Если достигли нужного кол-ва проверок — применяем лимит
            logger.info(
                "%s %s: %.2f GB >= %.2f GB",
                "[DRY-RUN]" if dry else "LIMIT", username, traffic_gb, user_eff
            )

            if dry:
                self.db.clear_pending(user_uuid, node_uuid, period_key)
                self.db.audit(
                    "limit_verified_dry", user_uuid=user_uuid, username=username, node_name=node_name,
                    details={"traffic_gb": traffic_gb, "limit_gb": user_limit_gb}, dry_run=True,
                )
                self._notify_verification_passed(username, node_name, traffic_gb, user_limit_gb, dry=True)
                continue

            # РЕАЛЬНОЕ ДЕЙСТВИЕ
            if self.api.set_user_external_squad(user_uuid, limited_external, remove_from_all=True):
                self.db.add_limited(
                    user_uuid, username, node_uuid, node_name,
                    traffic_gb, user_limit_gb, dry_run=False, period_key=period_key,
                    billing_reset_at=billing_reset,
                )
                self.db.audit(
                    "limit_enforce", user_uuid=user_uuid, username=username, node_name=node_name,
                    details={"traffic_gb": traffic_gb, "limit_gb": user_limit_gb}, dry_run=False,
                )
                self._notify_verification_passed(username, node_name, traffic_gb, user_limit_gb, dry=False)
            else:
                logger.error("Failed to limit %s", username)

    @staticmethod
    def _valid_external_uuid(value) -> Optional[str]:
        if not value: return None
        s = str(value).strip()
        if not s or s.upper().startswith("UUID-"): return None
        return s

    @classmethod
    def _limited_external_from_node(cls, node_cfg: dict) -> Optional[str]:
        return cls._valid_external_uuid(node_cfg.get("limited_external_squad_uuid"))

    @classmethod
    def _full_external_from_node(cls, node_cfg: dict) -> Optional[str]:
        return cls._valid_external_uuid(node_cfg.get("full_external_squad_uuid"))
