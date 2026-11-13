"""user_id 鉴权与库隔离。"""

from __future__ import annotations

import unittest
from unittest import mock

from app.auth.migrate import LEGACY_USER
from app.auth.user import is_personal_path, resolve_user_id
from app.main import create_app
from app.services.holdings_store import HoldingsStore
from app.synthesis.store import Store as SynthesisStore


class ResolveUserIdTest(unittest.TestCase):
    def test_header_beats_bearer(self):
        class R:
            headers = {"X-User-Id": "alice", "Authorization": "Bearer bob"}

        self.assertEqual(resolve_user_id(R()), "alice")

    def test_bearer_only(self):
        class R:
            headers = {"Authorization": "Bearer tok-1"}

        self.assertEqual(resolve_user_id(R()), "tok-1")

    def test_missing(self):
        class R:
            headers = {}

        self.assertIsNone(resolve_user_id(R()))

    def test_personal_paths(self):
        self.assertTrue(is_personal_path("/api/v1/holdings/summary"))
        self.assertTrue(is_personal_path("/notify/inbox"))
        self.assertTrue(is_personal_path("/api/v1/watchlist"))
        self.assertTrue(is_personal_path("/api/v1/market/settings"))
        self.assertTrue(is_personal_path("/api/v1/market/notifications"))
        self.assertFalse(is_personal_path("/api/v1/market/summary"))
        self.assertFalse(is_personal_path("/api/v1/shared/kline/600519.SH"))
        self.assertFalse(is_personal_path("/healthz"))


class HoldingsIsolationTest(unittest.TestCase):
    def test_users_cannot_see_each_other(self):
        base = HoldingsStore(":memory:")
        alice = base.for_user("alice")
        bob = base.for_user("bob")
        alice.add_trade("600036.SH", "招商银行", "buy", 10.0, 100, "2026-11-01")
        self.assertEqual(len(alice.trades()), 1)
        self.assertEqual(len(bob.trades()), 0)
        bob.add_trade("000001.SZ", "平安银行", "buy", 11.0, 200, "2026-11-01")
        self.assertEqual(len(alice.trades()), 1)
        self.assertEqual(alice.trades()[0]["ts_code"], "600036.SH")
        self.assertEqual(bob.trades()[0]["ts_code"], "000001.SZ")


class SynthesisWatchIsolationTest(unittest.TestCase):
    def test_watchlist_per_user(self):
        base = SynthesisStore(":memory:")
        a = base.for_user("alice")
        b = base.for_user("bob")
        self.assertTrue(a.watch_add("600001.SH", "测试"))
        self.assertEqual([r["ts_code"] for r in a.watch_list()], ["600001.SH"])
        self.assertEqual(b.watch_list(), [])


class HttpAuthTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def test_holdings_requires_user(self):
        self.assertEqual(self.client.get("/api/v1/holdings/summary").status_code, 401)

    def test_holdings_with_header(self):
        with mock.patch("app.api.holdings.Data") as data_cls:
            from test_holdings_page import FakeData

            data_cls.side_effect = lambda *a, **k: FakeData()
            from app.services import holdings_store
            from app.notify import pipeline
            from app.notify.pipeline import Notifier
            from app.notify.store import NotifyStore

            store = HoldingsStore(":memory:")
            holdings_store.set_store(store)
            pipeline.install(Notifier(NotifyStore(":memory:")))
            self.addCleanup(holdings_store.set_store, None)
            self.addCleanup(pipeline.install, None)
            res = self.client.get("/api/v1/holdings/summary", headers={"X-User-Id": "alice"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.get_json()["status"], "ok")

    def test_bearer_works(self):
        res = self.client.get("/notify/inbox", headers={"Authorization": "Bearer user-z"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("items", res.get_json())

    def test_public_market_anonymous(self):
        with mock.patch("app.services.market_page.build_summary", return_value={"ok": True}):
            res = self.client.get("/api/v1/market/summary")
            self.assertEqual(res.status_code, 200)

    def test_public_shared_anonymous(self):
        res = self.client.get("/api/v1/shared/index-list")
        self.assertNotEqual(res.status_code, 401)

    def test_cors_allows_user_headers(self):
        res = self.client.open(
            "/api/v1/holdings/summary",
            method="OPTIONS",
            headers={"Origin": "http://127.0.0.1:8080", "Access-Control-Request-Method": "GET",
                     "Access-Control-Request-Headers": "X-User-Id,Authorization"},
        )
        self.assertEqual(res.status_code, 204)
        allowed = res.headers.get("Access-Control-Allow-Headers", "")
        self.assertIn("X-User-Id", allowed)
        self.assertIn("Authorization", allowed)

    def test_isolation_over_http(self):
        from app.services import holdings_store
        from app.notify import pipeline
        from app.notify.pipeline import Notifier
        from app.notify.store import NotifyStore
        from test_holdings_page import FakeData

        store = HoldingsStore(":memory:")
        holdings_store.set_store(store)
        pipeline.install(Notifier(NotifyStore(":memory:")))
        self.addCleanup(holdings_store.set_store, None)
        self.addCleanup(pipeline.install, None)
        with mock.patch("app.api.holdings.Data", lambda *a, **k: FakeData()):
            self.client.post(
                "/api/v1/holdings/trades",
                json={"ts_code": "600036.SH", "side": "buy", "price": 10, "qty": 100, "date": "2026-11-01"},
                headers={"X-User-Id": "alice"},
            )
            alice = self.client.get("/api/v1/holdings/summary", headers={"X-User-Id": "alice"}).get_json()
            bob = self.client.get("/api/v1/holdings/summary", headers={"X-User-Id": "bob"}).get_json()
        self.assertEqual(alice["tabs"]["now"], 1)
        self.assertTrue(bob.get("empty") or bob["tabs"]["now"] == 0)

    def test_legacy_migrate_default_user(self):
        # 旧库无 user_id 列时回填 default
        import sqlite3
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE trades (
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts_code TEXT, name TEXT, side TEXT,
              price REAL, qty INTEGER, trade_date TEXT, reason TEXT DEFAULT '',
              invalid TEXT DEFAULT '[]', context TEXT DEFAULT '{}', note TEXT DEFAULT '',
              wish_id INTEGER, created_at REAL
            );
            INSERT INTO trades (ts_code, name, side, price, qty, trade_date, created_at)
            VALUES ('600036.SH','招商银行','buy',10,100,'2026-11-01',1);
            """
        )
        conn.commit()
        conn.close()
        store = HoldingsStore(path, user_id=LEGACY_USER)
        self.assertEqual(len(store.trades()), 1)
        self.assertEqual(len(store.for_user("other").trades()), 0)


if __name__ == "__main__":
    unittest.main()
