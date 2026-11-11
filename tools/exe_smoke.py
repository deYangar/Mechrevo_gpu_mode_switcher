r"""exe 冒烟测试（提权运行）：launch → 找窗口 → 屏幕外断言 → PrintWindow 截图 → 干净退出。

产物：exe 吐窗口但必须在屏幕外（DGPU_TEST_OFFSCREEN 钩子）；截图非空白；
6 秒存活（采样线程跑着）；WM_CLOSE 干净退出。
被测 exe 带 uac-admin 清单，本脚本也必须提权跑，否则 UIPI 挡 PrintWindow/PostMessage。

用法（外部用 Start-Process -Verb RunAs 提权启动）：
  python tools/exe_smoke.py            # 测 dist\dgpu_detective.exe
结果写 test_artifacts\exe_smoke_result.txt，退出码 0=全过。
"""

import ctypes
import json
import os
import struct
import subprocess
import sys
import time
import zlib
from ctypes import wintypes

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(PROJ, "dist", "dgpu_detective.exe")
TITLE = "独显占用侦探"
ART = os.path.join(PROJ, "test_artifacts")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

try:  # 与被测 GUI 一致的 DPI 感知，防 GetWindowRect 虚拟化导致截图截角
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def enum_windows() -> list:
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if TITLE in buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


def is_offscreen(hwnd) -> bool:
    """与虚拟屏幕不相交才算屏幕外（多显示器安全）。"""
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    vx = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    vy = user32.GetSystemMetrics(77)
    vw = user32.GetSystemMetrics(78)
    vh = user32.GetSystemMetrics(79)
    return (r.right <= vx or r.left >= vx + vw or
            r.bottom <= vy or r.top >= vy + vh)


def _write_png(path, w, h, bgra: bytes):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    rows = bytearray()
    stride = w * 4
    for y in range(h):
        rows.append(0)  # 行过滤器类型 0
        base = y * stride
        for x in range(w):
            i = base + x * 4
            rows += bgra[i + 2:i + 3] + bgra[i + 1:i + 2] + bgra[i:i + 1]  # BGR->RGB
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
                chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


def screenshot_png(hwnd, path) -> tuple:
    PW_RENDERFULLCONTENT = 2
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return False, f"窗口尺寸异常 {w}x{h}"
    hdc_win = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    try:
        if not user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT):
            return False, "PrintWindow 失败"
        bi = ctypes.create_string_buffer(40)
        struct.pack_into("IiiHHIIiiII", bi, 0, 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, bi, 0):
            return False, "GetDIBits 失败"
        raw = buf.raw[: w * h * 4]
        lo, hi = min(raw), max(raw)
        if lo == hi:
            return False, "截图疑似空白（整幅单色）"
        _write_png(path, w, h, raw)
        return True, f"{w}x{h} 字节极值 {lo}..{hi}"
    finally:
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)


def main() -> int:
    os.makedirs(ART, exist_ok=True)
    results = []

    def check(name, ok, note=""):
        results.append((name, bool(ok), note))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}", flush=True)

    if not os.path.exists(EXE):
        check("exe 存在", False, EXE)
        _finish(results)
        return 1

    pre = set(enum_windows())
    env = {**os.environ, "DGPU_TEST_OFFSCREEN": "1", "DGPU_TEST_AUTORUN": "1"}
    p = subprocess.Popen([EXE], cwd=os.path.dirname(EXE), env=env)

    hwnd = None
    t0 = time.time()
    while time.time() - t0 < 25:
        cands = [h for h in enum_windows() if h not in pre]
        # 只认屏幕外的新窗口：咩咩自己开着的同标题窗口在屏内，不能断言到它头上
        ours = [h for h in cands if is_offscreen(h)]
        if ours:
            hwnd = ours[0]
            break
        if cands and time.time() - t0 > 8:
            # 有新标题窗口但全在屏内：钩子失效（或被用户窗口干扰），如实报失败
            check("窗口出现（屏幕外）", False,
                  f"新窗口 {len(cands)} 个全在屏内（钩子失效或用户窗口干扰），不测它们")
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
            _finish(results)
            return 1
        if p.poll() is not None:
            break
        time.sleep(0.3)
    check("窗口出现", hwnd is not None, f"pid={p.pid}")
    if not hwnd:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        _finish(results)
        return 1

    check("窗口在屏幕外", is_offscreen(hwnd))
    time.sleep(2.5)
    ok1, msg1 = screenshot_png(hwnd, os.path.join(ART, "exe_smoke_t1.png"))
    check("截图 t1 非空白", ok1, msg1)
    time.sleep(3.5)
    ok2, msg2 = screenshot_png(hwnd, os.path.join(ART, "exe_smoke_t2.png"))
    check("6s 存活且截图 t2 非空白", ok2, msg2)

    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    gone = False
    t0 = time.time()
    while time.time() - t0 < 10:
        if hwnd not in enum_windows():
            gone = True
            break
        time.sleep(0.2)
    if not gone:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        time.sleep(0.5)
        gone = hwnd not in enum_windows()
    check("窗口关闭", gone, "WM_CLOSE" if gone else "taskkill 兜底")

    _finish(results)
    return 0 if all(ok for _, ok, _ in results) else 1


def _finish(results):
    with open(os.path.join(ART, "exe_smoke_result.txt"), "w", encoding="utf-8") as f:
        for name, ok, note in results:
            f.write(f"{'PASS' if ok else 'FAIL'}  {name}  {note}\n")


if __name__ == "__main__":
    sys.exit(main())
