import unittest
import os
import tempfile
from datetime import datetime, timedelta, timezone

import billing
from database import QuotaDatabase
from nodes import resolve_monitored_nodes


class BillingTests(unittest.TestCase):
    def test_expired_aware_subscription_cycle_unblocks(self):
        reset = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self.assertTrue(billing.should_unblock_user(
            {"billing_reset_at": reset}, {},
            {"billing": {"mode": "subscription", "subscription_cycle_days": 30}},
        ))

    def test_future_subscription_cycle_stays_limited(self):
        reset = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.assertFalse(billing.should_unblock_user(
            {"billing_reset_at": reset}, {},
            {"billing": {"mode": "subscription", "subscription_cycle_days": 30}},
        ))

    def test_cycle_anchor_rolls_forward_from_created_at(self):
        cfg = {"billing": {"mode": "subscription", "subscription_cycle_days": 30}}
        created = datetime.now(timezone.utc) - timedelta(days=75)  # 2.5 cycles ago
        anchor = billing.cycle_anchor({"createdAt": created.isoformat()}, cfg)
        # anchor is the start of the current cycle: created + 60d (2 whole cycles)
        self.assertEqual(anchor, created + timedelta(days=60))
        start, end = billing.user_period_dates({"createdAt": created.isoformat()}, cfg)
        self.assertEqual(start, (created + timedelta(days=60)).strftime("%Y-%m-%d"))
        self.assertEqual(end, (created + timedelta(days=90)).strftime("%Y-%m-%d"))

    def test_no_reset_user_unblocks_after_one_cycle(self):
        cfg = {"billing": {"mode": "subscription", "subscription_cycle_days": 30}}
        # limited 40 days ago, no lastTrafficResetAt anywhere -> must still unblock
        info = {"limited_at": (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()}
        self.assertTrue(billing.should_unblock_user(info, {}, cfg))
        info_recent = {"limited_at": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()}
        self.assertFalse(billing.should_unblock_user(info_recent, {}, cfg))


class DatabaseTests(unittest.TestCase):
    def _db(self) -> QuotaDatabase:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        return QuotaDatabase(path)

    def test_hysteresis_counter_and_clear(self):
        db = self._db()
        self.assertEqual(db.bump_pending("U", "user", "N", "period", 2, 1), 1)
        self.assertEqual(db.bump_pending("U", "user", "N", "period", 3, 1), 2)
        db.clear_pending("U", "N", "period")
        self.assertEqual(db.list_pending(), [])

    def test_remove_limited_is_scoped_to_a_single_node(self):
        db = self._db()
        db.add_limited("u1", "user", "nodeA", "A", 10, 5, period_key="2026-01")
        db.add_limited("u1", "user", "nodeB", "B", 10, 5, period_key="2026-01")
        db.remove_limited("u1", "nodeA")
        rows = db.list_limited()
        self.assertEqual([r["node_uuid"] for r in rows], ["nodeB"])

    def test_remove_limited_without_node_removes_all(self):
        db = self._db()
        db.add_limited("u1", "user", "nodeA", "A", 10, 5, period_key="2026-01")
        db.add_limited("u1", "user", "nodeB", "B", 10, 5, period_key="2026-01")
        db.remove_limited("u1")
        self.assertEqual(db.list_limited(), [])


class EvaluateTests(unittest.TestCase):
    class _FakeAPI:
        GB = 1024 ** 3

        def __init__(self):
            self._users = [
                {"uuid": "a-uuid", "username": "alice", "lastTrafficResetAt": "2026-09-01T00:00:00Z"},
                {"uuid": "b-uuid", "username": "bob", "lastTrafficResetAt": "2026-09-01T00:00:00Z"},
                {"uuid": "c-uuid", "username": "carol", "lastTrafficResetAt": "2026-09-01T00:00:00Z"},
            ]

        def get_users(self, limit=5000):
            return self._users

        def get_nodes(self):
            return []

        def get_user(self, uuid):
            return next((u for u in self._users if u["uuid"] == uuid), None)

        def get_node_bandwidth(self, node_uuid, start, end, top_limit=100):
            return [
                {"username": "alice", "uuid": "a-uuid", "total": 12 * self.GB},
                {"username": "carol", "uuid": "c-uuid", "total": 11 * self.GB},
                {"username": "bob", "uuid": "b-uuid", "total": 5 * self.GB},
            ]

    def _monitor(self):
        from monitor import TrafficMonitor
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        db = QuotaDatabase(path)
        db.add_whitelist("c-uuid")
        cfg = {
            "billing": {"mode": "subscription", "subscription_cycle_days": 30},
            "monitored_nodes": [{"uuid": "n1", "name": "node-1", "limit_gb": 10,
                                 "limited_external_squad_uuid": "lim-1"}],
            "dry_run": True,
        }
        return TrafficMonitor(self._FakeAPI(), db, cfg)

    def test_evaluate_classifies_over_and_whitelist_and_skips_ok(self):
        result = self._monitor().evaluate()
        verdicts = {r["username"]: r["verdict"] for r in result["rows"]}
        self.assertEqual(verdicts.get("alice"), "over")
        self.assertEqual(verdicts.get("carol"), "whitelist")
        self.assertNotIn("bob", verdicts)   # under limit → not reported
        self.assertTrue(result["dry_run"])


class BedolagaTests(unittest.TestCase):
    def test_short_uuid_from_sub_url(self):
        from bedolaga import short_uuid_from_sub_url
        self.assertEqual(
            short_uuid_from_sub_url("https://sub.example.com/aB3xY9zK"), "aB3xY9zK"
        )
        self.assertEqual(
            short_uuid_from_sub_url("https://sub.example.com/path/aB3xY9zK/"), "aB3xY9zK"
        )
        self.assertIsNone(short_uuid_from_sub_url(""))
        self.assertIsNone(short_uuid_from_sub_url(None))

    def test_bedolaga_mode_uses_tariff_limit_and_start_date(self):
        from monitor import TrafficMonitor
        GB = 1024 ** 3

        class FakeRemna:
            def get_users(self, limit=5000):
                return [{"uuid": "ru-1", "username": "user_777", "shortUuid": "SHORT1",
                         "telegramId": 777, "createdAt": "2026-01-01T00:00:00Z"}]

            def get_nodes(self):
                return []

            def get_user(self, uuid):
                return None

            def get_node_bandwidth(self, node_uuid, start, end, top_limit=100):
                return [{"username": "user_777", "uuid": "ru-1", "total": 12 * GB}]

        class FakeBedolaga:
            def iter_users(self):
                start = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
                return iter([{
                    "telegram_id": 777,
                    "subscription": {
                        "status": "active", "start_date": start,
                        "traffic_limit_gb": 10,
                        "subscription_url": "https://sub.example.com/SHORT1",
                        "connected_squads": [],
                    },
                }])

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        db = QuotaDatabase(path)
        cfg = {
            "billing": {"mode": "bedolaga", "subscription_cycle_days": 30},
            "bedolaga": {"base_url": "http://x", "token": "t"},
            "monitored_nodes": [{"uuid": "n1", "name": "n1", "limit_gb": 999,
                                 "limited_external_squad_uuid": "lim"}],
            "dry_run": True,
        }
        m = TrafficMonitor(FakeRemna(), db, cfg)
        m.bedolaga = FakeBedolaga()
        result = m.evaluate()
        row = next((r for r in result["rows"] if r["username"] == "user_777"), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["limit_gb"], 10.0)        # tariff limit, not node's 999
        self.assertEqual(row["verdict"], "over")       # 12 GB >= 10 GB tariff

    def test_unlinked_user_falls_back_to_node_limit(self):
        from monitor import TrafficMonitor
        GB = 1024 ** 3

        class FakeRemna:
            def get_users(self, limit=5000):
                return [{"uuid": "ru-x", "username": "legacy_guy",
                         "createdAt": "2026-08-20T00:00:00Z"}]

            def get_nodes(self):
                return []

            def get_user(self, uuid):
                return None

            def get_node_bandwidth(self, node_uuid, start, end, top_limit=100):
                return [{"username": "legacy_guy", "uuid": "ru-x", "total": 60 * GB}]

        class EmptyBedolaga:
            def iter_users(self):
                return iter([])

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        db = QuotaDatabase(path)
        base = {
            "billing": {"mode": "bedolaga", "subscription_cycle_days": 30},
            "bedolaga": {"base_url": "http://x", "token": "t"},
            "monitored_nodes": [{"uuid": "n1", "name": "n1", "limit_gb": 50,
                                 "limited_external_squad_uuid": "lim"}],
            "dry_run": True,
        }
        m = TrafficMonitor(FakeRemna(), db, dict(base))
        m.bedolaga = EmptyBedolaga()
        row = next((r for r in m.evaluate()["rows"] if r["username"] == "legacy_guy"), None)
        self.assertIsNotNone(row)                       # fallback on by default
        self.assertEqual(row["limit_gb"], 50.0)         # node limit

        cfg2 = dict(base)
        cfg2["bedolaga"] = {**base["bedolaga"], "fallback_unlinked": False}
        m2 = TrafficMonitor(FakeRemna(), db, cfg2)
        m2.bedolaga = EmptyBedolaga()
        self.assertFalse(any(r["username"] == "legacy_guy" for r in m2.evaluate()["rows"]))


class ApprovalFlowTests(unittest.TestCase):
    def test_manual_mode_creates_approval_and_does_not_enforce(self):
        from monitor import TrafficMonitor
        GB = 1024 ** 3
        calls = []

        class FakeRemna:
            def get_users(self, limit=5000):
                return [{"uuid": "ru-1", "username": "hog",
                         "createdAt": "2026-08-25T00:00:00Z"}]

            def get_nodes(self):
                return []

            def get_user(self, uuid):
                return None

            def get_node_bandwidth(self, node_uuid, start, end, top_limit=100):
                return [{"username": "hog", "uuid": "ru-1", "total": 90 * GB}]

            def get_user_period_traffic_gb(self, user, node_uuid, s, e):
                return 90.0

            def set_user_external_squad(self, *a, **k):
                calls.append(a)
                return True

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        db = QuotaDatabase(path)
        cfg = {
            "billing": {"mode": "subscription", "subscription_cycle_days": 30},
            "monitored_nodes": [{"uuid": "n1", "name": "n1", "limit_gb": 50,
                                 "limited_external_squad_uuid": "lim-1"}],
            "dry_run": False, "enforcement_mode": "manual",
            "limit_hysteresis_checks": 1,
        }
        m = TrafficMonitor(FakeRemna(), db, cfg)
        m._build_user_map()
        m.check_limits()

        self.assertEqual(calls, [])                       # nothing enforced
        waiting = db.list_waiting_approvals()
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["username"], "hog")
        self.assertEqual(db.list_limited(), [])          # not limited yet

        # second pass must not create a duplicate
        m.check_limits()
        self.assertEqual(len(db.list_waiting_approvals()), 1)

        # skip -> exempt for the cycle, still not limited
        aid = waiting[0]["id"]
        self.assertTrue(db.decide_approval(aid, "skipped", 1))
        m.check_limits()
        self.assertEqual(db.list_limited(), [])
        self.assertEqual(calls, [])


class RemnawaveSecretCookieTests(unittest.TestCase):
    def test_secret_key_sets_session_cookie_with_colon(self):
        from remnawave import RemnawaveAPI
        api = RemnawaveAPI("https://panel.example.com", "tok", secret_key="aEmFnBcC:WbYWpixX")
        self.assertEqual(api.session.cookies.get("aEmFnBcC"), "WbYWpixX")

    def test_secret_key_sets_session_cookie_with_equals(self):
        from remnawave import RemnawaveAPI
        api = RemnawaveAPI("https://panel.example.com", "tok", secret_key="aEmFnBcC=WbYWpixX")
        self.assertEqual(api.session.cookies.get("aEmFnBcC"), "WbYWpixX")

    def test_missing_secret_key_sets_no_cookie(self):
        from remnawave import RemnawaveAPI
        api = RemnawaveAPI("https://panel.example.com", "tok")
        self.assertEqual(len(api.session.cookies), 0)

    def test_malformed_secret_key_is_ignored(self):
        from remnawave import RemnawaveAPI
        api = RemnawaveAPI("https://panel.example.com", "tok", secret_key="no-colon-here")
        self.assertEqual(len(api.session.cookies), 0)


class NodeResolutionTests(unittest.TestCase):
    class _FakeAPI:
        def get_nodes(self):
            return [
                {"uuid": "n1", "name": "alpha", "isDisabled": False},
                {"uuid": "n2", "name": "beta", "isDisabled": False},
                {"uuid": "n3", "name": "gamma", "isDisabled": True},
            ]

    def test_auto_discover_merges_policy_and_skips_unpoliced(self):
        cfg = {"nodes": {"auto_discover": True, "policies": {
            "n1": {"limit_gb": 20, "full_external_squad_uuid": "f1", "limited_external_squad_uuid": "l1"},
        }}}
        nodes = resolve_monitored_nodes(self._FakeAPI(), cfg)
        self.assertEqual([n["uuid"] for n in nodes], ["n1"])
        self.assertEqual(nodes[0]["limit_gb"], 20)
        self.assertEqual(nodes[0]["name"], "alpha")

    def test_disabled_node_included_when_requested(self):
        cfg = {"nodes": {"auto_discover": True, "include_disabled": True, "policies": {
            "n3": {"limit_gb": 5, "full_external_squad_uuid": "f", "limited_external_squad_uuid": "l"},
        }}}
        nodes = resolve_monitored_nodes(self._FakeAPI(), cfg)
        self.assertEqual([n["uuid"] for n in nodes], ["n3"])

    def test_without_auto_discover_returns_configured(self):
        cfg = {"monitored_nodes": [{"uuid": "n1", "limit_gb": 5}]}
        self.assertEqual(resolve_monitored_nodes(None, cfg), cfg["monitored_nodes"])


if __name__ == "__main__":
    unittest.main()
