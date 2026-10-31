"""财报多 Agent。具体角色由 configs/financial.json 描述。"""

from app.agents.financial.graph import analyze_financials, debate_financials

__all__ = ["analyze_financials", "debate_financials"]
