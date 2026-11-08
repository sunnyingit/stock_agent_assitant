"""百度热搜榜单，默认取财经榜。

榜单页把整张榜放在 <!--s-data:{...}--> 里，按里面的字段名取，比按页面样式名抓稳得多；
页面取不到时退到手机版接口。百度对连续请求会限流，所以结果缓存一会儿，
取不到时返回上一次成功的结果并标明「stale」，不让调用方拿到一张空榜。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime

from app import config
from app.tools import cache
from app.tools.http import get_session

BOARD_URL = "https://top.baidu.com/board"
API_URL = "https://top.baidu.com/api/board"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://top.baidu.com/",
}
BOARDS = {
    "finance": "财经榜",
    "realtime": "实时热搜榜",
    "civil": "民生榜",
    "car": "汽车榜",
    "game": "游戏榜",
    "movie": "电影榜",
    "teleplay": "电视剧榜",
    "novel": "小说榜",
}
TAGS = {"1": "新", "3": "热"}
FRESH_TTL = 120          # 秒：这段时间内直接用缓存，不再问百度
STALE_TTL = 6 * 3600     # 秒：百度取不到时，最多还能给这么久以前的结果
_S_DATA = re.compile(r"<!--s-data:(.*?)-->", re.S)


class BoardError(RuntimeError):
    """榜单取不到，或者页面结构变了。"""


def _hot(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _hot_text(score: int | None) -> str:
    if score is None:
        return ""
    return f"{score / 10000:.1f}万" if score >= 10000 else str(score)


def _item(rank: int, title: str, url: str, score, tag: str, desc: str, top: bool) -> dict:
    hot = _hot(score)
    return {"rank": rank, "title": title, "hot": hot, "hot_text": _hot_text(hot), "tag": tag, "desc": desc, "url": url, "pinned": top}


def parse_page(html: str) -> tuple[list[dict], str]:
    """从榜单页取出条目和榜单的更新时间。"""
    found = _S_DATA.search(html or "")
    if not found:
        raise BoardError("榜单页里没有数据（可能被限流，或页面改版）")
    try:
        cards = json.loads(found.group(1))["data"]["cards"]
        card = next(c for c in cards if c.get("component") == "hotList" or c.get("content"))
        rows = card["content"]
    except (ValueError, KeyError, StopIteration, TypeError) as exc:
        raise BoardError(f"榜单页的数据结构变了：{exc}") from exc
    items = []
    for row in rows:
        title = str(row.get("word") or row.get("query") or "").strip()
        if not title:
            continue
        url = str(row.get("rawUrl") or row.get("url") or row.get("appUrl") or "")
        items.append(_item(len(items) + 1, title, url, row.get("hotScore"), TAGS.get(str(row.get("hotTag")), ""), str(row.get("desc") or "").strip(), bool(row.get("isTop"))))
    return items, str(card.get("updateTime") or "")


def parse_api(payload: dict) -> list[dict]:
    """手机版接口：没有热度数值，只有名次、标题、链接和「新」标。"""
    try:
        cards = payload["data"]["cards"]
        rows = [row for card in cards for block in card.get("content", []) for row in (block.get("content") if isinstance(block, dict) and "content" in block else [block])]
    except (KeyError, TypeError, AttributeError) as exc:
        raise BoardError(f"榜单接口的数据结构变了：{exc}") from exc
    items = []
    for row in rows:
        title = str(row.get("word") or row.get("query") or "").strip()
        if not title:
            continue
        items.append(_item(len(items) + 1, title, str(row.get("url") or ""), None, str(row.get("newHotName") or TAGS.get(str(row.get("hotTag")), "")), str(row.get("desc") or "").strip(), bool(row.get("isTop"))))
    return items


def _get(url: str, params: dict) -> object:
    response = get_session().get(url, params=params, headers=HEADERS, timeout=config.HTTP_TIMEOUT)
    response.raise_for_status()
    return response


def _download(board: str) -> tuple[list[dict], str]:
    try:
        items, updated = parse_page(_get(BOARD_URL, {"tab": board}).text)
        if items:
            return items, updated
    except (BoardError, OSError, ValueError) as page_error:
        last = page_error
    else:
        last = BoardError("榜单页是空的")
    try:
        items = parse_api(_get(API_URL, {"platform": "wise", "tab": board}).json())
    except (BoardError, OSError, ValueError) as api_error:
        raise BoardError(f"{last}；手机版接口也失败：{api_error}") from api_error
    if not items:
        raise BoardError(f"{last}；手机版接口也是空的")
    return items, ""


def _updated_text(raw: str) -> str:
    try:
        return datetime.fromtimestamp(int(raw)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def fetch_board(board: str = "finance", limit: int = 30) -> dict:
    """取一张榜。board 是 BOARDS 里的名字；limit 1~50。"""
    board = (board or "finance").strip().lower()
    if board not in BOARDS:
        raise ValueError(f"不认识的榜单：{board}，可选 {'、'.join(BOARDS)}")
    limit = max(1, min(int(limit), 50))
    key = f"baidu:board:{board}"
    fresh = cache.get(key)
    stale = False
    if fresh is None:
        try:
            items, updated = _download(board)
            fresh = {"items": items, "updated_at": _updated_text(updated), "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            cache.set(key, fresh, FRESH_TTL)
            cache.set(key + ":last", fresh, STALE_TTL)
        except BoardError as exc:
            last = cache.get(key + ":last")
            if last is None:
                raise
            fresh, stale = last, str(exc)
    return {
        "board": board, "board_name": BOARDS[board], "source": "百度热搜", "updated_at": fresh["updated_at"], "fetched_at": fresh["fetched_at"],
        "stale": bool(stale), "stale_reason": stale or "", "count": min(limit, len(fresh["items"])), "items": fresh["items"][:limit],
    }
