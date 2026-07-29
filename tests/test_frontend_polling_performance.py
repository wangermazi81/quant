from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# 工单7 拆分后：JS 逻辑在 static/app/js/，index.html 只剩骨架；
# /v2 双轨(frontend/)已于工单7c 移除。
APP_JS = ROOT / "static" / "app" / "js"
APP_CSS = ROOT / "static" / "app" / "css"
STRATEGY_SCAN_FILES = [
    APP_JS / "app-core.js",
]
FRONTEND_BOOT_FILES = [
    APP_JS / "app-core.js",
]
AI_TRADER_FILES = [
    APP_JS / "app-ai-trader.js",
]
FRONTEND_AUTH_FILES = [
    ROOT / "index.html",
    APP_JS / "app-core.js",
    APP_JS / "app-ai-gen.js",
]
FRONTEND_STYLE_FILES = [
    ROOT / "index.html",
    APP_CSS / "app-base.css",
    ROOT / "static" / "lib" / "theme-candy.css",
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
        assert "pollEvery(fetchSt, 10000)" in text, path
        assert "pollEvery(fetchPositions, 8000, _isCorePollingPage)" in text, path
        assert "pollEvery(fetchFunds, 15000, _isCorePollingPage)" in text, path
        assert "pollEvery(refreshBridgeProcessBadge, 15000)" in text, path
        assert "_pollInflight.bridge" in text, path
        assert "_bridgeStatusPromise" in text, path
        assert "_bridgeProcessTimer" not in text, path
        visibility_block = text[text.find("document.addEventListener('visibilitychange'"):][:220]
        assert "if(_isCorePollingPage())" in visibility_block, path


def test_market_context_polling_uses_cache_and_only_runs_on_relevant_pages():
    for path in STRATEGY_SCAN_FILES:
        text = path.read_text(encoding="utf-8")
        assert "_mktInflight" in text, path
        assert "_isMarketContextPage()" in text, path
        assert "loadMarketContext(false)" in text, path
        assert "setInterval(()=>loadMarketContext(true), 60000)" not in text, path
        assert "_mktFollowupTimer" in text, path


def test_ai_account_polling_stops_outside_ai_page_and_deduplicates_requests():
    for path in AI_TRADER_FILES:
        text = path.read_text(encoding="utf-8")
        assert "aitS.accountLoading" in text, path
        assert "document.hidden" in text, path
        assert "page-aitrader" in text, path
        assert "function aitSetPageActive(active)" in text, path
        assert "aitS.es.close()" in text, path
        assert "setInterval(aitLoadAccount, 15000)" not in text, path


def test_performance_scripts_use_current_cache_version():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "/static/app/js/app-core.js?v=20260729-strategy-scan" in html
    assert "/static/app/js/app-ai-trader.js?v=20260729-cloud-refresh" in html


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
        # 防的是外网字体拉取阻塞首屏；本地字体栈里的 'Noto Sans SC' fallback
        # 声明（theme-candy.css 线上一直有）不触发网络请求，不在禁止之列。
        assert "fonts.googleapis.com" not in text, path

    for path in FRONTEND_BOOT_FILES:
        text = path.read_text(encoding="utf-8")
        assert "function initAuthenticatedShell()" in text, path
        assert "if(!_currentUser) return" in text, path
        assert "loadStratList(); loadDbList(); loadMarketContext()" in text, path


def test_strategy_activation_updates_monitor_status_immediately():
    for path in STRATEGY_SCAN_FILES:
        text = path.read_text(encoding="utf-8")
        start = text.find("async function activateStrat()")
        end = text.find("async function runScreen", start)
        assert start != -1 and end != -1, path
        block = text[start:end]
        assert "const r = await api('/strategies/activate','POST',{filename:curStrat})" in block, path
        assert "if(!r?.ok)" in block, path
        assert "_markMonitorRunning" in block, path
        assert "await fetchSt(true)" in block, path

    for path in STRATEGY_SCAN_FILES:
        text = path.read_text(encoding="utf-8")
        start = text.find("async function fetchSt")
        assert start != -1, path
        head = text[start:text.find("{", start) + 120]
        assert "force=false" in head, path
        assert "_pollInflight.status && !force" in text[start:start + 250], path


def test_vibe_activation_updates_monitor_status_and_status_poll_recognizes_vibe():
    for path in STRATEGY_SCAN_FILES:
        text = path.read_text(encoding="utf-8")
        start = text.find("async function confirmVibeActivate")
        end = text.find("async function renderVibeActivationCard", start)
        assert start != -1 and end != -1, path
        block = text[start:end]
        assert "_markMonitorRunning('Alpha监控中')" in block, path
        assert "await fetchSt(true)" in block, path

        start = text.find("async function fetchSt")
        end = text.find("function _markMonitorRunning", start)
        assert start != -1 and end != -1, path
        status_block = text[start:end]
        assert "const monitoring = !!(d.running || d.vibe_active)" in status_block, path
        assert "d.vibe_active?'Alpha监控中':'监控中'" in status_block, path
