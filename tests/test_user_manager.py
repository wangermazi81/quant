# -*- coding: utf-8 -*-
"""
tests/test_user_manager.py
用户/认证/积分/找回密码/邀请码 单元测试（钱与账号安全的关键路径）。

运行：
  cd ths_auto_trader
  python -m pytest tests/test_user_manager.py -v
"""
import sys
import hashlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def um(tmp_path, monkeypatch):
    """每个测试一套独立临时库：建表 + 迁移（含 password_reset* 表）。"""
    from core import user_manager as _um
    from core import db_migration as _dbm
    db = str(tmp_path / "users_test.db")
    monkeypatch.setattr(_um, "DB_PATH", db)
    monkeypatch.setattr(_dbm, "USERS_DB", db)
    _um.init_db()
    _dbm.migrate_users_db()
    return _um


def _invite(um, **kw):
    """管理员邀请码（默认 5 名额、奖励 0），返回 code。"""
    kw.setdefault("max_uses", 5)
    kw.setdefault("is_admin_code", True)
    kw.setdefault("reward", 0)
    return um.create_invite_code(creator_uid=1, **kw)["code"]


def _mkuser(um, name="u1", email=None, pw="pass123", code=None, ip="1.1.1.1"):
    code = code or _invite(um)
    email = email or f"{name}@example.com"
    r = um.register(name, email, pw, code, ip=ip)
    assert r["ok"], r
    return um.login(name, pw)["user_id"]


# ── 密码哈希 ──────────────────────────────────────────────
class TestPasswordHash:
    def test_pbkdf2_format(self, um):
        h = um._hash_password("secret123")
        assert h.startswith("pbkdf2:sha256$")
        assert um._verify_password("secret123", h)
        assert not um._verify_password("wrong", h)

    def test_salt_random(self, um):
        assert um._hash_password("x") != um._hash_password("x")  # 每次盐不同

    def test_legacy_sha256_verified_and_upgraded(self, um):
        # 旧明文 sha256 哈希应能校验，登录后透明升级为 pbkdf2
        legacy = hashlib.sha256("oldpw1".encode()).hexdigest()
        with um._conn() as c:
            c.execute("UPDATE users SET password_hash=? WHERE id=1", (legacy,))
            c.commit()
        assert um._is_legacy_hash(legacy)
        assert um.login("admin", "oldpw1")["ok"]
        with um._conn() as c:
            newh = c.execute("SELECT password_hash FROM users WHERE id=1").fetchone()[0]
        assert newh.startswith("pbkdf2:")  # 已升级


# ── 注册（邀请制 + 防刷）────────────────────────────────────
class TestRegister:
    def test_requires_invite(self, um):
        assert not um.register("a", "a@x.com", "pass123", "")["ok"]

    def test_bad_invite(self, um):
        assert not um.register("a", "a@x.com", "pass123", "BADCODE")["ok"]

    def test_validation(self, um):
        code = _invite(um)
        assert not um.register("a", "a@x.com", "pass123", code)["ok"]      # 用户名<2
        assert not um.register("ab", "a@x.com", "123", code)["ok"]         # 密码<6
        assert not um.register("ab", "noat", "pass123", code)["ok"]        # 邮箱无@

    def test_success_grants_bonus(self, um):
        uid = _mkuser(um, "alice")
        assert um.get_user(uid)["points"] == um.REGISTER_BONUS

    def test_duplicate_rejected(self, um):
        code = _invite(um, max_uses=5)
        assert um.register("bob", "bob@x.com", "pass123", code)["ok"]
        assert not um.register("bob", "other@x.com", "pass123", code)["ok"]   # 重名
        assert not um.register("bob2", "bob@x.com", "pass123", code)["ok"]    # 重邮箱

    def test_invite_use_consumed_and_capped(self, um):
        code = _invite(um, max_uses=1)
        assert um.register("c1", "c1@x.com", "pass123", code)["ok"]
        assert not um.register("c2", "c2@x.com", "pass123", code)["ok"]       # 名额用完

    def test_inviter_rewarded(self, um):
        inviter = _mkuser(um, "inv")
        before = um.get_user(inviter)["points"]
        # inviter 自建用户码（强制奖励 INVITE_REWARD、名额夹到<=20）
        ucode = um.create_invite_code(creator_uid=inviter, max_uses=99)["code"]
        assert um.register("newbie", "nb@x.com", "pass123", ucode)["ok"]
        assert um.get_user(inviter)["points"] == before + um.INVITE_REWARD

    def test_user_invite_capped_at_20(self, um):
        uid = _mkuser(um, "inv2")
        r = um.create_invite_code(creator_uid=uid, max_uses=999)
        assert r["max_uses"] <= 20            # 用户码名额硬上限，防批量薅
        assert r["reward"] == um.INVITE_REWARD  # 用户码奖励被强制


# ── 登录 ──────────────────────────────────────────────────
class TestLogin:
    def test_wrong_password(self, um):
        _mkuser(um, "dave", pw="right123")
        assert not um.login("dave", "wrong")["ok"]

    def test_correct(self, um):
        _mkuser(um, "dave", pw="right123")
        r = um.login("dave", "right123")
        assert r["ok"] and r["username"] == "dave"

    def test_inactive_rejected(self, um):
        uid = _mkuser(um, "eve")
        um.set_active(uid, False)
        assert not um.login("eve", "pass123")["ok"]


# ── 改密 / 管理员重置 ──────────────────────────────────────
class TestChangePassword:
    def test_change_requires_old(self, um):
        uid = _mkuser(um, "cp")
        assert not um.change_password(uid, "wrong", "newpass1")["ok"]
        assert um.change_password(uid, "pass123", "newpass1")["ok"]
        assert um.login("cp", "newpass1")["ok"]

    def test_admin_reset(self, um):
        uid = _mkuser(um, "ar")
        assert um.admin_reset_password(uid, "reset123")["ok"]
        assert um.login("ar", "reset123")["ok"]


# ── 积分 ──────────────────────────────────────────────────
class TestPoints:
    def test_add(self, um):
        uid = _mkuser(um, "pa")
        base = um.get_user(uid)["points"]
        um.add_points(uid, 50, "测试充值")
        assert um.get_user(uid)["points"] == base + 50

    def test_deduct_with_cost(self, um, monkeypatch):
        monkeypatch.setattr(um, "POINT_COSTS", {"act": 30})
        uid = _mkuser(um, "pb")
        um.set_points(uid, 100)
        assert um.deduct_points(uid, "act")["ok"]
        assert um.get_user(uid)["points"] == 70

    def test_deduct_insufficient(self, um, monkeypatch):
        monkeypatch.setattr(um, "POINT_COSTS", {"act": 999})
        uid = _mkuser(um, "pc")
        um.set_points(uid, 10)
        assert not um.deduct_points(uid, "act")["ok"]
        assert um.get_user(uid)["points"] == 10   # 失败不扣

    def test_free_action_no_charge(self, um, monkeypatch):
        monkeypatch.setattr(um, "POINT_COSTS", {"free": 0})
        uid = _mkuser(um, "pd")
        um.set_points(uid, 5)
        assert um.deduct_points(uid, "free")["ok"]
        assert um.get_user(uid)["points"] == 5


# ── 找回密码：邮件 token 流程 ──────────────────────────────
class TestResetToken:
    def test_full_flow(self, um):
        _mkuser(um, "leo", email="leo@x.com", pw="old12345")
        user, token = um.create_password_reset("leo@x.com")
        assert user and token and user["username"] == "leo"
        assert um.verify_reset_token(token) is not None
        assert um.reset_password_with_token(token, "new12345")["ok"]
        assert um.login("leo", "new12345")["ok"]
        assert not um.login("leo", "old12345")["ok"]

    def test_token_single_use(self, um):
        _mkuser(um, "mm", email="mm@x.com")
        _, token = um.create_password_reset("mm")
        assert um.reset_password_with_token(token, "abcdef1")["ok"]
        assert not um.reset_password_with_token(token, "another1")["ok"]   # 用过即废
        assert um.verify_reset_token(token) is None

    def test_new_request_invalidates_old(self, um):
        _mkuser(um, "nn", email="nn@x.com")
        _, t1 = um.create_password_reset("nn")
        _, t2 = um.create_password_reset("nn")
        assert um.verify_reset_token(t1) is None         # 旧 token 被作废
        assert um.verify_reset_token(t2) is not None

    def test_unknown_returns_none(self, um):
        assert um.create_password_reset("ghost@nowhere.com") == (None, None)

    def test_expired_rejected(self, um):
        _mkuser(um, "oo", email="oo@x.com")
        _, token = um.create_password_reset("oo")
        th = hashlib.sha256(token.encode()).hexdigest()
        with um._conn() as c:   # 把过期时间改到过去
            c.execute("UPDATE password_resets SET expires_at='2000-01-01 00:00:00' WHERE token_hash=?", (th,))
            c.commit()
        assert um.verify_reset_token(token) is None
        assert not um.reset_password_with_token(token, "abcdef1")["ok"]


# ── 找回密码：后台审批流程 ─────────────────────────────────
class TestResetRequest:
    def test_request_and_resolve(self, um):
        uid = _mkuser(um, "rq", email="rq@x.com", pw="old12345")
        assert um.create_reset_request(uid, "rq", "rq@x.com")
        reqs = um.list_reset_requests("pending")
        assert len(reqs) == 1 and reqs[0]["username"] == "rq"
        assert um.count_pending_reset_requests() == 1
        res = um.resolve_reset_request(reqs[0]["id"], admin_uid=1)
        assert res["ok"] and len(res["temp_password"]) >= 8
        assert um.login("rq", res["temp_password"])["ok"]
        assert not um.login("rq", "old12345")["ok"]
        assert um.count_pending_reset_requests() == 0

    def test_dedup(self, um):
        uid = _mkuser(um, "dd", email="dd@x.com")
        um.create_reset_request(uid, "dd", "dd@x.com")
        um.create_reset_request(uid, "dd", "dd@x.com")
        assert um.count_pending_reset_requests() == 1   # 同用户 pending 去重

    def test_resolve_twice_fails(self, um):
        uid = _mkuser(um, "rs", email="rs@x.com")
        um.create_reset_request(uid, "rs", "rs@x.com")
        rid = um.list_reset_requests("pending")[0]["id"]
        assert um.resolve_reset_request(rid, 1)["ok"]
        assert not um.resolve_reset_request(rid, 1)["ok"]

    def test_reject(self, um):
        uid = _mkuser(um, "rj", email="rj@x.com")
        um.create_reset_request(uid, "rj", "rj@x.com")
        rid = um.list_reset_requests("pending")[0]["id"]
        assert um.reject_reset_request(rid, 1)["ok"]
        assert um.count_pending_reset_requests() == 0
        assert not um.reject_reset_request(rid, 1)["ok"]

    def test_forgot_via_route_is_generic(self, um):
        # /api/auth/forgot 对存在/不存在邮箱返回同一文案，防枚举
        from flask import Flask
        import app_security
        from web.auth import bp
        app = Flask(__name__); app_security.init_security(app); app.register_blueprint(bp)
        _mkuser(um, "tt", email="tt@x.com")
        c = app.test_client()
        r1 = c.post("/api/auth/forgot", json={"email": "tt@x.com"}).get_json()    # 存在
        r2 = c.post("/api/auth/forgot", json={"email": "ghost@x.com"}).get_json()  # 不存在
        assert r1["ok"] and r2["ok"] and r1["msg"] == r2["msg"]
