"""枚举指定 pid 加载的图形相关模块（opengl32/D3D/DXGI/NV），定位 nvapi64 的加载通道。"""
import ctypes
import ctypes.wintypes as wt
import sys

k = ctypes.WinDLL('kernel32')
k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
k.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k.Module32FirstW.restype = wt.BOOL
k.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k.Module32NextW.restype = wt.BOOL
k.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k.CloseHandle.argtypes = [ctypes.c_void_p]

INVALID = ctypes.c_void_p(-1).value


class MEW(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('th32ModuleID', wt.DWORD),
                ('th32ProcessID', wt.DWORD), ('GlblcntUsage', wt.DWORD),
                ('ProccntUsage', wt.DWORD), ('modBaseAddr', ctypes.c_void_p),
                ('modBaseSize', wt.DWORD), ('hModule', wt.HMODULE),
                ('szModule', ctypes.c_wchar * 256),
                ('szExePath', ctypes.c_wchar * 260)]


INTEREST = ('nvapi', 'nvoglv', 'nvd3d', 'nvml', 'nvspcaps', 'nvlddm',
            'opengl32', 'd3d9', 'd3d10', 'd3d11', 'd3d12', 'dxgi', 'vulkan')


def mods(pid: int) -> list[str]:
    msnap = k.CreateToolhelp32Snapshot(0x8 | 0x10, pid)
    if not msnap or msnap == INVALID:
        return []
    try:
        me = MEW(); me.dwSize = ctypes.sizeof(MEW)
        out: list[str] = []
        ok = k.Module32FirstW(msnap, ctypes.byref(me))
        while ok:
            low = me.szModule.lower()
            if any(t in low for t in INTEREST):
                out.append(f"{me.szModule}  <-  {me.szExePath}")
            ok = k.Module32NextW(msnap, ctypes.byref(me))
        return out
    finally:
        k.CloseHandle(msnap)


if __name__ == '__main__':
    for arg in sys.argv[1:]:
        pid = int(arg)
        print(f"=== pid {pid} ===")
        for line in mods(pid):
            print(f"  {line}")
        print()
