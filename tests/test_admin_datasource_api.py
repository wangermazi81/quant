from tests.test_admin_permissions import _create_admin, _login_session, isolated_app  # noqa: F401


def test_admin_datasource_config_requires_data_permission(isolated_app, monkeypatch, tmp_path):
    app, um, _main = isolated_app
    import web.admin as admin_api

    monkeypatch.setattr(admin_api.datasource_config, "CONFIG_FILE", tmp_path / "datasource_config.json")
    uid = _create_admin(um, permissions=["dashboard_view"])
    client = app.test_client()
    _login_session(client, uid, "ops")

    denied = client.get("/api/admin/datasource/config")
    assert denied.status_code == 403
    assert denied.get_json()["missing_permission"] == "data_config"

    assert um.set_admin_permissions(uid, ["data_config"], granted_by=1)["ok"]
    allowed = client.get("/api/admin/datasource/config")
    assert allowed.status_code == 200
    assert allowed.get_json()["provider"] == "current"


def test_admin_datasource_config_save_masks_token(isolated_app, monkeypatch, tmp_path):
    app, _um, _main = isolated_app
    import web.admin as admin_api

    monkeypatch.setattr(admin_api.datasource_config, "CONFIG_FILE", tmp_path / "datasource_config.json")
    client = app.test_client()
    _login_session(client, 1, "admin")

    resp = client.post("/api/admin/datasource/config", json={
        "provider": "tushare",
        "tushare_enabled": True,
        "tushare_token": "abcdef1234567890",
        "fallback_enabled": True,
    })
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["config"]["provider"] == "tushare"
    assert data["config"]["tushare_token_masked"] == "abcd********7890"
    assert "tushare_token" not in data["config"]


def test_admin_datasource_test_uses_posted_or_saved_token(isolated_app, monkeypatch, tmp_path):
    app, _um, _main = isolated_app
    import web.admin as admin_api

    monkeypatch.setattr(admin_api.datasource_config, "CONFIG_FILE", tmp_path / "datasource_config.json")
    calls = []
    monkeypatch.setattr(admin_api.tushare_provider, "test_connection", lambda token=None: calls.append(token) or {"ok": True, "rows": 1})

    client = app.test_client()
    _login_session(client, 1, "admin")
    client.post("/api/admin/datasource/config", json={"provider": "tushare", "tushare_token": "saved-token"})
    resp = client.post("/api/admin/datasource/test", json={})

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert calls == ["saved-token"]
