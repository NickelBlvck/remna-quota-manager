import asyncio
import logging
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

import billing
from nodes import resolve_monitored_nodes
from notify import esc
from reports import generate_traffic_report

logger = logging.getLogger("bot")

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class QuotaBot:
    def __init__(self, token: str, api_client=None, db=None, config=None):
        self.token = token
        self.api = api_client
        self.db = db
        self.config = config or {}
        self.admin_ids = self._load_admin_ids(self.config)
        self.app = Application.builder().token(token).build()
        self._waiting_for_uuid = {}  # Для ввода UUID через текст
        self._setup_handlers()

    def _setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CallbackQueryHandler(self.on_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("Доступ запрещён.")
            return
        await self._show_menu(update)

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    @staticmethod
    def _load_admin_ids(config):
        try:
            return {int(value) for value in config.get("telegram", {}).get("admin_user_ids", [])}
        except (TypeError, ValueError):
            logger.error("telegram.admin_user_ids must contain integer Telegram IDs")
            return set()

    async def _show_menu(self, update_or_query):
        keyboard = [
            [InlineKeyboardButton("📊 Статус", callback_data="status")],
            [InlineKeyboardButton("📈 Отчёт", callback_data="report")],
            [InlineKeyboardButton("👥 Ограниченные", callback_data="list_limited")],
            [InlineKeyboardButton("🔓 Разблокировать", callback_data="unblock_manual")],
            [InlineKeyboardButton("🛡 Whitelist", callback_data="whitelist_menu")],
            [InlineKeyboardButton("📋 Аудит", callback_data="audit_menu")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = "<b>Remnawave Quota Manager</b>\nВыбери действие:"

        if isinstance(update_or_query, Update):
            await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await update_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not self._is_admin(query.from_user.id):
            await query.edit_message_text("Доступ запрещён.")
            return
        data = query.data

        try:
            if data == "status":
                await self._show_status(query)
            elif data == "report":
                await self._show_report(query)
            elif data in ("list_limited", "unblock_manual"):
                await self._list_limited(query)
            elif data.startswith("confirm_unblock:"):
                await self._do_unblock(query, data.split(":", 1)[1])
            elif data == "whitelist_menu":
                await self._show_whitelist(query)
            elif data == "add_whitelist":
                self._waiting_for_uuid[query.from_user.id] = "whitelist"
                await query.edit_message_text("✏️ Отправь UUID для добавления в whitelist:")
            elif data == "audit_menu":
                await self._show_audit(query)
            elif data == "back":
                await self._show_menu(query)
        except Exception as e:
            logger.error("Bot error: %s", e, exc_info=True)
            try:
                await query.edit_message_text(f"❌ Ошибка: {esc(str(e)[:200])}", parse_mode="HTML")
            except Exception:
                pass

    async def _show_status(self, query):
        if not self.db:
            await query.edit_message_text("❌ DB not connected")
            return

        dry = self.db.is_dry_run(self.config.get("dry_run", True))
        limited_count = len(self.db.list_limited())
        pending_count = len(self.db.list_pending())

        msg = (
            f"📊 <b>System Status</b>\n\n"
            f"🔧 Dry Run: <code>{dry}</code>\n"
            f"🔒 Limited: <code>{limited_count}</code>\n"
            f"⏳ Pending: <code>{pending_count}</code>\n"
        )
        kb = [[InlineKeyboardButton("↩️ Назад", callback_data="back")]]
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    async def _show_report(self, query):
        if not self.api:
            await query.edit_message_text("❌ API not connected")
            return
        await query.edit_message_text("⏳ Собираю статистику по нодам…")

        def _build() -> str:
            cfg = dict(self.config)
            cfg["monitored_nodes"] = resolve_monitored_nodes(self.api, self.config)
            start, end = billing.scan_window_dates(self.config)
            return generate_traffic_report(
                self.api, cfg, start=start, end=end, title="Текущий период", top_n=10
            )

        try:
            text = await asyncio.to_thread(_build)
        except Exception as e:
            logger.error("report failed: %s", e, exc_info=True)
            text = f"❌ Не удалось собрать отчёт: {esc(str(e)[:200])}"

        kb = [[InlineKeyboardButton("↩️ Назад", callback_data="back")]]
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    async def _list_limited(self, query):
        limited = [u for u in self.db.list_limited() if not u.get("dry_run")]
        if not limited:
            msg = "✅ Никто не ограничен."
            kb = [[InlineKeyboardButton("↩️ Назад", callback_data="back")]]
        else:
            msg = f"🔒 <b>Ограниченные ({len(limited)})</b>\n\n"
            kb = []
            seen = set()
            for u in limited:
                label = u.get("username") or u["uuid"]
                msg += f"👤 <code>{esc(label)}</code> — <code>{esc(u.get('node_name') or '?')}</code>\n"
                if u["uuid"] not in seen and len(kb) < 10:
                    seen.add(u["uuid"])
                    kb.append([InlineKeyboardButton(f"🔓 {label[:20]}", callback_data=f"confirm_unblock:{u['uuid']}")])
            kb.append([InlineKeyboardButton("↩️ Назад", callback_data="back")])

        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    async def _do_unblock(self, query, uuid):
        if not self.api or not self.db:
            return

        uuid = uuid.lower()
        records = [r for r in self.db.list_limited() if r["uuid"] == uuid and not r.get("dry_run")]
        if not records:
            await query.edit_message_text("❌ Пользователь не найден в списке ограниченных.")
            return

        nodes = await asyncio.to_thread(resolve_monitored_nodes, self.api, self.config)
        by_uuid = {n.get("uuid"): n for n in nodes}
        by_name = {n.get("name"): n for n in nodes}

        targets, missing = [], []
        for rec in records:
            node = by_uuid.get(rec["node_uuid"]) or by_name.get(rec.get("node_name")) or {}
            squad = node.get("full_external_squad_uuid")
            if squad:
                targets.append(squad)
            else:
                missing.append(rec.get("node_name") or rec["node_uuid"])

        if missing:
            await query.edit_message_text(
                f"❌ Нет full_external_squad_uuid для: {esc(', '.join(missing))}", parse_mode="HTML"
            )
            return

        ok = await asyncio.to_thread(self.api.set_user_external_squads, uuid, targets, True)
        if not ok:
            await query.edit_message_text("❌ Панель не подтвердила разблокировку; запись оставлена.")
            return

        await asyncio.to_thread(self.db.remove_limited, uuid)
        self.db.audit(
            "manual_unblock_bot", user_uuid=uuid,
            details={"by": query.from_user.id, "nodes": len(records)},
        )
        await query.edit_message_text(
            f"✅ Пользователь <code>{esc(uuid[:8])}…</code> разблокирован.", parse_mode="HTML"
        )

    async def _show_whitelist(self, query):
        wl = self.db.list_whitelist()
        msg = f"🛡 <b>Whitelist ({len(wl)})</b>\n\n"
        for uuid in wl:
            msg += f"• <code>{esc(uuid)}</code>\n"

        kb = [
            [InlineKeyboardButton("➕ Добавить", callback_data="add_whitelist")],
            [InlineKeyboardButton("↩️ Назад", callback_data="back")],
        ]
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    async def _show_audit(self, query):
        logs = self.db.recent_audit(limit=10)
        msg = "📋 <b>Recent Audit</b>\n\n"
        for log in logs:
            msg += f"🕒 <code>{esc(log['created_at'][:16])}</code> <code>{esc(log['action'])}</code>\n"
            if log["username"]:
                msg += f"   👤 {esc(log['username'])}\n"

        kb = [[InlineKeyboardButton("↩️ Назад", callback_data="back")]]
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not self._is_admin(uid):
            return
        text = update.message.text.strip()

        if self._waiting_for_uuid.get(uid) == "whitelist":
            if UUID_RE.match(text.lower()):
                self.db.add_whitelist(text.lower())
                await update.message.reply_text(
                    f"✅ <code>{esc(text.lower())}</code> добавлен в whitelist.", parse_mode="HTML"
                )
                self._waiting_for_uuid[uid] = None
            else:
                await update.message.reply_text("❌ Неверный формат UUID.")

    def run(self):
        logger.info("🤖 Bot starting polling...")
        self.app.run_polling(drop_pending_updates=True)
