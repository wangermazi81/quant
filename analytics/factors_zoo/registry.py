# -*- coding: utf-8 -*-
"""见微 Alpha 因子库 Registry

设计原则：
  - 零侵入：本模块和现有 analytics/factor_manager.py 完全独立。
  - AST 扫描：不导入因子模块也能拿到元数据，避免破坏性 import。
  - 懒加载：调用 compute() 时才 importlib.exec_module。
  - 失败隔离：单个因子坏不影响其他；错误记录到 health() 不抛。
  - 输出校验：禁止 +/- inf，禁止 >95% NaN（防止脏因子混入策略链）。

因子文件契约（每个 zoo/<zoo_id>/<alpha_id>.py）：
  - 顶部声明 ``__alpha_meta__`` 字典字面量
    必填: id, columns_required, universe, decay_horizon, min_warmup_bars
    可选: nickname, theme, formula_latex, extras_required, requires_sector, frequency, notes
  - 提供 ``compute(panel) -> pd.DataFrame``
    panel: dict[str, pd.DataFrame]，key=open/high/low/close/volume/vwap/amount
    每个 DataFrame: index=DatetimeIndex, columns=股票代码
  - 算子从同级 base 模块导入：``from analytics.factors_zoo.base import rank, ts_corr, ...``
"""
from __future__ import annotations

import ast
import importlib.util
import logging
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_PY_BYTES = 200_000

# 元数据必填字段
_REQUIRED_META_KEYS = ("id", "columns_required", "universe", "decay_horizon", "min_warmup_bars")
# 合法的 panel 列名（与你 OHLCV / Tushare 对齐）
_VALID_PANEL_COLS = {"open", "high", "low", "close", "volume", "vwap", "amount"}


@dataclass(frozen=True)
class AlphaHandle:
    """注册表中一个因子的轻量句柄"""
    id: str
    zoo: str
    py_path: Path
    meta: dict


@dataclass(frozen=True)
class LoadError:
    alpha_id: str
    reason: str


class SkipAlpha(Exception):
    """panel 缺必需列时抛，告知调用方此因子在当前数据下不可计算"""


class RegistryError(Exception):
    """注册表/计算/输出校验失败"""


def _validate_id(token: str, kind: str) -> None:
    if not _ID_RE.fullmatch(token):
        raise RegistryError(f"非法 {kind} {token!r}：必须匹配 {_ID_RE.pattern}")


def load_alpha_meta_from_py(path: Path) -> dict:
    """AST 静态读取 __alpha_meta__ 字面量，不 import 因子文件"""
    size = path.stat().st_size
    if size > _MAX_PY_BYTES:
        raise RegistryError(f"{path.name}: {size}B 超过 {_MAX_PY_BYTES}B 上限")

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    meta_node = None
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        for tgt in stmt.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "__alpha_meta__":
                meta_node = stmt.value
                break
        if meta_node is not None:
            break

    if meta_node is None:
        raise RegistryError(f"{path.name}: 未找到 __alpha_meta__ 赋值")

    try:
        raw = ast.literal_eval(meta_node)
    except (ValueError, SyntaxError) as exc:
        raise RegistryError(f"{path.name}: __alpha_meta__ 不是字面量字典：{exc}") from exc

    if not isinstance(raw, dict):
        raise RegistryError(f"{path.name}: __alpha_meta__ 必须是 dict，得到 {type(raw).__name__}")

    missing = [k for k in _REQUIRED_META_KEYS if k not in raw]
    if missing:
        raise RegistryError(f"{path.name}: __alpha_meta__ 缺必填字段 {missing}")

    cols = raw.get("columns_required", [])
    if not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):
        raise RegistryError(f"{path.name}: columns_required 必须是 str 列表")
    bad_cols = [c for c in cols if c not in _VALID_PANEL_COLS]
    if bad_cols:
        raise RegistryError(f"{path.name}: columns_required 含非法列 {bad_cols}，合法 {sorted(_VALID_PANEL_COLS)}")

    return raw


class Registry:
    """进程内因子注册表 —— 扫一次 zoo/ 目录，懒加载因子模块"""

    def __init__(self, zoo_root: Optional[Path] = None) -> None:
        self._zoo_root: Path = (zoo_root or (Path(__file__).parent / "zoo")).resolve()
        self._alphas: dict[str, AlphaHandle] = {}
        self._load_errors: list[LoadError] = []
        self._module_cache: dict[str, ModuleType] = {}
        self._scan()

    # ---------------- 扫描 ----------------

    def _scan(self) -> None:
        if not self._zoo_root.is_dir():
            return
        for zoo_dir in sorted(self._zoo_root.iterdir()):
            if not zoo_dir.is_dir():
                continue
            zoo_id = zoo_dir.name
            if zoo_id.startswith("_") or zoo_id == "__pycache__":
                continue
            try:
                _validate_id(zoo_id, "zoo_id")
            except RegistryError as exc:
                self._load_errors.append(LoadError(zoo_id, str(exc)))
                continue
            for py_file in sorted(zoo_dir.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                self._try_register(zoo_id, py_file)

    def _try_register(self, zoo_id: str, py_file: Path) -> None:
        short_id = py_file.stem
        try:
            _validate_id(short_id, "alpha_id_short")
        except RegistryError as exc:
            self._load_errors.append(LoadError(f"{zoo_id}.{short_id}", str(exc)))
            return
        try:
            meta = load_alpha_meta_from_py(py_file)
        except RegistryError as exc:
            self._load_errors.append(LoadError(f"{zoo_id}.{short_id}", str(exc)))
            return

        alpha = AlphaHandle(id=meta["id"], zoo=zoo_id, py_path=py_file, meta=meta)
        if alpha.id in self._alphas:
            self._load_errors.append(LoadError(alpha.id, "重复 alpha id"))
            return
        self._alphas[alpha.id] = alpha

    # ---------------- 公共 API ----------------

    def list(
        self,
        zoo: Optional[str] = None,
        theme: Optional[str] = None,
        universe: Optional[str] = None,
    ) -> list[str]:
        out = []
        for a in self._alphas.values():
            if zoo and a.zoo != zoo:
                continue
            if theme and theme not in a.meta.get("theme", []):
                continue
            if universe and universe not in a.meta.get("universe", []):
                continue
            out.append(a.id)
        return sorted(out)

    def get(self, alpha_id: str) -> AlphaHandle:
        if alpha_id not in self._alphas:
            raise KeyError(f"alpha_id {alpha_id!r} 不在注册表中")
        return self._alphas[alpha_id]

    def health(self) -> dict:
        return {
            "loaded": len(self._alphas),
            "failed": len(self._load_errors),
            "zoos": sorted({a.zoo for a in self._alphas.values()}),
            "errors": [{"alpha_id": e.alpha_id, "reason": e.reason} for e in self._load_errors],
        }

    def compute(self, alpha_id: str, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
        alpha = self.get(alpha_id)
        meta = alpha.meta

        missing = [c for c in meta.get("columns_required", []) if c not in panel]
        if missing:
            raise SkipAlpha(f"{alpha_id}: panel 缺必需列 {missing}")
        missing_extra = [c for c in meta.get("extras_required", []) if c not in panel]
        if missing_extra:
            raise SkipAlpha(f"{alpha_id}: panel 缺 extras {missing_extra}")
        if meta.get("requires_sector") and "sector" not in panel:
            raise SkipAlpha(f"{alpha_id}: panel 缺 sector")

        try:
            module = self._load_module(alpha)
        except Exception as exc:
            raise RegistryError(f"{alpha_id}: import 失败：{exc}") from exc

        compute_fn = getattr(module, "compute", None)
        if compute_fn is None:
            raise RegistryError(f"{alpha_id}: 模块未提供 compute()")

        try:
            result = compute_fn(panel)
        except Exception as exc:
            raise RegistryError(f"{alpha_id}: compute() 抛错：{exc}") from exc

        return self._validate_output(alpha_id, result, panel)

    def _load_module(self, alpha: AlphaHandle) -> ModuleType:
        cache_key = f"factors_zoo.{alpha.zoo}.{alpha.py_path.stem}"
        cached = self._module_cache.get(cache_key)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(cache_key, alpha.py_path)
        if spec is None or spec.loader is None:
            raise RegistryError(f"{alpha.id}: 无法构造 import spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[cache_key] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(cache_key, None)
            raise
        self._module_cache[cache_key] = module
        return module

    @staticmethod
    def _validate_output(alpha_id: str, result: Any, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
        if not isinstance(result, pd.DataFrame):
            raise RegistryError(
                f"{alpha_id}: compute() 返回 {type(result).__name__}，应该是 DataFrame"
            )
        ref = panel.get("close")
        if ref is not None and result.shape != ref.shape:
            raise RegistryError(
                f"{alpha_id}: 输出 shape {result.shape} != close shape {ref.shape}"
            )
        arr = result.to_numpy(dtype=np.float64, na_value=np.nan)
        if np.isinf(arr).any():
            raise RegistryError(f"{alpha_id}: 输出含 +/- inf")
        nan_ratio = float(np.isnan(arr).mean()) if arr.size > 0 else 1.0
        if nan_ratio > 0.95:
            raise RegistryError(f"{alpha_id}: 输出 NaN 占比 {nan_ratio:.3f} 过高")
        return result


# 进程级单例
_registry_lock = threading.Lock()
_registry_cache: Optional[Registry] = None


def get_default_registry() -> Registry:
    global _registry_cache
    with _registry_lock:
        if _registry_cache is None:
            _registry_cache = Registry()
        return _registry_cache


def reset_default_registry() -> None:
    """测试钩子，重新扫描 zoo/"""
    global _registry_cache
    with _registry_lock:
        _registry_cache = None
