"""财报的硬阈值和判断标准。模型无权越过；改阈值只改这里，并升 THRESHOLD_VERSION。

可以按行业覆盖：OVERRIDES[行业规则名] 里写的键覆盖默认值（行业规则名见 industry_rule）。
"""

from __future__ import annotations

THRESHOLD_VERSION = "finance-th-1"

DEFAULTS = {
    # 增长
    "adj_qoq_abnormal_pp": 0.10,        # 校正环比偏离超过 ±10 个百分点才算异常加速或减速
    "accel_pp": 0.05,                    # 同比增速和上一季相差 5 个百分点以上才叫加速或减速
    "scissors_warn_pp": 0.20,            # 营收和利润增速差 20 个百分点以上算明显的剪刀差
    # 含金量
    "ded_to_np_support_min": 0.70,       # 扣非/归母低于它，第 4 题不能判偏正面
    "ded_to_np_good": 0.80,
    "nonrec_share_max": 0.30,            # 非经常损益占归母超过它，第 5 题不能判偏正面
    "fin_exp_shift_to_np": 0.30,         # 财务费用同比变动超过归母的这个比例，算"大幅变动"
    # 利润率
    "margin_pp_warn": 0.02,              # 毛利率或净利率同比下降 2 个百分点以上算恶化
    "margin_pp_good": 0.01,
    # 现金流
    "ocf_to_np_support_min": 0.50,       # 经营现金流/净利润低于它，第 7 题不能判偏正面
    "ocf_to_np_good": 0.80,
    "ocf_negative_run": 2,               # 单季经营现金流连续为负这么多期，必须判需警惕
    # 资产负债表
    "ar_gap_pp": 0.20,                   # 应收增速比营收增速高这么多，第 8 题不能判偏正面
    "inv_gap_pp": 0.20,                  # 存货增速比成本增速高这么多，同时命中就必须判需警惕
    "goodwill_to_equity": 0.30,
    # 时效（天）
    "fresh_days": 30,
    "stale_days": 60,
    # 预期
    "naive_pp": 0.10,                    # 实际比朴素预期高或低超过 10% 才算好于或差于预期
    "preview_wan": 10000,                # 预告金额的单位是万元
    # 市场反应
    "reaction_pp": 0.02,                 # 披露后超额收益超过 ±2 个百分点才算"有明显反应"
    "peer_min": 5,                       # 同行少于 5 家不做同行分位
    # 房地产：合同负债同比下降到这个幅度，第 8 题不能判偏正面；下降到更低必须判需警惕
    "contract_liab_drop": -0.20,
    "contract_liab_collapse": -0.40,
    # 持续亏损公司：剩余可用季数低于这个必须判需警惕，低于下一个不能判偏正面
    "runway_warn_q": 4,
    "runway_support_q": 8,
    "consolidation_share": 0.20,         # 新出现的产品线占营收增量的这个比例以上，标记疑似并表
    # 周期高位
    "gross_margin_cycle_high_pctile": 0.90,
}
OVERRIDES: dict[str, dict] = {
    # 房地产：合同负债是主要观察指标；存货是开发成本、应收口径也不同，第 8 题的应收、存货阈值不适用
    "realestate": {"ar_gap_pp": 99.0, "inv_gap_pp": 99.0},
}


def thresholds(rule: str = "general") -> dict:
    return {**DEFAULTS, **OVERRIDES.get(rule, {})}
