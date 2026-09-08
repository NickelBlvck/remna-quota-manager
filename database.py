import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "quota.db")


def _utcnow_iso() -> str:
    """Timezone-aware UTC timestamp — all rows store the same clock."""
    return datetime.now(timezone.utc).isoformat()


class QuotaDatabase:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        """Контекстный менеджер для безопасной работы с БД"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL lets the monitor thread and the bot thread read/write concurrently
        # without spurious "database is locked" errors.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        """Безопасная инициализация схемы"""
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS limited_users (
                    uuid TEXT NOT NULL,
                    username TEXT,
                    node_uuid TEXT NOT NULL,
                    node_name TEXT,
                    limited_at TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    traffic_gb REAL,
                    limit_gb REAL,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    billing_reset_at TEXT,
                    PRIMARY KEY (uuid, node_uuid, period_key)
                );

                CREATE TABLE IF NOT EXISTS whitelist (
                    uuid TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL,
                    note TEXT
                );

                CREATE TABLE IF NOT EXISTS pending_limits (
                    uuid TEXT NOT NULL,
                    node_uuid TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    username TEXT,
                    node_name TEXT,
                    traffic_gb REAL,
                    limit_gb REAL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    checks_count INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (uuid, node_uuid, period_key)
                );

                CREATE TABLE IF NOT EXISTS kv_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT NOT NULL,
                    node_uuid TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    username TEXT,
                    node_name TEXT,
                    traffic_gb REAL,
                    limit_gb REAL,
                    billing_reset_at TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by INTEGER,
                    status TEXT NOT NULL DEFAULT 'waiting',
                    UNIQUE (uuid, node_uuid, period_key)
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    user_uuid TEXT,
                    username TEXT,
                    node_name TEXT,
                    details TEXT,
                    dry_run INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_limited_period ON limited_users(period_key);
                CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
                """
            )
            
            # Безопасная миграция колонок
            migrations = [
                ("pending_limits", "node_name", "ALTER TABLE pending_limits ADD COLUMN node_name TEXT"),
                ("pending_limits", "last_seen_at", "ALTER TABLE pending_limits ADD COLUMN last_seen_at TEXT"),
                ("limited_users", "billing_reset_at", "ALTER TABLE limited_users ADD COLUMN billing_reset_at TEXT"),
            ]
            
            for table, col, ddl in migrations:
                try:
                    conn.execute(f"SELECT {col} FROM {table} LIMIT 1")
                except sqlite3.OperationalError:
                    conn.execute(ddl)
                    logger.info(f"🔧 Added column '{col}' to table '{table}'")
                    
                    if col == "last_seen_at" and table == "pending_limits":
                        conn.execute(
                            "UPDATE pending_limits SET last_seen_at = first_seen_at "
                            "WHERE last_seen_at IS NULL"
                        )

    @staticmethod
    def current_period_key(when: Optional[datetime] = None) -> str:
        dt = when or datetime.now(timezone.utc)
        return dt.strftime("%Y-%m")

    @staticmethod
    def month_period_dates(when: Optional[datetime] = None) -> tuple[str, str]:
        dt = when or datetime.now(timezone.utc)
        start = dt.replace(day=1).strftime("%Y-%m-%d")
        end = dt.strftime("%Y-%m-%d")
        return start, end

    def migrate_from_config(self, config: Dict[str, Any]) -> None:
        migrated = False
        with self._conn() as conn:
            for uuid in config.get("whitelist", []):
                if not uuid:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO whitelist (uuid, added_at) VALUES (?, ?)",
                    (uuid.lower(), _utcnow_iso()),
                )
                if cur.rowcount:
                    migrated = True

            period_key = self.current_period_key()
            for u in config.get("limited_users", []):
                uuid = (u.get("uuid") or "").lower()
                if not uuid:
                    continue
                node = self._node_by_name(config, u.get("node_name"))
                node_uuid = node["uuid"] if node else "unknown"
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO limited_users
                    (uuid, username, node_uuid, node_name, limited_at, period_key, traffic_gb, limit_gb, dry_run)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        uuid,
                        u.get("username"),
                        node_uuid,
                        u.get("node_name"),
                        u.get("limited_at") or _utcnow_iso(),
                        period_key,
                        None,
                        node.get("limit_gb") if node else None,
                    ),
                )
                if cur.rowcount:
                    migrated = True

        if migrated:
            logger.info("✅ Migrated whitelist/limited_users from config.json into SQLite")

    def _node_by_name(self, config: Dict[str, Any], node_name: Optional[str]) -> Optional[Dict[str, Any]]:
        if not node_name:
            return None
        for n in config.get("monitored_nodes", []):
            if n.get("name") == node_name:
                return n
        return None

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM kv_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO kv_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def is_dry_run(self, config_default: bool = True) -> bool:
        raw = self.get_setting("dry_run")
        if raw is None:
            return config_default
        return raw.lower() in ("1", "true", "yes", "on")

    def set_dry_run(self, enabled: bool) -> None:
        self.set_setting("dry_run", "true" if enabled else "false")

    def enforcement_mode(self, config_default: str = "manual") -> str:
        """'manual' — блокировка только по апруву админа; 'auto' — сразу."""
        raw = (self.get_setting("enforcement_mode") or config_default or "manual").lower()
        return "auto" if raw == "auto" else "manual"

    def set_enforcement_mode(self, mode: str) -> None:
        self.set_setting("enforcement_mode", "auto" if mode == "auto" else "manual")

    # ---- pending_approvals -------------------------------------------------

    def request_approval(
        self, uuid: str, username: str, node_uuid: str, node_name: str,
        traffic_gb: float, limit_gb: float, *, period_key: str,
        billing_reset_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Создать (или вернуть существующую) заявку на блокировку."""
        uuid = uuid.lower()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO pending_approvals
                (uuid, node_uuid, period_key, username, node_name, traffic_gb,
                 limit_gb, billing_reset_at, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting')
                ON CONFLICT(uuid, node_uuid, period_key) DO UPDATE SET
                    username = excluded.username,
                    traffic_gb = excluded.traffic_gb,
                    limit_gb = excluded.limit_gb
                """,
                (uuid, node_uuid, period_key, username, node_name, traffic_gb,
                 limit_gb, billing_reset_at, _utcnow_iso()),
            )
            row = conn.execute(
                "SELECT * FROM pending_approvals WHERE uuid=? AND node_uuid=? AND period_key=?",
                (uuid, node_uuid, period_key),
            ).fetchone()
        return dict(row) if row else {}

    def approval_status(self, uuid: str, node_uuid: str, period_key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM pending_approvals WHERE uuid=? AND node_uuid=? AND period_key=?",
                (uuid.lower(), node_uuid, period_key),
            ).fetchone()
        return row["status"] if row else None

    def get_approval(self, approval_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pending_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return dict(row) if row else None

    def decide_approval(self, approval_id: int, status: str, decided_by: Optional[int] = None) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE pending_approvals SET status=?, decided_at=?, decided_by=? "
                "WHERE id=? AND status='waiting'",
                (status, _utcnow_iso(), decided_by, approval_id),
            )
        return cur.rowcount > 0

    def list_waiting_approvals(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_approvals WHERE status='waiting' ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def purge_stale_approvals(self, before_period: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM pending_approvals WHERE period_key < ?", (before_period,)
            )
        return cur.rowcount

    def audit(
        self,
        action: str,
        *,
        user_uuid: Optional[str] = None,
        username: Optional[str] = None,
        node_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (created_at, action, user_uuid, username, node_name, details, dry_run)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow_iso(),
                    action,
                    user_uuid,
                    username,
                    node_name,
                    json.dumps(details or {}, ensure_ascii=False),
                    1 if dry_run else 0,
                ),
            )

    def list_whitelist(self) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT uuid FROM whitelist ORDER BY added_at").fetchall()
        return [r["uuid"] for r in rows]

    def add_whitelist(self, uuid: str, note: str = "") -> bool:
        uuid = uuid.lower()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO whitelist (uuid, added_at, note) VALUES (?, ?, ?)",
                (uuid, _utcnow_iso(), note),
            )
        return cur.rowcount > 0

    def remove_whitelist(self, uuid: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM whitelist WHERE uuid = ?", (uuid.lower(),))
        return cur.rowcount > 0

    def is_whitelisted(self, uuid: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM whitelist WHERE uuid = ?", (uuid.lower(),)).fetchone()
        return row is not None

    def list_limited(self, period_key: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            if period_key:
                rows = conn.execute(
                    "SELECT * FROM limited_users WHERE period_key = ? ORDER BY limited_at DESC",
                    (period_key,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM limited_users ORDER BY limited_at DESC").fetchall()
        return [dict(r) for r in rows]

    def is_limited(
        self,
        uuid: str,
        node_uuid: str,
        period_key: Optional[str] = None,
        billing_reset_at: Optional[str] = None,
    ) -> bool:
        with self._conn() as conn:
            if billing_reset_at:
                row = conn.execute(
                    """
                    SELECT 1 FROM limited_users
                    WHERE uuid = ? AND node_uuid = ? AND dry_run = 0 AND billing_reset_at = ?
                    """,
                    (uuid.lower(), node_uuid, billing_reset_at),
                ).fetchone()
                if row:
                    return True
            period_key = period_key or self.current_period_key()
            row = conn.execute(
                """
                SELECT 1 FROM limited_users
                WHERE uuid = ? AND node_uuid = ? AND period_key = ? AND dry_run = 0
                """,
                (uuid.lower(), node_uuid, period_key),
            ).fetchone()
        return row is not None

    def add_limited(
        self,
        uuid: str,
        username: str,
        node_uuid: str,
        node_name: str,
        traffic_gb: float,
        limit_gb: float,
        *,
        dry_run: bool = False,
        period_key: Optional[str] = None,
        billing_reset_at: Optional[str] = None,
    ) -> None:
        period_key = period_key or self.current_period_key()
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM pending_limits WHERE uuid = ? AND node_uuid = ? AND period_key = ?",
                (uuid.lower(), node_uuid, period_key),
            )
            conn.execute(
                """
                INSERT INTO limited_users
                (uuid, username, node_uuid, node_name, limited_at, period_key,
                 traffic_gb, limit_gb, dry_run, billing_reset_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(uuid, node_uuid, period_key) DO UPDATE SET
                    username = excluded.username,
                    limited_at = excluded.limited_at,
                    traffic_gb = excluded.traffic_gb,
                    limit_gb = excluded.limit_gb,
                    dry_run = excluded.dry_run,
                    billing_reset_at = excluded.billing_reset_at
                """,
                (
                    uuid.lower(),
                    username,
                    node_uuid,
                    node_name,
                    _utcnow_iso(),
                    period_key,
                    traffic_gb,
                    limit_gb,
                    1 if dry_run else 0,
                    billing_reset_at,
                ),
            )

    def remove_limited(self, uuid: str, node_uuid: Optional[str] = None) -> int:
        with self._conn() as conn:
            if node_uuid:
                cur = conn.execute(
                    "DELETE FROM limited_users WHERE uuid = ? AND node_uuid = ?",
                    (uuid.lower(), node_uuid),
                )
            else:
                cur = conn.execute("DELETE FROM limited_users WHERE uuid = ?", (uuid.lower(),))
            conn.execute("DELETE FROM pending_limits WHERE uuid = ?", (uuid.lower(),))
            if node_uuid:
                conn.execute(
                    "DELETE FROM pending_approvals WHERE uuid = ? AND node_uuid = ?",
                    (uuid.lower(), node_uuid),
                )
            else:
                conn.execute("DELETE FROM pending_approvals WHERE uuid = ?", (uuid.lower(),))
        return cur.rowcount

    def list_pending(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM pending_limits ORDER BY first_seen_at DESC").fetchall()
        return [dict(r) for r in rows]

    def bump_pending(
        self,
        uuid: str,
        username: str,
        node_uuid: str,
        period_key: str,
        traffic_gb: float,
        limit_gb: float,
        node_name: str = "",
    ) -> int:
        now = _utcnow_iso()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT checks_count FROM pending_limits WHERE uuid = ? AND node_uuid = ? AND period_key = ?",
                (uuid.lower(), node_uuid, period_key),
            ).fetchone()
            if row:
                new_count = row["checks_count"] + 1
                conn.execute(
                    """
                    UPDATE pending_limits
                    SET checks_count = ?, traffic_gb = ?, limit_gb = ?, username = ?,
                        node_name = ?, last_seen_at = ?
                    WHERE uuid = ? AND node_uuid = ? AND period_key = ?
                    """,
                    (new_count, traffic_gb, limit_gb, username, node_name, now,
                     uuid.lower(), node_uuid, period_key),
                )
                return new_count
            conn.execute(
                """
                INSERT INTO pending_limits
                (uuid, node_uuid, period_key, username, node_name, traffic_gb, limit_gb,
                 first_seen_at, last_seen_at, checks_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (uuid.lower(), node_uuid, period_key, username, node_name,
                 traffic_gb, limit_gb, now, now),
            )
            return 1

    def clear_pending(self, uuid: str, node_uuid: str, period_key: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM pending_limits WHERE uuid = ? AND node_uuid = ? AND period_key = ?",
                (uuid.lower(), node_uuid, period_key),
            )

    def list_enforced_limited(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM limited_users WHERE dry_run = 0 ORDER BY limited_at").fetchall()
        return [dict(r) for r in rows]

    def list_for_monthly_unblock(self, current_period: Optional[str] = None) -> List[Dict[str, Any]]:
        current_period = current_period or self.current_period_key()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM limited_users WHERE period_key < ? AND dry_run = 0 ORDER BY limited_at",
                (current_period,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_for_personal_unblock(self, billing_reset_at: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            if billing_reset_at:
                rows = conn.execute(
                    "SELECT * FROM limited_users WHERE dry_run = 0 AND billing_reset_at = ? ORDER BY limited_at",
                    (billing_reset_at,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM limited_users WHERE dry_run = 0 ORDER BY limited_at").fetchall()
        return [dict(r) for r in rows]

    def purge_stale_pending(self, before_period: str) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM pending_limits WHERE period_key < ?", (before_period,))
        return cur.rowcount

    def recent_audit(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def recent_verification_audit(self, limit: int = 20) -> List[Dict[str, Any]]:
        actions = ("limit_pending", "limit_verified_dry", "limit_would_enforce", "limit_enforce")
        placeholders = ",".join("?" * len(actions))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM audit_log WHERE action IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                (*actions, limit),
            ).fetchall()
        return [dict(r) for r in rows]
