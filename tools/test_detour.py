"""Self-check for menu._detour on a fake function (no CSP needed): python tools/test_detour.py"""
import ctypes
import os
import sys
import types

k32 = ctypes.WinDLL("kernel32")
k32.VirtualAlloc.restype = ctypes.c_void_p
k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]

# stand-ins for the in-CSP modules menu.py imports
mem = types.ModuleType("mem")
mem.BASE = 0x140000000
mem.va = lambda x: x
mem.read = lambda a, n: ctypes.string_at(a, n)
mem.read_u64 = lambda a: ctypes.c_uint64.from_address(a).value
mem.rtti_name = lambda o: None
sys.modules["mem"], sys.modules["csp"], sys.modules["startup"] = mem, types.ModuleType("csp"), types.ModuleType("startup")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "cspy"))
import menu  # noqa: E402

# fake "post" function: same prologue as 1411a61b0, then returns cmd * 2
prologue = "48895c2408 55 56 57 488bec 4883ec70"
body = bytes.fromhex("8d0412" "4883c470 5f 5e 5d 488b5c2408 c3")  # lea eax,[rdx+rdx]; epilogue; ret
fn = k32.VirtualAlloc(None, 0x1000, 0x3000, 0x40)
ctypes.memmove(fn, bytes.fromhex(prologue) + body, len(bytes.fromhex(prologue)) + len(body))
f = menu.EXEC_T(fn)
assert f(0, 21, 0) == 42

seen = []
orig = {}


def hook(view, cmd, a):
    seen.append((cmd, menu._caller(orig["x"][1])))
    return 7 if cmd == 5 else orig["x"][0](view, cmd, a)


orig["x"] = menu._detour(fn, prologue, menu.EXEC_T, hook)
assert f(0, 21, 0) == 42, "original not reached through trampoline"
assert f(0, 5, 0) == 7, "hook result not returned"
assert [c for c, _ in seen] == [21, 5] and seen[0][1] != "0x0", seen
print("ok", seen)
