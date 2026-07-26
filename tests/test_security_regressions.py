import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["QUANT_TEST_IMPORT"] = "1"
os.environ["THS_NO_SCHEDULER"] = "1"

import main
import research_center
import web.auth as web_auth


@pytest.mark.parametrize(
    "payload",
    [
        {"username": {"$ne": None}, "password": "x"},
        {"username": 123, "password": 456},
        ["not", "an", "object"],
    ],
)
def test_login_rejects_non_string_json_fields(monkeypatch, payload):
    calls = []
    monkeypatch.setattr(web_auth.um, "login", lambda *args: calls.append(args) or {"ok": False})

    response = main.app.test_client().post("/api/auth/login", json=payload)

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert calls == []


@pytest.mark.parametrize("payload", [{"email": {"$ne": None}}, {"username": 123}, ["bad"]])
def test_forgot_rejects_non_string_json_fields(monkeypatch, payload):
    calls = []
    monkeypatch.setattr(web_auth.um, "create_password_reset", lambda ident: calls.append(ident))

    response = main.app.test_client().post("/api/auth/forgot", json=payload)

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert calls == []


@pytest.mark.parametrize("payload", [["bad"], "bad", 7, True])
def test_all_api_json_requests_reject_non_object_payloads(payload):
    response = main.app.test_client().post("/api/auth/login", json=payload)

    assert response.status_code == 400
    assert response.get_json() == {
        "ok": False,
        "error": "JSON 请求体必须是对象",
        "code": 400,
    }


def test_all_api_json_requests_reject_malformed_json():
    response = main.app.test_client().post(
        "/api/auth/login",
        data="{bad",
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON 请求体格式错误"


@pytest.mark.parametrize("name", ["'\"<script>", "A" * 41, "AI/../../etc"])
def test_chain_stocks_rejects_invalid_name_before_query(monkeypatch, name):
    calls = []
    monkeypatch.setattr(research_center, "get_concept_stocks", lambda value: calls.append(value))

    response = main.app.test_client().get("/api/chain/stocks", query_string={"name": name})

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert calls == []


def test_dynamic_user_data_is_not_embedded_in_inline_handlers():
    root = Path(__file__).parent.parent
    core_js = (root / "static/app/js/app-core.js").read_text(encoding="utf-8")
    ai_trader_js = (root / "static/app/js/app-ai-trader.js").read_text(encoding="utf-8")
    research_js = (root / "static/app/js/app-research.js").read_text(encoding="utf-8")
    admin_js = (root / "static/app/js/admin-app.js").read_text(encoding="utf-8")

    assert "manualBuy(${JSON.stringify" not in core_js
    assert "addOneQ(${JSON.stringify" not in core_js
    assert "manualBuy(${btnPayload})" not in core_js
    assert "addOneQ(${btnPayload})" not in core_js
    assert "data-core-action" in core_js
    assert "onclick=\"aitShowDec" not in ai_trader_js
    assert "onclick=\"aitConfirm" not in ai_trader_js
    assert "onclick=\"aitRemoveWatch" not in ai_trader_js
    assert "data-ai-action" in ai_trader_js
    assert "onclick=\"openConcept(" not in research_js
    assert "onclick=\"resolveResetReq(${q.id}" not in admin_js
    assert "openPointsModal(${u.id},'${uname}'" not in admin_js
    assert "openResetPwdModal(${u.id},'${uname}'" not in admin_js
    assert "toggleActive(${u.id},${u.is_active},'${uname}'" not in admin_js
    assert "openAdminPermissions(${u.id},'${uname}'" not in admin_js
    assert "toggleAdmin(${u.id},${u.is_admin},'${uname}'" not in admin_js
    assert "delUser(${u.id},'${uname}'" not in admin_js
    assert "data-rv-concept" in research_js
    assert "data-reset-request" in admin_js
    assert "data-admin-user-action" in admin_js



def test_app_shell_uses_delegated_ui_events_instead_of_inline_handlers():
    root = Path(__file__).parent.parent
    files = [
        root / "index.html",
        root / "admin.html",
        *sorted((root / "static/app/js").glob("*.js")),
    ]
    forbidden = (
        "onclick=",
        "onchange=",
        "oninput=",
        "onkeydown=",
        "onkeyup=",
        "onblur=",
        "onfocus=",
        "onsubmit=",
        "oncontextmenu=",
        ".onclick =",
        ".onchange =",
    )

    offenders = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        lowered = source.lower()
        for token in forbidden:
            if token in lowered:
                offenders.append(f"{path.relative_to(root)} contains {token}")

    assert offenders == []
    assert (root / "static/app/js/ui-events.js").exists()


def test_external_research_urls_are_protocol_filtered():
    source = (Path(__file__).parent.parent / "static/app/js/app-research.js").read_text(encoding="utf-8")

    assert "function _rvSafeUrl" in source
    helper = source[source.index("function _rvSafeUrl"):source.index("function _rvSafeUrl") + 400]
    assert "http:" in helper and "https:" in helper


def test_csp_separates_elements_from_legacy_inline_attributes():
    """CSP 必须把「元素」与「遗留内联属性」分开控制，且不得放开 eval。

    这里断言的是**安全性质**，不是某一份具体的外部主机白名单：
    白名单只会越收越紧（2026-07-26 起全站资源已本地化，jsdelivr/Google Fonts/
    cdnjs 全部从 CSP 移除），把主机名写死会让「变得更安全」反而把测试打红 ——
    这条用例此前就是这么红的。
    """
    source = (Path(__file__).parent.parent / "app_security.py").read_text(encoding="utf-8")
    csp = source[source.index("    csp = ("):source.index("    @app.before_request")]

    # ① 绝不允许 eval：ui-events.js 已改为「安全调用解析器 + 预编译静态表」
    assert "'unsafe-eval'" not in csp
    # ② 脚本/样式的**元素**必须与**属性**分开声明，属性放宽不得连带放宽元素
    assert "script-src-elem" in csp and "script-src-attr 'unsafe-inline'" in csp
    assert "style-src-elem" in csp and "style-src-attr 'unsafe-inline'" in csp
    # ③ 元素级绝不允许内联脚本/样式（这才是 XSS 注入真正的落地点）
    for directive in ("script-src-elem", "style-src-elem"):
        line = next(ln for ln in csp.splitlines() if directive in ln)
        assert "'unsafe-inline'" not in line, f"{directive} 不得放开内联：{line.strip()}"
    # ④ 兜底指令不能缺
    for must in ("default-src 'self'", "object-src 'none'", "base-uri 'self'",
                 "frame-ancestors 'self'"):
        assert must in csp, f"CSP 缺少 {must}"


def test_expensive_public_routes_have_endpoint_rate_limits():
    root = Path(__file__).parent.parent
    cases = {
        "web/lhb.py": '@rate_limit(6, 60, scope="public_refresh")',
        "web/market.py": '@rate_limit(12, 60, scope="public_refresh")',
        "web/review.py": '@rate_limit(6, 60, scope="public_refresh")',
        "web/research.py": '@rate_limit(20, 60, scope="public_research")',
    }
    for relative, marker in cases.items():
        assert marker in (root / relative).read_text(encoding="utf-8")


def test_shared_data_mutations_require_admin_and_download_is_bounded():
    source = (Path(__file__).parent.parent / "web/data.py").read_text(encoding="utf-8")

    assert "MAX_DOWNLOAD_CODES = 200" in source
    assert '@rate_limit(3, 3600, scope="data_download")' in source
    assert "@require_admin\ndef api_data_delete" in source
    assert "@require_admin\ndef api_refresh_names" in source


def test_quant_backup_script_creates_valid_sqlite_copy(tmp_path):
    import importlib.util
    import sqlite3

    root = Path(__file__).parent.parent
    script = root / "deploy/backup_quant_data.py"
    spec = importlib.util.spec_from_file_location("backup_quant_data", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    with sqlite3.connect(source) as con:
        con.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO sample(value) VALUES ('ok')")

    module.backup_database(source, target)

    with sqlite3.connect(target) as con:
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert con.execute("SELECT value FROM sample").fetchone()[0] == "ok"
