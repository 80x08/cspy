"""Runs inside CLIP STUDIO PAINT. Starts a main-thread job runner and a local Python console.

Console: `python cspy/console.py "<code>"` (or no args for interactive). Code runs on CSP's UI thread.
"""
import contextlib
import ctypes
import io
import os
import queue
import socket
import threading
import time
import traceback
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HOOKPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = wintypes.LPARAM
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

WH_GETMESSAGE = 3
WM_NULL = 0
PORT = 50555  # localhost only

_jobs = queue.Queue()
namespace = {"__name__": "__cspy__"}


def windows_by_thread():
    """{tid: [(area, class, title, visible), ...]} for this process's top-level windows."""
    pid, out = os.getpid(), {}

    def cb(hwnd, _):
        p = wintypes.DWORD()
        tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            r, cls, title = wintypes.RECT(), ctypes.create_unicode_buffer(128), ctypes.create_unicode_buffer(128)
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            user32.GetClassNameW(hwnd, cls, 128)
            user32.GetWindowTextW(hwnd, title, 128)
            area = (r.right - r.left) * (r.bottom - r.top)
            out.setdefault(tid, []).append((area, cls.value, title.value, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]


kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.OpenThread.restype = wintypes.HANDLE


def first_thread_id():
    """Earliest-created thread of this process (the exe's main thread)."""
    pid, best = os.getpid(), (None, 0)
    snap = kernel32.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
    te = THREADENTRY32(dwSize=ctypes.sizeof(THREADENTRY32))
    ok = kernel32.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            h = kernel32.OpenThread(0x0800, False, te.th32ThreadID)  # THREAD_QUERY_LIMITED_INFORMATION
            times = [wintypes.FILETIME() for _ in range(4)]
            if h and kernel32.GetThreadTimes(h, *[ctypes.byref(t) for t in times]):
                created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                if best[0] is None or created < best[0]:
                    best = (created, te.th32ThreadID)
            if h:
                kernel32.CloseHandle(h)
        ok = kernel32.Thread32Next(snap, ctypes.byref(te))
    kernel32.CloseHandle(snap)
    return best[1]


def main_thread_id():
    """The UI thread: the exe's main thread if it owns windows, else the owner of the largest visible window."""
    wins = windows_by_thread()
    first = first_thread_id()
    if first in wins:
        return first
    visible = [(a, tid) for tid, ws in wins.items() for a, _, _, v in ws if v]
    return max(visible)[1] if visible else 0


def _run_jobs():
    while True:
        try:
            fn, done = _jobs.get_nowait()
        except queue.Empty:
            return
        try:
            done.result = fn()
        except BaseException as e:  # never let an exception unwind into CSP's message loop
            done.error = e
        done.set()


hook_calls = 0


@HOOKPROC
def _hook(code, wparam, lparam):
    global hook_calls
    hook_calls += 1
    if code >= 0:
        _run_jobs()
    return user32.CallNextHookEx(None, code, wparam, lparam)


def later(fn):
    """Queue fn() on the UI thread without waiting; safe to call from inside CSP callbacks."""
    done = threading.Event()
    done.result = done.error = None
    _jobs.put((fn, done))
    user32.PostThreadMessageW(UI_TID, WM_NULL, 0, 0)


def on_main(fn, timeout=30):
    """Run fn() on CSP's UI thread and return its result."""
    done = threading.Event()
    done.result = done.error = None
    _jobs.put((fn, done))
    user32.PostThreadMessageW(UI_TID, WM_NULL, 0, 0)  # wake the message loop
    if not done.wait(timeout):
        raise TimeoutError(f"UI thread {UI_TID} did not pick up the job (hook_calls={hook_calls}); "
                           "try `python console.py !diag()`")
    if done.error:
        raise done.error
    return done.result


def _exec(code):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            try:
                expr = compile(code, "<console>", "eval")
            except SyntaxError:  # statements: run them below, no echo
                expr = None
            if expr is None:
                exec(compile(code, "<console>", "exec"), namespace)
            else:
                r = eval(expr, namespace)
                if r is not None:
                    print(repr(r))
        except BaseException:
            traceback.print_exc()
    return out.getvalue()


def _serve():
    # Windows removes a hook when its installing thread exits, so this long-lived thread owns it.
    global _hook_handle
    _hook_handle = user32.SetWindowsHookExW(WH_GETMESSAGE, _hook, None, UI_TID)
    _hook_err = ctypes.get_last_error()
    _hook_ready.set()
    if not _hook_handle:
        open(os.path.join(os.path.dirname(__file__), "cspy_error.log"), "w").write(
            f"SetWindowsHookExW failed: {_hook_err}\n")
        return
    srv = socket.create_server(("127.0.0.1", PORT))
    while True:
        conn, _ = srv.accept()
        with conn:
            try:
                _handle(conn)
            except OSError:
                pass  # the client went away (killed, timed out): keep serving


def _handle(conn):
    data = b""
    while not data.endswith(b"\0"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    code = data.rstrip(b"\0").decode()
    try:
        if code.startswith("!"):  # run on this console thread: for diagnostics, never touch CSP objects
            reply = _exec(code[1:])
        else:
            reply = on_main(lambda: _exec(code))
    except BaseException:
        reply = traceback.format_exc()
    conn.sendall(reply.encode() + b"\0")


class _NoMenu:
    status = "not loaded"


# Load the CSP API and patch the menu code now, while CSP is still loading plugins: then the first menu bar CSP
# sets already gets our Scripts entry, with no slow heap scan. Failures keep the console up for diagnosis.
csp = csp_error = None
menu = _NoMenu()
try:
    import csp  # noqa: E402  (CSP API lives here; reload with importlib.reload(csp))
except Exception:
    csp_error = traceback.format_exc()
if csp and not os.environ.get("CSPY_NO_MENU"):  # set CSPY_NO_MENU=1 to skip the Scripts menu
    try:
        import menu  # noqa: E402
        menu.hook()
    except Exception:
        menu = _NoMenu()
        _NoMenu.status = "menu.py failed:\n" + traceback.format_exc()

# As a PlugIn\PAINT plugin we load during CSP startup, before its windows exist: wait for them.
for _ in range(1200):
    UI_TID = main_thread_id()
    if UI_TID:
        break
    time.sleep(0.5)
assert UI_TID, "CSP main window not found after 10 minutes"
_hook_handle = 0
_hook_ready = threading.Event()


def diag():
    """Which thread we hooked vs. which threads own windows."""
    print(f"UI_TID={UI_TID} first_thread={first_thread_id()} hook_calls={hook_calls} hook={_hook_handle:#x}")
    if csp_error:
        print("csp.py failed to load:\n" + csp_error)
    print("menu:", menu.status)
    for tid, ws in sorted(windows_by_thread().items()):
        for area, cls, title, vis in sorted(ws, reverse=True)[:5]:
            print(f"  tid={tid}{' <-hooked' if tid == UI_TID else ''} vis={vis} area={area} class={cls!r} title={title!r}")


namespace.update(csp=csp, menu=menu, on_main=on_main, diag=diag)
threading.Thread(target=_serve, daemon=True, name="cspy-console").start()
_hook_ready.wait(5)


def _menu_fallback():
    """If CSP set its menu before we patched (e.g. injected late), find the window off the UI thread and add it."""
    time.sleep(20)
    for _ in range(60):
        if menu.status.startswith("installed"):
            return
        try:
            wins = menu.find_windows()  # heap scan: seconds, but safe here
            if wins and on_main(lambda: menu.install(wins)):
                return
        except Exception:
            menu.status = "menu install failed:\n" + traceback.format_exc()
            return
        time.sleep(10)


if not isinstance(menu, _NoMenu):
    threading.Thread(target=_menu_fallback, daemon=True, name="cspy-menu").start()
