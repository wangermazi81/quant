from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _index_html() -> str:
    return (ROOT / "index.html").read_text(encoding="utf-8")


def test_lhb_frontend_uses_existing_kline_modal_and_sector_badges():
    html = _index_html()
    render_list = html.split("function renderLhbList", 1)[1].split("// ── 复盘数据", 1)[0]

    assert "loadKline(" not in render_list
    assert "showKLine(" in render_list
    assert "_lhbSectorBadges(s, false)" in render_list
    assert "toggleLhbSeats(" in render_list


def test_lhb_frontend_renders_expandable_full_seat_rows():
    html = _index_html()
    render_list = html.split("function renderLhbList", 1)[1].split("// ── 复盘数据", 1)[0]

    assert "lhb-seat-list" in render_list
    assert "lhb-seat-extra" in render_list
    assert "展开全部席位" in render_list
    assert "lhb-seat-name" in render_list
    assert "lhb-seat-amt" in render_list


def test_lhb_frontend_shows_sector_pending_badge_when_source_is_empty():
    html = _index_html()
    helper = html.split("function _lhbSectorBadges", 1)[1].split("function _wireLhbGraphHover", 1)[0]

    assert "行业待更新" in helper
    assert "lhb-sector-badge pending" in html
