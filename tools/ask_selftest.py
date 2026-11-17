r"""切换问答窗自测（CTk 版）：两个真实 prompt（重启三选/清场确认）+ 兜底输入框分支。

断言：
- 按钮文本逐项对应编号选项
- 焦点落预期项（重启三选=2 安全侧，清场确认=1）
- 配色档位：危险项（立即重启）红、回车默认项主色、其余中性、取消描边
- 窗口居中于主窗口、点按钮应答回传
窗口在屏幕外且不抢前台焦点。产物打印 RESULT: PASS/FAIL，退出码同。

用法：python tools/ask_selftest.py
"""
import ctypes
import queue
import sys
import tkinter as tk
import types

sys.path.insert(0, r"C:\Users\Yang\.zcode\workspace\default\projects\dgpu_detective")
import customtkinter as ctk

import dgpu_gui

PROMPTS = [
    ("restart", "[1] 立即重启（5 秒可取消倒计时后由 GCU 执行重启）  "
                "[2] 稍后自行重启（保持待重启态）  "
                "[回车/其他] 取消重启安排（命令已发，同样处于待重启态）"),
    ("clear", "\n[1] 结束以上候选（含全部同名实例与子进程）并继续  "
              "[2] 不关闭直接继续  [回车/其他] 取消"),
    ("fallback", "（控制台提问，详见日志）"),
]

root = tk.Tk()
root.geometry("800x600+30000+30000")
try:  # 不抢前台焦点（同 _move_offscreen 的 NOACTIVATE 口径）
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
    ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x80 | 0x08000000)
except Exception:
    pass

results = []


def run_case(name, prompt, use_keyboard=False):
    bridge = dgpu_gui._AskBridge(queue.Queue())
    fake = types.SimpleNamespace(root=root, _sink=types.SimpleNamespace(pending=prompt))
    dgpu_gui.App._show_ask(fake, bridge)
    wins = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    win = wins[-1]
    root.update_idletasks()
    root.update()
    btns = []

    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, ctk.CTkButton):
                btns.append(c)
            walk(c)

    walk(win)
    texts = [b.cget("text") for b in btns]
    fgs = [b.cget("fg_color") for b in btns]
    off = (win.winfo_x() - root.winfo_x(), win.winfo_y() - root.winfo_y())
    rsize = (root.winfo_width(), root.winfo_height())
    win_w = win.winfo_width()  # 应答会 destroy 窗口，尺寸先取
    if use_keyboard:
        # 回车=安全侧（CTkButton canvas 不吃键盘焦点，绑定在问答窗级，
        # 断言走事件路径：<Return> 应答应落在默认项）
        win.focus_force()
        root.update()
        win.event_generate("<Return>")
        root.update()
        ans = bridge.ans_q.get_nowait()
    else:
        btns[0].invoke()
        ans = bridge.ans_q.get_nowait()
    results.append(dict(name=name, buttons=texts, fgs=fgs, off=off,
                        rsize=rsize, click_ans=ans, w=win_w))
    win.destroy()
    root.update()


def main():
    try:
        for name, p in PROMPTS[:2]:
            run_case(name + "_kb", p, use_keyboard=True)  # 回车路径
            run_case(name, p)                             # 点击路径
        run_case("fallback", PROMPTS[2][1])
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        root.quit()


root.after(150, main)
root.mainloop()

ok = True
rk = results[0]  # 重启三选·回车路径：应答必须落 2=稍后自行重启（安全侧）
ok &= rk["click_ans"] == "2"
print("restart_kb ans:", rk["click_ans"], "（回车→安全侧 2）")

r = results[1]  # 重启三选·点击路径
exp = ["1｜立即重启（5 秒可取消倒计时后由 GCU 执行重启）",
       "2｜稍后自行重启（保持待重启态）",
       "回车/其他｜取消重启安排（命令已发，同样处于待重启态）"]
ok &= r["buttons"] == exp
ok &= r["click_ans"] == "1"
# 配色档位：1=危险红、2=回车默认主色、取消=描边 transparent
ok &= (r["fgs"][0] == dgpu_gui.RED and r["fgs"][1] == dgpu_gui.ACCENT
       and r["fgs"][2] == "transparent")
print("restart buttons:", r["buttons"])
print("restart fgs:", r["fgs"], " click_ans:", r["click_ans"])

ck = results[2]  # 清场确认·回车路径：默认 1
ok &= ck["click_ans"] == "1"
print("clear_kb ans:", ck["click_ans"], "（回车→默认 1）")

r = results[3]  # 清场确认·点击路径
print("clear buttons:", r["buttons"])
print("clear fgs:", r["fgs"])
ok &= len(r["buttons"]) == 3 and "结束以上候选" in r["buttons"][0]
# 配色档位：1=回车默认主色、2=中性、取消=描边
ok &= (r["fgs"][0] == dgpu_gui.ACCENT and r["fgs"][1] == dgpu_gui.SURFACE2
       and r["fgs"][2] == "transparent")

r = results[4]  # 兜底：无编号 → Entry 分支
print("fallback buttons:", r["buttons"], " click_ans:", r["click_ans"])
ok &= r["click_ans"] == ""  # 兜底分支无编号按钮，首按钮=确定，应答 Entry 空文本

for r in results:  # 居中断言：偏移在主窗尺寸内（不落屏幕左上角）
    inside = 0 <= r["off"][0] < r["rsize"][0] and 0 <= r["off"][1] < r["rsize"][1]
    print(f"{r['name']}: off={r['off']} root_size={r['rsize']} win_w={r['w']} centered={inside}")
    ok &= inside

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
