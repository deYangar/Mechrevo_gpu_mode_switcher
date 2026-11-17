"""独显占用侦探 —— 图形界面版（单文件，复用 dgpu_detective 引擎，界面层 CustomTkinter 深色主题）。

- 持有者表格 = --report/--watch 的常驻版：自动刷新，新持有黄底高亮
- 显示模式徽章 = --status 四态凭证（内屏路由 / PnP problem / DXGI / GCU 意图回报），
  附能力角标（代际 + 直连/省电能力位，启动后台探测一次）
- 「独显直连 / 混合模式 / 核显省电」（按性能排）= 内嵌控制台原样运行
  cmd_direct_on / cmd_hybrid / cmd_eco_on（cmd_hybrid 按当前态自动路由
  cmd_eco_off / cmd_direct_off）：
  print 重定向进日志面板（双写 logs/eco_*.txt、logs/hybrid_*.txt、
  logs/direct_*.txt 留档），
  ask() 的 input() 弹问答框应答（重启三选默认安全侧），
  编排代码零改动——CLI 双向真测过的路径就是 GUI 跑的路径
- 驱动层持有者（NVAPI/NVML 口径，补 WDDM 盲区）合并进主表格：后台线程自动扫，
  WDDM 已有的行补「NV 模块」列，WDDM 抓不到的（HWiNFO 等无图形上下文者）追加行
- 打包：dgpu_detective.spec（onefile + windowed + uac_admin），build_exe.ps1 一键构建
"""

import contextlib
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import dgpu_detective as eng

# DPI 感知交 CTk 内部处理（自调 shcore 会与 CTk 的 scaling 叠加缩放，已删）

# ---------------------------------------------------------------------------


def co_init() -> None:
    """工作线程里跑 DXGI 枚举（read_display_mode_state 会调 enumerate_adapters）需要 COM；
    重复初始化返回 S_FALSE，无害。"""
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # APARTMENTTHREADED
    except Exception:
        pass


def taskkill_pid(pid: int) -> tuple[bool, str]:
    """taskkill /F /T /PID：结束单个进程及其子进程树；错误原文返回，不吞。
    返回码 128 = 进程已不在（常见于刚按名批量杀过、树已退出），按已结束算成功。"""
    r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    if r.returncode == 128:
        return True, ""
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


MODE_COLORS = {  # 徽章配色（深色主题）：fg 文字, bg 底
    "hybrid": ("#86e29b", "#1f5130"),
    "igpu_only": ("#8fc4ff", "#1d3a6b"),
    "pnp_disabled": ("#ffcf8f", "#6b4a1a"),
    "dgpu_direct": ("#d4b3ff", "#4a2a6b"),
    "direct_pending": ("#ffe08f", "#6b551a"),
    "unknown": ("#ff9f9f", "#6b1f1f"),
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
    NV_SCAN_S = 3.0  # 驱动层扫描节奏：全系统模块快照单轮数百 ms，独立线程跑不拖采样

    def __init__(self, root: ctk.CTk, adapters: list):
        self.root = root
        root.title("独显占用侦探")
        # 三切换按钮（核显省电/混合模式/独显直连）在工具栏单行可见；
        # 曾是四按钮时代定的 1180（1040 下末键被裁，2026-11-17 exe 截图实测），
        # 2026-11-17 两回混合按钮合一后沿用，余量让给表格列
        root.geometry("1180x720")
        root.minsize(1000, 560)
        self._autorun_done = False    # DGPU_TEST_AUTORUN 钩子只触发一次
        self._autorun_dumped = False  # DGPU_TEST_DUMP 钩子只写一次

        # ---- 跨线程状态（工作线程与主线程各管各的写方向，GIL 下安全）----
        self.q = queue.Queue()      # 工作线程 -> 主线程：snap/err/ask/ecodone/nvtab/gcuinfo/gcu_intent
        self.nv_rows = []           # 最近一轮驱动层扫描 [(pid, name, mods)]，主线程渲染用
        self._nv_req = True         # 立即扫一轮标志（启动即扫；手动刷新/切换结束置位）
        self._nv_due = 0.0          # 下次自动扫描的时刻（monotonic）
        self.log_q = queue.Queue()  # 日志流（raw str）-> 主线程 Text
        self.stop_evt = threading.Event()
        self.adapters = adapters
        self._auto = True
        self._paused = False        # 切换编排期间暂停采样（PDH 句柄不跨线程并发）
        self._manual_req = False
        self._include_all = False
        self._interval = 1.0
        self.switch_running = False  # 切换编排（eco/direct 任一）进行中
        self.last_data = None
        self.last_st = None
        self._sink = None           # 当前切换 worker 的日志 sink（问答框读 pending）
        self.gcu_info = None        # 启动后台探测的 GcuInfo（能力角标 + 按钮启用）
        self._gcu_direct_cache = ""  # 最近一次 GCU 意图回报（凭证 B 缓存，注入每轮快照）
        self._gcu_igpu_cache = ""

        self.ask_bridge = _AskBridge(self.q)

        # 表格状态
        self.chips = {}        # luid_str -> (mem_label, eng_label)
        self.rowmeta = {}      # iid -> row dict
        self.new_marks = {}    # iid -> 剩余高亮轮数
        self.prev_keys = set()  # (luid_str, pid)

        self._build_ui()

        self.sampler = threading.Thread(target=self._sampler_loop, daemon=True)
        self.sampler.start()
        self.nv_scanner = threading.Thread(target=self._nv_loop, daemon=True)
        self.nv_scanner.start()
        threading.Thread(target=self._gcu_probe_once, daemon=True).start()
        self.root.after(self.POLL_MS, self._poll)

    # ------------------------------------------------------------- UI 构建

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        # 顶部：GPU 概要条
        self.gpu_bar = ttk.Frame(self.root, padding=(10, 6, 10, 2))
        self.gpu_bar.grid(row=0, column=0, sticky="ew")

        # 工具条
        bar = ctk.CTkFrame(self.root, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=10, pady=2)

        self.auto_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(bar, text="自动刷新", variable=self.auto_var,
                        command=self._on_auto_toggle).pack(side="left")
        ctk.CTkLabel(bar, text="间隔(s)").pack(side="left", padx=(10, 2))
        # CTk 无 Spinbox：ComboBox 可输可选兜住（预设 0.5~10.0 步进 0.5，
        # 手输其他值仍按 _on_interval 的 0.5~10 校验收）
        self.interval_spin = ctk.CTkComboBox(
            bar, values=[f"{0.5 * i:.1f}" for i in range(1, 21)], width=88,
            height=28, command=self._on_interval)
        self.interval_spin.set("1.0")
        self.interval_spin.pack(side="left")
        self.interval_spin.bind("<FocusOut>", lambda e: self._on_interval())
        ctk.CTkButton(bar, text="立即刷新", width=88, height=28,
                      command=self._refresh_now).pack(side="left", padx=6)
        self.all_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(bar, text="含核显", variable=self.all_var,
                        command=self._on_include_all).pack(side="left", padx=(6, 0))
        ctk.CTkButton(bar, text="导出 JSON", width=88, height=28,
                      command=self._export_json).pack(side="left", padx=6)

        ctk.CTkFrame(bar, width=2, corner_radius=0,
                     fg_color="#3a3a3a").pack(side="left", fill="y", padx=8)
        # 按性能排：独显直连（独显全力）→ 混合模式 → 核显省电（独显下线）；
        # 混合模式 = 目标态入口，cmd_hybrid 按当前态自动挑路
        # （省电态走 MQTT 回线、直连态走 MUX 重启三选），曾是两个互斥按钮
        self.direct_on_btn = ctk.CTkButton(
            bar, text="独显直连", width=96, height=28,
            fg_color="#6a3fa0", hover_color="#59348a",
            command=lambda: self.run_switch("direct_on"))
        self.direct_on_btn.pack(side="left")
        self.hybrid_btn = ctk.CTkButton(
            bar, text="混合模式", width=96, height=28,
            fg_color="#1f6aa5", hover_color="#1a5789",
            command=lambda: self.run_switch("hybrid"))
        self.hybrid_btn.pack(side="left", padx=6)
        self.eco_on_btn = ctk.CTkButton(
            bar, text="核显省电", width=96, height=28,
            fg_color="#2f7d4f", hover_color="#276a42",
            command=lambda: self.run_switch("on"))
        self.eco_on_btn.pack(side="left")

        # 中央：上持有者表格 / 下 Notebook，可拖分界
        pw = ttk.PanedWindow(self.root, orient="vertical")
        pw.grid(row=2, column=0, sticky="nsew", padx=10, pady=4)

        tree_frame = ttk.Frame(pw)
        cols = ("gpu", "pid", "name", "local", "nonlocal", "committed", "engines",
                "nvmods")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                 selectmode="browse")
        for cid, text, w, anchor in (
                ("gpu", "显卡", 150, "w"), ("pid", "PID", 60, "e"),
                ("name", "进程", 140, "w"), ("local", "显存(MB)", 80, "e"),
                ("nonlocal", "共享(MB)", 80, "e"), ("committed", "提交(MB)", 80, "e"),
                ("engines", "引擎活动", 210, "w"), ("nvmods", "NV 模块", 160, "w")):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor=anchor,
                             stretch=(cid in ("name", "engines", "nvmods")))
        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        # 明细条必须最先 pack（pack 按顺序分配空间）：先占满底部整行，
        # 表格和滚动条再填剩余——若表格先 pack 且 expand，整行宽度被吃光，
        # 明细只剩右侧零头，默认窗口宽度下被挤成一条竖缝、路径全截断
        self.detail = ttk.Label(tree_frame, text="选中进程后这里显示完整路径与引擎明细",
                                padding=(2, 3), wraplength=980, anchor="w",
                                foreground="#444")
        self.detail.pack(side="bottom", fill="x")
        self.detail.bind(
            "<Configure>",
            lambda e: self.detail.configure(wraplength=max(200, e.width - 8)))
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.tag_configure("new", background="#fff3b2")     # 新持有高亮
        self.tree.tag_configure("igpu", background="#eef4fd")    # 核显行
        self.tree.tag_configure("nvdrv", background="#f4eefb")   # 驱动层口径行（WDDM 不可见）
        self.tree.tag_configure("prot", foreground="#8a2f2f")    # 系统/服务组件
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._popup_menu)

        pw.add(tree_frame, weight=3)
        self._build_log_pane(pw)

        # 底部状态栏（两行）：上行=模式徽章＋管理员＋刷新时间，下行=三凭证独占全宽
        # （单行布局下窗口一窄，三凭证后半截被右侧元素挤没）
        sb = ctk.CTkFrame(self.root, fg_color="transparent")
        sb.grid(row=3, column=0, sticky="ew", padx=10, pady=(2, 6))
        sb.columnconfigure(0, weight=1)
        self.mode_badge = ctk.CTkLabel(sb, text=" … ", corner_radius=8,
                                       fg_color="#3a3a3a", text_color="#dce4ee",
                                       padx=10, pady=2)
        self.mode_badge.grid(row=0, column=0, sticky="w")
        admin = "管理员 ✓" if eng.is_admin() else "管理员 ✗（杀提权进程/切换需提权）"
        self.admin_label = ctk.CTkLabel(
            sb, text=admin,
            text_color=("#86e29b" if eng.is_admin() else "#ff9f9f"))
        self.admin_label.grid(row=0, column=1, sticky="e")
        self.refresh_label = ctk.CTkLabel(sb, text="", text_color="#8a94a3")
        self.refresh_label.grid(row=0, column=2, sticky="e", padx=(10, 0))
        self.evidence = ctk.CTkLabel(sb, text="三凭证读取中…", text_color="#8a94a3")
        self.evidence.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="结束进程树（PID）", command=lambda: self._kill(by_pid=True))
        self.menu.add_command(label="结束全部同名进程（含子进程树）", command=lambda: self._kill(by_pid=False))
        self.menu.add_separator()
        self.menu.add_command(label="复制 PID", command=self._copy_pid)
        self.menu.add_command(label="复制进程路径", command=self._copy_path)
        self.menu.add_command(label="打开文件位置", command=self._open_location)

    def _build_log_pane(self, pw):
        frame = ttk.Frame(pw, padding=4)
        pw.add(frame, weight=1)
        topbar = ttk.Frame(frame)
        topbar.pack(side="top", fill="x")
        ttk.Button(topbar, text="清空", width=6,
                   command=self._clear_log).pack(side="left")
        ttk.Button(topbar, text="打开日志目录", width=12,
                   command=self._open_log_dir).pack(side="left", padx=6)
        ttk.Label(topbar, text="切换按钮在下方运行完整切换编排，"
                               "输出同步留档 logs/（eco_/hybrid_/direct_）",
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
                # 切换期间彻底暂停（_manual_req 也挡住，留到恢复后补一轮采样）
                if active or (self._manual_req and not self._paused):
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
            # 每轮重枚举：切换显示模式后适配器会消失/重现（LUID 可能变化），
            # 启动时的快照在「省电→混合」循环后会失配（独显行永远查不到）；
            # 枚举失败（DXGI 瞬时异常）沿用上次列表
            self.adapters = eng.enumerate_adapters()
        except OSError:
            pass
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
                elif kind == "gcuinfo":
                    self.gcu_info = rest[0]
                    self._update_switch_buttons()
                elif kind == "gcu_intent":
                    self._gcu_direct_cache, self._gcu_igpu_cache = rest[0], rest[1]
                    if self.last_st is not None:
                        self._apply_status(self.last_st)  # 意图到了，重判四态
        except queue.Empty:
            pass
        self._drain_log()
        self.root.after(self.POLL_MS, self._poll)

    def _gcu_probe_once(self):
        """启动后台探测一次 GcuInfo（能力角标 + 按钮启用依据）。"""
        co_init()  # 工作线程跑 DXGI 枚举（probe_gcu → enumerate_adapters）需要 COM
        try:
            info = eng.probe_gcu()
        except Exception as e:
            self.log_q.put(f"（GCU 探测失败：{e!r}）\n")
            info = None
        self.q.put(("gcuinfo", info))

    def _refresh_gcu_intent(self):
        """GETSTATUS 拉一次 GCU 意图回报（凭证 B）更新缓存；切换结束后调用。"""
        probe = eng.DisplayModeState(problems=[], dxgi_names=[])
        try:
            eng.enrich_with_gcu_status(probe, listen_secs=2.5)
        except Exception:
            return
        self.q.put(("gcu_intent", probe.gcu_direct_status, probe.gcu_igpu_status))

    # ------------------------------------------------------------- 快照渲染

    def _apply_snapshot(self, data, st):
        self.last_data, self.last_st = data, st
        keys_now = set()
        rows_total = 0
        new_count = 0
        for gpu in data:
            for r in gpu["processes"]:
                rows_total += 1
                key = (gpu["luid"], r["pid"])
                keys_now.add(key)
                if self.prev_keys and key not in self.prev_keys:
                    self.new_marks[f"{gpu['luid']}:{r['pid']}"] = 4  # 高亮 4 轮后消退
                    new_count += 1
        self.prev_keys = keys_now
        self._render_table()

        # 高亮衰减（按采样轮次，不受 nv 重画加速）
        for iid in list(self.new_marks):
            self.new_marks[iid] -= 1
            if self.new_marks[iid] <= 0 or not self.tree.exists(iid):
                del self.new_marks[iid]
                if self.tree.exists(iid):
                    left = [t for t in self.tree.item(iid, "tags") if t != "new"]
                    self.tree.item(iid, tags=left)

        # GPU 概要条（适配器消失时撤 chip：切核显省电后独显从 DXGI 层消失）
        seen_luids = set()
        for gpu in data:
            seen_luids.add(gpu["luid"])
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
        for luid in list(self.chips):
            if luid not in seen_luids:
                self.chips[luid][0].master.destroy()  # 撤掉已消失适配器的 LabelFrame
                del self.chips[luid]

        self._apply_status(st)
        nv_only = self._nv_only_count()
        nv_extra = f"｜ 驱动层 {nv_only}" if nv_only else ""
        extra = f"｜ 新持有 {new_count}" if new_count else ""
        self.refresh_label.configure(
            text=f"{time.strftime('%H:%M:%S')} · {rows_total} 持有{extra}{nv_extra}")

        # 后台自测钩子：首帧数据就绪后自动选中首行（只读操作）
        if not self._autorun_done and os.environ.get("DGPU_TEST_AUTORUN") == "1":
            self._autorun_done = True
            kids = self.tree.get_children()
            if kids:
                self.tree.selection_set(kids[0])
                self.tree.focus(kids[0])

    def _nv_only_count(self) -> int:
        """驱动层独有（WDDM 表抓不到）的持有者数。"""
        wdm = {r["pid"] for gpu in (self.last_data or []) for r in gpu["processes"]}
        return sum(1 for pid, _, _ in self.nv_rows if pid not in wdm)

    def _render_table(self):
        """按 last_data + nv_rows 重画主表格：WDDM 行 + 驱动层合并渲染。"""
        data = self.last_data or []
        sel = self.tree.selection()
        sel_iid = sel[0] if sel else None

        self.tree.delete(*self.tree.get_children())
        self.rowmeta.clear()
        nv_map = {pid: mods for pid, _name, mods in self.nv_rows}
        wdm_pids = set()
        for gpu in data:
            short = short_gpu(gpu["name"])
            for r in gpu["processes"]:
                iid = f"{gpu['luid']}:{r['pid']}"
                wdm_pids.add(r["pid"])
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
                mods = nv_map.get(r["pid"])
                mods_str = ", ".join(sorted(mods)) if mods else ""
                self.tree.insert("", "end", iid=iid, tags=tags, values=(
                    short, r["pid"], name, f"{r['local_mb']:.2f}",
                    f"{r['nonlocal_mb']:.2f}", f"{r['committed_mb']:.2f}", engstr,
                    mods_str))
                self.rowmeta[iid] = dict(r, gpu=gpu["name"], discrete=gpu["discrete"])

        # 驱动层独有行：WDDM 表抓不到的 NVAPI/NVML 持有者（HWiNFO 等无图形上下文者）
        for pid, name, mods in sorted(self.nv_rows):
            if pid in wdm_pids:
                continue
            iid = f"nv:{pid}"
            tags = ["nvdrv"]
            if is_protected(name):
                tags.append("prot")
            self.tree.insert("", "end", iid=iid, tags=tags, values=(
                "驱动层（无上下文）", pid, name, "", "", "", "",
                ", ".join(sorted(mods))))
            self.rowmeta[iid] = {
                "pid": pid, "image": name, "engines": {},
                "local_mb": 0.0, "nonlocal_mb": 0.0, "committed_mb": 0.0,
                "gpu": "驱动层（NVAPI/NVML 口径）", "discrete": True,
                "nv_only": True, "nv_mods": mods,
            }
        if sel_iid and self.tree.exists(sel_iid):
            self.tree.selection_set(sel_iid)

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
        # 注入最近一次 GCU 意图回报缓存（快照线程不查 MQTT——每秒轮询会互踢常驻客户端）
        if not st.gcu_direct_status and self._gcu_direct_cache:
            st.gcu_direct_status = self._gcu_direct_cache
        if not st.gcu_igpu_status and self._gcu_igpu_cache:
            st.gcu_igpu_status = self._gcu_igpu_cache
        mode = st.mode
        fg, bg = MODE_COLORS.get(mode, MODE_COLORS["unknown"])
        self.mode_badge.configure(text=f" {eng.MODE_LABELS.get(mode, mode)} ",
                                  text_color=fg, fg_color=bg)
        panel = {"discrete": "内屏:独显", "integrated": "内屏:核显"}.get(st.panel, "内屏:未知")
        probs = "; ".join(eng.STATE_LABELS.get(p, p).split("（")[0] for _, p in st.problems)
        probs = f"PnP: {probs}" if probs else "PnP: 不可读"
        if st.dxgi_error:
            dxgi = f"DXGI 枚举失败: {st.dxgi_error}"
        elif st.dxgi_names:
            dxgi = f"DXGI 可见: {'; '.join(short_gpu(n) for n in st.dxgi_names)}"
        else:
            dxgi = "DXGI 不可见独显"
        intent = f"IGPUonly={st.igpu_only}" if st.igpu_only is not None else "IGPUonly=未写"
        gcu_intent = ""
        di = eng.resolve_direct_status(st.gcu_direct_status)
        if di is not None:
            gcu_intent = {2: "直连", 0: "核显", 1: "混合"}.get(di, "?")
            gcu_intent = f"  ｜  GCU意图: {gcu_intent}"
        caps = ""
        i = self.gcu_info
        if i is not None:
            dcap = {True: "✓", False: "✗", None: "?"}.get(i.dgpu_direct, "?")
            icap = {True: "✓", False: "✗", None: "?"}.get(i.igpu_only, "?")
            caps = (f"  ｜  Gen{i.generation.removeprefix('gen') or '?'}"
                    f" 直连{dcap} 省电{icap}")
        self.evidence.configure(text=f"{panel}  ｜  {probs}  ｜  {dxgi}  ｜  {intent}"
                                  f"{gcu_intent}{caps}")
        self._update_switch_buttons()

    # ------------------------------------------------------------- 表格交互

    def _on_select(self, _ev=None):
        sel = self.tree.selection()
        if not sel:
            return
        m = self.rowmeta.get(sel[0])
        if not m:
            return
        if m.get("nv_only"):
            mods = ", ".join(sorted(m["nv_mods"]))
            self.detail.config(
                text=f"驱动层口径：{m['image']}  (pid {m['pid']})\n"
                     f"加载 {mods}  ｜  无图形上下文，WDDM 表不可见（传感器轮询类软件在此现形）",
                foreground="#222")
            return
        active = {k: v for k, v in m["engines"].items() if v > 0.05}
        engines = "  ".join(f"{k} {v:.1f}%" for k, v in active.items()) or "仅上下文(0%)"
        prot = "  ｜ 系统/服务组件（保护名单，不建议结束）" if is_protected(m["image"]) else ""
        self.detail.config(text=f"{m['image']}  (pid {m['pid']})\n"
                                 f"显存 {m['local_mb']:.2f} MB  ｜  {engines}{prot}",
                           foreground="#222")

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
        self._nv_req = True

    def _on_interval(self):
        try:
            v = float(self.interval_spin.get())
            if 0.5 <= v <= 10.0:  # 与 Spinbox 范围一致；太短 rate 计数器读数不稳
                self._interval = v
        except ValueError:
            pass

    def _on_include_all(self):
        self._include_all = self.all_var.get()
        self._manual_req = True

    def _refresh_now(self):
        self._manual_req = True
        self._nv_req = True

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

    def run_switch(self, target: str):
        """target: direct_on（独显直连）/ hybrid（回混合，按态自动路由）/ on（核显省电）。

        hybrid/direct 方向不杀进程不动 PnP（省电态串联的 cmd_eco_off 同样无需提权），
        所以只有 eco on 要求管理员——与 CLI 口径一致。
        """
        if self.switch_running:
            return
        if target == "on" and not eng.is_admin():
            messagebox.showwarning(
                "独显占用侦探",
                "未以管理员运行：清场要杀以管理员运行的软件（HWiNFO 等），切不动。\n"
                "exe 版自带 UAC 提权；源码运行请用管理员终端启动。")
            return
        self.switch_running = True
        self._paused = True  # PDH 句柄不与切换 worker 并发；nv 扫描同停
        self._update_switch_buttons()
        threading.Thread(target=self._switch_worker, args=(target,), daemon=True).start()

    def _switch_buttons(self) -> tuple:
        return (self.direct_on_btn, self.hybrid_btn, self.eco_on_btn)

    def _update_switch_buttons(self):
        """按当前态与能力启用按钮（方案 §三 F：按态与能力启用）。"""
        if self.switch_running:
            for b in self._switch_buttons():
                b.configure(state="disabled")
            return
        mode = self.last_st.mode if self.last_st else "unknown"
        i = self.gcu_info
        eco_ok = not (i and i.igpu_only is False)
        direct_ok = not (i and i.dgpu_direct is False)
        self.direct_on_btn.configure(
            state="normal" if mode != "dgpu_direct" and direct_ok else "disabled")
        # 混合模式在三种非混合态可点；走哪条路由 cmd_hybrid 现场判，
        # 词表/能力细 gate 也在各分支里（同 CLI 口径）
        self.hybrid_btn.configure(
            state="normal" if mode in ("igpu_only", "dgpu_direct", "direct_pending")
            else "disabled")
        self.eco_on_btn.configure(state="normal" if mode != "igpu_only" and eco_ok else "disabled")

    def _switch_worker(self, target: str):
        co_init()
        try:
            logf = open(eng.eco_log_path({"on": "eco", "hybrid": "hybrid",
                                          "direct_on": "direct"}[target]),
                        "w", encoding="utf-8", errors="replace", buffering=1)
        except OSError as e:
            self.log_q.put(f"（日志文件创建失败，仅面板显示：{e}）\n")
            logf = None
        sink = _LogSink(self.log_q, logf)
        self._sink = sink
        titles = {
            "direct_on": "独显直连切换（探测 → 发令 → GCU 确认 → 重启三选）",
            "hybrid": "回混合模式（按当前态自动路由：省电→MQTT 回线 / 直连→MUX 重启三选）",
            "on": "核显省电切换（检测 → 清场 → 官方同款命令 → 凭证验证）",
        }
        sink.write(f"\n===== {titles.get(target, target)} "
                   f"{time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
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
                elif target == "hybrid":
                    rc = eng.cmd_hybrid()
                else:
                    rc = eng.cmd_direct_on()
        except Exception:
            sink.write(traceback.format_exc())
        finally:
            if logf:
                logf.close()
            self._sink = None
            self.q.put(("ecodone", target, rc))

    def _on_ecodone(self, _target, rc):
        self.switch_running = False
        self._paused = False
        self._manual_req = True
        self._nv_req = True
        self._update_switch_buttons()
        self.log_q.put(f"\n（切换编排结束，返回码 {rc}）\n")
        # 切换后 GCU 意图可能已翻转（直连待重启态判定依据），拉一次凭证 B
        threading.Thread(target=self._refresh_gcu_intent, daemon=True).start()

    def _show_ask(self, bridge: _AskBridge):
        prompt = (self._sink.pending if self._sink else "").strip() or "（控制台提问，详见日志）"
        win = tk.Toplevel(self.root)
        win.title("切换问答")
        win.transient(self.root)
        win.resizable(False, False)
        ttk.Label(win, text=prompt, wraplength=540, padding=14).pack()

        def answer(v: str):
            bridge.ans_q.put(v)
            win.destroy()

        # 编号问答按钮化：解析 prompt 里的「[N] 描述」逐项一钮，[回车/其他] 描述=取消项；
        # 解析不出编号（兜底问答）才退回输入框
        opts = re.findall(r"\[(\d+)\]\s*([^\[\]]+)", prompt)
        m_cancel = re.search(r"\[回车[^\]]*\]\s*([^\[\]]+)", prompt)
        if opts:
            body = ttk.Frame(win)
            body.pack(padx=14, pady=(0, 12), fill="x")
            focused = None

            def hook_enter(b: tk.Button):
                b.bind("<Return>", lambda e: b.invoke())

            for num, desc in opts:
                b = tk.Button(body, text=f"{num}｜{desc.strip()}", anchor="w",
                              justify="left", wraplength=500, padx=10, pady=4,
                              command=lambda n=num: answer(n))
                b.pack(fill="x", pady=3)
                # 回车默认落在预期项（旧口径：重启三选焦点在 2=稍后自行重启，
                # 一键回车不触发立即重启；清场确认仍默认 1）
                want = "2" if "立即重启" in prompt else "1"
                if num == want:
                    focused = b
            cancel_desc = m_cancel.group(1).strip() if m_cancel else "取消切换"
            cb = tk.Button(body, text=f"回车/其他｜{cancel_desc}", anchor="w",
                           justify="left", wraplength=500, padx=10, pady=4,
                           command=lambda: answer(""))
            cb.pack(fill="x", pady=(3, 0))
            if focused:
                focused.focus_set()
                hook_enter(focused)
            win.bind("<Escape>", lambda e: answer(""))
        else:
            entry = ttk.Entry(win, width=30)
            entry.pack(padx=14, pady=(0, 8))
            ttk.Button(win, text="确定", command=lambda: answer(entry.get())).pack(pady=(0, 12))
            entry.bind("<Return>", lambda e: answer(entry.get()))
            entry.focus_set()
        # 居中于主窗口（Toplevel 默认落屏幕左上角，切换问答该跟着主窗走）
        win.update_idletasks()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        ww, wh = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{rx + max(0, (rw - ww) // 2)}+{ry + max(0, (rh - wh) // 3)}")
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", lambda: answer(""))

    # ------------------------------------------------------------- 驱动层扫描

    def _nv_loop(self):
        """后台驱动层扫描：自动模式每 NV_SCAN_S 一轮；_nv_req 立即触发（启动、
        手动刷新、切换结束）；_paused（切换期间）全停。全系统模块快照单轮数百 ms，
        独立线程跑，不占采样节奏、不卡 UI。"""
        while not self.stop_evt.is_set():
            if not self._paused and (
                    self._nv_req or (self._auto and time.monotonic() >= self._nv_due)):
                self._nv_req = False
                self._nv_due = time.monotonic() + self.NV_SCAN_S
                try:
                    # 不排除保护名单——保护名单行照常显示、标红不杀
                    self.q.put(("nvtab", eng.nv_module_holders(set())))
                except Exception as e:
                    self.q.put(("err", f"驱动层扫描失败: {e!r}"))
            self.stop_evt.wait(0.3)

    def _apply_nvtab(self, rows):
        self.nv_rows = rows
        if self.last_data:
            self._render_table()  # 自动刷新关闭时也立即并入表格
        # 后台自测钩子（DGPU_TEST_DUMP=路径）：nv 首轮到达后 10s 再 dump——
        # GCU 探测（MQTT ~5s）须已到位，早期快照会把 gcu_info 误读成 None
        #（PrintWindow 抓不到屏幕外 Treeview 内容，像素验证不了，只能 dump）
        if not self._autorun_dumped and os.environ.get("DGPU_TEST_DUMP"):
            self._autorun_dumped = True
            self.root.after(10000, self._dump_tree)

    def _dump_tree(self):
        path = os.environ["DGPU_TEST_DUMP"]
        cols = [self.tree.heading(c)["text"] for c in self.tree["columns"]]
        lines = ["\t".join(cols)]
        for iid in self.tree.get_children():
            tags = ",".join(self.tree.item(iid, "tags"))
            vals = "\t".join(str(v) for v in self.tree.item(iid, "values"))
            lines.append(f"[{tags}]\t{vals}")
        lines.append(f"nv_rows: {self.nv_rows!r}")
        # 明细条几何：验证「先 pack 占满底行」没回退（被挤成竖缝时 w 只有百余像素）
        lines.append(f"detail: w={self.detail.winfo_width()} "
                     f"wraplength={self.detail.cget('wraplength')} "
                     f"tree_w={self.tree.winfo_width()}")
        # 切换编排状态（通用化自测）：GCU 探测与三按钮态
        gi = self.gcu_info
        lines.append(f"gcu_info: {None if gi is None else (gi.connected, gi.generation, gi.client_id, gi.igpu_only, gi.dgpu_direct)}")
        lines.append("buttons: " + ",".join(
            f"{str(b.cget('text'))}={b.cget('state')}" for b in self._switch_buttons()))
        lines.append(f"badge: {self.mode_badge.cget('text')!r} evidence: {self.evidence.cget('text')!r}")
        # 状态栏几何：验证两行布局（evidence 须在徽章下一行且占全宽；
        # 单行回退时两者 y 相等）
        lines.append(f"statusbar: badge_y={self.mode_badge.winfo_y()} "
                     f"ev_y={self.evidence.winfo_y()} ev_w={self.evidence.winfo_width()}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    # ------------------------------------------------------------- 日志面板

    def _drain_log(self):
        at_bottom = self.log_text.yview()[1] > 0.98  # 排空前是否贴底，整批统一判断
        n = 0
        while True:
            try:
                s = self.log_q.get_nowait()
            except queue.Empty:
                break
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
        if self.switch_running:
            if not messagebox.askyesno(
                    "独显占用侦探",
                    "切换编排正在进行（详见切换日志），现在退出可能停在"
                    "「清了场没切完」的中间态。\n确定退出吗？"):
                return
        self.stop_evt.set()
        self.root.destroy()


def _move_offscreen(root: ctk.CTk) -> None:
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
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
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
