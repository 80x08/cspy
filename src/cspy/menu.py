"""A "Scripts" entry in CSP's own (custom-drawn) main menu. See RE_NOTES.md "Main menu".

How it works:
- The menu bar is built from a PWMainMenu model (window+0x98). We append a PWMenuItem "Scripts" with a
  submenu of script items, then re-apply the model with CSP's setter (141247490) so the bar rebuilds.
- On Windows a menu click calls PWView::ExecuteCommand 1411a8100(view, item+0x64, item+0x68).
  We give our items a command number there and detour 1411a8100: ours run scripts, the rest go on.
- 141247490 is detoured too, so a menu CSP sets later (new canvas, workspace) gets our entry as well.
All of this must run on CSP's UI thread. Self-check of the detour code: tools/test_detour.py.
"""
import contextlib
import ctypes
import io
import os
import traceback
from ctypes import c_int16, c_int32, c_size_t, c_uint32, c_uint64, c_void_p, wintypes

import csp
import mem
import startup
from mem import read, read_u64, rtti_name, va

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
TITLE = "Scripts"

VT_MAINMENU, VT_MENU, VT_ITEM = va(0x14334B158), va(0x143348468), va(0x143349D38)
_new = ctypes.CFUNCTYPE(c_void_p, c_size_t)(va(0x142A32EB4))  # CSP's operator new
_item_ctor = ctypes.CFUNCTYPE(c_void_p, c_void_p)(va(0x141235120))
_item_sp = ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p)(va(0x14120A380))  # (ctrl* out, item) -> sp ctrl
_menu_sp = ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p)(va(0x1401AA2A0))  # (ctrl* out, menu) -> sp ctrl
_vec_insert = ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p, c_void_p)(va(0x1401AAF30))  # vector<sp>, where, sp*
_pws_assign = ctypes.CFUNCTYPE(None, c_void_p, c_void_p)(va(0x1417FBF30))
_set_main_menu = ctypes.CFUNCTYPE(None, c_void_p, c_void_p)(va(0x141247490))  # (window, sp<PWMainMenu> by value)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.VirtualProtect.argtypes = [c_void_p, c_size_t, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.FlushInstructionCache.argtypes = [wintypes.HANDLE, c_void_p, c_size_t]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.FindWindowW.restype = wintypes.HWND

status = "not installed"
# First item id / command number we own. CSP's item ids are 0xa0xxx and its commands interned 0x10000xxx.
_base = 0x7C5A0000
_actions = {}  # command id -> callable
_hooked = False
_fresh = set()  # PWMainMenus whose Scripts submenu is current
_wins = set()  # windows CSP set a menu on (seen by the 141247490 detour or found by install)


# ---------- reading the model
def items(menu):
    """Item pointers of a PWMenu (vector<sp<PWMenuItem>> at +8)."""
    return [ptr for ptr, _ in mem.sp_vector(menu + 8)]


def info(item):
    return {"id": c_int32.from_address(item + 8).value, "title": csp._pws_read(item + 0x10),
            "flags": c_int16.from_address(item + 0x40).value, "ints": read(item + 0x68, 0x14).hex(),
            "sub": read_u64(item + 0xB0) or 0}


def windows(menus):
    """Window objects whose +0x98 shared_ptr points at one of `menus` (what 141247490 sets)."""
    out = []
    for m in menus:
        for ref in mem.find_objects(m):
            w, ctrl = ref - 0x98, read_u64(ref + 8)
            uses = read(ctrl + 8, 4) if ctrl else None
            if uses and int.from_bytes(uses, "little") > 0 and rtti_name(w) and read_u64(w + 0x58):
                out.append(w)
    return out


def main_menus():
    """Live PWMainMenus, i.e. ones a window holds. Stale copies in freed memory are skipped."""
    # shortcut: CSP's master copy (if it re-sets the menu, e.g. on workspace change) isn't patched;
    # use "Reload Scripts List" / menu.install() again, or find the master in 140281e10.
    found = [m for m in mem.find_objects(VT_MAINMENU) if rtti_name(m) == ".?AVPWMainMenu@Planeswalker@@"
             and read_u64(m + 8)]
    return [m for m in found if windows([m])]


def raw(item):
    """Hex of an item's fields after the vtable, for working out where the label text lives."""
    print(read(item + 8, 0xB8).hex(" ", 8))


def dump(depth=3):
    """Print every live main menu (ids, titles, flags) and the windows using it."""
    ms = main_menus()
    for m in ms:
        print(f"PWMainMenu {m:#x}  windows={[f'{w:#x} {rtti_name(w)}' for w in windows([m])]}")

        def walk(menu, d):
            for it in items(menu):
                i = info(it)
                print(f"{'  ' * d}{it:#x} id={i['id']:#x} {i['title']!r} flags={i['flags']:#x} ints={i['ints']}"
                      f"{' >' if i['sub'] else ''}")
                if i["sub"] and d + 1 < depth:
                    walk(i["sub"], d + 1)

        walk(read_u64(m + 8), 1)
    if not ms:
        print("no PWMainMenu found")


# ---------- building items
def _sp_ptr_ctrl(make, obj):
    ctrl = c_uint64()
    make(ctypes.addressof(ctrl), obj)
    return obj, ctrl.value


def _new_item(template, cmd_id, title, submenu=None, key=None):
    """New PWMenuItem; type fields copied from a template item. submenu: (ptr, ctrl), ownership taken.
    key: Alt-mnemonic letter (+0x40 holds it, e.g. 0x66 'f' for File)."""
    it = _new(0xC0)
    _item_ctor(it)
    c_int16.from_address(it + 0x40).value = ord((key or title[0]).lower())
    ctypes.memmove(it + 0x68, template + 0x68, 0x14)
    c_int32.from_address(it + 8).value = cmd_id
    # +0x48 is the item's command key (PWResourceKey); with no resource bundle (+0x50 == 0) its resolver
    # 1412fbac0 just returns +0x64. A click runs PWView::ExecuteCommand (1411a8100) with that number.
    c_int32.from_address(it + 0x64).value = cmd_id
    # +0x6c enabled: a popup row starts from it and only a view answering the command query overrides it.
    # Nothing answers ours, so it must be 1 (the template's 0 greys the item out). +0x78: template bit, clear.
    c_int32.from_address(it + 0x6C).value = 1
    c_int32.from_address(it + 0x78).value = 0
    s = csp._pws_new(title)
    try:
        _pws_assign(it + 0x10, ctypes.addressof(s))
    finally:
        csp._pws_free(ctypes.addressof(s))
    if submenu:
        c_uint64.from_address(it + 0xB0).value, c_uint64.from_address(it + 0xB8).value = submenu
    return _sp_ptr_ctrl(_item_sp, it)


def _new_menu(item_sps):
    m = _new(0x20)
    ctypes.memmove(m, (c_uint64 * 4)(VT_MENU, 0, 0, 0), 32)
    for sp in item_sps:
        _push(m + 8, sp)
    return _sp_ptr_ctrl(_menu_sp, m)


def _push(vec, sp):
    """Append sp to a vector<sp<T>>, handing over our reference."""
    tmp = (c_uint64 * 2)(*sp)
    _vec_insert(vec, read_u64(vec + 8) or 0, ctypes.addressof(tmp))  # copies (+1)
    csp._sp_release(ctypes.addressof(tmp))  # drop ours


WINDOW_ID = 0xA0153  # "Window"; Scripts goes right after it


def _templates(root):
    """(group submenu to add to, bar item, plain leaf item).
    Layout: root -> untitled groups -> bar items (File, Edit, ...) -> commands."""
    groups = [read_u64(g + 0xB0) for g in items(root) if read_u64(g + 0xB0)]
    target = next((g for g in groups if WINDOW_ID in [info(it)["id"] for it in items(g)]), groups[-1])
    top = items(groups[0])[0]  # File
    for it in items(read_u64(top + 0xB0)):
        i = info(it)
        if not i["sub"] and i["id"] and i["title"]:
            return target, top, it
    raise RuntimeError("no plain menu item found to use as template")


def _script_menu(leaf):
    """Fresh submenu: one item per scripts/*.py, then Reload / Open Folder."""
    _actions.clear()
    entries = []
    names = sorted(f for f in os.listdir(SCRIPTS) if f.endswith(".py")) if os.path.isdir(SCRIPTS) else []
    for n, name in enumerate(names[:200]):
        path = os.path.join(SCRIPTS, name)
        _actions[_base + 1 + n] = lambda p=path: run_script(p)
        entries.append(_new_item(leaf, _base + 1 + n, name[:-3]))
    _actions[_base + 250] = refresh
    entries.append(_new_item(leaf, _base + 250, "Reload Scripts List"))
    _actions[_base + 251] = lambda: (os.makedirs(SCRIPTS, exist_ok=True), os.startfile(SCRIPTS))
    entries.append(_new_item(leaf, _base + 251, "Open Scripts Folder"))
    return _new_menu(entries)


def _add(m, rebuild=False):
    """Put our "Scripts" entry into PWMainMenu m (or refresh its submenu if rebuild)."""
    root = read_u64(m + 8)
    group, top_t, leaf_t = _templates(root)
    ours = [it for it in items(group) if c_int32.from_address(it + 8).value == _base]
    if ours and not rebuild:
        return
    sub = _script_menu(leaf_t)
    if ours:  # swap in the new submenu, release the old one
        old = (c_uint64 * 2)(read_u64(ours[0] + 0xB0), read_u64(ours[0] + 0xB8))
        c_uint64.from_address(ours[0] + 0xB0).value, c_uint64.from_address(ours[0] + 0xB8).value = sub
        csp._sp_release(ctypes.addressof(old))
    else:
        _push(group + 8, _new_item(top_t, _base, TITLE, sub, key="r"))  # 's' is taken by Select


def _apply(wins, rebuild=False):
    """Add our entry to each window's main menu, then make the window rebuild its bar."""
    for w in wins:
        mm, ctrl = read_u64(w + 0x98), read_u64(w + 0xA0)
        if not mm:
            continue
        _add(mm, rebuild)
        # shortcut: non-atomic +1 on the menu's refcount; fine on the UI thread, which owns the menu.
        c_int32.from_address(ctrl + 8).value += 1
        sp = (c_uint64 * 2)(mm, ctrl)
        _set_main_menu(w, ctypes.addressof(sp))  # consumes sp


# ---------- click handling
def run_script(path):
    g = {"__name__": "__main__", "__file__": path, "csp": csp, "on_main": startup.on_main}
    out = io.StringIO()
    owner = user32.FindWindowW("742DEA58-ED6B-4402-BC11-20DFC6D08040", None)
    try:
        with open(path, encoding="utf-8") as f, contextlib.redirect_stdout(out):
            exec(compile(f.read(), path, "exec"), g)
    except BaseException:
        user32.MessageBoxW(owner, traceback.format_exc()[-3000:], f"cspy: {os.path.basename(path)} failed", 0x10)
        return
    if out.getvalue():
        user32.MessageBoxW(owner, out.getvalue()[-3000:], f"cspy: {os.path.basename(path)}", 0x40)


def _safe(action):
    _log("running action")
    try:
        action()
    except BaseException:
        user32.MessageBoxW(None, traceback.format_exc()[-3000:], "cspy: menu action failed", 0x10)


log = []  # recent (kind, id) for our ids, plus any error; console: menu.log


def _log(entry):
    log.append(entry)
    del log[:-50]


# ---------- detours: jmp from a CSP function into Python; the trampoline runs the original
kernel32.VirtualAlloc.restype = c_void_p
kernel32.VirtualAlloc.argtypes = [c_void_p, c_size_t, wintypes.DWORD, wintypes.DWORD]
JMP = b"\xff\x25\x00\x00\x00\x00"  # jmp [rip+0] ; followed by dq target
_keep = []  # callbacks/trampolines must stay alive while patched


def _write_code(addr, data):
    old = wintypes.DWORD()
    if not kernel32.VirtualProtect(addr, len(data), 0x40, ctypes.byref(old)):
        raise ctypes.WinError(ctypes.get_last_error())
    ctypes.memmove(addr, data, len(data))
    kernel32.VirtualProtect(addr, len(data), old.value, ctypes.byref(old))
    kernel32.FlushInstructionCache(kernel32.GetCurrentProcess(), addr, len(data))


def _detour(static_va, prologue_hex, functype, pyfunc):
    """Redirect static_va to pyfunc. prologue_hex: the whole, position-independent instructions (>= 14 bytes)
    moved into the trampoline. Returns (call_original, caller_slot): caller_slot holds the return address
    of the latest call (which CSP code called us)."""
    addr, prologue = va(static_va), bytes.fromhex(prologue_hex)
    if read(addr, len(prologue)) != prologue:
        raise RuntimeError(f"{static_va:#x} has unexpected bytes (already patched? restart CSP)")
    page = kernel32.VirtualAlloc(None, 0x1000, 0x3000, 0x40)  # MEM_COMMIT|RESERVE, RWX
    cb = functype(pyfunc)
    tramp, stub, slot = page, page + 0x40, page + 0x80
    ctypes.memmove(tramp, prologue + JMP + (addr + len(prologue)).to_bytes(8, "little"), len(prologue) + 14)
    # stub: mov rax,[rsp] ; mov [rip+slot],rax ; jmp [rip] -> cb
    disp = slot - (stub + 4 + 7)
    code = b"\x48\x8b\x04\x24" + b"\x48\x89\x05" + disp.to_bytes(4, "little", signed=True)
    code += JMP + ctypes.cast(cb, c_void_p).value.to_bytes(8, "little")
    ctypes.memmove(stub, code, len(code))
    _keep.append(cb)
    _write_code(addr, JMP + stub.to_bytes(8, "little") + b"\x90" * (len(prologue) - 14))
    return functype(tramp), slot


def _caller(slot):
    return hex(c_uint64.from_address(slot).value - mem.BASE + 0x140000000)


# Clicks: the popup calls PWView::ExecuteCommand(view, command, param) = 1411a8100 with the item's +0x64.
EXEC_T = ctypes.CFUNCTYPE(c_uint32, c_void_p, c_int32, c_int32)
SET_T = ctypes.CFUNCTYPE(None, c_void_p, c_void_p)
_orig = {}


def _on_exec(view, cmd, param):
    try:
        if cmd in _actions:
            _log(("exec", hex(cmd), _caller(_orig["exec"][1])))
            # Run after CSP finishes dispatching the click, not inside its menu code.
            startup.later(lambda a=_actions[cmd]: _safe(a))
            return 1
    except BaseException:  # never unwind into CSP
        _log(traceback.format_exc())
    return _orig["exec"][0](view, cmd, param)


def _on_update(menu_sp, view_sp):
    """141234a30(sp<PWMenu>, sp<view>): before a menu opens, CSP asks the views about each item's command
    and disables (item+0x6c = 0) any nobody answers. Nothing answers ours, so switch them back on."""
    m = read_u64(menu_sp) if menu_sp else 0
    _orig["update"][0](menu_sp, view_sp)  # consumes both shared_ptrs
    try:
        for it in items(m) if m else ():
            if c_int32.from_address(it + 8).value in _actions:
                c_int32.from_address(it + 0x6C).value = 1
    except BaseException:
        _log(traceback.format_exc())


def _on_set_menu(window, sp):
    """CSP (re)sets the menu bar (startup, new canvas, ...): add our entry to the incoming menu first."""
    global status
    try:
        mm = read_u64(sp) if sp else 0
        if mm:
            _add(mm, rebuild=mm not in _fresh)  # rebuild once after each refresh()
            _fresh.add(mm)
            _wins.add(window)
            status = f"installed, ids {_base:#x}+, last menu {mm:#x}"
    except BaseException:
        _log(traceback.format_exc())
    _orig["set"][0](window, sp)


def hook():
    """Patch CSP so every menu it sets gets our entry. Safe to call early, before the menu bar exists."""
    global _hooked, status
    if _hooked:
        return
    # prologues from Ghidra: mov [rsp+x],reg / push / mov rbp,rsp / sub rsp — no rip-relative operands
    _orig["exec"] = _detour(0x1411A8100, "4055 53 56 57 4154 4156 4157 488dac2470ffffff", EXEC_T, _on_exec)
    _orig["update"] = _detour(0x141234A30, "48895c2418 4889742420 55 57 4154 4156 4157 488d6c24a0", SET_T,
                              _on_update)
    _orig["set"] = _detour(0x141247490, "4889542410 53 55 56 57 4154 4156 4157 4883ec50", SET_T, _on_set_menu)
    _hooked = True
    if status == "not installed":
        status = "hooked, waiting for CSP to set its menu"


def find_windows():
    """Windows showing a main menu. Heap scan (seconds); reads memory safely, so run it off the UI thread."""
    return windows(main_menus())


def install(wins=None):
    """Add the Scripts menu to the menus CSP already shows. Run on the UI thread (console code already is).
    wins: from find_windows(), found beforehand off the UI thread; default scans here (freezes CSP a few s)."""
    global status
    wins = wins if wins is not None else find_windows()
    if not wins:
        status = "no main menu yet"
        return False
    hook()
    _wins.update(wins)
    _apply(wins)
    status = f"installed, ids {_base:#x}+, windows {[hex(w) for w in wins]}"
    return True


def refresh():
    """Re-read scripts/ into the menu."""
    _fresh.clear()
    _apply(list(_wins), rebuild=True)
