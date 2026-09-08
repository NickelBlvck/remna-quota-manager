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
