import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask

from app.api.shared import bp
from app.services import baidu_board as board
from app.tools import cache


def page(rows, update="1794115062"):
    card = {"component": "hotList", "typeName": "finance", "updateTime": update, "content": rows}
    return "<html><!--s-data:" + json.dumps({"data": {"cards": [card]}}, ensure_ascii=False) + "--></html>"


ROWS = [
    {"word": "央企中国国新：将举报", "hotScore": "4842081", "hotTag": "1", "desc": "", "rawUrl": "https://www.baidu.com/s?wd=a", "isTop": False},
    {"query": "工作人员回应电动鞋售价7999元", "hotScore": "3901820", "hotTag": "3", "desc": "简介", "url": "https://www.baidu.com/s?wd=b"},
    {"word": "", "hotScore": "1"},
    {"word": "没有热度的一条", "hotScore": "", "hotTag": "0"},
]


class ParseTest(unittest.TestCase):
    def test_page_gives_rank_title_heat_and_tag_and_skips_empty_titles(self):
        items, updated = board.parse_page(page(ROWS))
        self.assertEqual([i["title"] for i in items], ["央企中国国新：将举报", "工作人员回应电动鞋售价7999元", "没有热度的一条"])
        self.assertEqual([i["rank"] for i in items], [1, 2, 3])
        self.assertEqual((items[0]["hot"], items[0]["hot_text"], items[0]["tag"]), (4842081, "484.2万", "新"))
        self.assertEqual((items[1]["tag"], items[1]["desc"]), ("热", "简介"))
        self.assertEqual((items[2]["hot"], items[2]["hot_text"], items[2]["tag"]), (None, "", ""))
        self.assertEqual(updated, "1794115062")

    def test_a_page_without_the_data_block_or_with_a_changed_shape_is_an_error(self):
        with self.assertRaises(board.BoardError):
            board.parse_page("<html>限流了</html>")
        with self.assertRaises(board.BoardError):
            board.parse_page("<!--s-data:" + json.dumps({"data": {"cards": []}}) + "-->")

    def test_phone_api_has_no_heat_but_keeps_the_new_mark(self):
        payload = {"data": {"cards": [{"component": "tabTextList", "content": [{"content": [{"word": "甲", "index": 1, "url": "u1", "newHotName": "新"}, {"word": "乙", "index": 2, "url": "u2"}]}]}]}}
        items = board.parse_api(payload)
        self.assertEqual([(i["rank"], i["title"], i["tag"], i["hot"]) for i in items], [(1, "甲", "新", None), (2, "乙", "", None)])


class FetchTest(unittest.TestCase):
    def setUp(self):
        cache.clear()

    def test_limit_is_applied_and_the_answer_is_cached(self):
        calls = []

        def download(name):
            calls.append(name)
            return board.parse_page(page(ROWS))

        with patch.object(board, "_download", download):
            first = board.fetch_board("finance", 2)
            again = board.fetch_board("finance", 30)
        self.assertEqual((first["count"], len(first["items"]), first["stale"]), (2, 2, False))
        self.assertEqual(again["count"], 3)
        self.assertEqual(calls, ["finance"])  # the second call did not ask Baidu again

    def test_when_baidu_fails_the_last_good_board_is_given_and_marked_stale(self):
        with patch.object(board, "_download", return_value=board.parse_page(page(ROWS))):
            board.fetch_board("finance")
        cache.set("baidu:board:finance", None, 0)  # the fresh copy expires, the last good one stays
        with patch.object(board, "_download", side_effect=board.BoardError("被限流")):
            got = board.fetch_board("finance")
        self.assertTrue(got["stale"])
        self.assertIn("限流", got["stale_reason"])
        self.assertEqual(got["count"], 3)

    def test_failure_with_nothing_to_fall_back_on_raises_and_unknown_boards_are_refused(self):
        with patch.object(board, "_download", side_effect=board.BoardError("被限流")):
            with self.assertRaises(board.BoardError):
                board.fetch_board("finance")
        with self.assertRaises(ValueError):
            board.fetch_board("nope")

    def test_page_is_tried_first_then_the_phone_api(self):
        def fake_get(url, params):
            if url == board.BOARD_URL:
                raise OSError("连不上")

            class R:
                @staticmethod
                def json():
                    return {"data": {"cards": [{"content": [{"content": [{"word": "甲", "url": "u"}]}]}]}}

            return R()

        with patch.object(board, "_get", fake_get):
            items, updated = board._download("finance")
        self.assertEqual((items[0]["title"], updated), ("甲", ""))


class EndpointTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(bp)
        self.client = app.test_client()

    def test_ok_bad_request_and_bad_gateway(self):
        fake = {"board": "finance", "count": 1, "items": [{"rank": 1, "title": "甲"}]}
        with patch("app.services.baidu_board.fetch_board", return_value=fake) as call:
            body = self.client.get("/api/v1/shared/baidu-board?limit=5").get_json()
        self.assertEqual((body["status"], body["items"][0]["title"]), ("ok", "甲"))
        call.assert_called_once_with("finance", 5)
        self.assertEqual(self.client.get("/api/v1/shared/baidu-board?limit=x").status_code, 400)
        with patch("app.services.baidu_board.fetch_board", side_effect=ValueError("不认识的榜单")):
            self.assertEqual(self.client.get("/api/v1/shared/baidu-board?board=zz").status_code, 400)
        with patch("app.services.baidu_board.fetch_board", side_effect=board.BoardError("限流")):
            self.assertEqual(self.client.get("/api/v1/shared/baidu-board").status_code, 502)


if __name__ == "__main__":
    unittest.main()
