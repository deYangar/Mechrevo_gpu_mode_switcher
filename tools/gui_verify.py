r"""源码 GUI 后台验证：launch（屏幕外）→ PrintWindow 截图 → WM_CLOSE 干净退出。

与 exe_smoke.py 同一套 PrintWindow 手法，测的是源码版 python dgpu_gui.py——
改完 GUI 代码先跑这个看渲染，再谈打包。
被测窗口在 _move_offscreen 里挂了 LAYERED+alpha=1（屏外窗口无 DWM 表面，
不挂则 PrintWindow 抓白板），本工具无需再操心绘制链路。

用法：
  python tools/gui_verify.py
产物：test_artifacts\gui_verify_t1.png / t2.png，退出码 0=正常。
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exe_smoke import (ART, enum_windows, is_offscreen, screenshot_png,
                       user32)

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    os.makedirs(ART, exist_ok=True)
    for n in ("gui_verify_t1.png", "gui_verify_t2.png"):  # 清上一轮产物，防误读旧图
        try:
            os.remove(os.path.join(ART, n))
        except OSError:
            pass
    pre = set(enum_windows())
    env = {**os.environ, "DGPU_TEST_OFFSCREEN": "1", "DGPU_TEST_AUTORUN": "1"}
    p = subprocess.Popen([sys.executable, os.path.join(PROJ, "dgpu_gui.py")],
                         cwd=PROJ, env=env)
    try:
        hwnd = None
        t0 = time.time()
        while time.time() - t0 < 25:
            ours = [h for h in enum_windows() if h not in pre and is_offscreen(h)]
            if ours:
                hwnd = ours[0]
                break
            if p.poll() is not None:
                print(f"FAIL  进程提前退出 rc={p.returncode}")
                return 1
            time.sleep(0.3)
        if not hwnd:
            print("FAIL  窗口未出现")
            return 1
        print(f"PASS  窗口出现（屏幕外）pid={p.pid}")

        time.sleep(5.0)  # nv 首轮扫描 + 数轮采样
        ok1, msg1 = screenshot_png(hwnd, os.path.join(ART, "gui_verify_t1.png"))
        print(f"{'PASS' if ok1 else 'FAIL'}  截图 t1  {msg1}")
        time.sleep(4.0)
        ok2, msg2 = screenshot_png(hwnd, os.path.join(ART, "gui_verify_t2.png"))
        print(f"{'PASS' if ok2 else 'FAIL'}  截图 t2（9s 存活）  {msg2}")

        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        t0 = time.time()
        while time.time() - t0 < 10:
            if hwnd not in enum_windows():
                print("PASS  WM_CLOSE 干净退出")
                return 0 if (ok1 and ok2) else 1
            time.sleep(0.2)
        print("FAIL  WM_CLOSE 未退出")
        return 1
    finally:
        if p.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
