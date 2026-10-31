"""生产层和决策层之间的契约：维度快照、事实表。

决策层只认识 FactTable，不 import 任何 app.services。线上和回测都用同一种 FactTable。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# 维度状态：ok 可用；stale 过期；pending 还在整理；unavailable 取不到或不适用；invalid 体检不通过
STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_PENDING = "pending"
STATUS_UNAVAILABLE = "unavailable"
STATUS_INVALID = "invalid"


@dataclass
class Snapshot:
    key: str
    status: str = STATUS_UNAVAILABLE
    reason: str = ""
    lean: str = "unclear"            # bullish | bearish | neutral | unclear
    level: int = 0
    certainty: str | None = None     # high | medium | low
    as_of: str = ""
    rule_version: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.status == STATUS_OK

    def brief(self) -> dict:
        return {
            "key": self.key,
            "status": self.status,
            "reason": self.reason,
            "lean": self.lean,
            "level": self.level,
            "certainty": self.certainty,
            "as_of": self.as_of,
            "rule_version": self.rule_version,
        }


@dataclass
class Fact:
    id: str
    value: Any
    text: str

    def as_dict(self) -> dict:
        return {"id": self.id, "value": self.value, "text": self.text}


@dataclass
class FactTable:
    facts: list[Fact] = field(default_factory=list)
    unavailable: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)       # {id, dim, kind, text}
    candidates: list[dict] = field(default_factory=list)   # {id, dir, name, dist, sources, consequence}
    rule: dict = field(default_factory=dict)               # 规则版结论（只给主 Agent，标为"供参考"）
    context: dict = field(default_factory=dict)            # 完成数、分母、新鲜度、各维度方向

    def fact(self, fact_id: str) -> Fact | None:
        for item in self.facts:
            if item.id == fact_id:
                return item
        return None

    def ids(self) -> set[str]:
        return {item.id for item in self.facts}

    def dim_of(self, fact_id: str) -> str:
        return fact_id.split(".", 1)[0]

    def sides_view(self) -> dict:
        """正反方看到的内容：不含规则版结论，也不含价位候选。"""
        return {
            "facts": [{"id": item.id, "value": item.text} for item in self.facts],
            "unavailable": self.unavailable,
            "events": [{"id": item["id"], "text": item["text"]} for item in self.events],
        }

    def chief_view(self) -> dict:
        view = self.sides_view()
        view["candidates"] = [
            {key: item.get(key) for key in ("id", "dir", "name", "dist", "sources", "consequence", "verified")}
            for item in self.candidates
        ]
        view["rule_reference"] = self.rule_reference()
        return view

    def rule_reference(self) -> dict:
        from app.synthesis.policy import DIRECTION_TEXT, LEVEL_TEXT, POSITION_TEXT

        rule = self.rule or {}
        return {
            "direction": DIRECTION_TEXT.get(rule.get("direction"), "不明"),
            "strength": LEVEL_TEXT.get(rule.get("level") or 0, ""),
            "position": POSITION_TEXT.get(rule.get("position"), "适中"),
            "note": "供参考；可以不同意，但不同意时要在理由里说清楚",
        }

    def sides_digest(self) -> str:
        return digest(self.sides_view())

    def digest(self) -> str:
        """缓存键：事实表 + 价位候选 + 规则版结论。"""
        payload = self.chief_view()
        payload["rule_detail"] = {
            key: (self.rule or {}).get(key) for key in ("direction", "level", "position", "score")
        }
        return digest(payload)

    def as_dict(self) -> dict:
        return {
            "facts": [item.as_dict() for item in self.facts],
            "unavailable": self.unavailable,
            "events": self.events,
            "candidates": self.candidates,
            "rule": self.rule,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FactTable":
        return cls(
            facts=[Fact(item["id"], item.get("value"), item.get("text", "")) for item in data.get("facts") or []],
            unavailable=list(data.get("unavailable") or []),
            events=list(data.get("events") or []),
            candidates=list(data.get("candidates") or []),
            rule=dict(data.get("rule") or {}),
            context=dict(data.get("context") or {}),
        )


def digest(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
