# -*- coding: utf-8 -*-
"""
tests/test_app_security.py
应用安全模块测试：限流 / 登录防爆破锁定 / 真实 IP / 安全响应头 / Cookie 硬化。

运行：
  cd ths_auto_trader
  python -m pytest tests/test_app_security.py -v
"""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

import app_security as sec
from flask import Flask, jsonify


# ── 限流装饰器 ────────────────────────────────────────────
class TestRateLimit:
    def _app(self, scope, max_calls, per):
        app = Flask(__name__)

        @app.route("/ping")
        @sec.rate_limit(max_calls, per, scope=scope)
        def ping():
            return jsonify({"ok": True})

        return app

    def test_within_limit_ok(self):
        app = self._app("t1", 3, 60)
        c = app.test_client()
        for _ in range(3):
            assert c.get("/ping", headers={"X-Real-IP": "9.9.9.1"}).status_code == 200

    def test_over_limit_429(self):
        app = self._app("t2", 2, 60)
        c = app.test_client()
        c.get("/ping", headers={"X-Real-IP": "9.9.9.2"})
        c.get("/ping", headers={"X-Real-IP": "9.9.9.2"})
        r = c.get("/ping", headers={"X-Real-IP": "9.9.9.2"})
        assert r.status_code == 429
        assert r.get_json()["ok"] is False

    def test_per_ip_isolated(self):
        app = self._app("t3", 1, 60)
        c = app.test_client()
        assert c.get("/ping", headers={"X-Real-IP": "1.0.0.1"}).status_code == 200
        assert c.get("/ping", headers={"X-Real-IP": "1.0.0.2"}).status_code == 200   # 不同 IP 独立配额
        assert c.get("/ping", headers={"X-Real-IP": "1.0.0.1"}).status_code == 429   # 同 IP 超限


# ── 登录防爆破锁定 ────────────────────────────────────────
class TestLoginLock:
    def test_lock_after_max_fails(self):
        ident = "user_a|7.7.7.1"
        assert sec.login_locked(ident) == 0
        for _ in range(sec._LOGIN_MAX_FAILS):
            sec.login_fail(ident)
        assert sec.login_locked(ident) > 0          # 达阈值后被锁

    def test_success_clears(self):
        ident = "user_b|7.7.7.2"
        for _ in range(sec._LOGIN_MAX_FAILS - 1):
            sec.login_fail(ident)
        assert sec.login_locked(ident) == 0          # 未达阈值
        sec.login_ok(ident)                          # 成功清零
        for _ in range(sec._LOGIN_MAX_FAILS - 1):
            sec.login_fail(ident)
        assert sec.login_locked(ident) == 0          # 清零后重新计数，仍未锁

    def test_independent_idents(self):
        a, b = "x|1", "y|1"
        for _ in range(sec._LOGIN_MAX_FAILS):
            sec.login_fail(a)
        assert sec.login_locked(a) > 0
        assert sec.login_locked(b) == 0              # 不同标识互不影响


# ── 真实客户端 IP ─────────────────────────────────────────
class TestClientIP:
    def test_x_real_ip_priority(self):
        app = Flask(__name__)

        @app.route("/ip")
        def ip():
            return sec.client_ip()

        c = app.test_client()
        # nginx 设的 X-Real-IP 优先于 X-Forwarded-For
        r = c.get("/ip", headers={"X-Real-IP": "203.0.113.5", "X-Forwarded-For": "10.0.0.9"})
        assert r.get_data(as_text=True) == "203.0.113.5"

    def test_xff_fallback_first_hop(self):
        app = Flask(__name__)

        @app.route("/ip")
        def ip():
            return sec.client_ip()

        c = app.test_client()
        r = c.get("/ip", headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1"})
        assert r.get_data(as_text=True) == "198.51.100.7"


# ── 安全响应头 + Cookie 硬化 ───────────────────────────────
class TestSecurityHeaders:
    def test_headers_present(self):
        app = Flask(__name__)
        sec.init_security(app)

        @app.route("/")
        def home():
            return "ok"

        r = app.test_client().get("/")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert "Content-Security-Policy" in r.headers

    def test_cookie_hardening_config(self):
        app = Flask(__name__)
        sec.init_security(app)
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_hsts_only_when_secure(self, monkeypatch):
        # SECURE 关闭时不应发 HSTS（HTTP 下发 HSTS 会把用户锁死在无法访问）
        monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")
        app = Flask(__name__)
        sec.init_security(app)

        @app.route("/")
        def home():
            return "ok"

        r = app.test_client().get("/")
        assert "Strict-Transport-Security" not in r.headers
