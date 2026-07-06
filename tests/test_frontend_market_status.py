from pathlib import Path


def test_daily_review_frontend_shows_market_data_quality():
    html = Path("index.html").read_text(encoding="utf-8")

    assert "_rvDataQuality" in html
    assert "_rvDataQuality(b, '涨跌结构')" in html
    assert "_rvDataQuality(tv, '成交额')" in html
    assert "stat.degraded" in html
    assert "stat.coverage" in html
    assert "stat.source" in html
