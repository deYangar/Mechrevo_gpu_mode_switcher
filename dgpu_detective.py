#!/usr/bin/env python3
"""dgpu_detective —— 识别到底是谁在占用独立显卡。

数据来源：Windows WDDM 图形内核的性能计数器（任务管理器「性能→GPU」同款数据），
经 pdh.dll 的 PDH API 读取；显卡身份经 dxgi.dll 的 DXGI 枚举确认。
对 D3D / Vulkan / OpenGL / CUDA 全部生效——它们都过 WDDM 这一层。

计数器实例名自带 pid + luid，天然构成「哪个进程在哪块显卡上」的映射：
    pid_1234_luid_0x00000000_0x0000C837_phys_0
    pid_1234_luid_0x00000000_0x0000C837_phys_0_eng_0_engtype_3D

注意：进程只要在显卡上创建了 GPU 上下文，引擎计数器实例就会出现，
即使利用率 0%、显存 0——这正是抓「导致独显关不掉的隐藏持有者」的依据。

仅 Python 标准库，无需管理员权限。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from ctypes import wintypes

if sys.stdout and sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# DXGI：枚举显卡适配器（精简自 gputaskman/dxgi.py）
# ---------------------------------------------------------------------------

VENDOR_NVIDIA = 0x10DE
VENDOR_AMD = 0x1002
VENDOR_INTEL = 0x8086
VENDORS = {0x10DE: "NVIDIA", 0x1002: "AMD", 0x8086: "Intel", 0x1414: "Microsoft", 0x5143: "Qualcomm"}

# 核显的 DedicatedVideoMemory 一般是 0 或很小的 carve-out，达到该值视为独显
DISCRETE_VRAM_THRESHOLD = 512 * 1024 * 1024


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", wintypes.UINT),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                ("d4", ctypes.c_ubyte * 8)]


_IID_IDXGIFactory1 = _GUID(
    0x770AAE78, 0xF26F, 0x4DBA,
    (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87),
)


@dataclass
class Adapter:
    luid: int
    name: str
    vendor_id: int
    dedicated_vram: int
    shared_system: int
    software: bool

    @property
    def vendor(self) -> str:
        return VENDORS.get(self.vendor_id, hex(self.vendor_id))

    @property
    def discrete(self) -> bool:
        """判定独显：软件适配器不算；NVIDIA 直接是独显；其余按独立显存规模判。"""
        if self.software:
            return False
        if self.vendor_id == VENDOR_NVIDIA:
            return True
        return self.dedicated_vram >= DISCRETE_VRAM_THRESHOLD


def _vcall(obj: ctypes.c_void_p, index: int, restype, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def enumerate_adapters() -> list[Adapter]:
    dxgi = ctypes.WinDLL("dxgi")
    factory = ctypes.c_void_p()
    if dxgi.CreateDXGIFactory1(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory)) != 0:
        raise OSError("DXGI adapter discovery failed")
    out: list[Adapter] = []
    try:
        enum_adapters1 = _vcall(factory, 12, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
        i = 0
        while True:
            adapter = ctypes.c_void_p()
            if enum_adapters1(factory, i, ctypes.byref(adapter)) != 0:
                break  # DXGI_ERROR_NOT_FOUND：枚举结束
            try:
                desc = _DESC1()
                if _vcall(adapter, 10, ctypes.c_long, ctypes.POINTER(_DESC1))(adapter, ctypes.byref(desc)) == 0:
                    luid = ((desc.AdapterLuid.HighPart & 0xFFFFFFFF) << 32) | desc.AdapterLuid.LowPart
                    out.append(Adapter(
                        luid=luid,
                        name=desc.Description,
                        vendor_id=desc.VendorId,
                        dedicated_vram=desc.DedicatedVideoMemory,
                        shared_system=desc.SharedSystemMemory,
                        software=bool(desc.Flags & 2),
                    ))
            finally:
                _vcall(adapter, 2, wintypes.ULONG)(adapter)  # Release
            i += 1
    finally:
        _vcall(factory, 2, wintypes.ULONG)(factory)  # Release
    return out


# ---------------------------------------------------------------------------
# PDH：Windows GPU 性能计数器（精简自 gputaskman/pdh.py）
# ---------------------------------------------------------------------------

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2


class _Value(ctypes.Union):
    _fields_ = [
        ("longValue", ctypes.c_long),
        ("doubleValue", ctypes.c_double),
        ("largeValue", ctypes.c_longlong),
        ("str", ctypes.c_wchar_p),
    ]


class _FmtValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("u", _Value)]


class _Item(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _FmtValue)]


_pdh = None


def _load_pdh():
    """pdh.dll 首次使用时加载，模块在非 Windows 上也能被 import 检查。"""
    global _pdh
    if _pdh is None:
        lib = ctypes.WinDLL("pdh")
        lib.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
        lib.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
        lib.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
        lib.PdhGetFormattedCounterArrayW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        lib.PdhCloseQuery.argtypes = [wintypes.HANDLE]
        for _fn in ("PdhOpenQueryW", "PdhAddEnglishCounterW", "PdhCollectQueryData",
                    "PdhGetFormattedCounterArrayW", "PdhCloseQuery"):
            getattr(lib, _fn).restype = wintypes.DWORD
        _pdh = lib
    return _pdh


_LUID_RE = r"luid_0x([0-9a-fA-F]+)_0x([0-9a-fA-F]+)"
_PROC_RE = re.compile(rf"pid_(\d+)_{_LUID_RE}_phys_(\d+)")
_ENGINE_RE = re.compile(rf"pid_(\d+)_{_LUID_RE}_phys_(\d+)_eng_(\d+)_engtype_(.*)$")
_ADAPTER_RE = re.compile(rf"^{_LUID_RE}_phys_(\d+)$")


def luid_key(hi: str, lo: str) -> int:
    return (int(hi, 16) << 32) | int(lo, 16)


def norm_engine(t: str) -> str:
    t = t.lower()
    if t.startswith("3d"):
        return "3D"
    if t.startswith("compute") or t.startswith("cuda"):
        return "Compute"  # CUDA 在部分驱动上跑在 3D 引擎，类型归 Compute
    if t.startswith("copy"):
        return "Copy"
    if t.startswith("videodecode"):
        return "Decode"
    if t.startswith("videoencode"):
        return "Encode"
    return t or "other"


@dataclass
class ProcGpu:
    dedicated: float = 0.0   # 驻留 VRAM 字节（Local Usage，记在进程账上的，无共享面重复计数）
    shared: float = 0.0      # 溅到系统 RAM 的字节（Non Local Usage）
    committed: float = 0.0   # 提交的 GPU 内存总量（Total Committed）
    engines: dict[str, float] = field(default_factory=lambda: defaultdict(float))  # engtype -> util%


class GpuCounters:
    """一次性查询句柄；引擎利用率是 rate 计数器，需要两次采样间隔。"""

    _COUNTERS = {
        "dedicated": (r"\GPU Process Memory(*)\Local Usage", r"\GPU Process Memory(*)\Dedicated Usage"),
        "shared": (r"\GPU Process Memory(*)\Non Local Usage", r"\GPU Process Memory(*)\Shared Usage"),
        "committed": (r"\GPU Process Memory(*)\Total Committed",),
        "engine": (r"\GPU Engine(*)\Utilization Percentage",),
        "a_dedicated": (r"\GPU Adapter Memory(*)\Dedicated Usage",),
        "a_shared": (r"\GPU Adapter Memory(*)\Shared Usage",),
    }

    def __init__(self) -> None:
        _load_pdh()
        self._query = wintypes.HANDLE()
        status = _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query))
        if status != 0:
            raise OSError(f"PdhOpenQuery failed: 0x{status:08X}")
        self._handles: dict[str, wintypes.HANDLE] = {}
        for key, paths in self._COUNTERS.items():
            for path in paths:
                h = wintypes.HANDLE()
                # AddEnglishCounter：中文系统上计数器路径本地化，英文名不受影响
                if _pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(h)) == 0:
                    self._handles[key] = h
                    break
        _pdh.PdhCollectQueryData(self._query)  # rate 计数器打底采样

    def _read(self, key: str) -> list[tuple[str, float]]:
        h = self._handles.get(key)
        if h is None:
            return []
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        fmt = PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
        status = _pdh.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size), ctypes.byref(count), None)
        if status != PDH_MORE_DATA:
            return []
        buf = ctypes.create_string_buffer(size.value)
        status = _pdh.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size), ctypes.byref(count), buf)
        if status != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_Item))
        out = []
        for i in range(count.value):
            it = items[i]
            if it.FmtValue.CStatus in (0, 1):  # VALID_DATA / NEW_DATA
                out.append((it.szName, it.FmtValue.u.doubleValue))
        return out

    def sample(self) -> tuple[dict[int, dict[int, ProcGpu]], dict[int, dict[str, float]]]:
        """返回 ({luid: {pid: ProcGpu}}, {luid: {dedicated/shared: 字节}})。"""
        if _pdh.PdhCollectQueryData(self._query) != 0:
            return {}, {}
        procs: dict[int, dict[int, ProcGpu]] = defaultdict(dict)

        def proc(luid: int, pid: int) -> ProcGpu:
            p = procs[luid].get(pid)
            if p is None:
                p = procs[luid][pid] = ProcGpu()
            return p

        for attr in ("dedicated", "shared", "committed"):
            for name, val in self._read(attr):
                m = _PROC_RE.match(name)
                if m:
                    setattr(proc(luid_key(m[2], m[3]), int(m[1])), attr,
                            getattr(procs[luid_key(m[2], m[3])][int(m[1])], attr) + val)
        for name, val in self._read("engine"):
            m = _ENGINE_RE.match(name)
            if m:
                proc(luid_key(m[2], m[3]), int(m[1])).engines[norm_engine(m[6])] += val

        adapters: dict[int, dict[str, float]] = defaultdict(lambda: {"dedicated": 0.0, "shared": 0.0})
        for attr, key in (("dedicated", "a_dedicated"), ("shared", "a_shared")):
            for name, val in self._read(key):
                m = _ADAPTER_RE.match(name)
                if m:
                    adapters[luid_key(m[1], m[2])][attr] += val
        return procs, adapters

    def close(self) -> None:
        if self._query:
            _pdh.PdhCloseQuery(self._query)
            self._query = wintypes.HANDLE()


# ---------------------------------------------------------------------------
# 进程身份
# ---------------------------------------------------------------------------

_kernel32 = None


def _load_kernel32():
    global _kernel32
    if _kernel32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32 = k
    return _kernel32


def process_image(pid: int) -> str:
    """进程可执行文件完整路径；受保护进程返回 <拒绝访问 pid>。"""
    if pid == 4:
        return "System"  # 内核 System 进程固定 pid 4，OpenProcess 必然拒绝
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k = _load_kernel32()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return f"<拒绝访问 pid={pid}>"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return f"<查询失败 pid={pid}>"
    finally:
        k.CloseHandle(h)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def fmt_mb(bytes_val: float) -> str:
    return f"{bytes_val / (1024 * 1024):.2f}"


def sample_once(counters: GpuCounters, adapters_meta: list[Adapter],
                include_all: bool) -> list[dict]:
    procs, adapter_totals = counters.sample()
    out = []
    for a in adapters_meta:
        if not (include_all or a.discrete):
            continue
        rows = []
        for pid, g in procs.get(a.luid, {}).items():
            rows.append({
                "pid": pid,
                "image": process_image(pid),
                "local_mb": g.dedicated / (1024 * 1024),
                "nonlocal_mb": g.shared / (1024 * 1024),
                "committed_mb": g.committed / (1024 * 1024),
                "engines": dict(sorted(g.engines.items(), key=lambda kv: -kv[1])),
            })
        rows.sort(key=lambda r: (-r["local_mb"], -max(r["engines"].values(), default=0.0), r["pid"]))
        totals = adapter_totals.get(a.luid, {})
        out.append({
            "luid": f"0x{a.luid:016X}",
            "name": a.name,
            "vendor": a.vendor,
            "discrete": a.discrete,
            "adapter_dedicated_mb": totals.get("dedicated", 0.0) / (1024 * 1024),
            "adapter_shared_mb": totals.get("shared", 0.0) / (1024 * 1024),
            "card_vram_mb": a.dedicated_vram / (1024 * 1024),
            "processes": rows,
        })
    return out


def print_report(gpus: list[dict]) -> None:
    if not gpus:
        print("未发现符合条件的显卡适配器。")
        return
    for gpu in gpus:
        tag = "独显" if gpu["discrete"] else "非独显"
        print(f"\n=== {gpu['name']}（{gpu['vendor']}，{tag}，LUID {gpu['luid']}）===")
        used = gpu["adapter_dedicated_mb"]
        card = gpu["card_vram_mb"]
        print(f"适配器显存占用: {fmt_mb(used * 1024 * 1024)} MB / {fmt_mb(card * 1024 * 1024)} MB"
              f"（共享内存 {fmt_mb(gpu['adapter_shared_mb'] * 1024 * 1024)} MB）")
        rows = gpu["processes"]
        if not rows:
            print("  无任何进程持有此显卡的 GPU 上下文。")
            if gpu["discrete"]:
                print("  提示：独显无进程持有却仍不歇机时，查非进程因素——"
                      "显示器是否直连独显、驱动面板的电源设置等。")
            continue
        name_w = max([len(r["image"].rsplit("\\", 1)[-1]) for r in rows] + [8])
        print(f"  {'PID':>7}  {'进程名':<{name_w}}  {'VRAM(MB)':>10}  {'NonLocal':>10}  "
              f"{'Committed':>10}  引擎活动")
        for r in rows:
            name = r["image"].rsplit("\\", 1)[-1]
            active = {k: v for k, v in r["engines"].items() if v > 0.05}
            engines = "  ".join(f"{k} {v:.1f}%" for k, v in active.items()) or "仅上下文(0%)"
            print(f"  {r['pid']:>7}  {name:<{name_w}}  {r['local_mb']:>10.2f}  "
                  f"{r['nonlocal_mb']:>10.2f}  {r['committed_mb']:>10.2f}  {engines}")
        total_local = sum(r["local_mb"] for r in rows)
        print(f"  共 {len(rows)} 个进程持有上下文，进程显存合计 {total_local:.2f} MB")


def print_list_gpus(adapters: list[Adapter]) -> None:
    if not adapters:
        print("未枚举到 DXGI 适配器。")
        return
    print(f"{'LUID':>18}  {'厂商':<8} {'独显':<4} {'独立显存(MB)':>12}  名称")
    for a in adapters:
        print(f"{a.luid:>18X}  {a.vendor:<8} {'是' if a.discrete else '否':<4} "
              f"{a.dedicated_vram // (1024 * 1024):>12}  {a.name}"
              + ("（软件适配器）" if a.software else ""))


def watch(counters: GpuCounters, adapters_meta: list[Adapter], interval: float) -> None:
    """持续盯独显：新持有进程冒头/退出即时报告，每轮打印简报。"""
    print(f"开始监视（每 {interval:g}s 一轮，Ctrl+C 退出）…", flush=True)
    seen: dict[tuple[int, int], float] = {}  # (luid, pid) -> 上次 local_mb
    try:
        while True:
            time.sleep(interval)
            procs, _ = counters.sample()
            now: dict[tuple[int, int], float] = {}
            stamp = time.strftime("%H:%M:%S")
            for a in adapters_meta:
                if not a.discrete:
                    continue
                for pid, g in procs.get(a.luid, {}).items():
                    now[(a.luid, pid)] = g.dedicated
            for key, v in now.items():
                if key not in seen:
                    luid, pid = key
                    gname = next((a.name for a in adapters_meta if a.luid == luid), "?")
                    img = process_image(pid).rsplit("\\", 1)[-1]
                    print(f"[{stamp}] 新持有: {img} (pid {pid}) → {gname}，VRAM {fmt_mb(v)} MB", flush=True)
            for key in seen:
                if key not in now:
                    luid, pid = key
                    gname = next((a.name for a in adapters_meta if a.luid == luid), "?")
                    print(f"[{stamp}] 退出: pid {pid} 已不再持有 {gname}", flush=True)
            seen = now
            dgpu = [(a, procs.get(a.luid, {})) for a in adapters_meta if a.discrete]
            brief = "; ".join(
                f"{a.name}: {len(ps)} 进程" + (f"，进程显存 {fmt_mb(sum(g.dedicated for g in ps.values()))} MB" if ps else "，无持有")
                for a, ps in dgpu)
            print(f"[{stamp}] {brief}", flush=True)
    except KeyboardInterrupt:
        print("\n监视结束。")


# ---------------------------------------------------------------------------
# 核显省电切换编排（--eco）：检测独显持有 → 提示并关闭 → 移交 gpu_switch on
# ---------------------------------------------------------------------------

# gpu_switch.py（gpu_mode_switch 项目，官方同款终态序列 CLI）默认位置，--gpu-switch 可覆盖
GPU_SWITCH_DEFAULT = (r"C:\Users\Yang\.zcode\workspace\default\projects"
                      r"\gpu_mode_switch\tools\gpu_switch.py")

# 系统/服务组件：杀了会被系统拉回或影响桌面（输入/开始菜单/窗口管理），且 gpu_mode_switch
# 的 E7 实测「独显上下文进程不阻碍 PnP 禁用」——只列出说明，不自动关闭
PROTECTED_PROCESSES = {
    "system", "dwm.exe", "explorer.exe", "csrss.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "sihost.exe", "fontdrvhost.exe",
    "runtimebroker.exe", "textinputhost.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "searchindexer.exe",
    "applicationframehost.exe", "systemsettings.exe", "widgetservice.exe",
    # NVIDIA 服务/容器：SCM 托管，杀了自动复活（禁用对应服务才是根治）
    "nvcontainer.exe", "nvdisplay.container.exe", "nvidia overlay.exe",
    "nvidia share.exe", "nvidia web helper.exe",
}


def classify_pid(pid: int) -> tuple[bool, bool]:
    """进程状态分类，返回 (存活, 受保护)。

    OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) 被拒 err=5 是受保护进程（活着）；
    err=87 等 = pid 已不存在——PDH 计数器里残留的陈旧实例。
    """
    if pid == 4:
        return True, True  # System 内核进程
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k = _load_kernel32()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        k.CloseHandle(h)
        return True, False
    if ctypes.get_last_error() == 5:  # ERROR_ACCESS_DENIED：受保护进程
        return True, True
    return False, False  # pid 已不存在（陈旧实例）


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def ask(prompt: str, default: str = "") -> str:
    """CLI 问答；管道喂空（EOF）视同默认值（默认都是「不动作」，安全）。"""
    try:
        return input(f"{prompt}: ").strip()
    except EOFError:
        return default


def ask_yes_no(prompt: str) -> bool:
    return ask(f"{prompt} [y/N]").lower() in ("y", "yes")


def graceful_close(pid: int, timeout: float = 3.0) -> bool:
    """taskkill 不带 /f（对窗口进程等效 WM_CLOSE），轮询等待退出。"""
    subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        alive, _ = classify_pid(pid)
        if not alive:
            return True
    return False


def force_kill(pid: int) -> bool:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    time.sleep(1.0)
    alive, _ = classify_pid(pid)
    return not alive


def proc_name(image: str) -> str:
    return image.rsplit("\\", 1)[-1]


def active_engines(row: dict) -> dict[str, float]:
    """有真实利用率的引擎（阈值与 print_report 一致）。"""
    return {k: v for k, v in row["engines"].items() if v > 0.05}


def print_eco_rows(rows: list[dict], title: str) -> None:
    print(f"\n--- {title} ---")
    if not rows:
        print("  （无）")
        return
    name_w = max(len(proc_name(r["image"])) for r in rows)
    for r in rows:
        active = active_engines(r)
        eng = " ".join(f"{k} {v:.0f}%" for k, v in active.items()) or "仅上下文"
        mark = "  ⚠活动中（强杀可能丢未保存数据）" if active else ""
        print(f"  pid {r['pid']:<7} {proc_name(r['image']):<{name_w}}"
              f"  VRAM {r['local_mb']:>8.2f} MB  {eng}{mark}")


def close_candidates(candidates: list[dict]) -> None:
    """优雅关闭候选进程；退不掉的列出，经确认后才强杀。"""
    print("\n--- 优雅关闭（taskkill，等效点窗口的 X）---")
    stubborn: list[dict] = []
    for r in candidates:
        pid = r["pid"]
        print(f"  … {proc_name(r['image'])} (pid {pid})", end="", flush=True)
        if graceful_close(pid):
            print(" ✓ 已退出")
        else:
            print(" ✗ 未退出")
            stubborn.append(r)
    if not stubborn:
        return
    print_eco_rows(stubborn, f"未能优雅退出（{len(stubborn)} 个）")
    if ask_yes_no(f"强制结束以上 {len(stubborn)} 个进程？"):
        for r in stubborn:
            pid = r["pid"]
            ok = force_kill(pid)
            tail = "" if ok else " —— 残留（不阻碍切换，上下文将被强制剥离）"
            print(f"  {'✓' if ok else '✗'} 强杀 {proc_name(r['image'])} (pid {pid}){tail}")
    else:
        print("  跳过强杀（残留不阻碍切换，上下文将被强制剥离）")


def cmd_eco(counters: GpuCounters, adapters_meta: list[Adapter], gpu_switch_path: str) -> int:
    """核显省电切换编排：检测独显持有 → 提示并关闭 → 复查 → 移交 gpu_switch on。"""
    print("=== 核显省电切换编排（检测 → 清场 → gpu_switch on）===")
    switch_ready = os.path.isfile(gpu_switch_path)
    if not switch_ready:
        print(f"! 未找到 gpu_switch.py：{gpu_switch_path}")
        print("  检测/关闭环节照常可用；切换环节无法执行（--gpu-switch 可指定路径）")

    # ① 抓独显持有清单，按可关闭/系统组件分类（过滤已退出的陈旧实例）
    time.sleep(0.5)  # 引擎利用率是 rate 计数器，需要真实间隔才有读数
    candidates: list[dict] = []  # 用户软件，可关闭候选
    skipped: list[dict] = []     # 系统/服务/受保护组件
    stale = 0
    for gpu in sample_once(counters, adapters_meta, include_all=False):
        for r in gpu["processes"]:
            alive, protected = classify_pid(r["pid"])
            if not alive:
                stale += 1
                continue
            if protected or proc_name(r["image"]).lower() in PROTECTED_PROCESSES \
                    or r["image"].startswith("<"):
                skipped.append(r)
            else:
                candidates.append(r)
    if stale:
        print(f"（已过滤陈旧计数器实例 {stale} 个：进程已退出）")

    # ② 分类提示 + 选择
    candidates.sort(key=lambda r: (-r["local_mb"], -max(r["engines"].values(), default=0.0)))
    if candidates:
        print_eco_rows(candidates, f"可关闭候选（{len(candidates)} 个，按显存排序）")
        print_eco_rows(skipped, f"系统/服务组件（{len(skipped)} 个，跳过不动：杀了被拉回或不阻碍禁用）")
        choice = ask("\n[1] 关闭以上候选并切换  [2] 不关闭直接切换  [回车/其他] 取消")
        if choice == "1":
            close_candidates(candidates)
        elif choice != "2":
            print("已取消，未做任何改动。")
            return 1
    else:
        print("独显当前无用户软件持有 GPU 上下文，无需关闭。")
        print_eco_rows(skipped, f"系统/服务组件（{len(skipped)} 个，不阻碍切换）")

    # ③ 复查：重新采样，如实报告剩余持有
    print("\n--- 复查（重新采样）---")
    time.sleep(2.0)  # 给 WDDM 上下文销毁、计数器实例消失留时间
    remaining = []
    for gpu in sample_once(counters, adapters_meta, include_all=False):
        for r in gpu["processes"]:
            alive, _ = classify_pid(r["pid"])
            if alive:
                remaining.append(r)
    if remaining:
        print_eco_rows(remaining, f"剩余持有 {len(remaining)} 个（多为系统组件，不阻碍 PnP 禁用）")
    else:
        print("  ✓ 独显已无任何存活持有进程")

    # ④ 移交 gpu_switch 切换
    if not switch_ready:
        print(f"\nX gpu_switch.py 不存在（{gpu_switch_path}），清场完成但切换未执行。")
        return 1
    if not ask_yes_no("\n开始切换核显省电（gpu_switch on，需管理员/UAC）？"):
        print("已取消切换（本次已关闭的进程不自动恢复）。")
        return 1
    print(f"\n--- 移交 gpu_switch：{gpu_switch_path} on ---")
    if not is_admin():
        print("当前非管理员：gpu_switch 将弹 UAC 在新窗口执行，"
              "日志见 gpu_mode_switch/evidence/gpu_switch/。")
    return subprocess.run([sys.executable, gpu_switch_path, "on"]).returncode


def main() -> int:
    ap = argparse.ArgumentParser(
        description="识别哪个进程在占用独立显卡（WDDM 性能计数器，任务管理器同款数据）")
    ap.add_argument("--list-gpus", action="store_true", help="列出所有显卡适配器后退出")
    ap.add_argument("--all", action="store_true", help="非独显适配器（核显等）也一并报告")
    ap.add_argument("--json", action="store_true", help="JSON 输出（一次性快照）")
    ap.add_argument("--watch", action="store_true", help="持续监视独显持有变化")
    ap.add_argument("--eco", action="store_true",
                    help="核显省电切换编排：列出独显占用软件并关闭后，调 gpu_switch 切换")
    ap.add_argument("--gpu-switch", default=GPU_SWITCH_DEFAULT, metavar="PATH",
                    help="--eco 调用的 gpu_switch.py 路径（默认指向 gpu_mode_switch 项目）")
    ap.add_argument("--interval", type=float, default=1.0, help="watch 间隔秒数（默认 1）")
    args = ap.parse_args()

    try:
        adapters_meta = enumerate_adapters()
    except OSError as e:
        print(f"DXGI 枚举失败: {e}", file=sys.stderr)
        return 1

    if args.list_gpus:
        print_list_gpus(adapters_meta)
        return 0

    try:
        counters = GpuCounters()
    except OSError as e:
        print(f"PDH 计数器打开失败: {e}", file=sys.stderr)
        return 1

    try:
        if args.eco:
            return cmd_eco(counters, adapters_meta, args.gpu_switch)
        if args.watch:
            watch(counters, adapters_meta, args.interval)
            return 0
        time.sleep(0.5)  # 引擎利用率是 rate 计数器，打底后需真实间隔才有读数
        if args.json:
            print(json.dumps(sample_once(counters, adapters_meta, args.all),
                             ensure_ascii=False, indent=2))
            return 0
        print_report(sample_once(counters, adapters_meta, args.all))
        return 0
    finally:
        counters.close()


if __name__ == "__main__":
    sys.exit(main())
