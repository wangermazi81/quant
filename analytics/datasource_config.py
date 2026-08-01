# -*- coding: utf-8 -*-
"""Global market-data source configuration."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).parent.parent / "data" / "datasource_config.json"
VALID_PROVIDERS = {"agudata", "current", "tushare"}
VALID_TUSHARE_MODES = {"replay", "sdk"}
DEFAULT_AGUDATA_BASE_URL = (
    "https://api-hk.agudata.cn/app-api/openapi/v1"
)

DEFAULT_CONFIG = {
    "schema_version": 3,
    "provider": "tushare",
    "agudata_enabled": False,
    "agudata_api_key": "",
    "agudata_base_url": DEFAULT_AGUDATA_BASE_URL,
    "tushare_enabled": True,
    "tushare_mode": "replay",
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
    if raw and not raw.get("schema_version") and str(raw.get("provider") or "").strip().lower() == "current":
        cfg["provider"] = "tushare"
        cfg["tushare_enabled"] = True
    cfg["schema_version"] = int(cfg.get("schema_version") or DEFAULT_CONFIG["schema_version"])
    provider = str(cfg.get("provider") or "current").strip().lower()
    if provider not in VALID_PROVIDERS:
        raise ValueError("provider must be agudata, current or tushare")
    cfg["provider"] = provider
    cfg["agudata_enabled"] = _coerce_bool(
        cfg.get("agudata_enabled"),
        provider == "agudata",
    )
    cfg["tushare_enabled"] = _coerce_bool(cfg.get("tushare_enabled"), provider == "tushare")
    cfg["fallback_enabled"] = _coerce_bool(cfg.get("fallback_enabled"), True)
    mode = str(cfg.get("tushare_mode") or "replay").strip().lower()
    cfg["tushare_mode"] = mode if mode in VALID_TUSHARE_MODES else "replay"
    cfg["tushare_token"] = str(cfg.get("tushare_token") or "").strip()
    cfg["agudata_api_key"] = str(
        cfg.get("agudata_api_key") or ""
    ).strip()
    base_url = str(
        cfg.get("agudata_base_url") or DEFAULT_AGUDATA_BASE_URL
    ).strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise ValueError("agudata_base_url must use https")
    cfg["agudata_base_url"] = base_url
    return cfg


# 按 mtime+size 判定的进程内缓存：get_internal_config() 在全市场扫描热路径上
# 每只股票都会被调用一次（data_manager.get_kline_by_config 内），5000 只即 5000 次
# 磁盘读 + json.loads。配置文件几乎不变，用 stat 指纹判断即可安全复用。
_RAW_CACHE: dict | None = None
_RAW_CACHE_SIG: tuple | None = None


def _config_signature() -> tuple:
    try:
        st = CONFIG_FILE.stat()
        return (str(CONFIG_FILE), st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, -1)          # 文件不存在也是一种确定状态，可缓存


def _read_raw() -> dict:
    global _RAW_CACHE, _RAW_CACHE_SIG

    sig = _config_signature()
    if _RAW_CACHE is not None and _RAW_CACHE_SIG == sig:
        return _RAW_CACHE.copy()

    if not CONFIG_FILE.exists():
        cfg = DEFAULT_CONFIG.copy()
        _RAW_CACHE, _RAW_CACHE_SIG = cfg.copy(), sig
        return cfg
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        cfg = _normalize(data)
        _RAW_CACHE, _RAW_CACHE_SIG = cfg.copy(), sig
        return cfg.copy()
    except ValueError:
        raise
    except Exception as e:
        # 配置损坏 → 静默回退默认会丢失用户的数据源选择（如已配 tushare），必须留痕
        logger.warning(f"[datasource] 配置读取失败，回退默认数据源: {e}")
        return DEFAULT_CONFIG.copy()


def _public(cfg: dict) -> dict:
    visible = {
        k: v
        for k, v in cfg.items()
        if k not in {"agudata_api_key", "tushare_token"}
    }
    visible["agudata_api_key_masked"] = mask_token(
        cfg.get("agudata_api_key", "")
    )
    visible["has_agudata_api_key"] = bool(
        cfg.get("agudata_api_key")
    )
    visible["tushare_token_masked"] = mask_token(cfg.get("tushare_token", ""))
    visible["has_tushare_token"] = bool(cfg.get("tushare_token"))
    return visible


def get_internal_config() -> dict:
    cfg = _read_raw()
    cfg["agudata_api_key"] = str(
        cfg.get("agudata_api_key")
        or os.environ.get("AGUDATA_API_KEY", "")
    ).strip()
    cfg["agudata_base_url"] = str(
        os.environ.get("AGUDATA_BASE_URL", "")
        or cfg.get("agudata_base_url")
        or DEFAULT_AGUDATA_BASE_URL
    ).strip().rstrip("/")
    mode = str(cfg.get("tushare_mode") or "replay").strip().lower()
    if mode == "replay":
        token = cfg.get("tushare_token") or os.environ.get("TUSHARE_REPLAY_API_KEY", "") or os.environ.get("TUSHARE_TOKEN", "")
    else:
        token = cfg.get("tushare_token") or os.environ.get("TUSHARE_TOKEN", "")
    cfg["tushare_token"] = str(token or "").strip()
    return cfg


def get_config() -> dict:
    return _public(get_internal_config())


def save_config(patch: dict | None) -> dict:
    current = _read_raw()
    patch = patch or {}
    merged = current.copy()
    for key in (
        "provider",
        "agudata_enabled",
        "agudata_base_url",
        "tushare_mode",
        "tushare_enabled",
        "fallback_enabled",
    ):
        if key in patch:
            merged[key] = patch[key]
    if str(patch.get("agudata_api_key") or "").strip():
        merged["agudata_api_key"] = str(
            patch.get("agudata_api_key") or ""
        ).strip()
    if str(patch.get("tushare_token") or "").strip():
        merged["tushare_token"] = str(patch.get("tushare_token") or "").strip()
    merged = _normalize(merged)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    # 显式失效读缓存：不能只依赖 mtime 指纹（同一时钟刻度内改写可能撞上同一签名）。
    # 数据源切换要立刻生效，否则用户在页面上改了 provider 却发现还在走旧源。
    global _RAW_CACHE, _RAW_CACHE_SIG
    _RAW_CACHE, _RAW_CACHE_SIG = None, None
    try:
        from analytics import tushare_provider as _tp
        _tp.clear_kline_cache()      # 换数据源/token 后旧日线缓存必须作废
    except Exception:
        pass
    try:
        from analytics import agudata_provider as _ap
        _ap.clear_cache()
    except Exception:
        pass
    return _public(merged)


def resolve_tushare_token(cfg: dict | None = None, mode: str | None = None) -> str:
    data = cfg or get_internal_config()
    token = str((data or {}).get("tushare_token") or "").strip()
    if token:
        return token
    resolved_mode = str(mode or (data or {}).get("tushare_mode") or "replay").strip().lower()
    if resolved_mode == "replay":
        return str(os.environ.get("TUSHARE_REPLAY_API_KEY", "") or os.environ.get("TUSHARE_TOKEN", "")).strip()
    return str(os.environ.get("TUSHARE_TOKEN", "")).strip()


def resolve_agudata_api_key(cfg: dict | None = None) -> str:
    data = cfg or get_internal_config()
    return str(
        (data or {}).get("agudata_api_key")
        or os.environ.get("AGUDATA_API_KEY", "")
    ).strip()


def clear_config_cache() -> None:
    global _RAW_CACHE, _RAW_CACHE_SIG
    _RAW_CACHE, _RAW_CACHE_SIG = None, None

