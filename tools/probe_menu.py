# Diagnostics: run with  .\python\python.exe console.py <path>\probe_menu.py  then open CSP's File menu within 8 s.
# Writes cspy\probe.txt: every window of CSP (with children) and the items of any open native popup menu.
import ctypes
import os
import threading
import time
from ctypes import wintypes

u = ctypes.WinDLL("user32")
u.GetMenu.restype = u.GetParent.restype = wintypes.HWND
u.SendMessageW.restype = ctypes.c_void_p
u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u.GetMenuStringW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
u.GetMenuItemCount.argtypes = [ctypes.c_void_p]
PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
OUT = os.path.join(os.path.dirname(__file__) if "__file__" in globals() else ".", "probe.txt")


def desc(h):
    cls, title, r = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256), wintypes.RECT()
    u.GetClassNameW(h, cls, 256)
    u.GetWindowTextW(h, title, 256)
    u.GetWindowRect(h, ctypes.byref(r))
    depth, p = 0, u.GetParent(h)
    while p:
        depth, p = depth + 1, u.GetParent(p)
    line = (f"{'  ' * depth}{h:#x} cls={cls.value!r} title={title.value!r} vis={bool(u.IsWindowVisible(h))} "
            f"rect=({r.left},{r.top},{r.right},{r.bottom}) menu={u.GetMenu(h) or 0:#x}")
    if cls.value == "#32768":  # native popup menu window: list its items
        m = u.SendMessageW(h, 0x01E1, 0, 0)  # MN_GETHMENU
        buf = ctypes.create_unicode_buffer(128)
        items = [(u.GetMenuStringW(m, i, buf, 128, 0x400), buf.value)[1] for i in range(u.GetMenuItemCount(m))] if m else []
        line += f" hmenu={m or 0:#x} items={items}"
    return line


def job():
    time.sleep(8)
    pid, lines = os.getpid(), []

    def child(h, _):
        lines.append(desc(h))
        return True

    def top(h, _):
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid:
            lines.append(desc(h))
            u.EnumChildWindows(h, PROC(child), 0)
        return True

    u.EnumWindows(PROC(top), 0)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


threading.Thread(target=job, daemon=True).start()
print(f"open the File menu now; writing {OUT} in 8 s")
