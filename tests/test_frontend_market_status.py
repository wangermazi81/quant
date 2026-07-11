from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_daily_review_frontend_shows_market_data_quality():
    # 工单7 拆分后：复盘页 JS 在 static/app/js/app-research.js
    html = (ROOT / "static" / "app" / "js" / "app-research.js").read_text(encoding="utf-8")

    assert "_rvDataQuality" in html
    assert "_rvDataQuality(b, '涨跌结构')" in html
    assert "_rvDataQuality(tv, '成交额')" in html
    assert "stat.degraded" in html
    assert "stat.coverage" in html
    assert "stat.source" in html
