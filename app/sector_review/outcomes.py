"""板块归属的回测记录：每次生成解读时记下"属于"的主、次板块和驱动方式，按数据日去重。

先回答归属本身稳不稳：同一只股票相邻两次记录里，主板块换了几次、次板块换了几次。换得勤，说明阈值贴得太近，该调。
（归属对后续走势有没有解释力，等记录积累够了再回填，这里还没做。）
"""

from __future__ import annotations

from app.services.sector_detail.affiliation import VERSION


def record(store, ts_code: str, payload: dict) -> bool:
    """记一条归属。没有 store、或这次没有归属结论（没取到数据）时不记。"""
    if store is None:
        return False
    sectors = payload.get("sectors") or {}
    as_of = (payload.get("meta") or {}).get("as_of")
    if not as_of:
        return False
    belongs = [c["code"] for c in sectors.get("candidates") or [] if c.get("belongs")]
    return store.save_sector_affiliation(
        ts_code, as_of, VERSION, sectors.get("primary"), sectors.get("secondary"), (sectors.get("driver") or {}).get("type") or "", belongs,
    )


def stability(rows: list[dict]) -> dict:
    """rows 是 store.sector_affiliations() 的结果。按股票分组，数相邻记录里主、次板块变了几次。"""
    by_stock: dict[str, list[dict]] = {}
    for row in rows:
        by_stock.setdefault(row["ts_code"], []).append(row)
    pairs = primary_changes = secondary_changes = 0
    for series in by_stock.values():
        for before, after in zip(series, series[1:]):
            if before["version"] != after["version"]:
                continue
            pairs += 1
            primary_changes += before["primary_code"] != after["primary_code"]
            secondary_changes += before["secondary_code"] != after["secondary_code"]
    return {
        "stocks": len(by_stock), "pairs": pairs, "primary_changes": primary_changes, "secondary_changes": secondary_changes,
        "primary_change_rate": None if not pairs else round(primary_changes / pairs, 4),
        "secondary_change_rate": None if not pairs else round(secondary_changes / pairs, 4),
    }
