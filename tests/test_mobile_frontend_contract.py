from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
CORE_JS = ROOT / "static" / "app" / "js" / "app-core.js"
SHELL_CSS = ROOT / "static" / "app" / "css" / "app-mobile-shell.css"
PAGES_CSS = ROOT / "static" / "app" / "css" / "app-mobile-pages.css"
DIALOGS_CSS = ROOT / "static" / "app" / "css" / "app-mobile-dialogs.css"
LEGACY_MOBILE_CSS = ROOT / "static" / "app" / "css" / "app-mobile.css"
BROWSER_AUDIT = ROOT / "tests" / "browser" / "mobile_layout_audit.mjs"
BASE_CSS = ROOT / "static" / "app" / "css" / "app-base.css"
APP_PAGES_CSS = ROOT / "static" / "app" / "css" / "app-pages.css"
RESEARCH_JS = ROOT / "static" / "app" / "js" / "app-research.js"
SEARCH_JS = ROOT / "static" / "app" / "js" / "app-search.js"
RESEARCH_AUDIT = ROOT / "tests" / "browser" / "research_mobile_layout_audit.mjs"
FONT_AWESOME = ROOT / "static" / "lib" / "fontawesome" / "css" / "all.min.css"


def test_mobile_shell_is_independent_from_desktop_controls():
    html = INDEX.read_text(encoding="utf-8")
    core = CORE_JS.read_text(encoding="utf-8")

    for element_id in (
        "mobileHeader",
        "mobilePageTitle",
        "mobileModeState",
        "mobileRunState",
        "mobileRunTrigger",
        "mobileRunSheet",
        "mobileDock",
        "mobileMoreSheet",
    ):
        assert f'id="{element_id}"' in html

    for function_name in (
        "setMobileLayoutMode",
        "toggleMobileRunSheet",
        "renderRunState",
        "syncMobileHeader",
        "mobileNavigate",
        "toggleMobileMore",
        "syncMobileNavigation",
    ):
        assert f"function {function_name}" in core

    assert 'data-ui-click="mStart()"' in html
    assert 'data-ui-click="mStop()"' in html
    assert "data-ui-click=\"mobileNavigate('execution')\"" in html


def test_mobile_styles_are_split_and_load_after_desktop_shell():
    html = INDEX.read_text(encoding="utf-8")

    for css_file in (SHELL_CSS, PAGES_CSS, DIALOGS_CSS):
        assert css_file.exists()
        assert css_file.name in html
        assert html.index(css_file.name) > html.index("app-shell.css")

    assert "app-mobile.css" not in html
    assert "app-core.js?v=20260729-stock-name" in html
    assert "app-ai-gen.js?v=20260728-mobile-rebuild" in html
    assert not LEGACY_MOBILE_CSS.exists()


def test_mobile_shell_owns_the_1024px_boundary():
    css = SHELL_CSS.read_text(encoding="utf-8")

    assert "@media (max-width:1024px)" in css
    assert "body.mobile-layout .nav" in css
    assert "body.mobile-layout .side" in css
    assert "body.mobile-layout .mobile-header" in css
    assert "body.mobile-layout .mobile-run-sheet" in css
    assert "env(safe-area-inset-top)" in css
    assert "env(safe-area-inset-bottom)" in css


def test_mobile_only_shell_parts_are_hidden_by_default_on_desktop():
    css = SHELL_CSS.read_text(encoding="utf-8")
    desktop_defaults = css.split("@media (max-width:1024px)", 1)[0]

    for selector in (
        ".mobile-header",
        ".mobile-run-sheet",
        ".mobile-dock",
        ".mobile-more-sheet",
        ".strategy-mobile-tabs",
    ):
        assert selector in desktop_defaults

    assert "display:none" in desktop_defaults


def test_mobile_page_styles_cover_core_layouts():
    css = PAGES_CSS.read_text(encoding="utf-8")

    for selector in (
        ".page.active",
        ".kpi-bar",
        ".paper-top",
        ".paper-body",
        ".exec-overview-grid",
        ".exec-dual",
        ".ait-grid",
        ".risk-grid",
        ".risk-fields",
        ".CodeMirror",
        "#page-review",
        "#page-search",
    ):
        assert selector in css

    assert "max-width:100%" in css
    assert "min-width:0" in css


def test_mobile_touch_targets_cover_compact_controls():
    shell_css = SHELL_CSS.read_text(encoding="utf-8")
    pages_css = PAGES_CSS.read_text(encoding="utf-8")

    assert "label:has(input[type=\"checkbox\"])" in shell_css
    assert "label:has(input[type=\"radio\"])" in shell_css
    assert "#masFold" in pages_css
    assert "button.btn-xs" in pages_css
    assert "min-width:44px" in pages_css


def test_mobile_dialogs_are_viewport_and_keyboard_safe():
    css = DIALOGS_CSS.read_text(encoding="utf-8")
    core = CORE_JS.read_text(encoding="utf-8")

    assert "100dvh" in css
    assert "calc(100vw - 16px)" in css
    assert "visualViewport" in core
    assert "scrollIntoView" in core


def test_mobile_markup_keeps_delegated_event_contract():
    html = INDEX.read_text(encoding="utf-8")

    assert "viewport-fit=cover" in html
    assert " onclick=" not in html
    assert " onchange=" not in html
    assert " onkeydown=" not in html


def test_mobile_fixed_layers_do_not_compete_for_screen_space():
    shell_css = SHELL_CSS.read_text(encoding="utf-8")
    html = INDEX.read_text(encoding="utf-8")

    assert "body.mobile-layout .fb-fab" in shell_css
    assert "body.mobile-layout #beianFooter" in shell_css
    assert "display:none" in shell_css
    assert 'class="mobile-legal"' in html
    assert "2026011468" in html
    assert "36102102000155" in html


def test_shared_pages_allow_accessible_control_text_to_reflow():
    base_css = BASE_CSS.read_text(encoding="utf-8")
    pages_css = APP_PAGES_CSS.read_text(encoding="utf-8")

    assert ".exec-action-grid .btn" in base_css
    assert "white-space:normal" in base_css
    assert "#rv-news-q" in pages_css
    assert "min-width:0" in pages_css
    assert ".review-news-filter" in pages_css
    assert "flex-wrap:wrap" in pages_css


def test_browser_audit_waits_for_sheets_and_ignores_editor_internals():
    audit = BROWSER_AUDIT.read_text(encoding="utf-8")

    assert "waitForTimeout(320)" in audit
    assert "closest('.CodeMirror')" in audit
    assert "effectiveTouchRect" in audit
    assert "isExpectedAuthConsole" in audit


def test_new_mobile_assets_do_not_contain_replacement_question_runs():
    mobile_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (INDEX, CORE_JS, SHELL_CSS, PAGES_CSS, DIALOGS_CSS)
    )
    assert "???" not in mobile_text
    assert " ? ??" not in mobile_text
    assert "\u95f8\u5f00" in mobile_text
    assert "\u95f8\u5173" in mobile_text


def test_research_mobile_master_detail_and_report_contracts():
    html = INDEX.read_text(encoding="utf-8")
    research_js = RESEARCH_JS.read_text(encoding="utf-8")
    search_js = SEARCH_JS.read_text(encoding="utf-8")
    pages_css = PAGES_CSS.read_text(encoding="utf-8")
    audit = RESEARCH_AUDIT.read_text(encoding="utf-8")

    for token in (
        "research-chain-list-pane",
        "research-chain-detail-pane",
        "research-search-results-pane",
        "research-search-detail-pane",
        "chainMobileBack",
        "searchMobileBack",
        'data-mobile-pane="list"',
        'data-mobile-pane="results"',
        "research-report-query",
        "research-report-latest",
    ):
        assert token in html

    for token in (
        "setChainMobilePane",
        "setChainMobileSection",
        "research-chain-section-tabs",
        "data-chain-section-target",
        "research-report-link",
        "research-report-meta",
    ):
        assert token in research_js

    assert "setSearchMobilePane" in search_js
    assert 'data-mobile-section="graph"' in pages_css
    assert 'data-mobile-section="stocks"' in pages_css
    assert "research mobile layout audit passed" in audit


def test_mobile_control_icon_uses_bundled_font_glyph():
    html = INDEX.read_text(encoding="utf-8")

    assert FONT_AWESOME.exists()
    assert "/static/lib/fontawesome/css/all.min.css" in html
    assert 'class="fas fa-power-off"' in html
    assert "&#9211;" not in html


def test_news_polling_reads_local_feed_without_triggering_fetch():
    research_js = RESEARCH_JS.read_text(encoding="utf-8")

    assert "fetch:'1'" not in research_js
    assert "new URLSearchParams({limit:'100'})" in research_js


def test_loading_strategy_does_not_reload_strategy_list():
    core = CORE_JS.read_text(encoding="utf-8")
    load_body = core.split("async function loadStrat(fn){", 1)[1].split(
        "function newStrat()", 1
    )[0]

    assert "loadStratList()" not in load_body
