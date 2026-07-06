from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_FILES = [
    ROOT / "index.html",
    ROOT / "frontend" / "public" / "app" / "js" / "app-1.js",
]


def test_frontends_use_async_strategy_scan_progress_endpoint():
    for path in FRONTEND_FILES:
        text = path.read_text(encoding="utf-8")
        assert "/strategies/run_async" in text, path
        assert "api('/strategies/run','POST'" not in text, path
        assert "await api('/status')" not in text[text.find("async function runScreen") : text.find("function _renderScResults")], path


def test_frontends_guard_background_polling_and_overlapping_requests():
    for path in FRONTEND_FILES:
        text = path.read_text(encoding="utf-8")
        assert "_pollInflight" in text, path
        assert "document.hidden" in text, path
        assert "pollEvery(fetchSt, 5000)" in text, path
        assert "pollEvery(fetchPositions, 8000)" in text, path
        assert "pollEvery(fetchFunds, 15000)" in text, path
