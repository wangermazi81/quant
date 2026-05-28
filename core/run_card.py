# -*- coding: utf-8 -*-
"""
Run Card — 为每次回测 / 组合优化 / 因子筛选产出可复现快照。

设计目标：3 个月后想复现某次实验，能找到完整的输入、输出、环境、git commit。
设计原则：失败安全 —— 写盘异常绝不冒泡，业务代码该返回什么还返回什么。

用法：
    from run_card import RunCard

    card = RunCard.start("portfolio_optimization", inputs={
        "codes": codes, "mode": mode, "capital": capital, "lookback": lookback,
    })
    result = do_work()
    card.finish(outputs=result, status="ok" if result.get("ok") else "error")

落地：
    logs/runs/YYYYMMDD-HHMMSS-<task_slug>/run_card.json
    logs/runs/YYYYMMDD-HHMMSS-<task_slug>/run_card.md
"""
import json
import logging
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 项目根 = ths_auto_trader/
_ROOT = Path(__file__).resolve().parent.parent
_RUNS_DIR = _ROOT / "logs" / "runs"


def _safe_slug(s: str) -> str:
    """文件名 slug：只留 ASCII 字母数字、下划线、短横线。"""
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", s).strip("_")
    return s[:40] or "task"


def _git_commit() -> Optional[str]:
    """读取 git 短 hash；非 git 仓库或 git 不可用时返回 None。"""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(_ROOT), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode("ascii", errors="ignore").strip()
    except Exception:
        return None


def _to_jsonable(obj: Any) -> Any:
    """把常见非 JSON 类型转成可序列化形式（numpy / pandas / 其它）。"""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    # numpy
    try:
        import numpy as np
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.generic):
            return obj.item()
    except ImportError:
        pass
    # pandas
    try:
        import pandas as pd
        if isinstance(obj, pd.DataFrame):
            return obj.head(50).to_dict(orient="records")  # 限 50 行避免快照爆炸
        if isinstance(obj, pd.Series):
            return obj.head(50).to_dict()
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
    except ImportError:
        pass
    # 容器
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    # 兜底
    try:
        json.dumps(obj)
        return obj
    except Exception:
        return str(obj)


class RunCard:
    """单次任务的可复现快照。"""

    def __init__(self, task: str, inputs: dict):
        self.task = task
        self.inputs = inputs or {}
        self.started_at = datetime.now()
        self._t0 = time.perf_counter()
        self.dir: Optional[Path] = None

        slug = _safe_slug(task)
        stamp = self.started_at.strftime("%Y%m%d-%H%M%S")
        try:
            self.dir = _RUNS_DIR / f"{stamp}-{slug}"
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[run_card] 创建目录失败 {self.dir}: {e}")
            self.dir = None

    @classmethod
    def start(cls, task: str, inputs: Optional[dict] = None) -> "RunCard":
        return cls(task, inputs or {})

    def finish(
        self,
        outputs: Any = None,
        status: str = "ok",
        note: str = "",
    ) -> Optional[Path]:
        """写入 run_card.json + run_card.md。返回目录路径或 None（写盘失败）。"""
        if self.dir is None:
            return None
        finished_at = datetime.now()
        duration_ms = int((time.perf_counter() - self._t0) * 1000)
        payload = {
            "task":        self.task,
            "status":      status,
            "started_at":  self.started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "duration_ms": duration_ms,
            "inputs":      _to_jsonable(self.inputs),
            "outputs":     _to_jsonable(outputs),
            "note":        note,
            "env": {
                "python":     sys.version.split()[0],
                "git_commit": _git_commit(),
            },
        }
        try:
            (self.dir / "run_card.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (self.dir / "run_card.md").write_text(
                _render_md(payload),
                encoding="utf-8",
            )
            return self.dir
        except Exception as e:
            logger.warning(f"[run_card] 写盘失败 {self.dir}: {e}")
            return None


def _render_md(p: dict) -> str:
    """人类可读的 Markdown 快照。"""
    lines = [
        f"# Run Card: {p['task']}",
        "",
        f"- **Status**: `{p['status']}`",
        f"- **Started**: {p['started_at']}",
        f"- **Finished**: {p['finished_at']}",
        f"- **Duration**: {p['duration_ms']} ms",
        f"- **Python**: {p['env']['python']}",
        f"- **Git**: {p['env']['git_commit'] or 'N/A'}",
        "",
        "## Inputs",
        "```json",
        json.dumps(p["inputs"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Outputs",
        "```json",
        json.dumps(p["outputs"], ensure_ascii=False, indent=2),
        "```",
    ]
    if p.get("note"):
        lines += ["", "## Note", "", p["note"]]
    return "\n".join(lines) + "\n"


# 自检：直接运行此文件可验证写盘正常
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    c = RunCard.start("self_test", inputs={"hello": "world", "n": 3})
    out = {"ok": True, "items": [1, 2, 3], "value": 3.14}
    p = c.finish(outputs=out, note="smoke test")
    print(f"[ok] wrote: {p}")
