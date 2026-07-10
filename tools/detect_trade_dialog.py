# -*- coding: utf-8 -*-
"""探测同花顺交易弹窗的控件结构"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from pywinauto import Desktop

def print_tree(elem, depth=0, max_depth=6):
    if depth > max_depth:
        return
    try:
        ctrl_type = getattr(elem.element_info, 'control_type', '?')
        name = getattr(elem.element_info, 'name', '') or ""
        cls = getattr(elem.element_info, 'class_name', '') or ""
        auto_id = getattr(elem.element_info, 'automation_id', '') or ""
        try:
            rect = elem.element_info.rectangle
        except:
            rect = "?"
        indent = "  " * depth
        print(f"{indent}[{ctrl_type}] name='{name}' class='{cls}' auto_id='{auto_id}' rect={rect}")
    except Exception as e:
        return
    try:
        children = elem.children()
        for child in children[:30]:
            print_tree(child, depth + 1, max_depth)
    except:
        pass

d = Desktop(backend="uia")

print("当前所有顶层窗口:")
all_wins = []
for w in d.windows():
    try:
        title = w.window_text()
        cls = w.class_name()
        if title:
            all_wins.append((title, cls, w.handle))
            print(f"  [{title}] class={cls} handle={w.handle}")
    except:
        pass

print("\n同花顺主窗口结构:")
for w in d.windows():
    try:
        if "同花顺" in w.window_text():
            print_tree(w, 0, 3)
            break
    except:
        pass

# Win32 backend
print("\n========== Win32 Backend ==========")
d32 = Desktop(backend="win32")
for w in d32.windows():
    try:
        title = w.window_text()
        if "同花顺" in title or "买入" in title or "卖出" in title or "委托" in title:
            print(f"\n窗口: [{title}] class={w.class_name()}")
            try:
                w.print_control_identifiers(depth=4)
            except Exception as e:
                print(f"  (print_control_identifiers失败: {e})")
    except:
        pass
