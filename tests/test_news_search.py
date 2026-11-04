from app.services.news import search_query
from app.tools.stock import Stock


def test_search_query_is_the_users_words():
    stock = Stock(ts_code="601689.SH", code="601689", name="拓普集团", quote_id="", em_code="1.601689")
    assert search_query(stock, "最新的财经资讯") == "最新的财经资讯"
    assert search_query(stock, "  ") == ""
