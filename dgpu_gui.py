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


def _patch_ctk_button_hitbox() -> None:
    """CTkButton 6.0.0 命中缺陷治本（module 级类补丁，import 即生效）：
    command 触发 gate 在 _mouse_inside，而它只由内部 _text_label（文字区）
    的 <Enter>/<Leave> 维护，画布层连 <ButtonRelease-1> 都没绑——鼠标在
    文字外的空白区按下/释放时静默无反应（2026-11-17 真机「模式按钮点了
    没反应」根因：工具栏按钮文字占比不足一半，大面积点不中）。
    画布与文字层是兄弟层，给画布补绑 Enter/Leave/ButtonRelease 三件套后
    互补维护 _mouse_inside，整按钮区域命中；hover 高亮顺带覆盖空白区。
    只补缺失绑定，不动 CTk 状态机；防重入打补丁。"""
    if getattr(ctk.CTkButton, "_dgpu_hitbox_patched", False):
        return
    ctk.CTkButton._dgpu_hitbox_patched = True
    orig_init = ctk.CTkButton.__init__

    def patched_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        self._canvas.bind("<Enter>", self._on_enter, add="+")
        self._canvas.bind("<Leave>", self._on_leave, add="+")
        self._canvas.bind("<ButtonRelease-1>", self._on_release, add="+")

    ctk.CTkButton.__init__ = patched_init


_patch_ctk_button_hitbox()


# 模式名（日志反馈用）：与按钮文案一致
MODE_NAMES = {"direct_on": "独显直连", "hybrid": "混合模式", "on": "核显省电"}


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


# ---------------------------------------------------------------------------
# 设计系统：单色板源（GitHub Dark 系），全界面颜色/字体只从这里取。
# 中文统一 Microsoft YaHei UI（CTk 默认 Roboto 无中文字形会回退宋体——
# 迁移后「丑」的根源）；雅黑数字为 tabular 等宽（实测 0/1 同宽 9px@12pt），
# 表格数字列与时间戳排版不受比例宽影响，不需要 Consolas（其无中文字形）。

BG = "#0d1117"        # 窗口底
SURFACE = "#161b22"   # 卡片/面板
SURFACE2 = "#21262d"  # 次级面板/输入底/表格 field
BORDER = "#30363d"    # 边框/分隔
TEXT = "#e6edf3"      # 主文字
MUTED = "#8b949e"     # 次要文字
ACCENT = "#4493f8"    # 主色
ACCENT_DARK = "#1e3a5f"  # 选中底（表格行选中）
GREEN = "#3fb950"     # 成功/核显省电
GREEN_HOVER = "#46c05a"
PURPLE = "#a371f7"    # 独显直连
PURPLE_HOVER = "#b284f8"
YELLOW = "#e3b341"    # 警示/保护名单
RED = "#f85149"       # 危险/错误（只给「危险操作」与「故障态」，不做行底色）

FONT_FAMILY = "Microsoft YaHei UI"

MODE_COLORS = {  # 徽章配色：fg 文字, bg 底
    "hybrid": ("#7ee787", "#1b3522"),
    "igpu_only": ("#79c0ff", "#1c2e4a"),
    "pnp_disabled": ("#f0883e", "#3d2b12"),
    "dgpu_direct": ("#d2a8ff", "#31215c"),
    "direct_pending": ("#e3b341", "#3d2e12"),
    "unknown": ("#f85149", "#45191c"),
}

SWITCH_COLORS = {  # 切换按钮当前态实色：与徽章的模式-颜色心智映射呼应
    "direct_on": (PURPLE, PURPLE_HOVER),
    "hybrid": (ACCENT, "#65a0f8"),
    "on": (GREEN, GREEN_HOVER),
}


def apply_theme() -> None:
    """全局主题：窗口底色 + CTk 主题表 patch（checkbox/combobox/scrollbar 等
    不逐个显式配色的控件跟着 ACCENT 走）。在建控件前调用。"""
    theme = ctk.ThemeManager.theme
    for cls, key, val in (
            ("CTkCheckBox", "fg_color", ACCENT),
            ("CTkCheckBox", "hover_color", "#65a0f8"),
            ("CTkComboBox", "button_color", SURFACE2),
            ("CTkComboBox", "button_hover_color", BORDER),
            ("CTkComboBox", "border_color", BORDER),
            ("CTkComboBox", "fg_color", SURFACE2),
            ("CTkDropdownMenu", "fg_color", SURFACE2),
            ("CTkDropdownMenu", "hover_color", BORDER),
            ("CTkScrollbar", "button_color", BORDER),
            ("CTkScrollbar", "button_hover_color", MUTED),
            ("CTkProgressBar", "fg_color", ACCENT),
            ("CTkProgressBar", "progress_color", ACCENT),
            ("CTkTextbox", "border_color", BORDER)):
        try:
            theme[cls][key] = val
        except KeyError:
            pass  # 主题表结构随 CTk 版本变化，缺项跳过（显式配色不依赖它）


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

    def _ghost_button(self, master, text, width=88, command=None):
        """次级按钮统一 ghost 风：次级面板实底 + 描边（transparent 底+描边
        在深窗上发暗难认，2026-11-17 咩咩反馈；实底把按钮从背景里托出来），
        主色只留给「当前模式」。"""
        b = ctk.CTkButton(master, text=text, width=width, height=32,
                          fg_color=SURFACE2, hover_color=BORDER,
                          border_width=1, border_color=BORDER,
                          text_color=TEXT, corner_radius=6,
                          font=(FONT_FAMILY, 12), command=command)
        return b

    def _style_switch(self, btn, target: str, active: bool):
        """切换按钮态式：当前模式的按钮实色高亮，其余中性描边——
        一眼读出「现在在哪个模式、能去哪」；彩-模式映射与徽章呼应。
        当前态文字锁定白（含 disabled：切换进行中不被 CTk 压成灰黑）；
        非当前态可点时亮字（MUTED 暗字读不出「可点」，2026-11-17 咩咩反馈），
        disabled（能力不支持）才落 MUTED 灰。"""
        color, hover = SWITCH_COLORS[target]
        if active:
            btn.configure(fg_color=color, hover_color=hover,
                          border_width=0, text_color="#ffffff",
                          text_color_disabled="#ffffff")
        else:
            btn.configure(fg_color="transparent", hover_color=SURFACE2,
                          border_width=1, border_color=BORDER,
                          text_color=TEXT, text_color_disabled=MUTED)

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        # 顶部：GPU 概要条（卡片横排，结构见 _chip）
        self.gpu_bar = ctk.CTkFrame(self.root, fg_color="transparent")
        self.gpu_bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))

        # 工具条
        bar = ctk.CTkFrame(self.root, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=10, pady=2)

        self.auto_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(bar, text="自动刷新", variable=self.auto_var,
                        font=(FONT_FAMILY, 12),
                        command=self._on_auto_toggle).pack(side="left")
        ctk.CTkLabel(bar, text="间隔(s)", font=(FONT_FAMILY, 12),
                     text_color=MUTED).pack(side="left", padx=(10, 2))
        # CTk 无 Spinbox：ComboBox 可输可选兜住（预设 0.5~10.0 步进 0.5，
        # 手输其他值仍按 _on_interval 的 0.5~10 校验收）
        self.interval_spin = ctk.CTkComboBox(
            bar, values=[f"{0.5 * i:.1f}" for i in range(1, 21)], width=88,
            height=28, font=(FONT_FAMILY, 12), dropdown_font=(FONT_FAMILY, 12),
            command=self._on_interval)
        self.interval_spin.set("1.0")
        self.interval_spin.pack(side="left")
        self.interval_spin.bind("<FocusOut>", lambda e: self._on_interval())
        self._ghost_button(bar, "立即刷新", command=self._refresh_now).pack(
            side="left", padx=6)
        self.all_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(bar, text="含核显", variable=self.all_var,
                        font=(FONT_FAMILY, 12),
                        command=self._on_include_all).pack(side="left", padx=(6, 0))
        self._ghost_button(bar, "导出 JSON", command=self._export_json).pack(
            side="left", padx=6)

        # 竖分隔线：CTkFrame 默认 height=200 逻辑，fill="y" 会把整条工具栏撑到
        # 300px 高、按钮挤中间留大片空白——高度必须显式给小
        ctk.CTkFrame(bar, width=1, height=22, corner_radius=0,
                     fg_color=BORDER).pack(side="left", fill="y", padx=10)
        # 按性能排：独显直连（独显全力）→ 混合模式 → 核显省电（独显下线）；
        # 混合模式 = 目标态入口，cmd_hybrid 按当前态自动挑路
        # （省电态走 MQTT 回线、直连态走 MUX 重启三选），曾是两个互斥按钮。
        # 态式：默认中性描边，当前模式的按钮由 _update_switch_buttons 点亮
        self.direct_on_btn = ctk.CTkButton(
            bar, text="独显直连", width=96, height=32,
            corner_radius=6, font=(FONT_FAMILY, 12),
            command=lambda: self.run_switch("direct_on"))
        self.direct_on_btn.pack(side="left")
        self.hybrid_btn = ctk.CTkButton(
            bar, text="混合模式", width=96, height=32,
            corner_radius=6, font=(FONT_FAMILY, 12),
            command=lambda: self.run_switch("hybrid"))
        self.hybrid_btn.pack(side="left", padx=6)
        self.eco_on_btn = ctk.CTkButton(
            bar, text="核显省电", width=96, height=32,
            corner_radius=6, font=(FONT_FAMILY, 12),
            command=lambda: self.run_switch("on"))
        self.eco_on_btn.pack(side="left")

        # 中央：上持有者表格 / 下 Notebook，可拖分界
        self.pw = ttk.PanedWindow(self.root, orient="vertical")
        self.pw.grid(row=2, column=0, sticky="nsew", padx=10, pady=4)

        tree_frame = ctk.CTkFrame(self.pw, fg_color="transparent")
        # ttk.Treeview 深色化一处配置（clam 主题才吃 fieldbackground；
        # 默认 vista/w11 主题的 Treeview 配色改不动）
        style = ttk.Style(self.root)
        style.theme_use("clam")
        # ttk 是原生控件，不吃 CTk 的字体缩放——高 DPI 屏（150%）上与 CTk
        # 控件同 pt 值也显小（2026-11-17 咩咩反馈：表格字体小）。
        # pt/行高显式乘窗口缩放系数，对齐 CTk 控件视觉
        #（不走 root._get_widget_scaling：主窗该属性初始化时序不稳，实测炸）
        try:
            scale = ctk.ScalingTracker.get_window_scaling(self.root)
        except Exception:
            scale = 1.0
        style.configure("Dgpu.Treeview", fieldbackground=SURFACE,
                        background=SURFACE, foreground=TEXT,
                        rowheight=int(34 * scale), borderwidth=0,
                        font=(FONT_FAMILY, round(12 * scale)))
        style.configure("Dgpu.Treeview.Heading", background=SURFACE2,
                        foreground="#c9d1d9", relief="flat", borderwidth=0,
                        font=(FONT_FAMILY, round(12 * scale), "bold"))
        style.map("Dgpu.Treeview",
                  background=[("selected", ACCENT_DARK)],
                  fieldbackground=[("selected", ACCENT_DARK)])
        style.map("Dgpu.Treeview.Heading", background=[("active", BORDER)])
        cols = ("gpu", "pid", "name", "local", "nonlocal", "committed", "engines",
                "nvmods")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                 selectmode="browse", style="Dgpu.Treeview")
        for cid, text, w, anchor in (
                ("gpu", "显卡", 178, "w"), ("pid", "PID", 70, "e"),
                ("name", "进程", 150, "w"), ("local", "显存(MB)", 88, "e"),
                ("nonlocal", "共享(MB)", 88, "e"), ("committed", "提交(MB)", 88, "e"),
                ("engines", "引擎活动", 200, "w"), ("nvmods", "NV 模块", 190, "w")):
            self.tree.heading(cid, text=text)
            # 列宽同样乘缩放：字体放大后固定列宽会截断数值/表头
            #（1251x 五位 PID 只剩四位、表头「显存(MB)」剩「显存(M」，2026-11-17 截图实测）
            self.tree.column(cid, width=int(w * scale), anchor=anchor,
                             stretch=(cid in ("name", "engines", "nvmods")))
        ysb = ctk.CTkScrollbar(tree_frame, orientation="vertical",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        # 明细条必须最先 pack（pack 按顺序分配空间）：先占满底部整行，
        # 表格和滚动条再填剩余——若表格先 pack 且 expand，整行宽度被吃光，
        # 明细只剩右侧零头，默认窗口宽度下被挤成一条竖缝、路径全截断
        self.detail = ctk.CTkLabel(
            tree_frame, text="选中进程后这里显示完整路径与引擎明细",
            wraplength=980, anchor="w", justify="left", text_color=MUTED,
            font=(FONT_FAMILY, 12), fg_color=SURFACE,
            corner_radius=6, padx=8, pady=3)
        self.detail.pack(side="bottom", fill="x")
        self.detail.bind(
            "<Configure>",
            lambda e: self.detail.configure(wraplength=max(200, e.width - 8)))
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.tag_configure("new", background="#3d2e12",
                                foreground=YELLOW)              # 新持有高亮
        self.tree.tag_configure("igpu", background="#141d2e")    # 核显行
        self.tree.tag_configure("nvdrv", background="#191430")   # 驱动层口径行
        self.tree.tag_configure("odd", background="#1c2128")     # 斑马纹（够读出隔行又不喧宾）
        # 系统/服务组件：警示黄（整行红是红色滥用，黄=「不建议结束」的份量）
        self.tree.tag_configure("prot", foreground=YELLOW)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._popup_menu)

        self.pw.add(tree_frame, weight=3)
        self._build_log_pane(self.pw)
        # 初始 sash 位置：表格为主（requested 高度只随首帧行数走，行少时
        # 日志区会反客为主；行高放大后 ~12 行需要更多纵向空间，
        # sash 同步乘缩放），用户仍可拖
        self.root.after(200, lambda: self.pw.sashpos(0, int(420 * scale)))

        # 底部状态栏（两行）：上行=模式徽章＋管理员＋刷新时间，下行=三凭证独占全宽
        # （单行布局下窗口一窄，三凭证后半截被右侧元素挤没）
        sb = ctk.CTkFrame(self.root, fg_color="transparent")
        sb.grid(row=3, column=0, sticky="ew", padx=10, pady=(2, 8))
        sb.columnconfigure(0, weight=1)
        self.mode_badge = ctk.CTkLabel(sb, text=" … ", corner_radius=10,
                                       fg_color=SURFACE2, text_color=TEXT,
                                       padx=12, pady=3, font=(FONT_FAMILY, 12))
        self.mode_badge.grid(row=0, column=0, sticky="w")
        admin = "管理员 ✓" if eng.is_admin() else "管理员 ✗（杀提权进程/切换需提权）"
        self.admin_label = ctk.CTkLabel(
            sb, text=admin, font=(FONT_FAMILY, 12),
            text_color=(GREEN if eng.is_admin() else RED))
        self.admin_label.grid(row=0, column=1, sticky="e")
        self.refresh_label = ctk.CTkLabel(sb, text="", text_color=MUTED,
                                          font=(FONT_FAMILY, 12))
        self.refresh_label.grid(row=0, column=2, sticky="e", padx=(10, 0))
        self.evidence = ctk.CTkLabel(sb, text="三凭证读取中…", text_color=MUTED,
                                     font=(FONT_FAMILY, 12))
        self.evidence.grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))

        self.menu = tk.Menu(self.root, tearoff=0,
                            bg=SURFACE2, fg=TEXT, activebackground=ACCENT_DARK,
                            activeforeground=TEXT, bd=0,
                            font=(FONT_FAMILY, 12))
        self.menu.add_command(label="结束进程树（PID）", command=lambda: self._kill(by_pid=True))
        self.menu.add_command(label="结束全部同名进程（含子进程树）", command=lambda: self._kill(by_pid=False))
        self.menu.add_separator()
        self.menu.add_command(label="复制 PID", command=self._copy_pid)
        self.menu.add_command(label="复制进程路径", command=self._copy_path)
        self.menu.add_command(label="打开文件位置", command=self._open_location)

    def _build_log_pane(self, pw):
        frame = ctk.CTkFrame(pw, fg_color="transparent")
        pw.add(frame, weight=1)
        topbar = ctk.CTkFrame(frame, fg_color="transparent")
        topbar.pack(side="top", fill="x", padx=4, pady=(4, 0))
        self._ghost_button(topbar, "清空", width=64,
                           command=self._clear_log).pack(side="left")
        self._ghost_button(topbar, "打开日志目录", width=110,
                           command=self._open_log_dir).pack(side="left", padx=6)
        ctk.CTkLabel(topbar, text="切换按钮在上方运行完整切换编排，"
                                  "输出同步留档 logs/（eco_/hybrid_/direct_）",
                     text_color=MUTED, font=(FONT_FAMILY, 12)).pack(
            side="left", padx=6)

        body = ctk.CTkFrame(frame, fg_color="transparent")
        body.pack(side="top", fill="both", expand=True, padx=4, pady=4)
        # CTkTextbox：底色面板色（与窗口底区分出边界）；wrap=word 长行换行
        #（切换编排输出是行文本，横滚找内容不如换行直读，横滚条撤掉）
        self.log_text = ctk.CTkTextbox(body, height=6, wrap="word",
                                       font=(FONT_FAMILY, 12),
                                       fg_color=SURFACE,
                                       text_color="#a8b3c2",
                                       state="disabled")
        ysb = ctk.CTkScrollbar(body, orientation="vertical",
                               command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ysb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
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
        # 处理段整体兜底：任何一条消息/日志渲染异常都不得杀死 after 重排——
        # 曾经 _drain_log 一个 AttributeError 就冻结整条事件链（日志不显示、
        # 快照停更、问答框弹不出，2026-11-17 真机事故）
        try:
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
        except Exception:
            self.log_q.put(traceback.format_exc() + "\n")
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
            mem_l, eng_l, pbar = self._chip(gpu["luid"], gpu["name"], gpu["discrete"])
            mem_l.configure(text=f"显存 {gpu['adapter_dedicated_mb']:.2f} / "
                                 f"{gpu['card_vram_mb']:.2f} MB")
            ratio = (gpu["adapter_dedicated_mb"] / gpu["card_vram_mb"]
                     if gpu["card_vram_mb"] > 0 else 0.0)
            pbar.set(min(1.0, max(0.0, ratio)))
            best_k, best_v = "", 0.0
            for r in gpu["processes"]:
                for k, v in r["engines"].items():
                    if v > best_v:
                        best_k, best_v = k, v
            peak = f"引擎峰值 {best_k} {best_v:.1f}%" if best_v > 0.05 else "引擎空闲"
            eng_l.configure(text=f"{len(gpu['processes'])} 持有 · {peak}")
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
        row_i = 0
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
                if row_i % 2:
                    tags.append("odd")  # 斑马纹挂在末位：语义 tag（new/prot）优先
                row_i += 1
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
            if row_i % 2:
                tags.append("odd")
            row_i += 1
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
            # 横条面板段：GPU 名 | 显存数值 | 占用条 | 持有/引擎 横向排——
            # 全宽铺满（多卡平分），顶行高度 ~56px，不留竖卡时代的两侧空白；
            # 左缘色条区分独显（紫）/核显（蓝），与模式徽章配色呼应
            f = ctk.CTkFrame(self.gpu_bar, corner_radius=8, border_width=1,
                             border_color=("#31215c" if discrete else "#1c2e4a"),
                             fg_color=SURFACE, height=56)
            f.pack(side="left", fill="both", expand=True, padx=(0, 8))
            f.pack_propagate(False)
            ctk.CTkFrame(f, width=3, height=40, corner_radius=2,
                         fg_color=("#a371f7" if discrete else "#4493f8")
                         ).grid(row=0, column=0, rowspan=4,
                                sticky="ns", padx=(10, 8), pady=8)
            ctk.CTkLabel(f, text=("独显 · " if discrete else "") + short_gpu(name),
                         text_color=TEXT, font=(FONT_FAMILY, 12, "bold")).grid(
                row=0, column=1, sticky="w", padx=(0, 14))
            mem_l = ctk.CTkLabel(f, text="显存 …", text_color=MUTED,
                                 font=(FONT_FAMILY, 12))
            mem_l.grid(row=0, column=2, sticky="w", padx=(0, 10))
            pbar = ctk.CTkProgressBar(f, height=5, corner_radius=3,
                                      fg_color=SURFACE2, progress_color=ACCENT)
            pbar.grid(row=0, column=3, sticky="ew", pady=2)
            eng_l = ctk.CTkLabel(f, text="…", text_color=MUTED,
                                 font=(FONT_FAMILY, 12))
            eng_l.grid(row=0, column=4, sticky="w", padx=(14, 12))
            f.columnconfigure(3, weight=1)  # 占用条吸收余宽
            self.chips[luid] = (mem_l, eng_l, pbar)
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
            self.detail.configure(
                text=f"驱动层口径：{m['image']}  (pid {m['pid']})\n"
                     f"加载 {mods}  ｜  无图形上下文，WDDM 表不可见（传感器轮询类软件在此现形）",
                text_color=TEXT)
            return
        active = {k: v for k, v in m["engines"].items() if v > 0.05}
        engines = "  ".join(f"{k} {v:.1f}%" for k, v in active.items()) or "仅上下文(0%)"
        prot = "  ｜ 系统/服务组件（保护名单，不建议结束）" if is_protected(m["image"]) else ""
        self.detail.configure(text=f"{m['image']}  (pid {m['pid']})\n"
                                   f"显存 {m['local_mb']:.2f} MB  ｜  {engines}{prot}",
                              text_color=TEXT)

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
        # 当前态点击 = 落一行日志反馈，不静默（disabled 哑交互让咩咩以为
        # 按钮坏了，2026-11-17 反馈；「点击必须有可感知响应」与取消同哲学）
        mode = self.last_st.mode if self.last_st else "unknown"
        already = {"dgpu_direct": "direct_on", "igpu_only": "on",
                   "hybrid": "hybrid"}.get(mode)
        if target == already:
            self.log_q.put(f"[模式] 当前已是{MODE_NAMES[target]}，无需切换。\n")
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
        """按钮启用只按能力 gate（方案 §三 F）；当前态按钮保持可点，
        点击由 run_switch 落日志「已在当前模式」反馈——disabled 哑交互
        已废（2026-11-17 咩咩反馈）。态式配色：当前模式实色点亮，其余中性描边。"""
        mode = self.last_st.mode if self.last_st else "unknown"
        active = {"dgpu_direct": "direct_on", "igpu_only": "on",
                  "hybrid": "hybrid"}.get(mode)
        for target, b in (("direct_on", self.direct_on_btn),
                          ("hybrid", self.hybrid_btn),
                          ("on", self.eco_on_btn)):
            self._style_switch(b, target, active == target)
        if self.switch_running:
            for b in self._switch_buttons():
                b.configure(state="disabled")
            return
        i = self.gcu_info
        eco_ok = not (i and i.igpu_only is False)
        direct_ok = not (i and i.dgpu_direct is False)
        self.direct_on_btn.configure(state="normal" if direct_ok else "disabled")
        # 混合模式恒可点；走哪条路由 cmd_hybrid 现场判，
        # 词表/能力细 gate 也在各分支里（同 CLI 口径）
        self.hybrid_btn.configure(state="normal")
        self.eco_on_btn.configure(state="normal" if eco_ok else "disabled")

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
        # 外壳用原生 Toplevel（旧版久经真机考验的窗口行为：显示/居中/grab）；
        # CTkToplevel 在 Windows 下标题栏着色流程会撤回 withdrawn 初始态，
        # 实测 mainloop 中窗口整个不出现。内容套一层 CTkFrame 深色容器：
        # CTk 控件的 transparent 取父链 CTk 色，直接挂原生 Toplevel 会落回
        # 主题默认浅色
        win = tk.Toplevel(self.root, bg=BG)
        win.title("切换问答")
        win.transient(self.root)
        win.resizable(False, False)
        holder = ctk.CTkFrame(win, fg_color=BG, corner_radius=0)
        holder.pack(fill="both", expand=True)
        ctk.CTkLabel(holder, text=prompt, wraplength=540,
                     justify="left", text_color=TEXT,
                     font=(FONT_FAMILY, 12)).pack(padx=16, pady=14)

        def answer(v: str):
            bridge.ans_q.put(v)
            win.destroy()

        # 编号问答按钮化：解析 prompt 里的「[N] 描述」逐项一钮，[回车/其他] 描述=取消项；
        # 解析不出编号（兜底问答）才退回输入框。
        # 三档视觉：危险项（立即重启）红、回车默认安全项主色、其余中性、取消描边
        opts = re.findall(r"\[(\d+)\]\s*([^\[\]]+)", prompt)
        m_cancel = re.search(r"\[回车[^\]]*\]\s*([^\[\]]+)", prompt)
        if opts:
            body = ctk.CTkFrame(holder, fg_color="transparent")
            body.pack(padx=16, pady=(0, 14), fill="x")
            focused = None
            want = "2" if "立即重启" in prompt else "1"
            for num, desc in opts:
                desc = desc.strip()
                if "立即重启" in desc:
                    fg, hover, tc = RED, "#c53a32", "#ffffff"
                elif num == want:
                    fg, hover, tc = ACCENT, "#65a0f8", "#ffffff"
                else:
                    fg, hover, tc = SURFACE2, BORDER, TEXT
                b = ctk.CTkButton(body, text=f"{num}｜{desc}",
                                  anchor="w", width=560,
                                  height=34, corner_radius=6,
                                  fg_color=fg, hover_color=hover, text_color=tc,
                                  font=(FONT_FAMILY, 12),
                                  command=lambda n=num: answer(n))
                b.pack(fill="x", pady=3)
                # 回车默认落在预期项（旧口径：重启三选焦点在 2=稍后自行重启，
                # 一键回车不触发立即重启；清场确认仍默认 1）
                if num == want:
                    focused = b
            cancel_desc = m_cancel.group(1).strip() if m_cancel else "取消切换"
            cb = ctk.CTkButton(body, text=f"回车/其他｜{cancel_desc}",
                               anchor="w", width=560, height=34,
                               corner_radius=6, fg_color="transparent",
                               hover_color=SURFACE2, border_width=1,
                               border_color=BORDER, text_color=MUTED,
                               font=(FONT_FAMILY, 12),
                               command=lambda: answer(""))
            cb.pack(fill="x", pady=(3, 0))
            if focused:
                focused.focus_set()
                # 回车=默认安全侧：绑在问答窗上（焦点在窗内任何位置都生效；
                # CTkButton 的 canvas 不吃键盘焦点，单绑按钮会漏）
                win.bind("<Return>", lambda e: focused.invoke())
            win.bind("<Escape>", lambda e: answer(""))
        else:
            entry = ctk.CTkEntry(holder, width=280, font=(FONT_FAMILY, 12))
            entry.pack(padx=16, pady=(0, 8))
            ctk.CTkButton(holder, text="确定", width=88, height=30,
                          corner_radius=6, font=(FONT_FAMILY, 12),
                          command=lambda: answer(entry.get())).pack(pady=(0, 14))
            entry.bind("<Return>", lambda e: answer(entry.get()))
            entry.focus_set()
        # 居中于主窗口（Toplevel 默认落屏幕左上角，切换问答该跟着主窗走）
        win.update_idletasks()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        ww, wh = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{rx + max(0, (rw - ww) // 2)}+{ry + max(0, (rh - wh) // 3)}")
        # LAYERED + alpha=255（完全不透明，视觉零差异）：layered 窗口自带
        # 绘制表面，屏外自测 PrintWindow 才能抓全（同 _move_offscreen 的结论）；
        # 顺带 DWM 深色标题栏（原生 Toplevel 默认系统浅色标题栏，与主题不搭）
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(win.winfo_id())
            user32.SetWindowLongW(hwnd, -20,
                                  user32.GetWindowLongW(hwnd, -20) | 0x80000)  # WS_EX_LAYERED
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, 0x2)  # LWA_ALPHA
            _dwm_dark_titlebar(hwnd)
        except Exception:
            pass
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
        # 设计系统落地核对：表格字体族（雅黑生效=不吃宋体回退）+
        # 三切换按钮底色（态式=至多一个实色，其余 transparent 描边）
        tree_font = self.root.tk.call("ttk::style", "lookup", "Dgpu.Treeview", "-font")
        lines.append(f"fonts: tree={self.root.tk.call('font', 'actual', tree_font, '-family')} "
                     f"buttons=" + ",".join(
                         f"{str(b.cget('text'))}={b.cget('fg_color')}" for b in self._switch_buttons()))
        # 主窗行几何（排「工具栏上方空白带」类布局问题：行高应贴内容）
        bar_w = self.pw.master.grid_slaves(1, 0)[0]
        lines.append(f"layout: gpu_bar_y={self.gpu_bar.winfo_y()} gpu_bar_h={self.gpu_bar.winfo_height()} "
                     f"bar_y={bar_w.winfo_y()} bar_h={bar_w.winfo_height()} "
                     f"pw_y={self.pw.winfo_y()} sash0={self.pw.sashpos(0)}")
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
            self.log_text.configure(state="normal")
            self.log_text.insert("end", s)
            if int(self.log_text.index("end-1c").split(".")[0]) > 3000:
                self.log_text.delete("1.0", "500.0")
            self.log_text.configure(state="disabled")
            if at_bottom:
                self.log_text.see("end")
            n += 1
            if n > 400:  # 每轮限量，防积压卡 UI
                break

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

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
    不抢焦点——正常启动不触发。加 LAYERED+alpha=1：完全屏外的窗口没有 DWM
    重定向表面，tk 的 GDI 输出无处落、PrintWindow 自测截图全白；layered
    窗口自带 surface，alpha=1（255 分之一）视觉无感而绘制链路完整。"""
    root.update_idletasks()
    root.geometry("+30000+30000")
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id())
        style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        user32.SetWindowLongW(hwnd, -20, style | 0x80 | 0x08000000 | 0x80000)  # TOOLWINDOW | NOACTIVATE | LAYERED
        user32.SetLayeredWindowAttributes(hwnd, 0, 1, 0x2)  # LWA_ALPHA, alpha=1
    except Exception:
        pass


def _dwm_dark_titlebar(hwnd: int, bg: str = BG) -> None:
    """DWM 深色标题栏/边框/标题文字（主窗与问答窗共用）。
    IMMERSIVE_DARK_MODE(20/19) 在本机 build 26300 实测不生效（真机标题栏
    仍系统蓝，2026-11-17 咩咩截图），改为 Win11 的 CAPTION/BORDER/TEXT
    三色直涂（22000+），涂不动（旧系统）再回退 attribute 20/19。"""
    try:
        dwm = ctypes.windll.dwmapi
        r, g, b = (int(bg[i:i + 2], 16) for i in (1, 3, 5))
        caption = (b << 16) | (g << 8) | r          # COLORREF 0x00BBGGRR
        r, g, b = 0xE6, 0xED, 0xF3                  # TEXT 色 = 主文字色
        text_c = (b << 16) | (g << 8) | r
        for attr, val in ((35, caption), (34, caption), (36, text_c)):
            dwm.DwmSetWindowAttribute(hwnd, attr,
                                      ctypes.byref(ctypes.c_int(val)), 4)
        v = ctypes.c_int(1)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        if dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(v), 4) != 0:
            dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(v), 4)  # 20H1 前
    except Exception:
        pass


def _dark_titlebar(root: ctk.CTk) -> None:
    """主窗 DWM 深色标题栏（CTk 自带的设置实测未生效，截图左缘浅色带）。"""
    try:
        _dwm_dark_titlebar(ctypes.windll.user32.GetParent(root.winfo_id()))
    except Exception:
        pass


def main() -> int:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk(fg_color=BG)  # 全窗底色：CTk dark 默认 #242424 换设计系统底色
    apply_theme()
    root.update_idletasks()
    _dark_titlebar(root)
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
