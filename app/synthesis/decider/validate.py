"""校验：只拦"明显错"，不替 Agent 做判断。任一项不通过，这一次输出作废（论据则只剔除那一条）。

五类检查：格式、引用、数字、底线、文案。错误信息以"类别:"开头，监控按类别统计。
"""

from __future__ import annotations

import re

from app.synthesis import policy
from app.synthesis.registry import definition, numbers
from app.synthesis.types import FactTable

_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:\.\d+)?)")
_PRICE = re.compile(r"\d+(?:\.\d+)?\s*(?:元|块钱|块)|[¥￥]\s*\d")
_PERCENT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")


# ---------------------------------------------------------------- 文字检查
def known_ids(table: FactTable) -> set[str]:
    """可以被引用的编号：事实，以及事件候选（事件的文字也是代码给的）。"""
    return table.ids() | {item["id"] for item in table.events}


def allowed_numbers(table: FactTable, cites: list[str]) -> set[float]:
    allowed: set[float] = set()
    events = {item["id"]: item["text"] for item in table.events}
    for cite in cites:
        fact = table.fact(cite)
        if fact is not None:
            allowed |= numbers(cite, fact.value, fact.text)
        elif cite in events:
            allowed |= numbers(cite, None, events[cite])
    return allowed


def _matches(number: float, decimals: int, allowed: set[float]) -> bool:
    slack = 0.5 * 10 ** -decimals + 1e-9
    return any(abs(item - number) <= slack for item in allowed)


def bad_numbers(text: str, allowed: set[float]) -> list[str]:
    bad = []
    for token in _NUMBER.findall(text or ""):
        value = float(token)
        decimals = len(token.split(".")[1]) if "." in token else 0
        if value in policy.CONTEXT_NUMBERS or (value == int(value) and value <= policy.SMALL_COUNT_MAX):
            continue
        if not _matches(value, decimals, allowed):
            bad.append(token)
    return bad


def check_text(text: str, cites: list[str], table: FactTable, where: str, need_cites: bool = True) -> list[str]:
    errors = []
    if not str(text or "").strip():
        return [f"格式:{where}为空"]
    ids = known_ids(table)
    unknown = [item for item in cites if item not in ids]
    if unknown:
        errors.append(f"引用:{where}引用了不存在的事实 {unknown}")
    if need_cites and not cites:
        errors.append(f"引用:{where}没有引用")
    for word in policy.BANNED_WORDS:
        if word in text:
            errors.append(f"文案:{where}含禁用词「{word}」")
    if _PRICE.search(text):
        errors.append(f"数字:{where}出现了价格")
    wrong = bad_numbers(text, allowed_numbers(table, [item for item in cites if item in ids]))
    if wrong:
        errors.append(f"数字:{where}里的数字 {wrong} 在所引用的事实中找不到")
    return errors


def _cite_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


# ---------------------------------------------------------------- 正反方论据
def validate_side(raw: dict, table: FactTable, side: str) -> tuple[list[dict], list[str]]:
    points, errors = [], []
    rows = raw.get("points") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return [], ["格式:points 不是列表"]
    for index, row in enumerate(rows, start=1):
        if len(points) >= policy.SIDE_MAX_POINTS:
            break
        if not isinstance(row, dict):
            errors.append(f"格式:第 {index} 条不是对象")
            continue
        claim = str(row.get("claim") or "").strip()
        cites = _cite_list(row.get("cites"))
        try:
            weight = int(row.get("weight"))
        except (TypeError, ValueError):
            weight = 0
        problems = check_text(claim, cites, table, f"{side} 第 {index} 条")
        if weight not in (1, 2, 3):
            problems.append(f"格式:{side} 第 {index} 条分量 {row.get('weight')!r} 不在 1–3")
        if problems:
            errors.extend(problems)
            continue
        points.append({"ref": f"{side}.{len(points) + 1}", "claim": claim, "cites": cites, "weight": weight})
    return points, errors


# ---------------------------------------------------------------- 主 Agent
def _buffer(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        matched = _PERCENT.match(str(value or ""))
        if not matched:
            return None
        number = float(matched.group(1)) / 100
    for option in policy.DOWN_BUFFERS:
        if abs(option - number) < 1e-9:
            return option
    return None


def _verdicts(raw: dict, arguments: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    errors = []
    by_ref = {item["ref"]: item for item in arguments}
    seen: dict[str, dict] = {}
    for row in raw.get("verdicts") or []:
        if isinstance(row, dict):
            seen[str(row.get("ref") or "")] = row
    adopted, rejected = [], []
    for ref, argument in by_ref.items():
        row = seen.get(ref)
        if row is None:
            errors.append(f"格式:论据 {ref} 没有裁决")
            continue
        adopt = row.get("adopt") is True
        try:
            weight = int(row.get("weight", argument["weight"]))
        except (TypeError, ValueError):
            weight = -1
        reason = str(row.get("reason") or "").strip()
        if adopt:
            if weight < 1 or weight > argument["weight"]:
                errors.append(f"格式:论据 {ref} 最终分量 {row.get('weight')!r} 不能高于自评 {argument['weight']}")
                continue
            if weight < argument["weight"] and reason not in policy.ADOPT_REASONS:
                errors.append(f"格式:论据 {ref} 调低分量但没有给出原因代码")
                continue
            adopted.append({**argument, "weight": weight, "side": ref.split(".")[0], "reason": reason})
        else:
            if reason not in policy.ADOPT_REASONS:
                errors.append(f"格式:论据 {ref} 不采信但没有给出原因代码")
                continue
            rejected.append({"ref": ref, "reason": reason, "side": ref.split(".")[0]})
    extra = [ref for ref in seen if ref not in by_ref]
    if extra:
        errors.append(f"格式:裁决了不存在的论据 {extra}")
    return adopted, rejected, errors


def _levels(raw_levels, table: FactTable, direction: str) -> tuple[dict, list[str]]:
    errors: list[str] = []
    out: dict = {"down": None, "up": None}
    if not isinstance(raw_levels, dict):
        return out, ["格式:levels 不是对象"]
    pool = {item["id"]: item for item in table.candidates}
    for side in ("down", "up"):
        row = raw_levels.get(side)
        if row is None or not any(item["dir"] == side for item in table.candidates):
            continue   # 这个方向没有候选可选：模型写了什么都忽略，用参考表
        if not isinstance(row, dict):
            errors.append(f"格式:levels.{side} 不是对象")
            continue
        base = str(row.get("base") or "")
        confirm = str(row.get("confirm") or "")
        cites = _cite_list(row.get("cites"))
        why = str(row.get("why") or "")
        if base not in pool or pool[base]["dir"] != side:
            errors.append(f"引用:levels.{side} 的基准 {base!r} 不在候选里")
            continue
        errors += check_text(why, cites, table, f"levels.{side}.why")
        if side == "down":
            buffer = _buffer(row.get("buffer", 0))
            if buffer is None:
                errors.append(f"格式:levels.down 缓冲 {row.get('buffer')!r} 不在菜单里")
                continue
            if confirm not in policy.DOWN_CONFIRMS:
                errors.append(f"格式:levels.down 确认条件 {confirm!r} 不在菜单里")
                continue
            if direction == "bearish" and buffer != 0:
                errors.append("底线:综合偏空时下方缓冲只能选 0")
                continue
            out["down"] = {"base": base, "buffer": buffer, "confirm": confirm, "why": why, "cites": cites}
        else:
            meaning = str(row.get("meaning") or "")
            if confirm not in policy.UP_CONFIRMS or meaning not in policy.UP_MEANINGS:
                errors.append(f"格式:levels.up 的确认条件 {confirm!r} 或含义 {meaning!r} 不在菜单里")
                continue
            if meaning == "priced_in" and not {"news.priced_in"} & set(cites):
                errors.append("底线:含义选「利好基本兑现」时，引用里必须有「已反应」相关的事实")
                continue
            out["up"] = {"base": base, "confirm": confirm, "meaning": meaning, "why": why, "cites": cites}
    return out, errors


def _dims(cites: list[str]) -> set[str]:
    dims = set()
    for item in cites:
        defn = definition(item)
        if defn is not None and defn.dim != "market":
            dims.add(defn.dim)
    return dims


def validate_chief(raw: dict, table: FactTable, arguments: list[dict]) -> tuple[dict | None, list[str]]:
    if not isinstance(raw, dict):
        return None, ["格式:输出不是对象"]
    errors: list[str] = []
    direction = policy.TEXT_DIRECTION.get(str(raw.get("direction") or "").strip())
    position = policy.TEXT_POSITION.get(str(raw.get("position") or "").strip())
    if direction is None:
        errors.append(f"格式:direction {raw.get('direction')!r} 不在「偏多/偏空/不明」里")
    if position is None:
        errors.append(f"格式:position {raw.get('position')!r} 不在「偏低/适中/偏高」里")
    headline = str(raw.get("headline") or "").strip()
    if len(headline) > policy.HEADLINE_MAX:
        errors.append(f"格式:标题超过 {policy.HEADLINE_MAX} 字")

    adopted, rejected, verdict_errors = _verdicts(raw, arguments)
    errors += verdict_errors
    spread = sum(item["weight"] * (1 if item["side"] == "bull" else -1) for item in adopted)

    if direction == "bullish" and spread <= 0 or direction == "bearish" and spread >= 0:
        errors.append(f"底线:方向「{policy.DIRECTION_TEXT[direction]}」和采信后的分差 {spread:+d} 不一致")
    elif direction in ("bullish", "bearish") and abs(spread) < policy.SPREAD_STEPS[0][0]:
        errors.append(f"底线:分差 {spread:+d} 不足以支持「{policy.DIRECTION_TEXT[direction]}」")

    direction_cites = _cite_list(raw.get("direction_cites"))
    position_cites = _cite_list(raw.get("position_cites"))
    direction_text = str(raw.get("direction_text") or "").strip()
    position_text = str(raw.get("position_text") or "").strip()
    errors += check_text(direction_text, direction_cites, table, "direction_text")
    errors += check_text(position_text, position_cites, table, "position_text")
    errors += check_text(headline, direction_cites, table, "标题", need_cites=False)

    note = raw.get("conflict_note") if isinstance(raw.get("conflict_note"), dict) else {}
    note_text = str(note.get("text") or "").strip()
    note_cites = _cite_list(note.get("cites"))
    if note_text:
        errors += check_text(note_text, note_cites, table, "conflict_note")
    post = raw.get("postmortem") if isinstance(raw.get("postmortem"), dict) else {}
    post_text = str(post.get("text") or "").strip()
    post_cites = _cite_list(post.get("cites"))
    errors += check_text(post_text, post_cites, table, "事后复盘")

    levels, level_errors = _levels(raw.get("levels") or {}, table, direction or "unclear")
    errors += level_errors

    watch = _cite_list(raw.get("watch_ids"))
    event_ids = {item["id"] for item in table.events}
    if any(item not in event_ids for item in watch):
        errors.append(f"引用:watch_ids {watch} 不全在事件候选里")
    if len(watch) > 2:
        errors.append("格式:watch_ids 最多 2 个")

    errors += _floors(direction, headline, adopted, table)
    if errors:
        return None, errors
    return {
        "direction": direction,
        "position": position,
        "headline": headline,
        "direction_text": direction_text,
        "direction_cites": direction_cites,
        "position_text": position_text,
        "position_cites": position_cites,
        "adopted": adopted,
        "rejected": rejected,
        "spread": spread,
        "conflict_note": {"text": note_text, "cites": note_cites} if note_text else None,
        "postmortem": {"text": post_text, "cites": post_cites},
        "levels": levels,
        "watch_ids": watch,
    }, []


def _floors(direction: str | None, headline: str, adopted: list[dict], table: FactTable) -> list[str]:
    """底线与文案的一致性。"""
    errors = []
    red = table.fact("fin.red_line")
    if direction == "bullish" and red is not None and red.value:
        errors.append("底线:命中财报红线时不能偏多")
    majority = (table.context or {}).get("majority")
    if direction in ("bullish", "bearish") and majority in ("bullish", "bearish") and direction != majority:
        cited = set()
        for item in adopted:
            cited |= _dims(item["cites"])
        lean = (table.context or {}).get("dim_lean") or {}
        supporting = [dim for dim in cited if lean.get(dim) == direction]
        if len(cited) < 2 or len(supporting) < 2:
            errors.append("底线:方向和多数维度相反时，理由必须引用至少 2 个本身支持新结论的维度")
    if direction == "bearish" and any(word in headline for word in policy.BULLISH_HEADLINE_WORDS):
        errors.append("文案:偏空时标题不能写成看多的意思")
    if direction == "bullish" and any(word in headline for word in policy.BEARISH_HEADLINE_WORDS):
        errors.append("文案:偏多时标题不能写成看空的意思")
    return errors
