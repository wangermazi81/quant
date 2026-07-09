import json

import analytics.datasource_config as cfg


def test_default_config_uses_current_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "datasource_config.json")

    data = cfg.get_config()

    assert data["provider"] == "current"
    assert data["fallback_enabled"] is True
    assert data["tushare_enabled"] is False
    assert data["tushare_token_masked"] == ""
    assert "tushare_token" not in data


def test_save_config_masks_and_preserves_blank_token(monkeypatch, tmp_path):
    path = tmp_path / "datasource_config.json"
    monkeypatch.setattr(cfg, "CONFIG_FILE", path)

    saved = cfg.save_config({
        "provider": "tushare",
        "tushare_enabled": True,
        "tushare_token": "abcdef1234567890",
        "fallback_enabled": False,
    })
    assert saved["provider"] == "tushare"
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

