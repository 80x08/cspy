"""Dev loader: inject cspy.dll into a running CLIPStudioPaint.exe that didn't auto-load it.

Usage: python tools/inject.py [path/to/cspy.dll]   (default: dist/PlugIn/PAINT/cspy.dll)
The DLL then runs the runtime installed at <CSP>/cspy.
"""
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.OpenProcess.restype = wintypes.HANDLE
k.VirtualAllocEx.restype = ctypes.c_void_p
k.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
k.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
k.GetModuleHandleW.restype = ctypes.c_void_p
k.GetProcAddress.restype = ctypes.c_void_p
k.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
k.CreateRemoteThread.restype = wintypes.HANDLE
k.CreateRemoteThread.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]

out = subprocess.run(["tasklist", "/fi", "imagename eq CLIPStudioPaint.exe", "/fo", "csv", "/nh"],
                     capture_output=True, text=True).stdout.strip()
if "CLIPStudioPaint" not in out:
    raise SystemExit("CLIP STUDIO PAINT is not running")
pid = int(out.split('","')[1])

dll = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else
                      os.path.join(os.path.dirname(__file__), "..", "dist", "PlugIn", "PAINT", "cspy.dll"))
path = ctypes.create_unicode_buffer(dll)
size = ctypes.sizeof(path)

h = k.OpenProcess(0x1F0FFF, False, pid)
assert h, f"OpenProcess failed: {ctypes.get_last_error()}"
mem = k.VirtualAllocEx(h, None, size, 0x3000, 0x04)
assert mem and k.WriteProcessMemory(h, mem, path, size, None)
# kernel32 is mapped at the same address in every process of a boot session.
load = k.GetProcAddress(k.GetModuleHandleW("kernel32"), b"LoadLibraryW")
t = k.CreateRemoteThread(h, None, 0, load, mem, 0, None)
assert t, f"CreateRemoteThread failed: {ctypes.get_last_error()}"
k.WaitForSingleObject(t, 10000)
print(f"injected into pid {pid}")
