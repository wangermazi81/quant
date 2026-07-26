#!/usr/bin/env python3
"""生成 static/app/js/ui-handlers.gen.js —— data-ui-* 复杂处理器的静态映射表。

背景
----
`ui-events.js` 不再用 `new Function` 执行 data-ui-* 属性里的源码（那需要 CSP 放行
'unsafe-eval'，且 CSP 一收紧全站点击层就静默失效）。它改为两级求值：

  ① 安全调用解析器 —— 处理 `fn(a,'b',this)` 这类调用（覆盖绝大多数）；
  ② 本脚本生成的静态映射表 —— 处理 HTML 里那些带 if/赋值/比较等复杂语法的
     **固定字符串**（不含 ${} 插值，因此键是稳定的）。

生成物是**真实 JS 源码**（由浏览器正常解析），不是运行时 eval。
函数体逐字复制原属性内容，因此行为与原来完全一致。

用法
----
    python tools/gen_ui_handlers.py          # 重新生成
    python tools/gen_ui_handlers.py --check  # 只校验是否为最新（CI 用，不写文件）

改动任何 HTML/JS 里的 data-ui-* 复杂处理器后，都要重跑本脚本。
"""
from __future__ import annotations
import html
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "app" / "js" / "ui-handlers.gen.js"
SOURCES = [ROOT / "index.html", ROOT / "admin.html"] + sorted((ROOT / "static" / "app" / "js").glob("*.js"))

ATTR = re.compile(
    r'data-ui-(?:click|change|input|keydown|keyup|blur|focus|submit|contextmenu)'
    r'\s*=\s*(["\'])(.*?)\1',
    re.S,
)

# 与 ui-events.js 的安全解析器保持一致的可解析性判定
CALL = re.compile(r'^\s*([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(([\s\S]*)\)\s*$')
ARG_OK = re.compile(
    r'''^\s*(?:
        "(?:[^"\\]|\\.)*"      |
        '(?:[^'\\]|\\.)*'      |
        -?\d+(?:\.\d+)?        |
        true|false|null|undefined |
        this|event             |
        [A-Za-z_$][\w$]*
    )\s*$''',
    re.X | re.S,
)
INTERP = re.compile(r'\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}')


def split_top(s: str, sep: str) -> list[str]:
    out, depth, i, cur, q = [], 0, 0, [], None
    while i < len(s):
        c = s[i]
        if q:
            cur.append(c)
            if c == "\\" and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in "\"'":
            q = c
            cur.append(c)
            i += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == sep and depth == 0:
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def parsable(code: str) -> bool:
    """能否被 ui-events.js 的安全调用解析器处理"""
    stmts = [s.strip() for s in split_top(code, ";") if s.strip()]
    if not stmts:
        return False
    for st in stmts:
        m = CALL.match(st)
        if not m:
            return False
        args = m.group(2).strip()
        if args and not all(ARG_OK.match(a) for a in split_top(args, ",")):
            return False
    return True


def collect() -> list[str]:
    """收集需要进静态映射表的处理器：非插值 且 安全解析器处理不了。

    注意：写在 .js 模板字符串里的属性带 JS 层转义（如 \\' ），运行时才会解开。
    静态表的键必须等于**运行时真实的属性值**，所以这里要先把这层转义还原。
    """
    found: dict[str, None] = {}
    for f in SOURCES:
        if f.name == "ui-handlers.gen.js":
            continue
        is_js = f.suffix == ".js"
        for m in ATTR.finditer(f.read_text(encoding="utf-8", errors="replace")):
            code = html.unescape(m.group(2)).strip()
            if is_js:
                code = code.replace("\\'", "'").replace('\\"', '"')
            if not code or INTERP.search(code):
                continue          # 带插值的键不稳定，必须让解析器处理
            if parsable(code):
                continue          # 解析器能处理，不用进表
            found[code] = None
    return sorted(found)


def render(codes: list[str]) -> str:
    lines = [
        "// ⚠️ 本文件由 tools/gen_ui_handlers.py 自动生成，请勿手工编辑。",
        "// 作用：把 data-ui-* 里那些含 if / 赋值 / 比较等复杂语法的固定处理器，",
        "//      预编译成真实函数，使 ui-events.js 无需 eval（CSP 可去掉 'unsafe-eval'）。",
        "// 函数体逐字复制自原属性，行为与改造前完全一致。",
        "// 改动这类处理器后请重跑：python tools/gen_ui_handlers.py",
        ";(function(){",
        "  window.__UI_HANDLERS = {",
    ]
    for c in codes:
        # 键 = 原字符串；值 = 以该字符串为函数体的真实函数
        lines.append(f"    {json.dumps(c, ensure_ascii=False)}: function(event){{ {c} }},")
    lines += ["  }", "})()", ""]
    return "\n".join(lines)


def main() -> int:
    codes = collect()
    content = render(codes)
    check = "--check" in sys.argv
    old = OUT.read_text(encoding="utf-8") if OUT.exists() else None
    if check:
        if old != content:
            print("ui-handlers.gen.js 已过期，请运行：python tools/gen_ui_handlers.py")
            return 1
        print(f"ui-handlers.gen.js 是最新的（{len(codes)} 条）")
        return 0
    OUT.write_text(content, encoding="utf-8")
    print(f"已生成 {OUT.relative_to(ROOT)}：{len(codes)} 条复杂处理器")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
