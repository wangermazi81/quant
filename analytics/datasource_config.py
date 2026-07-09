# -*- coding: utf-8 -*-
"""Global market-data source configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


CONFIG_FILE = Path(__file__).parent.parent / "data" / "datasource_config.json"
VALID_PROVIDERS = {"current", "tushare"}

DEFAULT_CONFIG = {
    "provider": "current",
    "tushare_enabled": False,
    "tushare_token": "",
    "fallback_enabled": True,
}


def mask_token(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return ""
    if len(token) <= 8:
        return token[:2] + "*" * max(0, len(token) - 2)
    return token[:4] + "********" + token[-4:]


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "启用"}
    return bool(value)


def _normalize(raw: dict | None) -> dict:
    cfg = {**DEFAULT_CONFIG, **(raw or {})}
    provider = str(cfg.get("provider") or "current").strip().lower()
    if provider not in VALID_PROVIDERS:
        raise ValueError("provider must be current or tushare")
    cfg["provider"] = provider
    cfg["tushare_enabled"] = _coerce_bool(cfg.get("tushare_enabled"), provider == "tushare")
    cfg["fallback_enabled"] = _coerce_bool(cfg.get("fallback_enabled"), True)
    cfg["tushare_token"] = str(cfg.get("tushare_token") or "").strip()
    return cfg


def _read_raw() -> dict:
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG.copy()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        return _normalize(data)
    except ValueError:
        raise
    except Exception:
        return DEFAULT_CONFIG.copy()


def _public(cfg: dict) -> dict:
    visible = {k: v for k, v in cfg.items() if k != "tushare_token"}
    visible["tushare_token_masked"] = mask_token(cfg.get("tushare_token", ""))
    visible["has_tushare_token"] = bool(cfg.get("tushare_token"))
    return visible


def get_internal_config() -> dict:
    cfg = _read_raw()
    token = cfg.get("tushare_token") or os.environ.get("TUSHARE_TOKEN", "")
    cfg["tushare_token"] = str(token or "").strip()
    return cfg


def get_config() -> dict:
    return _public(get_internal_config())


def save_config(patch: dict | None) -> dict:
    current = _read_raw()
    patch = patch or {}
    merged = current.copy()
    for key in ("provider", "tushare_enabled", "fallback_enabled"):
        if key in patch:
            merged[key] = patch[key]
    if str(patch.get("tushare_token") or "").strip():
        merged["tushare_token"] = str(patch.get("tushare_token") or "").strip()
    merged = _normalize(merged)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return _public(merged)


def resolve_tushare_token(cfg: dict | None = None) -> str:
    data = cfg or get_internal_config()
    return str((data or {}).get("tushare_token") or os.environ.get("TUSHARE_TOKEN", "")).strip()

