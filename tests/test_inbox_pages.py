"""收件箱的通知并进市场页和持仓页的通知抽屉。"""

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import create_app
from app.notify import pageview, pipeline
from app.notify.pipeline import Notifier
from app.notify.store import NotifyStore
from app.scheduler.clock import FakeClock
from app.services import holdings_page as hp
from app.services import market_notify
from app.services.holdings_store import HoldingsStore
from app.services.market_store import MarketStore
import tempfile
from test_holdings_page import TODAY, Base


class InboxCase(Base):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock("2026-11-04 10:00")
        self.n = Notifier(NotifyStore(":memory:"), self.clock)
        self.n.store.put_channel({"channel_id": "ch_desktop", "enabled": 0})

    def inbox_ctx(self):
        return hp.Context(self.store, self.data, TODAY, inbox=self.n.store)

    def announce(self, code, name, title, key):
        return self.n.publish("announcement.published", {"kind": "stock", "id": code},
                              {"ts_code": code, "name": name, "item_id": key, "title": title, "topic": "监管函", "importance": "P0"}, key, severity="urgent")


class HoldingsDrawerTest(InboxCase):
    def test_new_notifications_for_my_stocks_show_up_and_others_do_not(self):
        self.seed()
        self.announce("601689.SH", "拓普集团", "关于收到监管函的公告", "a1")
        self.announce("000001.SZ", "平安银行", "别人的公告", "a2")
        self.n.publish("task.failed", {"kind": "system", "id": "stock.nightly"}, {"task_id": "stock.nightly", "title": "每晚个股分析", "dead": 2, "total": 9, "run_id": "r"}, "r")
        data = hp.notifications(self.inbox_ctx())
        inbox = [i for i in data["items"] if i["id"].startswith("inbox:")]
        self.assertEqual(sorted(i["kind"] for i in inbox), ["ann", "system"])
        ann = next(i for i in inbox if i["kind"] == "ann")
        self.assertEqual((ann["code"], ann["group"], ann["unread"]), ("601689.SH", "今天", True))
        self.assertNotIn("别人的公告", str(data["items"]))

    def test_without_an_inbox_nothing_changes(self):
        self.seed()
        self.announce("601689.SH", "拓普集团", "关于收到监管函的公告", "a1")
        self.assertFalse(any(i["id"].startswith("inbox:") for i in hp.notifications(self.ctx())["items"]))

    def test_a_fired_alert_replaces_the_page_computed_one_instead_of_showing_twice(self):
        self.seed()
        self.data.quote_map["601689.SH"]["price"] = 56.42
        before = [i for i in hp.notifications(self.inbox_ctx())["items"] if i["kind"] == "alert"]
        self.assertEqual(len(before), 1)
        self.n.publish("price.alert.fired", {"kind": "stock", "id": "601689.SH"}, {"alert_id": "1", "ts_code": "601689.SH", "name": "拓普集团", "side": "down",
                       "trigger_price": 56.5, "observed_price": 56.42}, "alert-1:1")
        after = [i for i in hp.notifications(self.inbox_ctx())["items"] if i["kind"] == "alert"]
        self.assertEqual([i["id"].startswith("inbox:") for i in after], [True])

    def test_marking_read_marks_the_inbox_too(self):
        self.seed()
        self.announce("601689.SH", "拓普集团", "关于收到监管函的公告", "a1")
        self.assertEqual(self.n.store.unread_count(), 1)
        hp.mark_read(self.inbox_ctx())
        self.assertEqual(self.n.store.unread_count(), 0)
        self.assertEqual(hp.notifications(self.inbox_ctx())["unread"], 0)

    def test_older_items_land_in_an_earlier_group(self):
        self.seed()
        self.announce("601689.SH", "拓普集团", "关于收到监管函的公告", "a1")
        self.clock.advance(days=3)
        self.n.clock = self.clock
        ann = [i for i in hp.notifications(self.inbox_ctx())["items"] if i["kind"] == "ann"]
        self.assertEqual(len(ann), 1)


class MarketDrawerTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock("2026-11-04 10:00")
        self.n = Notifier(NotifyStore(":memory:"), self.clock)
        self.n.store.put_channel({"channel_id": "ch_desktop", "enabled": 0})
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.market = MarketStore(Path(self.dir.name) / "m.db")

    def test_briefs_and_failures_are_merged_but_page_events_are_not_duplicated(self):
        self.n.publish_text("market:brief:2026-11-04:pre", "市场 · 盘前简报", "档位 中性。\n怎么做：先看")
        self.n.publish_text("market:2026-11-04:level:档位变化", "市场 · 档位变化", "由中性变逆风")      # 页面自己的事件已有，不重复
        self.n.publish("task.overdue", {"kind": "system", "id": "market.pre"}, {"task_id": "market.pre", "title": "盘前简报", "expected_by": "11-04 10:00", "state": "没有运行记录"}, "o")
        rows = pageview.for_market(self.n.store)
        self.assertEqual(sorted(r["type"] for r in rows), ["generic.notice", "task.overdue"])
        data = market_notify.notifications(self.market, today=date(2026, 11, 4), inbox=rows)
        items = [i for g in data["groups"] for i in g["items"]]
        self.assertEqual(sorted(i["kind"] for i in items), ["简报", "系统"])
        self.assertEqual(data["unread"], 2)
        self.assertTrue(all(i["id"].startswith("inbox:") for i in items))

    def test_read_endpoint_accepts_mixed_ids(self):
        import app.services.market_store as ms
        ms.set_store(self.market)
        self.addCleanup(ms.set_store, None)
        pipeline.install(self.n)
        self.addCleanup(pipeline.install, None)
        self.n.publish_text("market:brief:2026-11-04:pre", "市场 · 盘前简报", "正文")
        patcher = mock.patch("app.services.market_page.notify_payload",
                             side_effect=lambda inbox=None: market_notify.notifications(self.market, today=date(2026, 11, 4), inbox=inbox))
        patcher.start()
        self.addCleanup(patcher.stop)
        client = create_app().test_client()
        body = client.get("/api/v1/market/notifications").get_json()
        notif = [i for g in body["groups"] for i in g["items"] if i["id"].startswith("inbox:")][0]
        self.assertEqual(client.post("/api/v1/market/notifications/read", json={"ids": [notif["id"]]}).status_code, 200)
        self.assertEqual(self.n.store.unread_count(), 0)


if __name__ == "__main__":
    unittest.main()
