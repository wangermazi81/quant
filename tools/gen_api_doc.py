# -*- coding: utf-8 -*-
"""生成 docs/API.md：从 app.url_map 自动导出全部路由（工单8）。

用法（项目根目录）：
    QUANT_TEST_IMPORT=1 python tools/gen_api_doc.py

CI 在每次 push 重新生成并 `git diff --exit-code docs/API.md`——
文档与代码不同步即红，从根上防接口文档过时。
输出确定有序（按蓝图名/路径排序、无时间戳），保证可重复生成零 diff。
"""
import os
import sys
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("QUANT_TEST_IMPORT", "1")
os.environ.setdefault("THS_NO_SCHEDULER", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402

BP_LABEL = {
    "main": "main（app 组装层，正常应无业务路由）",
    "auth": "认证", "user": "用户自助", "admin": "管理后台", "pages": "页面/静态",
    "market": "市场概览", "data": "行情数据", "ai": "AI 分析", "search": "搜索/资讯",
    "notify": "通知", "feedback": "问题反馈", "review": "复盘/新闻",
    "research": "产业链投研", "lhb": "龙虎榜", "ai_trader": "AI 自主交易",
    "bridge": "实盘桥接", "trade": "监控/交易/账户", "paper": "模拟盘",
    "strategies": "策略中心", "factors": "因子库/因子动物园",
    "misc": "健康/回测/组合优化/Vibe激活",
}


def build() -> str:
    groups = defaultdict(list)
    for rule in main.app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        bp = rule.endpoint.split(".")[0] if "." in rule.endpoint else "main"
        view = main.app.view_functions[rule.endpoint]
        doc = (view.__doc__ or "").strip().splitlines()
        summary = (doc[0].strip() if doc else "").replace("|", "\\|")
        methods = ",".join(sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS")))
        groups[bp].append((str(rule.rule), methods, rule.endpoint, summary))

    total = sum(len(v) for v in groups.values())
    lines = [
        "# API 清单（自动生成，勿手改）",
        "",
        "> 由 `tools/gen_api_doc.py` 从 `app.url_map` 导出，CI 校验与代码同步。",
        f"> 共 {total} 条路由，按蓝图分组。",
        "",
    ]
    for bp in sorted(groups):
        lines += [f"## {bp} — {BP_LABEL.get(bp, '')}（{len(groups[bp])} 条）", "",
                  "| 路径 | 方法 | 端点 | 说明 |", "|---|---|---|---|"]
        for rule, methods, ep, summary in sorted(groups[bp]):
            lines.append(f"| `{rule}` | {methods} | {ep} | {summary} |")
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    out = ROOT / "docs" / "API.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(), encoding="utf-8", newline="")
    print(f"已生成 {out}")
