"""把旧的个人数据（持仓、想买、理由、自选、提醒）从 Python 的库里导出成一份 JSON，交给本机 Go 服务导入。

用法：
    python scripts/export_personal.py > personal-export.json
    curl -X POST http://127.0.0.1:8083/api/v1/personal/import -H 'Content-Type: application/json' --data-binary @personal-export.json

只读 data/holdings.db 和 data/synthesis.db，不改它们。Go 服务只在本机还没有任何成交、想买、自选时才导入，不会重复。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def rows(path: Path, sql: str) -> list[dict]:
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def load(text, default):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def ms(value) -> int:
    return int(float(value or 0) * 1000)


def main() -> None:
    holdings = Path(os.environ.get("HOLDINGS_DB") or ROOT / "data" / "holdings.db")
    synthesis = Path(os.environ.get("SYNTHESIS_DB") or ROOT / "data" / "synthesis.db")
    out = {
        "trades": [
            {"id": r["id"], "ts_code": r["ts_code"], "name": r["name"], "side": r["side"], "price": r["price"], "qty": r["qty"], "date": r["trade_date"],
             "reason": r["reason"], "invalid": load(r["invalid"], []), "context": load(r["context"], {}), "note": r["note"],
             "wish_id": r["wish_id"], "created_at": ms(r["created_at"])}
            for r in rows(holdings, "SELECT * FROM trades ORDER BY id")
        ],
        "wishes": [
            {"id": r["id"], "ts_code": r["ts_code"], "name": r["name"], "source": r["source"], "reason": r["reason"], "conditions": load(r["conditions"], []),
             "match": r["match"], "plan_pct": r["plan_pct"], "invalid": load(r["invalid"], []), "status": r["status"], "created_at": ms(r["created_at"]),
             "closed_at": ms(r["closed_at"])}
            for r in rows(holdings, "SELECT * FROM wishes ORDER BY id")
        ],
        "reasons": [
            {"ts_code": r["ts_code"], "text": r["text"], "invalid": load(r["invalid"], []), "written_at": r["written_at"], "updated_at": ms(r["updated_at"])}
            for r in rows(holdings, "SELECT * FROM reasons")
        ],
        "rules": [{"id": r["id"], "ts_code": r["ts_code"], "kind": r["kind"], "value": r["value"]} for r in rows(holdings, "SELECT * FROM rules ORDER BY id")],
        "checkups": [{"id": r["id"], "created_at": ms(r["created_at"]), "data": load(r["data"], {})} for r in rows(holdings, "SELECT * FROM checkups ORDER BY id")],
        "watch": [
            {"ts_code": r["ts_code"], "name": r["name"], "added_at": ms(r["added_at"]), "sort_order": r.get("sort_order") or ms(r["added_at"])}
            for r in rows(synthesis, "SELECT * FROM watchlist ORDER BY sort_order, added_at")
        ],
        "alerts": [
            {"id": r["id"], "ts_code": r["ts_code"], "side": r["side"], "price": r["price"], "confirm": r["confirm"], "base_name": r["base_name"], "created_at": ms(r["created_at"])}
            for r in rows(synthesis, "SELECT * FROM alerts ORDER BY id")
        ],
    }
    json.dump(out, sys.stdout, ensure_ascii=False)
    print("导出：" + "，".join(f"{key} {len(value)}" for key, value in out.items()), file=sys.stderr)


if __name__ == "__main__":
    main()
