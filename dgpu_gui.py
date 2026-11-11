"""独显占用侦探 —— 图形界面版（单文件，纯标准库，复用 dgpu_detective 引擎）。

- 持有者表格 = --report/--watch 的常驻版：自动刷新，新持有黄底高亮
- 显示模式徽章 = --status 三态凭证（PnP problem / DXGI / GCU 注册表）
- 「核显省电 / 恢复混合」= 内嵌控制台原样运行 cmd_eco_on / cmd_eco_off：
  print 重定向进日志面板（双写 logs/eco_*.txt 留档），ask() 的 input() 弹问答框应答，
  编排代码零改动——CLI 双向真测过的路径就是 GUI 跑的路径
- 驱动层持有者页 = nv_module_holders（NVAPI/NVML 口径，补 WDDM 盲区）
- 打包：dgpu_detective.spec（onefile + windowed + uac_admin），build_exe.ps1 一键构建
"""

import contextlib
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import dgpu_detective as eng

# ---------------------------------------------------------------------------


def enable_dpi_awareness() -> None:
    """SYSTEM_DPI_AWARE，防高 DPI 下文字发糊；必须在 Tk() 创建前调用。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def co_init() -> None:
    """工作线程里跑 DXGI 枚举（read_display_mode_state 会调 enumerate_adapters）需要 COM；
    重复初始化返回 S_FALSE，无害。"""
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # APARTMENTTHREADED
    except Exception:
        pass


def taskkill_pid(pid: int) -> tuple[bool, str]:
    """taskkill /F /T /PID：结束单个进程及其子进程树；错误原文返回，不吞。"""
    r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    msg = (r.stderr or r.stdout or b"").decode("gbk", "replace").strip().replace("\n", " | ")
    return r.returncode == 0, msg


def is_protected(image: str) -> bool:
    """与引擎清场口径一致的保护名单（小写映像名）。"""
    if image.startswith("<"):
        return True  # 读不到路径的进程不动
    return image.rsplit("\\", 1)[-1].lower() in eng.PROTECTED_PROCESSES


def short_gpu(name: str) -> str:
    n = name.replace("NVIDIA GeForce ", "").replace("Intel(R) Graphics", "Intel 核显")
    n = n.replace("Microsoft Basic Render Driver", "Basic 渲染器").strip()
    return n if len(n) <= 26 else n[:25] + "…"


MODE_COLORS = {  # 徽章配色：fg, bg
    "hybrid": ("#1a6b2f", "#e0f2e2"),
    "igpu_only": ("#1a4fa0", "#e2ecfa"),
    "pnp_disabled": ("#8a5200", "#fbeed8"),
    "unknown": ("#9c1f1f", "#fae2e2"),
}


@contextlib.contextmanager
def _redirect_stdin(new):
    """标准库没有 redirect_stdin（只有 stdout/stderr），自己补一个。"""
    old = sys.stdin
    sys.stdin = new
    try:
        yield
    finally:
        sys.stdin = old


class _AskBridge:
    """eco worker 的假 stdin：input() 读一行 = 主线程弹问答框，应答回传。"""

    def __init__(self, q: queue.Queue):
        self.q = q
        self.ans_q = queue.Queue()

    def readline(self, size=-1) -> str:
        self.q.put(("ask", self))
        ans = self.ans_q.get()  # 阻塞等 GUI 应答
        return "" if ans is None else ans + "\n"

    # input() 只依赖 readline；其余兜底
    def read(self, *a, **k) -> str:
        return self.readline()

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


class _LogSink:
    """eco worker 的假 stdout/stderr：写日志面板队列 + 双写日志文件（行缓冲留档）。"""

    def __init__(self, log_q: queue.Queue, file=None):
        self.log_q = log_q
        self.file = file
        self.pending = ""  # 未换行结尾的尾巴 = 正在进行的提问提示，问答框展示用

    def write(self, s) -> int:
        if self.file:
            self.file.write(s)
        self.log_q.put(s)
        if "\n" in s:
            self.pending = s.rsplit("\n", 1)[-1]
        else:
            self.pending += s
        return len(s)

    def flush(self) -> None:
        if self.file:
            self.file.flush()

    def isatty(self) -> bool:
        return False


class App:
    POLL_MS = 150

    def __init__(self, root: tk.Tk, adapters: list):
        self.root = root
        root.title("独显占用侦探")
        root.geometry("1040x700")
        root.minsize(860, 560)
        self._autorun_done = False  # DGPU_TEST_AUTORUN 钩子只触发一次

        # ---- 跨线程状态（工作线程与主线程各管各的写方向，GIL 下安全）----
        self.q = queue.Queue()      # 工作线程 -> 主线程：snap/err/ask/ecodone/nvtab
        self.log_q = queue.Queue()  # 日志流（raw str）-> 主线程 Text
        self.stop_evt = threading.Event()
        self.adapters = adapters
        self._auto = True
        self._paused = False        # eco 切换期间暂停采样（PDH 句柄不跨线程并发）
        self._manual_req = False
        self._include_all = False
        self._interval = 1.0
        self.eco_running = False
        self.last_data = None
        self.last_st = None
        self._sink = None           # 当前 eco worker 的日志 sink（问答框读 pending）

        self.ask_bridge = _AskBridge(self.q)

        # 表格状态
        self.chips = {}        # luid_str -> (mem_label, eng_label)
        self.rowmeta = {}      # iid -> row dict
        self.new_marks = {}    # iid -> 剩余高亮轮数
        self.prev_keys = set()  # (luid_str, pid)

        self._build_ui()

        self.sampler = threading.Thread(target=self._sampler_loop, daemon=True)
        self.sampler.start()
        self.root.after(self.POLL_MS, self._poll)

    # ------------------------------------------------------------- UI 构建

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        # 顶部：GPU 概要条
        self.gpu_bar = ttk.Frame(self.root, padding=(10, 6, 10, 2))
        self.gpu_bar.grid(row=0, column=0, sticky="ew")

        # 工具条
        bar = ttk.Frame(self.root, padding=(10, 2))
        bar.grid(row=1, column=0, sticky="ew")

        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动刷新", variable=self.auto_var,
                        command=self._on_auto_toggle).pack(side="left")
        ttk.Label(bar, text="间隔(s)").pack(side="left", padx=(10, 2))
        self.interval_spin = ttk.Spinbox(bar, from_=0.5, to=10.0, increment=0.5,
                                         width=4, command=self._on_interval)
        self.interval_spin.set("1.0")
        self.interval_spin.pack(side="left")
        self.interval_spin.bind("<FocusOut>", lambda e: self._on_interval())
        ttk.Button(bar, text="立即刷新", command=self._refresh_now).pack(side="left", padx=6)
        self.all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="含核显", variable=self.all_var,
                        command=self._on_include_all).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="导出 JSON", command=self._export_json).pack(side="left", padx=6)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.eco_on_btn = ttk.Button(bar, text="核显省电", command=lambda: self.run_eco("on"))
        self.eco_on_btn.pack(side="left")
        self.eco_off_btn = ttk.Button(bar, text="恢复混合", command=lambda: self.run_eco("off"))
        self.eco_off_btn.pack(side="left", padx=6)

        # 中央：上持有者表格 / 下 Notebook，可拖分界
        pw = ttk.PanedWindow(self.root, orient="vertical")
        pw.grid(row=2, column=0, sticky="nsew", padx=10, pady=4)

        tree_frame = ttk.Frame(pw)
        cols = ("gpu", "pid", "name", "local", "nonlocal", "committed", "engines")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                 selectmode="browse")
        for cid, text, w, anchor in (
                ("gpu", "显卡", 140, "w"), ("pid", "PID", 60, "e"),
                ("name", "进程", 150, "w"), ("local", "显存(MB)", 85, "e"),
                ("nonlocal", "共享(MB)", 85, "e"), ("committed", "提交(MB)", 85, "e"),
                ("engines", "引擎活动", 250, "w")):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor=anchor, stretch=(cid in ("name", "engines")))
        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.tag_configure("new", background="#fff3b2")     # 新持有高亮
        self.tree.tag_configure("igpu", background="#eef4fd")    # 核显行
        self.tree.tag_configure("prot", foreground="#8a2f2f")    # 系统/服务组件
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._popup_menu)

        self.detail = ttk.Label(tree_frame, text="选中进程后这里显示完整路径与引擎明细",
                                padding=(2, 3), wraplength=980, anchor="w",
                                foreground="#444")
        self.detail.pack(side="bottom", fill="x")

        pw.add(tree_frame, weight=3)

        nb = ttk.Notebook(pw)
        self.nb = nb
        self._build_log_tab(nb)
        self._build_nv_tab(nb)
        pw.add(nb, weight=1)

        # 底部状态栏
        sb = ttk.Frame(self.root, padding=(10, 2, 10, 6))
        sb.grid(row=3, column=0, sticky="ew")
        sb.columnconfigure(1, weight=1)
        self.mode_badge = tk.Label(sb, text=" … ", bg="#eeeeee", fg="#333",
                                   padx=10, pady=2)
        self.mode_badge.grid(row=0, column=0, sticky="w")
        self.evidence = ttk.Label(sb, text="三凭证读取中…", foreground="#444")
        self.evidence.grid(row=0, column=1, sticky="w", padx=10)
        admin = "管理员 ✓" if eng.is_admin() else "管理员 ✗（杀提权进程/切换需提权）"
        self.admin_label = ttk.Label(sb, text=admin, foreground=("#1a6b2f" if eng.is_admin() else "#9c1f1f"))
        self.admin_label.grid(row=0, column=2, sticky="e")
        self.refresh_label = ttk.Label(sb, text="", foreground="#666")
        self.refresh_label.grid(row=0, column=3, sticky="e", padx=(10, 0))

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="结束进程树（PID）", command=lambda: self._kill(by_pid=True))
        self.menu.add_command(label="结束全部同名进程（含子进程树）", command=lambda: self._kill(by_pid=False))
        self.menu.add_separator()
        self.menu.add_command(label="复制 PID", command=self._copy_pid)
        self.menu.add_command(label="复制进程路径", command=self._copy_path)
        self.menu.add_command(label="打开文件位置", command=self._open_location)

    def _build_log_tab(self, nb):
        frame = ttk.Frame(nb, padding=4)
        nb.add(frame, text=" 切换日志（内嵌控制台） ")
        topbar = ttk.Frame(frame)
        topbar.pack(side="top", fill="x")
        ttk.Button(topbar, text="清空", width=6,
                   command=self._clear_log).pack(side="left")
        ttk.Button(topbar, text="打开日志目录", width=12,
                   command=self._open_log_dir).pack(side="left", padx=6)
        ttk.Label(topbar, text="「核显省电/恢复混合」在下方运行完整切换编排，"
                               "输出同步留档 logs/eco_*.txt",
                  foreground="#666").pack(side="left", padx=6)

        body = ttk.Frame(frame)
        body.pack(side="top", fill="both", expand=True)
        self.log_text = tk.Text(body, height=7, state="disabled", wrap="none",
                                font=("Consolas", 9), background="#fbfbfb")
        ysb = ttk.Scrollbar(body, orient="vertical", command=self.log_text.yview)
        xsb = ttk.Scrollbar(body, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

    def _build_nv_tab(self, nb):
        frame = ttk.Frame(nb, padding=4)
        nb.add(frame, text=" 驱动层持有者（NVAPI/NVML 口径） ")
        topbar = ttk.Frame(frame)
        topbar.pack(side="top", fill="x")
        ttk.Button(topbar, text="刷新", width=6,
                   command=self._refresh_nv_tab).pack(side="left")
        self.nv_note = ttk.Label(topbar, text="传感器轮询类软件（HWiNFO 等）不建图形上下文，"
                                              "WDDM 表抓不到；此口径直查 NV 模块加载者（GCU 判据口径）。"
                                              "需管理员可见系统/提权进程。", foreground="#666")
        self.nv_note.pack(side="left", padx=6)
        body = ttk.Frame(frame)
        body.pack(side="top", fill="both", expand=True)
        cols = ("pid", "name", "mods", "mark")
        self.nv_tree = ttk.Treeview(body, columns=cols, show="headings")
        for cid, text, w in (("pid", "PID", 70), ("name", "进程", 160),
                             ("mods", "NV 模块", 220), ("mark", "标记", 240)):
            self.nv_tree.heading(cid, text=text)
            self.nv_tree.column(cid, width=w, anchor="w",
                                stretch=(cid in ("name", "mods", "mark")))
        ysb = ttk.Scrollbar(body, orient="vertical", command=self.nv_tree.yview)
        self.nv_tree.configure(yscrollcommand=ysb.set)
        self.nv_tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

    # ------------------------------------------------------------- 采样线程

    def _sampler_loop(self):
        co_init()
        try:
            counters = eng.GpuCounters()
        except OSError as e:
            self.q.put(("err", f"PDH 计数器打开失败: {e}"))
            return
        try:
            while not self.stop_evt.is_set():
                active = self._auto and not self._paused
                if active or self._manual_req:
                    self._manual_req = False
                    t0 = time.monotonic()
                    self._sample_once_send(counters)
                    wait = max(0.0, self._interval - (time.monotonic() - t0))
                else:
                    wait = 0.25
                self.stop_evt.wait(wait)
        finally:
            try:
                counters.close()
            except Exception:
                pass

    def _sample_once_send(self, counters):
        try:
            data = eng.sample_once(counters, self.adapters, self._include_all)
            st = eng.read_display_mode_state()
            self.q.put(("snap", data, st))
        except Exception as e:
            self.q.put(("err", f"采样失败: {e!r}"))

    # ------------------------------------------------------------- 主线程轮询

    def _poll(self):
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "snap":
                    self._apply_snapshot(rest[0], rest[1])
                elif kind == "err":
                    self.log_q.put(rest[0] + "\n")
                elif kind == "ask":
                    self._show_ask(rest[0])
                elif kind == "ecodone":
                    self._on_ecodone(rest[0], rest[1])
                elif kind == "nvtab":
                    self._apply_nvtab(rest[0])
        except queue.Empty:
            pass
        self._drain_log()
        self.root.after(self.POLL_MS, self._poll)

    # ------------------------------------------------------------- 快照渲染

    def _apply_snapshot(self, data, st):
        self.last_data, self.last_st = data, st
        keys_now = set()
        rows_total = 0
        new_count = 0
        sel = self.tree.selection()
        sel_iid = sel[0] if sel else None

        self.tree.delete(*self.tree.get_children())
        self.rowmeta.clear()
        for gpu in data:
            short = short_gpu(gpu["name"])
            for r in gpu["processes"]:
                rows_total += 1
                iid = f"{gpu['luid']}:{r['pid']}"
                keys_now.add((gpu["luid"], r["pid"]))
                if self.prev_keys and (gpu["luid"], r["pid"]) not in self.prev_keys:
                    self.new_marks[iid] = 4  # 高亮 4 轮后消退
                    new_count += 1
                tags = []
                if iid in self.new_marks:
                    tags.append("new")
                if not gpu["discrete"]:
                    tags.append("igpu")
                name = r["image"].rsplit("\\", 1)[-1]
                if is_protected(r["image"]):
                    tags.append("prot")
                active = {k: v for k, v in r["engines"].items() if v > 0.05}
                engstr = "  ".join(f"{k} {v:.1f}%" for k, v in active.items()) or "仅上下文(0%)"
                self.tree.insert("", "end", iid=iid, tags=tags, values=(
                    short, r["pid"], name, f"{r['local_mb']:.2f}",
                    f"{r['nonlocal_mb']:.2f}", f"{r['committed_mb']:.2f}", engstr))
                self.rowmeta[iid] = dict(r, gpu=gpu["name"], discrete=gpu["discrete"])

        # 高亮衰减
        for iid in list(self.new_marks):
            self.new_marks[iid] -= 1
            if self.new_marks[iid] <= 0 or not self.tree.exists(iid):
                del self.new_marks[iid]
                if self.tree.exists(iid):
                    left = [t for t in self.tree.item(iid, "tags") if t != "new"]
                    self.tree.item(iid, tags=left)
        self.prev_keys = keys_now
        if sel_iid and self.tree.exists(sel_iid):
            self.tree.selection_set(sel_iid)

        # GPU 概要条
        for gpu in data:
            mem_l, eng_l = self._chip(gpu["luid"], gpu["name"], gpu["discrete"])
            mem_l.config(text=f"显存 {gpu['adapter_dedicated_mb']:.2f} / "
                              f"{gpu['card_vram_mb']:.2f} MB")
            best_k, best_v = "", 0.0
            for r in gpu["processes"]:
                for k, v in r["engines"].items():
                    if v > best_v:
                        best_k, best_v = k, v
            peak = f"引擎峰值 {best_k} {best_v:.1f}%" if best_v > 0.05 else "引擎空闲"
            eng_l.config(text=f"{len(gpu['processes'])} 持有 · {peak}")

        self._apply_status(st)
        extra = f"｜ 新持有 {new_count}" if new_count else ""
        self.refresh_label.config(
            text=f"{time.strftime('%H:%M:%S')} · {rows_total} 持有{extra}")

        # 后台自测钩子：首帧数据就绪后自动选中首行 + 扫驱动层页（只读操作）
        if not self._autorun_done and os.environ.get("DGPU_TEST_AUTORUN") == "1":
            self._autorun_done = True
            kids = self.tree.get_children()
            if kids:
                self.tree.selection_set(kids[0])
                self.tree.focus(kids[0])
            self._refresh_nv_tab()

    def _chip(self, luid, name, discrete):
        if luid not in self.chips:
            f = ttk.LabelFrame(self.gpu_bar,
                               text=("独显 · " if discrete else "") + short_gpu(name),
                               padding=(10, 4))
            f.pack(side="left", fill="y", padx=(0, 8))
            mem_l = ttk.Label(f, text="显存 …")
            mem_l.grid(row=0, column=0, sticky="w")
            eng_l = ttk.Label(f, text="…", foreground="#555")
            eng_l.grid(row=1, column=0, sticky="w")
            self.chips[luid] = (mem_l, eng_l)
        return self.chips[luid]

    def _apply_status(self, st):
        mode = st.mode
        fg, bg = MODE_COLORS.get(mode, MODE_COLORS["unknown"])
        self.mode_badge.config(text=f" {eng.MODE_LABELS.get(mode, mode)} ", fg=fg, bg=bg)
        probs = "; ".join(eng.STATE_LABELS.get(p, p).split("（")[0] for _, p in st.problems)
        probs = f"PnP: {probs}" if probs else "PnP: 不可读"
        if st.dxgi_error:
            dxgi = f"DXGI 枚举失败: {st.dxgi_error}"
        elif st.dxgi_names:
            dxgi = f"DXGI 可见: {'; '.join(short_gpu(n) for n in st.dxgi_names)}"
        else:
            dxgi = "DXGI 不可见独显"
        intent = f"IGPUonly={st.igpu_only}" if st.igpu_only is not None else "IGPUonly=未写"
        self.evidence.config(text=f"{probs}  ｜  {dxgi}  ｜  {intent}")

    # ------------------------------------------------------------- 表格交互

    def _on_select(self, _ev=None):
        sel = self.tree.selection()
        if not sel:
            return
        m = self.rowmeta.get(sel[0])
        if not m:
            return
        active = {k: v for k, v in m["engines"].items() if v > 0.05}
        engines = "  ".join(f"{k} {v:.1f}%" for k, v in active.items()) or "仅上下文(0%)"
        prot = "  ｜ 系统/服务组件（保护名单，不建议结束）" if is_protected(m["image"]) else ""
        self.detail.config(text=f"{m['image']}  (pid {m['pid']})  显存 {m['local_mb']:.2f} MB"
                                 f"  ｜  {engines}{prot}", foreground="#222")

    def _popup_menu(self, ev):
        iid = self.tree.identify_row(ev.y)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            try:
                self.menu.tk_popup(ev.x_root, ev.y_root)
            finally:
                self.menu.grab_release()

    def _selected(self):
        sel = self.tree.selection()
        return (sel[0], self.rowmeta.get(sel[0])) if sel else (None, None)

    def _kill(self, by_pid: bool):
        iid, m = self._selected()
        if not m:
            return
        if is_protected(m["image"]):
            name = m["image"] if m["image"].startswith("<") else m["image"].rsplit("\\", 1)[-1]
            messagebox.showwarning(
                "独显占用侦探",
                f"{name} 在系统/服务组件保护名单里（杀了被拉回或影响系统），"
                "与命令行口径一致：不杀。")
            return
        name = m["image"].rsplit("\\", 1)[-1]
        target = f"pid {m['pid']}（{name}）" if by_pid else f"全部 {name} 实例（含各自子进程树）"
        if not messagebox.askyesno("确认结束进程", f"确定强制结束 {target}？"):
            return
        ok, msg = taskkill_pid(m["pid"]) if by_pid else eng.kill_by_name(name)
        line = f"[杀进程] {'✓' if ok else '✗'} {target}" + ("" if ok else f" —— {msg[:200]}")
        self.log_q.put(line + "\n")
        (messagebox.showinfo if ok else messagebox.showerror)("独显占用侦探", line)
        if ok:
            self._manual_req = True  # 立即复查一眼

    def _copy_pid(self):
        _, m = self._selected()
        if m:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(m["pid"]))

    def _copy_path(self):
        _, m = self._selected()
        if m and not m["image"].startswith("<"):
            self.root.clipboard_clear()
            self.root.clipboard_append(m["image"])

    def _open_location(self):
        _, m = self._selected()
        if m and not m["image"].startswith("<") and os.path.exists(m["image"]):
            subprocess.Popen(["explorer", "/select,", m["image"]])

    # ------------------------------------------------------------- 工具条动作

    def _on_auto_toggle(self):
        self._auto = self.auto_var.get()
        self._manual_req = True

    def _on_interval(self):
        try:
            v = float(self.interval_spin.get())
            if 0.2 <= v <= 30.0:
                self._interval = v
        except ValueError:
            pass

    def _on_include_all(self):
        self._include_all = self.all_var.get()
        self._manual_req = True

    def _refresh_now(self):
        self._manual_req = True

    def _export_json(self):
        if not self.last_data:
            messagebox.showinfo("独显占用侦探", "还没有采样数据，稍等片刻再导出。")
            return
        fn = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile=time.strftime("dgpu_%Y%m%d_%H%M%S.json"),
            filetypes=[("JSON", "*.json")])
        if not fn:
            return
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(self.last_data, f, ensure_ascii=False, indent=2)
        self.log_q.put(f"[导出] {fn}\n")

    # ------------------------------------------------------------- 切换编排（内嵌控制台）

    def run_eco(self, target: str):
        if self.eco_running:
            return
        if not eng.is_admin():
            messagebox.showwarning(
                "独显占用侦探",
                "未以管理员运行：清场要杀以管理员运行的软件（HWiNFO 等），切不动。\n"
                "exe 版自带 UAC 提权；源码运行请用管理员终端启动。")
            return
        self.eco_running = True
        self._paused = True  # PDH 句柄不与切换 worker 并发
        self.eco_on_btn.config(state="disabled")
        self.eco_off_btn.config(state="disabled")
        self.log_notebook_focus()
        threading.Thread(target=self._eco_worker, args=(target,), daemon=True).start()

    def log_notebook_focus(self):
        self.nb.select(0)  # 切到「切换日志」页

    def _eco_worker(self, target: str):
        co_init()
        try:
            logf = open(eng.eco_log_path(), "w", encoding="utf-8",
                        errors="replace", buffering=1)
        except OSError as e:
            self.log_q.put(f"（日志文件创建失败，仅面板显示：{e}）\n")
            logf = None
        sink = _LogSink(self.log_q, logf)
        self._sink = sink
        title = ("核显省电切换（检测 → 清场 → 官方同款命令 → 凭证验证）"
                 if target == "on" else "显示模式切换：核显省电 → 混合模式")
        sink.write(f"\n===== {title} {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        rc = -1
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink), \
                    _redirect_stdin(self.ask_bridge):
                if target == "on":
                    counters = eng.GpuCounters()  # 切换 worker 独立计数器，用完即关
                    try:
                        rc = eng.cmd_eco_on(counters, list(self.adapters))
                    finally:
                        counters.close()
                else:
                    rc = eng.cmd_eco_off(list(self.adapters))
        except Exception:
            sink.write(traceback.format_exc())
        finally:
            if logf:
                logf.close()
            self._sink = None
            self.q.put(("ecodone", target, rc))

    def _on_ecodone(self, _target, rc):
        self.eco_running = False
        self._paused = False
        self._manual_req = True
        self.eco_on_btn.config(state="normal")
        self.eco_off_btn.config(state="normal")
        self.log_q.put(f"\n（切换编排结束，返回码 {rc}）\n")

    def _show_ask(self, bridge: _AskBridge):
        prompt = (self._sink.pending if self._sink else "").strip() or "（控制台提问，详见日志）"
        win = tk.Toplevel(self.root)
        win.title("切换问答")
        win.transient(self.root)
        win.resizable(False, False)
        ttk.Label(win, text=prompt, wraplength=540, padding=14).pack()
        entry = ttk.Entry(win, width=30)
        entry.pack(padx=14, pady=(0, 8))
        entry.insert(0, "1")
        entry.select_range(0, "end")

        def answer(v: str):
            bridge.ans_q.put(v)
            win.destroy()

        btns = ttk.Frame(win)
        btns.pack(pady=(0, 12))
        ttk.Button(btns, text="确定", command=lambda: answer(entry.get())).pack(side="left", padx=6)
        ttk.Button(btns, text="取消（=回车，取消切换）",
                   command=lambda: answer("")).pack(side="left", padx=6)
        entry.bind("<Return>", lambda e: answer(entry.get()))
        entry.focus_set()
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", lambda: answer(""))

    # ------------------------------------------------------------- 驱动层页

    def _refresh_nv_tab(self):
        def work():
            co_init()
            try:
                rows = eng.nv_module_holders(set())  # 不排除，保护名单打标记展示
                self.q.put(("nvtab", rows))
            except Exception as e:
                self.q.put(("err", f"驱动层扫描失败: {e!r}"))
        threading.Thread(target=work, daemon=True).start()

    def _apply_nvtab(self, rows):
        self.nv_tree.delete(*self.nv_tree.get_children())
        for pid, name, mods in rows:
            mark = "系统/服务组件（保护名单）" if name.lower() in eng.PROTECTED_PROCESSES else ""
            self.nv_tree.insert("", "end", values=(
                pid, name, ", ".join(sorted(mods)), mark))

    # ------------------------------------------------------------- 日志面板

    def _drain_log(self):
        n = 0
        while True:
            try:
                s = self.log_q.get_nowait()
            except queue.Empty:
                break
            at_bottom = self.log_text.yview()[1] > 0.98
            self.log_text.config(state="normal")
            self.log_text.insert("end", s)
            if int(self.log_text.index("end-1c").split(".")[0]) > 3000:
                self.log_text.delete("1.0", "500.0")
            self.log_text.config(state="disabled")
            if at_bottom:
                self.log_text.see("end")
            n += 1
            if n > 400:  # 每轮限量，防积压卡 UI
                break

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _open_log_dir(self):
        os.startfile(os.path.dirname(eng.eco_log_path()))  # 顺带建目录

    # ------------------------------------------------------------- 退出

    def _on_close(self):
        self.stop_evt.set()
        self.root.destroy()


def _move_offscreen(root: tk.Tk) -> None:
    """测试钩子（DGPU_TEST_OFFSCREEN=1）：窗口映射前移屏幕外 + 不进任务栏 +
    不抢焦点——正常启动不触发。"""
    root.update_idletasks()
    root.geometry("+30000+30000")
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id())
        style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        user32.SetWindowLongW(hwnd, -20, style | 0x80 | 0x08000000)  # TOOLWINDOW | NOACTIVATE
    except Exception:
        pass


def main() -> int:
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        adapters = eng.enumerate_adapters()
    except OSError as e:
        root.withdraw()
        messagebox.showerror("独显占用侦探", f"DXGI 枚举失败: {e}")
        return 1
    app = App(root, adapters)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    if os.environ.get("DGPU_TEST_OFFSCREEN") == "1":
        _move_offscreen(root)  # 必须在 mainloop 首次绘制前
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
