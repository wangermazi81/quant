import json

import analytics.datasource_config as cfg


def test_default_config_uses_tushare_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "datasource_config.json")
    monkeypatch.delenv("TUSHARE_REPLAY_API_KEY", raising=False)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)

    data = cfg.get_config()

    assert data["provider"] == "tushare"
    assert data["fallback_enabled"] is True
    assert data["tushare_enabled"] is True
    assert data["tushare_mode"] == "replay"
    assert data["tushare_token_masked"] == ""
    assert "tushare_token" not in data


def test_save_config_masks_and_preserves_blank_token(monkeypatch, tmp_path):
    path = tmp_path / "datasource_config.json"
    monkeypatch.setattr(cfg, "CONFIG_FILE", path)

    saved = cfg.save_config({
        "provider": "tushare",
        "tushare_enabled": True,
        "tushare_mode": "replay",
        "tushare_token": "abcdef1234567890",
        "fallback_enabled": False,
    })
    assert saved["provider"] == "tushare"
    assert saved["tushare_mode"] == "replay"
    assert saved["tushare_token_masked"] == "abcd********7890"
    assert "tushare_token" not in saved

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["tushare_token"] == "abcdef1234567890"

    saved_again = cfg.save_config({"provider": "current", "tushare_token": ""})
    stored_again = json.loads(path.read_text(encoding="utf-8"))
    assert saved_again["provider"] == "current"
    assert stored_again["tushare_token"] == "abcdef1234567890"


def test_save_config_rejects_unknown_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "datasource_config.json")

    try:
        cfg.save_config({"provider": "mystery"})
    except ValueError as exc:
        assert "provider" in str(exc)
    else:
        raise AssertionError("unknown provider should fail")




def test_replay_mode_uses_replay_env_key(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "datasource_config.json")
    monkeypatch.setenv("TUSHARE_REPLAY_API_KEY", "replay-key")
    monkeypatch.setenv("TUSHARE_TOKEN", "sdk-token")

    cfg.save_config({"provider": "tushare", "tushare_mode": "replay", "tushare_enabled": True})
    data = cfg.get_internal_config()

    assert data["tushare_mode"] == "replay"
    assert data["tushare_token"] == "replay-key"
    assert cfg.resolve_tushare_token(data, mode="replay") == "replay-key"
    assert cfg.resolve_tushare_token({"tushare_mode": "sdk"}, mode="sdk") == "sdk-token"
