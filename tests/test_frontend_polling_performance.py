from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_SCAN_FILES = [
    ROOT / "index.html",
    ROOT / "frontend" / "public" / "app" / "js" / "app-1.js",
]
FRONTEND_BOOT_FILES = [
    ROOT / "index.html",
    ROOT / "frontend" / "public" / "app" / "js" / "app-1.js",
]
FRONTEND_AUTH_FILES = [
    ROOT / "index.html",
    ROOT / "frontend" / "index.html",
    ROOT / "frontend" / "public" / "app" / "js" / "app-1.js",
    ROOT / "frontend" / "public" / "app" / "js" / "app-2.js",
]
FRONTEND_STYLE_FILES = [
    ROOT / "index.html",
    ROOT / "frontend" / "index.html",
    ROOT / "frontend" / "public" / "app" / "css" / "app-1.css",
    ROOT / "frontend" / "public" / "app" / "css" / "theme-candy.css",
]


def test_frontends_use_async_strategy_scan_progress_endpoint():
    for path in STRATEGY_SCAN_FILES:
        text = path.read_text(encoding="utf-8")
        assert "/strategies/run_async" in text, path
        assert "api('/strategies/run','POST'" not in text, path
        assert "await api('/status')" not in text[text.find("async function runScreen") : text.find("function _renderScResults")], path


def test_frontends_guard_background_polling_and_overlapping_requests():
    for path in STRATEGY_SCAN_FILES:
        text = path.read_text(encoding="utf-8")
        assert "_pollInflight" in text, path
        assert "document.hidden" in text, path
        assert "pollEvery(fetchSt, 5000)" in text, path
        assert "pollEvery(fetchPositions, 8000)" in text, path
        assert "pollEvery(fetchFunds, 15000)" in text, path


def test_status_polling_skips_unchanged_heavy_renders():
    for path in STRATEGY_SCAN_FILES:
        text = path.read_text(encoding="utf-8")
        assert "_statusRenderSig" in text, path
        assert "_statusPayloadSig" in text, path
        assert "queueChanged" in text, path
        assert "poolSig!==_statusRenderSig.pool || queueChanged" in text, path


def test_public_shell_does_not_block_on_private_account_requests():
    private_boot = (
        "loadStratList(); loadDbList(); startPoll(); initAI(); "
        "initBrokerConfig(); initNotifyConfig(); loadMarketContext(); loadTradeMode()"
    )
    for path in FRONTEND_AUTH_FILES:
        text = path.read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in text, path
        assert private_boot not in text, path

    for path in FRONTEND_STYLE_FILES:
        text = path.read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in text, path
        assert "font-family:'Noto Sans SC'" not in text, path

    for path in FRONTEND_BOOT_FILES:
        text = path.read_text(encoding="utf-8")
        assert "function initAuthenticatedShell()" in text, path
        assert "if(!_currentUser) return" in text, path
        assert "loadStratList(); loadDbList(); loadMarketContext()" in text, path
