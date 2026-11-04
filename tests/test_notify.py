from app.notify.emit import emit
from app.notify.outbox import Outbox
from app.notify.price import check_prices, reached


class _Alerts:
    def __init__(self, rows):
        self.rows = rows

    def alerts(self):
        return self.rows


def test_down_alert_sends_once(tmp_path, monkeypatch):
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://example.test/hook")
    sent = []
    monkeypatch.setattr("app.notify.emit.send", lambda url, content: sent.append((url, content)))
    import app.notify.emit as box
    box._outbox = Outbox(tmp_path / "notify.db")
    store = _Alerts([{"id": 7, "ts_code": "601689.SH", "side": "down", "price": 50}])
    quotes = {"601689.SH": {"price": 49.5}}
    names = {"601689.SH": "拓普集团"}
    first = check_prices(store, quotes, names)
    second = check_prices(store, quotes, names)
    assert first[0]["status"] == "sent"
    assert second[0]["status"] == "duplicate"
    assert len(sent) == 1
    assert "49.50" in sent[0][1]
    assert "跌到" in sent[0][1]


def test_price_not_reached_does_not_send(monkeypatch):
    monkeypatch.setattr("app.notify.emit.send", lambda url, content: (_ for _ in ()).throw(AssertionError("不应发送")))
    assert reached({"side": "down", "price": 50}, 51) is False
    assert reached({"side": "up", "price": 50}, 51) is True


def test_missing_webhook_does_not_send(tmp_path, monkeypatch):
    monkeypatch.delenv("WECOM_WEBHOOK_URL", raising=False)
    import app.config as config
    monkeypatch.setattr(config, "WECOM_WEBHOOK_URL", "")
    import app.notify.emit as box
    box._outbox = Outbox(tmp_path / "notify.db")
    result = emit("demo", "标题", "正文")
    assert result["status"] == "unconfigured"
