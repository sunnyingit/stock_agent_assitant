"""新闻多 Agent。具体角色由 configs/news.json 描述。"""

from app.agents.news.graph import analyze_news, debate_news

__all__ = ["analyze_news", "debate_news"]
