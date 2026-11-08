"""Low-level helpers for poking at CLIP STUDIO PAINT 1.13.2 from inside its process (RE work, csp.py, menu.py)."""
import ctypes
from ctypes import c_uint32, c_void_p, wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleHandleW.restype = c_void_p
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, c_void_p, c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.VirtualQuery.argtypes = [c_void_p, c_void_p, ctypes.c_size_t]
kernel32.VirtualQuery.restype = ctypes.c_size_t
kernel32.VirtualAlloc.argtypes = [c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
kernel32.VirtualAlloc.restype = c_void_p

IMAGE_BASE = 0x140000000
BUILD_TIMESTAMP = 0x63CAD9A2  # PE header of the 1.13.2 exe these addresses came from

BASE = kernel32.GetModuleHandleW(None)
_pe = BASE + c_uint32.from_address(BASE + 0x3C).value
if c_uint32.from_address(_pe + 8).value != BUILD_TIMESTAMP:
    raise RuntimeError("CLIPStudioPaint.exe build differs from the one RE_NOTES.md was made from")


def va(static_va):
    """Static Ghidra address -> runtime address."""
    return BASE + (static_va - IMAGE_BASE)


_proc = kernel32.GetCurrentProcess()


def read(addr, size):
    """Safe memory read: returns None instead of crashing CSP on a bad address."""
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t()
    if not kernel32.ReadProcessMemory(_proc, addr, buf, size, ctypes.byref(n)) or n.value != size:
        return None
    return buf.raw


def read_u64(addr):
    b = read(addr, 8)
    return None if b is None else int.from_bytes(b, "little")


def sp_vector(addr):
    """(ptr, ctrl) of each element of a std::vector<boost::shared_ptr<T>> at addr."""
    b, e = read_u64(addr), read_u64(addr + 8)
    return [(read_u64(p), read_u64(p + 8)) for p in range(b, e, 16)] if b and e and e - b < 1 << 20 else []


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", c_void_p), ("AllocationBase", c_void_p), ("AllocationProtect", wintypes.DWORD),
                ("PartitionId", wintypes.WORD), ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]


def find_objects(vtable):
    """Heap scan for live objects whose first qword is `vtable` (seconds; any 8-aligned match counts)."""
    MEM_COMMIT, MEM_PRIVATE, PAGE_READWRITE = 0x1000, 0x20000, 0x04
    needle = vtable.to_bytes(8, "little")
    found, addr, mbi = [], 0, MBI()
    while kernel32.VirtualQuery(addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
        base, size = mbi.BaseAddress or 0, mbi.RegionSize
        if mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE and mbi.Protect == PAGE_READWRITE:
            for off in range(0, size, 1 << 24):  # 16 MB chunks
                chunk = read(base + off, min(1 << 24, size - off))
                if chunk is None:
                    continue
                i = chunk.find(needle)
                while i >= 0:
                    if i % 8 == 0:
                        found.append(base + off + i)
                    i = chunk.find(needle, i + 1)
        addr = base + size
    return found


def rtti_name(obj):
    """MSVC RTTI class name of a polymorphic object, or None (safe on bad pointers)."""
    vt = read_u64(obj)
    col = read_u64(vt - 8) if vt else None
    td = read(col + 12, 4) if col else None
    raw = read(BASE + int.from_bytes(td, "little") + 16, 160) if td else None
    return raw.split(b"\0")[0].decode(errors="replace") if raw else None
