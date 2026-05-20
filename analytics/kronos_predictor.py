# -*- coding: utf-8 -*-
"""
Kronos K-line Foundation Model 接口封装
https://github.com/shiyu-coder/Kronos

使用方式:
  from kronos_predictor import KronosPredictor
  predictor = KronosPredictor()        # 懒加载，第一次调用时下载模型
  score = predictor.score("000001", df)
  results = predictor.predict_batch(code_df_pairs)

Kronos 未安装或加载失败时，所有方法返回 None / 空列表，不影响主流程。
"""
import logging
import numpy as np
import pandas as pd
from typing import Optional

logger = logging.getLogger(__name__)

_KRONOS_AVAILABLE: Optional[bool] = None   # None=未检测, True/False=已知


def _check_kronos() -> bool:
    global _KRONOS_AVAILABLE
    if _KRONOS_AVAILABLE is not None:
        return _KRONOS_AVAILABLE
    try:
        import importlib
        importlib.import_module("kronos")
        _KRONOS_AVAILABLE = True
        logger.info("[Kronos] 库已找到")
    except ImportError:
        _KRONOS_AVAILABLE = False
        logger.info("[Kronos] 未安装，使用降级评分")
    return _KRONOS_AVAILABLE


def _fallback_score(df: pd.DataFrame) -> float:
    """
    Kronos 不可用时的技术指标降级评分 (0~1)。
    基于: MA排列 + RSI + 动量
    """
    if df.empty or len(df) < 20:
        return 0.5
    close = pd.Series(df["close"].values, dtype=float)

    ma5  = close.rolling(5).mean().iloc[-1]
    ma10 = close.rolling(10).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]

    # MA 多头排列
    ma_score = 0.0
    if ma5 > ma10 > ma20:
        ma_score = 1.0
    elif ma5 > ma20:
        ma_score = 0.6
    elif ma5 < ma20:
        ma_score = 0.2

    # RSI
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean().iloc[-1]
    loss  = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
    rsi   = gain / (gain + loss + 1e-9) if (gain + loss) > 0 else 0.5
    rsi_score = float(rsi)

    # 5日动量
    mom = (close.iloc[-1] / close.iloc[-5] - 1) if len(close) >= 5 else 0
    mom_score = float(np.clip((mom + 0.05) / 0.10, 0, 1))

    return round(float(0.4 * ma_score + 0.3 * rsi_score + 0.3 * mom_score), 4)


class KronosPredictor:
    """
    Kronos 基础模型推理接口。
    懒加载：第一次调用 score() / predict_batch() 时初始化模型。
    Kronos 不可用时无缝降级为技术指标评分。
    """

    def __init__(self, device: str = "cpu"):
        self._device  = device
        self._model   = None
        self._loaded  = False

    def _load_model(self):
        if self._loaded:
            return
        self._loaded = True
        if not _check_kronos():
            return
        try:
            from kronos import load_model
            self._model = load_model(device=self._device)
            logger.info("[Kronos] 模型加载完成")
        except Exception as e:
            logger.warning(f"[Kronos] 模型加载失败: {e}，降级为技术指标评分")
            self._model = None

    def _df_to_tensor(self, df: pd.DataFrame):
        """将 K 线 DataFrame 转为 Kronos 所需的输入张量。"""
        try:
            import torch
            cols  = ["open", "high", "low", "close", "volume"]
            data  = df[[c for c in cols if c in df.columns]].values.astype(np.float32)
            # Kronos 期望 (batch=1, seq_len, features)
            tensor = torch.tensor(data).unsqueeze(0).to(self._device)
            return tensor
        except Exception as e:
            logger.debug(f"[Kronos] tensor 转换失败: {e}")
            return None

    def score(self, code: str, df: pd.DataFrame) -> float:
        """
        对单只股票评分 (0~1, 越高越看涨)。
        Kronos 不可用 → 技术指标降级评分。
        """
        self._load_model()
        if self._model is None:
            return _fallback_score(df)
        try:
            inp = self._df_to_tensor(df)
            if inp is None:
                return _fallback_score(df)
            import torch
            with torch.no_grad():
                out = self._model(inp)
            # Kronos 输出 shape: (batch, 1) 或 (batch, seq, 1)
            if hasattr(out, "squeeze"):
                val = float(out.squeeze()[-1]) if out.ndim > 1 else float(out.squeeze())
            else:
                val = float(out)
            # 归一化到 0~1（Kronos 输出可能是 logit 或直接概率）
            import math
            val = 1.0 / (1.0 + math.exp(-val)) if abs(val) > 1 else val
            return round(max(0.0, min(1.0, val)), 4)
        except Exception as e:
            logger.debug(f"[Kronos] score({code}) 失败: {e}")
            return _fallback_score(df)

    def predict_batch(self, code_df_pairs: list[tuple[str, pd.DataFrame]]) -> list[dict]:
        """
        批量评分。
        输入: [(code, df), ...]
        输出: [{"code": str, "score": float}, ...]
        """
        self._load_model()
        results = []
        for code, df in code_df_pairs:
            s = self.score(code, df)
            results.append({"code": code, "score": s})
        return results


# 全局单例（懒加载）
_predictor: Optional[KronosPredictor] = None


def get_predictor() -> KronosPredictor:
    global _predictor
    if _predictor is None:
        _predictor = KronosPredictor()
    return _predictor
