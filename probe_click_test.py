r"""点击链路探针（排「模式按钮点击没反应」回归验证）：

1. dump 三切换按钮 state（改版后当前态也应 normal）
2. ghost 测试按钮两条点击路径：
   a. 文字区（Enter+Release 派发 _text_label）
   b. 空白区（Enter+Release 派发 _canvas，CTk 6.0.0 原生在此静默丢事件，
      补丁后必须触发）——command 只写日志，无副作用
3. 当前态按钮（hybrid）点击 → 日志出现「当前已是混合模式」反馈行
4. 切换按钮空白区点击链路（不 invoke 真实切换：发令有硬件副作用，
   只对当前态按钮做真点击验证反馈行）

用法：python probe_click_test.py  （屏外跑，~18s 退出）
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


def record(name: str, ok: bool, note: str = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}", flush=True)


def click(widget, label) -> None:
    """模拟真实点击序列：Enter（hover 进入）+ Release。CTkButton 6.0.0 的
    command gate 在 _mouse_inside，必须先 Enter。"""
    widget.event_generate("<Enter>")
    widget.event_generate("<ButtonPress-1>", x=5, y=5)
    widget.event_generate("<ButtonRelease-1>", x=5, y=5)


def main() -> int:
    ctk.set_appearance_mode("dark")
    root = ctk.CTk(fg_color=gui.BG)
    gui.apply_theme()
    root.update_idletasks()
    gui._dark_titlebar(root)
    adapters = eng.enumerate_adapters()
    app = gui.App(root, adapters)
    gui._move_offscreen(root)

    bar = app.root.grid_slaves(1, 0)[0]  # 工具栏（dump_tree 同款定位）
    test_btn = ctk.CTkButton(
        bar, text="PROBE", width=120, height=32,
        fg_color=gui.SURFACE2, hover_color=gui.BORDER,
        border_width=1, border_color=gui.BORDER, text_color=gui.TEXT,
        command=lambda: app.log_q.put("[PROBE] ghost command 触发\n"))
    test_btn.pack(side="left", padx=2)

    def step1():
        print("--- 按钮 state dump ---", flush=True)
        for b in app._switch_buttons():
            print(f"  {b.cget('text')}: state={b.cget('state')} "
                  f"fg={b.cget('fg_color')} tc={b.cget('text_color')}", flush=True)
        mode = app.last_st.mode if app.last_st else None
        print(f"  switch_running={app.switch_running} mode={mode} "
              f"gcu={'有' if app.gcu_info else '无'}", flush=True)
        record("当前态按钮 state=normal（A2）",
               app.hybrid_btn.cget("state") == "normal",
               f"mode={mode}")
        # 2a 文字区点击
        click(test_btn._text_label, "text")
        root.after(400, lambda: step2("文字区"))
        return

    def step2(path):
        txt = app.log_text.get("1.0", "end")
        record(f"ghost {path}点击 → command", "[PROBE]" in txt, "gate=_mouse_inside")
        if path == "文字区":
            click(test_btn._canvas, "canvas")
            root.after(400, lambda: step2("空白区"))
            return
        # 3 当前态按钮真点击 → 反馈行（不发切换命令：当前态 run_switch 早退）
        click(app.hybrid_btn._canvas, "canvas")
        root.after(400, step3)

    def step3():
        txt = app.log_text.get("1.0", "end")
        record("当前态按钮点击 → 日志反馈行",
               "当前已是混合模式" in txt, "")
        record("无切换编排误触发", "=====" not in txt, "run_switch 未进编排")
        print(f"--- {sum(results)}/{len(results)} PASS ---", flush=True)
        root.after(200, root.destroy)

    root.after(11000, step1)  # 等采样轮 + GCU 探测（~5s）到位
    root.mainloop()
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
