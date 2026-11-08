"""stock-facts 只读 detail 接口，不接受维度参数，也不调用 Tushare。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path("/Users/lisun/workplace/go-harness/web/agents/_shared/fetch_facts.py")


class _Detail(BaseHTTPRequestHandler):
    seen = ""

    def do_GET(self):
        _Detail.seen = self.path
        if self.path.startswith("/api/v1/stocks/600519.SH/dimensions/volume_flow/detail"):
            body = {
                "ts_code": "600519.SH",
                "stock_name": "贵州茅台",
                "meta": {"as_of": "2026-10-31"},
                "review": {
                    "as_of": "2026-10-31",
                    "facts": [{"id": "fr.big_net_5d", "label": "近5日大单", "group": "资金分档", "text": "净流入 1.2 亿元", "value": 1.2, "unit": "亿元"}],
                    "items": [{"key": "q3", "title": "大单方向", "status": "support", "answer": "近5日大单净流入"}],
                    "missing": [],
                },
                "review_state": "ready",
                "bars": [{"close": 1}],
            }
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def test_script_reads_detail_and_drops_series():
    if not SCRIPT.is_file():
        raise AssertionError(f"missing {SCRIPT}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Detail)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = os.environ.copy()
        env.pop("TUSHARE_TOKEN", None)
        env["STOCK_API_BASE"] = f"http://127.0.0.1:{server.server_address[1]}"
        env["STOCK_FACTS_DIMENSION"] = "volume_flow"
        env["STOCK_FACTS_QUERY"] = "window=5"
        done = subprocess.run([sys.executable, str(SCRIPT), "600519.SH"], env=env, capture_output=True, text=True, timeout=10)
    finally:
        server.shutdown()
    assert done.returncode == 0, done.stderr
    assert "/dimensions/volume_flow/detail?window=5" in _Detail.seen
    assert "tushare" not in _Detail.seen
    doc = json.loads(done.stdout)
    assert doc["source"] == "detail"
    assert doc["facts"][0]["id"] == "fr.big_net_5d"
    assert "bars" not in doc


def test_script_waits_out_a_computing_shell():
    class _Slow(BaseHTTPRequestHandler):
        n = 0

        def do_GET(self):
            _Slow.n += 1
            if _Slow.n == 1:
                body = {"ts_code": "600519.SH", "meta": {"state": "computing"}, "review": None, "review_state": "computing"}
            else:
                body = {
                    "ts_code": "600519.SH", "stock_name": "贵州茅台", "meta": {"state": "ok", "as_of": "2026-11-03"},
                    "review": {"facts": [{"id": "fd.biz_top", "label": "主营", "group": "业务", "text": "茅台酒占 84%", "value": 0.84, "unit": ""}]},
                    "review_state": "ready",
                }
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = os.environ.copy()
        env["STOCK_API_BASE"] = f"http://127.0.0.1:{server.server_address[1]}"
        env["STOCK_FACTS_DIMENSION"] = "fundamental"
        env["STOCK_FACTS_RETRY_PAUSE"] = "0.05"
        env["STOCK_FACTS_RETRY_DEADLINE"] = "5"
        done = subprocess.run([sys.executable, str(SCRIPT), "600519.SH"], env=env, capture_output=True, text=True, timeout=10)
    finally:
        server.shutdown()
    assert done.returncode == 0, done.stderr
    assert _Slow.n >= 2
    doc = json.loads(done.stdout)
    assert doc["facts"][0]["text"] == "茅台酒占 84%"
    assert doc["review_state"] == "ready"


def test_script_rejects_a_dimension_argument_and_a_missing_binding():
    extra = subprocess.run([sys.executable, str(SCRIPT), "600519.SH", "news"], capture_output=True, text=True)
    assert extra.returncode == 2
    env = os.environ.copy()
    env.pop("STOCK_FACTS_DIMENSION", None)
    missing = subprocess.run([sys.executable, str(SCRIPT), "600519.SH"], env=env, capture_output=True, text=True)
    assert missing.returncode == 2
    assert "STOCK_FACTS_DIMENSION" in missing.stderr
    source = SCRIPT.read_text()
    assert "import tushare" not in source
    assert "pro_api" not in source


def test_news_events_become_facts_when_there_is_no_review_sheet():
    import importlib.util

    spec = importlib.util.spec_from_file_location("fetch_facts", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    doc = mod.slim({
        "ts_code": "600519.SH",
        "key_events": [{
            "event_id": "abc123",
            "title": "公司回购",
            "impact": "positive",
            "source_grade": "A",
            "category": "capital",
            "summary": "回购进展",
            "reaction": {"text": "当日 +1.20%", "priced_in": "partly"},
        }],
        "bars": [{"close": 1}],
    }, "news")
    assert doc["facts"][0]["id"] == "abc123"
    assert "当日 +1.20%" in doc["facts"][0]["text"]
    assert doc["rows"][0]["标题"] == "公司回购"
    assert doc["rows"][0]["方向"] == "利好"
    assert doc["rows"][0]["来源"] == "A 公告"
    assert doc["rows"][0]["类别"] == "资本"
    assert doc["rows"][0]["说明"] == "回购进展"
    assert doc["rows"][0]["当日反应"] == "当日 +1.20%"
    assert doc["rows"][0]["是否反映"] == "部分反映"
    assert "bars" not in doc


def test_flow_daily_series_is_kept_apart_from_window_sums():
    import importlib.util

    spec = importlib.util.spec_from_file_location("fetch_facts", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    doc = mod.slim({
        "review": {"facts": [{"id": "fr.big_net_5d", "label": "近5日大单", "text": "净流入 1.2 亿元", "value": 1.2}]},
        "flow_daily": {"days": [
            {"date": "2026-10-28", "net": 1.2e8},
            {"date": "2026-10-29", "net": None},
            {"date": "2026-10-30", "net": -0.4e8},
        ]},
        "price_volume": {"chart": {"bars": [
            {"date": "2026-10-28", "close": 50.0, "chg": 0.015},
            {"date": "2026-10-30", "close": 48.2, "chg": -0.036},
        ]}},
    }, "volume_flow")
    assert [row["date"] for row in doc["series"]] == ["2026-10-28", "2026-10-29", "2026-10-30"]
    assert doc["series"][0]["net_yi"] == 1.2
    assert doc["series"][0]["close"] == 50.0
    assert doc["series"][0]["chg_pct"] == 1.5
    assert doc["series"][1]["net_yi"] is None
    assert doc["series"][1]["close"] is None
    assert doc["series"][2]["net_yi"] == -0.4
    assert doc["facts"][0]["id"] == "fr.big_net_5d"


def test_news_keyword_matches_storage_synonym():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fetch_news",
        "/Users/lisun/workplace/go-harness/web/skills/news-raw/scripts/fetch_news.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    items = [
        {"title": "公司存储芯片收入占比提升", "summary": ""},
        {"title": "回购股份公告", "summary": "拟回购"},
    ]
    kept = [item for item in items if mod.hit(item, "储存")]
    assert len(kept) == 1
    assert kept[0]["title"].startswith("公司存储")
    doc = mod.document({"ts_code": "603986.SH", "stock_name": "兆易创新"}, "储存", 2, kept)
    assert doc["facts"][0]["id"] == "nw.1"
    assert doc["matched"] == 1
    assert doc["rows"][0]["标题"] == kept[0]["title"]


def test_fundamental_parts_slice_detail():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fetch_part",
        "/Users/lisun/workplace/go-harness/web/skills/fundamental-reply/scripts/fetch_part.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    payload = {
        "ts_code": "601689.SH",
        "stock_name": "拓普集团",
        "meta": {"as_of": "2026-11-02", "industry": {"name": "汽车零部件", "peer_count": 40}},
        "business": {
            "main_business": "汽车零部件",
            "period": "2026-06-30",
            "items": [{"item": "减震", "sales_yi": 10, "share": 0.4, "yoy": 0.1, "gross_margin": 0.2}],
        },
        "valuation": {"metric_text": "扣非市盈率", "value": 18.2, "pctile_5y": 0.62, "peer_median": 15, "history": {"dates": ["2026-10-01"], "values": [18.2], "p50": 16}},
        "quality": {"items": [{"metric_text": "ROE", "value_text": "12%", "peer_pctile": 0.7}], "weakest": {}},
        "holder_events": [{"date": "2026-10-01", "title": "回购", "detail_text": "回购进展", "impact": "positive"}],
    }
    company = mod.pack("company", payload)
    assert company["industry"] == "汽车零部件"
    assert company["main_business"] == "汽车零部件"
    business = mod.pack("business", payload)
    assert business["lines"][0]["占比"] == 40.0
    assert business["series"][0]["label"] == "减震"
    valuation = mod.pack("valuation", payload)
    assert valuation["pctile_5y"] == 62
    assert valuation["series"][0]["value"] == 18.2
    assert mod.pack("holders", payload)["events"][0]["标题"] == "回购"
