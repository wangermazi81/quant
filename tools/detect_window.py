# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from pywinauto import Desktop
import traceback

def print_tree(elem, depth=0, max_depth=4):
    if depth > max_depth:
        return
    try:
        ctrl_type = getattr(elem.element_info, 'control_type', '?')
        name = getattr(elem.element_info, 'name', '') or ""
        cls = getattr(elem.element_info, 'class_name', '') or ""
        auto_id = getattr(elem.element_info, 'automation_id', '') or ""
        try:
            rect = elem.element_info.rectangle
        except Exception:
            rect = "?"
        indent = "  " * depth
        print(f"{indent}[{ctrl_type}] name='{name}' class='{cls}' auto_id='{auto_id}' rect={rect}")
    except Exception as e:
        print(f"{'  '*depth}(读取失败: {e})")
        return
    try:
        children = elem.children()
        for child in children[:20]:  # 每层最多20个
            print_tree(child, depth + 1, max_depth)
    except Exception as e:
        pass

# UIA backend
d = Desktop(backend="uia")
ths_win = None
for w in d.windows():
    try:
        title = w.window_text()
        if "同花顺" in title:
            ths_win = w
            print(f"找到窗口: [{title}]")
            print(f"类名: {w.class_name()}")
            print(f"句柄: {w.handle}")
            print()
            break
    except Exception:
        pass

if ths_win is None:
    print("ERROR: 未找到同花顺窗口")
    sys.exit(1)

print("========== UIA 控件树 (depth<=4) ==========")
try:
    print_tree(ths_win, 0, 4)
except Exception as e:
    print(f"遍历出错: {e}")
    traceback.print_exc()

# Win32 backend
print("\n========== Win32 子窗口 ==========")
try:
    d32 = Desktop(backend="win32")
    for w in d32.windows():
        try:
            if "同花顺" in w.window_text():
                w.print_control_identifiers(depth=3)
                break
        except Exception:
            pass
except Exception as e:
    print(f"win32 探测失败: {e}")
