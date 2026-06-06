# -*- coding: utf-8 -*-
"""
一次性脚本：把 legacy 单文件 index.html 的内联 <style>/<script> 块无损抽取为外部文件，
生成 Vite 项目的入口 frontend/index.html。

设计原则（保功能对等、风险最低）：
- 每个内联 <script> 块按【原文档位置、原顺序】各自外联为一个经典脚本（不是 type=module），
  因此顶层 `function foo(){}` 仍是全局函数，inline onclick="foo()" 行为零变化。
- <style> 块按原位各自外联为 css。
- 不改任何 JS/CSS 字节，只搬位置。

源： ../index.html （相对本脚本：ths_auto_trader/index.html）
出： frontend/public/app/css/*.css, frontend/public/app/js/*.js, frontend/index.html
"""
import re, os, shutil

HERE      = os.path.dirname(os.path.abspath(__file__))          # frontend/scripts
FRONTEND  = os.path.dirname(HERE)                               # frontend
THS       = os.path.dirname(FRONTEND)                           # ths_auto_trader
SRC_HTML  = os.path.join(THS, "index.html")
PUB       = os.path.join(FRONTEND, "public")
PUB_CSS   = os.path.join(PUB, "app", "css")
PUB_JS    = os.path.join(PUB, "app", "js")
PUB_LIB   = os.path.join(PUB, "lib")
LIB_SRC   = os.path.join(THS, "static", "lib")

for d in (PUB_CSS, PUB_JS, PUB_LIB):
    os.makedirs(d, exist_ok=True)

html = open(SRC_HTML, encoding="utf-8").read()

# ── 抽取 <style> 块（仅裸 <style>，head 里的 <link> 不动）──
styles = []
def _grab_style(m):
    styles.append(m.group(1))
    idx = len(styles)
    name = {1: "app-1.css", 2: "app-2.css", 3: "feedback.css"}.get(idx, f"style-{idx}.css")
    return f'<link rel="stylesheet" href="/app/css/{name}">'
html = re.sub(r"<style>(.*?)</style>", _grab_style, html, flags=re.S)

# ── 抽取内联 <script> 块（裸 <script>；带 src= 的 lib 脚本不匹配，保持不动）──
scripts = []
def _grab_script(m):
    scripts.append(m.group(1))
    idx = len(scripts)
    name = {1:"app-1.js",2:"app-2.js",3:"app-3.js",4:"app-4.js",5:"app-5.js",6:"feedback.js"}.get(idx, f"script-{idx}.js")
    return f'<script src="/app/js/{name}"></script>'
html = re.sub(r"<script>(.*?)</script>", _grab_script, html, flags=re.S)

# ── 把 head 里 vendored 库引用从 /static/lib 改为 /lib（迁到 public/lib，避免 Vite base 重写冲突）──
html = html.replace("/static/lib/", "/lib/")

# ── 落盘 css/js ──
css_names = ["app-1.css","app-2.css","feedback.css"]
for nm, content in zip(css_names, styles):
    open(os.path.join(PUB_CSS, nm), "w", encoding="utf-8").write(content)
js_names = ["app-1.js","app-2.js","app-3.js","app-4.js","app-5.js","feedback.js"]
for nm, content in zip(js_names, scripts):
    open(os.path.join(PUB_JS, nm), "w", encoding="utf-8").write(content)

# ── 拷贝 vendored 库到 public/lib ──
copied = 0
if os.path.isdir(LIB_SRC):
    for f in os.listdir(LIB_SRC):
        s = os.path.join(LIB_SRC, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(PUB_LIB, f)); copied += 1

open(os.path.join(FRONTEND, "index.html"), "w", encoding="utf-8").write(html)

print(f"styles 抽取={len(styles)}  scripts 抽取={len(scripts)}  lib 拷贝={copied}")
print("css:", css_names[:len(styles)])
print("js :", js_names[:len(scripts)])
print("frontend/index.html 字节=", len(html.encode("utf-8")))
