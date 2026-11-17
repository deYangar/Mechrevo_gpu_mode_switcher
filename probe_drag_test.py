r"""sash 拖动探针 v2：分离「事件层」与「configure 生效层」两个假设。

A. 事件层：合成 B1-Motion 是否触发 bind（指示条 y 是否跟随）
B. configure 层：直接调 _sash_motion/_sash_release（真 event 对象），
   验证 log_holder.configure(height=...) 是否真实落到 grid 分配

用法：python probe_drag_test.py  （屏外跑，~12s 退出）
"""

import os
import sys

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
os.environ["DGPU_TEST_OFFSCREEN"] = "1"

import customtkinter as ctk  # noqa: E402
import dgpu_detective as eng  # noqa: E402
import dgpu_gui as gui  # noqa: E402

results = []


def record(name, ok, note=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}", flush=True)


class FakeEvent:
    def __init__(self, y_root):
        self.y_root = y_root
        self.x_root = 0


def main() -> int:
    ctk.set_appearance_mode("dark")
    root = ctk.CTk(fg_color=gui.BG)
    gui.apply_theme()
    root.update_idletasks()
    gui._dark_titlebar(root)
    adapters = eng.enumerate_adapters()
    app = gui.App(root, adapters)
    gui._move_offscreen(root)
    root.update()
    sb = app.sash_bar
    cont = app.center

    def step_a():
        # ---- A: 合成事件层 ----
        h0 = app.log_holder.winfo_height()
        sb.event_generate("<ButtonPress-1>", x=30, y=2)
        root.update()
        sb.event_generate("<B1-Motion>", x=30, y=152)
        root.update()
        line_y = app._sash_line.winfo_y()
        record("A1 合成 B1-Motion 触发 bind（指示条 y 变化）",
               line_y > 100, f"line_y={line_y}")
        sb.event_generate("<ButtonRelease-1>", x=30, y=152)
        root.update()
        h_a = app.log_holder.winfo_height()
        record("A2 事件链后日志区高度变化", h_a != h0, f"{h0} → {h_a}")

        root.after(300, step_b)

    def step_b():
        # ---- B: 直接调方法（绕过事件层）----
        h0 = app.log_holder.winfo_height()
        center_h = cont.winfo_height()
        rooty = cont.winfo_rooty()
        app._sash_press(FakeEvent(rooty + 400))
        app._sash_motion(FakeEvent(rooty + 620))
        rel_y = app._sash_drag_rel
        app._sash_release(FakeEvent(rooty + 620))
        root.update()
        h1 = app.log_holder.winfo_height()
        expect = int(max(120, center_h - rel_y - 6))  # release 直接物理像素
        record("B1 release 计算值合理", 0 < expect < center_h,
               f"rel_y={rel_y} expect={expect}")
        record("B2 configure(height) 真实生效", abs(h1 - expect) <= 8,
               f"{h0} → {h1}（期望≈{expect}）")
        record("B3 指示条已隐藏", not app._sash_line.winfo_ismapped(), "")
        print(f"--- {sum(results)}/{len(results)} PASS ---", flush=True)
        root.after(200, root.destroy)

    root.after(9000, step_a)
    root.mainloop()
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
