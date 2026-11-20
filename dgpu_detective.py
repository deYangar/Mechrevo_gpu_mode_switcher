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
import socket
import struct
import subprocess
import sys
import time
import winreg
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
    device_id: int = 0   # PCI DeviceId（代际判定用，如 50 系 0x28xx）
    dedicated_vram: int = 0
    shared_system: int = 0
    software: bool = False

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
                        device_id=desc.DeviceId,
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
                    p = proc(luid_key(m[2], m[3]), int(m[1]))
                    setattr(p, attr, getattr(p, attr) + val)
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
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        for _fn in ("Module32FirstW", "Module32NextW"):
            _f = getattr(k, _fn)
            _f.restype = wintypes.BOOL
            _f.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
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
        print(f"适配器显存占用: {used:.2f} MB / {card:.2f} MB"
              f"（共享内存 {gpu['adapter_shared_mb']:.2f} MB）")
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
    name_by_luid = {a.luid: a.name for a in adapters_meta}
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
                    gname = name_by_luid.get(luid, "?")
                    img = process_image(pid).rsplit("\\", 1)[-1]
                    print(f"[{stamp}] 新持有: {img} (pid {pid}) → {gname}，VRAM {fmt_mb(v)} MB", flush=True)
            for key in seen:
                if key not in now:
                    luid, pid = key
                    gname = name_by_luid.get(luid, "?")
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
# 显示模式状态检测（四态判据）：内屏路由 + PnP problem code + DXGI + GCU 意图回报
# ---------------------------------------------------------------------------
# 2026-11-11 实测定案：官方「核显省电」成功 = EC 级断开（problem=45，设备从 PCI 层
# 消失、DXGI 枚举不到）；而 gpu_switch 的 PnP 软禁用 = problem=22，设备仍焊在总线上，
# 是假切换。
# 通用化改造（2026-11-17，docs/通用化改造方案.md §2.6）新增凭证 A「内屏路由探测」：
# 直连态的本机权威判据——内屏 target 挂的适配器是独显（VEN_10DE）即直连；
# 凭证 B「GCU 意图回报」与之不一致 → direct_pending（发令后、重启前的待重启态）。

# GCU MySetting 注册表三根（L-Mechrevo MechrevoDeviceCapabilities.cs 实录，
# 首见值优先依次尝试：GCU 新根 → 旧控制台根 → ControlCenter 根）
MYSETTING_PATHS = (
    r"SOFTWARE\OEM\GamingCenter2\MySetting",
    r"SOFTWARE\OEM\GamingCenter\MySetting",
    r"SOFTWARE\OEM\ControlCenter\MySetting",
)

# Display 设备类 GUID；本机注册表 serial 键下只有 ClassGUID 没有 Class 值
DISPLAY_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"

# devnode 状态（CM_Locate 双姿势判据，2026-11-11 实测：
# 幻影设备的 DEVPKEY/CM_Get_DevNode_Status 全部不可读，只有定位成败可判）
STATE_LABELS = {
    "online": "在线（problem=0）",
    "disabled": "软禁用（problem=22，PnP；设备仍在总线上——假切换残留）",
    "disconnected": "已断开（幻影设备，不在总线上——EC 级下线）",
    "absent": "实例定位不到（连注册表幻影都不在）",
}

MODE_LABELS = {
    "igpu_only": "核显省电（EC 级断开，官方真切换）",
    "pnp_disabled": "PnP 软禁用（设备仍在总线上——假切换残留）",
    "hybrid": "混合模式（独显在线）",
    "dgpu_direct": "独显直连（内屏由独显驱动）",
    "direct_pending": "直连切换待重启（GCU 已收到指令，重启后生效）",
    "unknown": "无法判定",
}

# --- 内屏路由探测：QueryDisplayConfig（user32）+ GET_ADAPTER_NAME ---
# L-Mechrevo GpuRouteProbe.cs / DisplayNative.cs 移植，纯标准库 ctypes 复刻。
_QDC_ALL_PATHS = 0x1
_DEVINFO_GET_ADAPTER_NAME = 0x4
# 内接输出形态（GpuRouteInference.IsInternalOutput 实录）
_INTERNAL_OUTPUT_TECH = frozenset({
    0x80000000,  # DISPLAYCONFIG_OUTPUT_TECHNOLOGY_INTERNAL
    0x6,         # LVDS
    0xB,         # DISPLAYPORT_EMBEDDED
    0xD,         # UDI_EMBEDDED
})


class _DC_LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _DC_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]


class _DC_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [("adapterId", _DC_LUID), ("id", wintypes.UINT),
                ("modeInfoIdx", wintypes.UINT), ("statusFlags", wintypes.UINT)]


class _DC_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [("adapterId", _DC_LUID), ("id", wintypes.UINT),
                ("modeInfoIdx", wintypes.UINT), ("outputTechnology", wintypes.UINT),
                ("rotation", wintypes.UINT), ("scaling", wintypes.UINT),
                ("refreshRate", _DC_RATIONAL), ("scanLineOrdering", wintypes.UINT),
                ("targetAvailable", wintypes.BOOL), ("statusFlags", wintypes.UINT)]


class _DC_PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", _DC_PATH_SOURCE_INFO),
                ("targetInfo", _DC_PATH_TARGET_INFO), ("flags", wintypes.UINT)]


class _DC_MODE_INFO(ctypes.Structure):
    # modeInfo union 只当 QueryDisplayConfig 的输出缓冲占位，内容不读
    # （union 实际大小 = DISPLAYCONFIG_TARGET_MODE 的 48 字节）
    _fields_ = [("infoType", wintypes.UINT), ("id", wintypes.UINT),
                ("adapterId", _DC_LUID), ("_modeInfo", ctypes.c_byte * 48)]


class _DC_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", wintypes.UINT), ("size", wintypes.UINT),
                ("adapterId", _DC_LUID), ("id", wintypes.UINT)]


class _DC_ADAPTER_NAME(ctypes.Structure):
    _fields_ = [("header", _DC_DEVICE_INFO_HEADER),
                ("adapterDevicePath", ctypes.c_wchar * 128)]


_user32 = None


def _load_user32():
    """user32 惰性单例（同 _load_pdh/_load_cfgmgr32 的模式）。"""
    global _user32
    if _user32 is None:
        u = ctypes.WinDLL("user32")
        u.GetDisplayConfigBufferSizes.argtypes = [
            wintypes.UINT, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
        u.GetDisplayConfigBufferSizes.restype = wintypes.LONG
        u.QueryDisplayConfig.argtypes = [
            wintypes.UINT, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(_DC_PATH_INFO),
            ctypes.POINTER(wintypes.UINT), ctypes.POINTER(_DC_MODE_INFO), ctypes.c_void_p]
        u.QueryDisplayConfig.restype = wintypes.LONG
        u.DisplayConfigGetDeviceInfo.argtypes = [ctypes.POINTER(_DC_ADAPTER_NAME)]
        u.DisplayConfigGetDeviceInfo.restype = wintypes.LONG
        _user32 = u
    return _user32


def _classify_adapter_path(device_path: str) -> str:
    """适配器设备路径 → discrete/integrated/unknown（GpuRouteInference 同款）。"""
    p = device_path.upper()
    if "VEN_10DE" in p:
        return "discrete"
    if "VEN_8086" in p or "VEN_1002" in p:
        return "integrated"
    return "unknown"


def internal_panel_route() -> tuple[str, list[str]]:
    """内屏挂在哪块 GPU（凭证 A，直连态的本机权威判据）。

    QueryDisplayConfig(QDC_ALL_PATHS) 枚举显示路径 → 过滤 targetAvailable 且
    内接形态的 target → GET_ADAPTER_NAME 拿适配器设备路径 → 按 VEN 判独显/核显。
    返回 (discrete/integrated/unknown, 内屏适配器设备路径列表)。
    多内屏挂不同卡 → unknown（GpuRouteProbe 同款：矛盾即不可判定）。
    """
    paths_out: list[str] = []
    u = _load_user32()
    try:
        for _attempt in range(3):  # ERROR_INSUFFICIENT_BUFFER(122) 重试，L-Mechrevo 同款
            n_path, n_mode = wintypes.UINT(0), wintypes.UINT(0)
            if u.GetDisplayConfigBufferSizes(_QDC_ALL_PATHS, ctypes.byref(n_path),
                                             ctypes.byref(n_mode)) != 0:
                return "unknown", paths_out
            paths = (_DC_PATH_INFO * n_path.value)()
            modes = (_DC_MODE_INFO * n_mode.value)()
            rc = u.QueryDisplayConfig(_QDC_ALL_PATHS, ctypes.byref(n_path), paths,
                                      ctypes.byref(n_mode), modes, None)
            if rc == 122:
                continue  # 显示路径在枚举间隙变了，重取大小再来
            if rc != 0:
                return "unknown", paths_out
            result = "unknown"
            seen: set[tuple[int, int, int]] = set()
            for i in range(n_path.value):
                t = paths[i].targetInfo
                if not t.targetAvailable or t.outputTechnology not in _INTERNAL_OUTPUT_TECH:
                    continue
                key = (t.adapterId.LowPart, t.adapterId.HighPart, t.id)
                if key in seen:
                    continue
                seen.add(key)
                an = _DC_ADAPTER_NAME()
                an.header.type = _DEVINFO_GET_ADAPTER_NAME
                an.header.size = ctypes.sizeof(_DC_ADAPTER_NAME)
                an.header.adapterId = t.adapterId
                an.header.id = 0
                if u.DisplayConfigGetDeviceInfo(ctypes.byref(an)) != 0:
                    continue
                paths_out.append(an.adapterDevicePath)
                kind = _classify_adapter_path(an.adapterDevicePath)
                if kind == "unknown":
                    continue
                if result == "unknown":
                    result = kind
                elif result != kind:
                    return "unknown", paths_out
            return result, paths_out
    except Exception:
        return "unknown", paths_out
    return "unknown", paths_out


_cfgmgr32 = None


def _load_cfgmgr32():
    """cfgmgr32 惰性单例——GUI 每秒读显示模式都要判 devnode，别每次重建 DLL 对象。"""
    global _cfgmgr32
    if _cfgmgr32 is None:
        cfg = ctypes.WinDLL("cfgmgr32")
        cfg.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                           wintypes.LPCWSTR, wintypes.UINT]
        cfg.CM_Get_DevNode_Status.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                              ctypes.POINTER(wintypes.DWORD),
                                              wintypes.DWORD, wintypes.UINT]
        _cfgmgr32 = cfg
    return _cfgmgr32


def _devnode_state(instance_id: str) -> str:
    """cfgmgr32 判定 devnode 状态。

    NORMAL 定位成功 = 设备在场 → CM_Get_DevNode_Status 读 problem
    （0=online / 22=disabled / 其他原样报）；NORMAL 失败而 PHANTOM 定位成功 =
    幻影设备（不在总线上）= EC 级下线；两者都失败 = absent。
    """
    cfg = _load_cfgmgr32()
    CM_LOCATE_DEVNODE_NORMAL, CM_LOCATE_DEVNODE_PHANTOM = 0, 1
    devinst = wintypes.DWORD(0)
    if cfg.CM_Locate_DevNodeW(ctypes.byref(devinst), instance_id,
                              CM_LOCATE_DEVNODE_NORMAL) == 0:
        status = wintypes.DWORD(0)
        problem = wintypes.DWORD(0)
        if cfg.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem),
                                     devinst, 0) != 0:
            return "absent"  # 在场但状态不可读，如实按不可用处理
        if problem.value == 0:
            return "online"
        if problem.value == 22:
            return "disabled"
        return f"problem-{problem.value}"
    if cfg.CM_Locate_DevNodeW(ctypes.byref(devinst), instance_id,
                              CM_LOCATE_DEVNODE_PHANTOM) == 0:
        return "disconnected"
    return "absent"


def nvidia_pnp_states() -> list[tuple[str, str]]:
    """枚举 NVIDIA 显示设备（Class=Display，滤掉 HDAudio 等）的 devnode 状态。

    实例从 Enum/PCI 注册表定位（断开的设备不在设备管理器默认视图里，但注册表仍在）。
    """
    out: list[tuple[str, str]] = []
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                              r"SYSTEM\CurrentControlSet\Enum\PCI", 0, winreg.KEY_READ)
    except OSError:
        return out
    with base:
        i = 0
        while True:
            try:
                dev = winreg.EnumKey(base, i)
            except OSError:
                break
            i += 1
            if not dev.upper().startswith("VEN_10DE"):
                continue
            try:
                dk = winreg.OpenKey(base, dev)
            except OSError:
                continue
            with dk:
                j = 0
                while True:
                    try:
                        serial = winreg.EnumKey(dk, j)
                    except OSError:
                        break
                    j += 1
                    try:
                        sk = winreg.OpenKey(dk, serial)
                    except OSError:
                        continue
                    with sk:
                        try:
                            cls, _ = winreg.QueryValueEx(sk, "ClassGUID")
                        except OSError:
                            cls = ""
                        if str(cls).lower() != DISPLAY_CLASS_GUID:
                            continue
                    out.append((f"PCI\\{dev}\\{serial}",
                                _devnode_state(f"PCI\\{dev}\\{serial}")))
    return out


def _read_igpuonly() -> int | None:
    """GCU 亲手写的模式意图位（切换成功时 GCU 会更新它）。

    三根回退（方案 §2.2）：GamingCenter2 → GamingCenter → ControlCenter，
    每根 Registry64/Registry32 双视图，首见值优先。
    """
    for root in MYSETTING_PATHS:
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root, 0,
                                    winreg.KEY_READ | view) as k:
                    v, _ = winreg.QueryValueEx(k, "IGPUonly")
                    return int(v)
            except OSError:
                continue
    return None


# GCU 意图回报的状态字段别名族（MechrevoHw.cs FirstField 实录）
DIRECT_STATUS_KEYS = ("DiscreteGpuDirectConnectionSwitch_Status",
                      "DGpuDirectConnectionSwitch_Status", "DGPU_DIRECT_STATUS")
IGPU_STATUS_KEYS = ("IGpuOnlyConnectionSwitch_Status",
                    "IGPUOnlyConnectionSwitch_Status", "IGPU_ONLY_STATUS")


def resolve_direct_status(direct_status: str) -> int | None:
    """直连状态字符串 → GpuMode（ResolveGpuModeStatus 移植）：2=直连 0=核显 1=混合。"""
    s = (direct_status or "").upper()
    if not s:
        return None
    if "DIRECT_CONNECT_TOGGLE_ON" in s or "DIRECT_CONNECT_ON" in s or "DIRECT_CONNECTION_ON" in s:
        return 2
    if "DIRECT_CONNECT_TOGGLE_IGPU" in s or "DIRECT_CONNECT_IGPU" in s:
        return 0
    if "DIRECT_CONNECT_TOGGLE_OFF" in s or "DIRECT_CONNECT_OFF" in s or "DIRECT_CONNECTION_OFF" in s:
        return 1
    return None


def resolve_igpu_status(igpu_status: str) -> int | None:
    """核显省电状态字符串 → 模式（ResolveIgpuOnlySaveMode 移植）：0=省电 3=自动 1=混合。"""
    s = (igpu_status or "").upper()
    if not s:
        return None
    if "IGPU_ONLY_CONNECT_RB_ON" in s or "IGPU_ONLY_ON" in s:
        return 0
    if "IGPU_ONLY_CONNECT_RB_AUTO" in s or "IGPU_ONLY_AUTO" in s:
        return 3
    if "IGPU_ONLY_CONNECT_RB_OFF" in s or "IGPU_ONLY_OFF" in s:
        return 1
    return None


@dataclass
class DisplayModeState:
    problems: list[tuple[str, str]]
    dxgi_names: list[str]
    dxgi_error: str = ""
    igpu_only: int | None = None
    panel: str = "unknown"                 # 凭证 A：内屏路由 discrete/integrated/unknown
    panel_paths: list[str] = field(default_factory=list)
    gcu_direct_status: str = ""            # 凭证 B：GCU 意图回报原文（enrich 填充）
    gcu_igpu_status: str = ""

    @property
    def gcu_intent(self) -> int | None:
        """GCU 意图回报解析出的 GpuMode（2=直连 0=核显 1=混合）；无回报 None。"""
        intent = resolve_direct_status(self.gcu_direct_status)
        if intent is None:
            intent = resolve_igpu_status(self.gcu_igpu_status)
        return intent

    @property
    def base_mode(self) -> str:
        """凭证 A 口径的实况模式（不含 GCU 意图比对）。"""
        ps = [p for _, p in self.problems]
        if self.panel == "discrete":
            return "dgpu_direct"  # 内屏挂独显即直连（此时 NV PnP 必然在场）
        if self.panel == "integrated":
            if ps and all(p == "online" for p in ps):
                return "hybrid"
            if ps and all(p == "disabled" for p in ps):
                return "pnp_disabled"
            if ps and all(p in ("disconnected", "absent") for p in ps):
                return "igpu_only"
            return "unknown"
        # 内屏路由不可判定（外接屏多路挂不同卡等）：回落纯 PnP 老三态，不猜直连
        if ps and all(p == "disconnected" for p in ps):
            return "igpu_only"
        if ps and all(p == "disabled" for p in ps):
            return "pnp_disabled"
        if ps and all(p == "online" for p in ps):
            return "hybrid"
        return "unknown"

    @property
    def mode(self) -> str:
        base = self.base_mode
        intent = self.gcu_intent
        if intent is not None:
            # 凭证 B 比对：仅直连方向记 pending——意图直连而实况未直连、
            # 或实况直连而意图已退出直连（发令后、重启前只有 GCU 知道 pending）
            if intent == 2 and base != "dgpu_direct":
                return "direct_pending"
            if intent in (0, 1) and base == "dgpu_direct":
                return "direct_pending"
        return base


def read_display_mode_state() -> DisplayModeState:
    dxgi_names: list[str] = []
    dxgi_error = ""
    try:
        dxgi_names = [a.name for a in enumerate_adapters()
                      if a.vendor_id == VENDOR_NVIDIA and not a.software]
    except OSError as e:
        dxgi_error = str(e)
    panel, panel_paths = internal_panel_route()
    return DisplayModeState(problems=nvidia_pnp_states(),
                            dxgi_names=dxgi_names, dxgi_error=dxgi_error,
                            igpu_only=_read_igpuonly(),
                            panel=panel, panel_paths=panel_paths)


def print_mode_state(st: DisplayModeState) -> None:
    print(f"当前显示模式: {MODE_LABELS.get(st.mode, st.mode)}")
    panel_label = {"discrete": "独显（VEN_10DE）", "integrated": "核显（Intel/AMD）"}.get(
        st.panel, "不可判定（外接屏多路挂不同卡等）")
    print(f"  内屏路由: {panel_label}")
    for inst, p in st.problems:
        print(f"  PnP  {inst}  →  {STATE_LABELS.get(p, p)}")
    if st.dxgi_error:
        print(f"  DXGI 枚举失败: {st.dxgi_error}")
    elif st.dxgi_names:
        print(f"  DXGI 可见独显: {'; '.join(st.dxgi_names)}")
    else:
        print("  DXGI 枚举不到独显（适配器已从图形栈消失）")
    if st.igpu_only is not None:
        intent = "核显省电意图" if st.igpu_only else "混合模式意图"
        print(f"  GCU 注册表 IGPUonly = {st.igpu_only}（{intent}）")
    if st.gcu_direct_status or st.gcu_igpu_status:
        print(f"  GCU 意图回报: 直连={st.gcu_direct_status or '—'}"
              f"  核显省电={st.gcu_igpu_status or '—'}")


# === GCU 控制通道段（MQTT，与官方控制中心同路线）===
# 控制中心架构：UI（CCUWinUI）→ GCUBridge（127.0.0.1:13688，MQTT 3.1.1 broker）
#   → GCUService 执行（判据检查 / NV 服务联动 / EC 写入 / 超时回滚全套官方逻辑）。
# 本段向 Setting/Control 发布与控制中心点击完全相同的命令 = 同路线程序化触发。
# 凭据与 clientId 白名单来自 L-Mechrevo 反编译源码 + 2026-11-11 实测
# （UWPClient_3 曾被 broker 拒绝、UWPClient_4 可用；同 id 互踢，占用窗口仅数秒）。
# 通用化改造（方案 §2.1）：clientId 槽位回退链 _4 → _3 → _2，CONNACK 被拒换下一槽，
# 三槽全被拒才报「通道不可用」（one-shot 短连接无需 L-Mechrevo 的「恢复首选」逻辑）。
GCU_MQTT_HOST, GCU_MQTT_PORT = "127.0.0.1", 13688
_GCU_PWD_SALT = "888881772688"  # 密码常量，三代一致（E1：40 系 GCUBridge ClientManager 实录）


def gcu_credential_prefix() -> tuple[str, str]:
    """凭据前缀分线（E2，40 系 GCUBridge MQTTServer.InitialzeClient 实录）。

    读 HKLM\\SOFTWARE\\OEM\\{GamingCenter2,GamingCenter,ControlCenter} 的
    BridgeType / CustomizeTarget（双视图，首见值优先）。BridgeType==1 时按
    CustomizeTarget 分线：16/25→UWPIntelClient、29→UWPADATAClient、其他→
    UWPSTDClient；否则 UWPClient。返回 (前缀, 判定依据描述)；读不到或值
    不可解析一律 ("UWPClient", "默认（注册表无线索）")。
    """
    roots = (r"SOFTWARE\OEM\GamingCenter2", r"SOFTWARE\OEM\GamingCenter",
             r"SOFTWARE\OEM\ControlCenter")
    values: dict[str, str] = {}
    for root in roots:
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root, 0,
                                    winreg.KEY_READ | view) as k:
                    for name in ("BridgeType", "CustomizeTarget"):
                        if name in values:
                            continue  # 首见值优先（与 read_gcu_registry_caps 的 TryAdd 同口径）
                        try:
                            v, _t = winreg.QueryValueEx(k, name)
                        except OSError:
                            continue
                        values[name] = str(v)
            except OSError:
                continue
    try:
        bridge = int(values.get("BridgeType", "").strip())
    except ValueError:
        return "UWPClient", "默认（注册表无线索）"
    if bridge != 1:
        return "UWPClient", f"默认族（BridgeType={bridge}≠1）"
    try:
        target = int(values.get("CustomizeTarget", "").strip())
    except ValueError:
        return "UWPClient", "默认（BridgeType=1 但 CustomizeTarget 缺失/不可解析）"
    if target in (16, 25):
        return "UWPIntelClient", f"BridgeType=1，CustomizeTarget={target}（Intel 线）"
    if target == 29:
        return "UWPADATAClient", "BridgeType=1，CustomizeTarget=29（ADATA 线）"
    return "UWPSTDClient", f"BridgeType=1，CustomizeTarget={target}（STD 线）"


def gcu_mqtt_slots() -> tuple[tuple[str, str, str], ...]:
    """按前缀生成槽位三元组（E1 公式），槽位顺序 _4 → _3 → _2 不变。"""
    prefix, _src = gcu_credential_prefix()
    return tuple(
        (f"{prefix}_{n}", f"{prefix}_User_{n}", f"{prefix}_Pwd{_GCU_PWD_SALT}_{n}")
        for n in (4, 3, 2))


GCU_MQTT_SLOTS = gcu_mqtt_slots()  # 兼容旧名（模块加载时求值）；连接时现算，注册表运行中可见
GCU_ECO_ON_CMD = '{"Action": "IGPU_ONLY_CONNECT_RB_ON", "SetToWMIEC": "OK"}'
GCU_ECO_OFF_CMD = '{"Action": "IGPU_ONLY_CONNECT_RB_OFF", "SetToWMIEC": "OK"}'
# 直连命令（方案 §2.4 V1 定案）：纯 Action 报文，不带 SetToWMIEC
GCU_DIRECT_ON_CMD = '{"Action": "DGPU_DIRECT_CONNECT_TOGGLE_ON"}'
GCU_DIRECT_OFF_CMD = '{"Action": "DGPU_DIRECT_CONNECT_TOGGLE_OFF"}'
# 直连态切核显省电的主命令（MechrevoService.CreateGpuSwitchPayload 实录）：
# 三模档 mode 0 不走 IGPU_ONLY 族（那是混合态省电开关专用），MUX 直达核显独占
GCU_DIRECT_IGPU_CMD = '{"Action": "DGPU_DIRECT_CONNECT_TOGGLE_IGPU"}'
GCU_DIRECT_RESTART_CMD = '{"Action": "DGPU_DIRECT_CONNECT_RESTART"}'
GCU_GETSTATUS_CMD = '{"Action": "GETSTATUS"}'
# 30/40 常规机型官方省电（lite）：纯 Action、toggle 语义（E4：MechrevoService.
# SwitchGpuPowerSaving 实录，发送后仅记日志不等确认；服务端按 currentSaveingMode 翻转）
GCU_ECO_LITE_CMD = '{"Action": "GPU_POWERSAVEINGMODE"}'


def _mqtt_enc_len(n: int) -> bytes:
    """MQTT Remaining Length 变长编码。"""
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            return bytes(out)


def _mqtt_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def _mqtt_packet(body: bytes, ptype: int, flags: int = 0) -> bytes:
    return bytes([(ptype << 4) | flags]) + _mqtt_enc_len(len(body)) + body


def _mqtt_read_packet(sock) -> tuple[int, int, bytes] | None:
    """读一个完整 MQTT 包 → (type, flags, body)；连接关闭返回 None。"""
    head = sock.recv(1)
    if not head:
        return None
    ptype, flags = head[0] >> 4, head[0] & 0xF
    mult, length = 1, 0
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("broker 在长度字段中断开")
        length += (b[0] & 0x7F) * mult
        mult *= 128
        if not b[0] & 0x80:
            break
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise ConnectionError("broker 在包体中断开")
        body += chunk
    return ptype, flags, body


def _mqtt_parse_publish(flags: int, body: bytes) -> tuple[str, str]:
    """解析 QoS0 PUBLISH → (topic, payload)。"""
    tlen = struct.unpack(">H", body[:2])[0]
    topic = body[2:2 + tlen].decode("utf-8", "replace")
    pos = 2 + tlen
    if flags & 0x6:  # QoS1/2 有包标识
        pos += 2
    return topic, body[pos:].decode("utf-8", "replace")


def _mqtt_connect_slot() -> tuple[socket.socket | None, str]:
    """槽位回退链连接（方案 §2.1）：_4 → _3 → _2，CONNACK 被拒换下一槽。

    返回 (sock, 错误描述)；sock 非 None 即连上（槽位记在 GCU_LAST_CLIENT_ID）。
    MQTT 规范：CONNACK 拒绝后服务端已关连接——换槽必须重建 socket。
    """
    global GCU_LAST_CLIENT_ID
    rejected: list[str] = []
    for client_id, user, pwd in gcu_mqtt_slots():
        try:
            sock = socket.create_connection((GCU_MQTT_HOST, GCU_MQTT_PORT), timeout=5)
        except OSError as e:
            return None, (f"无法连接 {GCU_MQTT_HOST}:{GCU_MQTT_PORT}（{e}）——"
                          "GCUBridge 未运行？")
        try:
            sock.settimeout(1.0)
            sock.sendall(_mqtt_packet(
                _mqtt_str("MQTT") + bytes([4, 0xC2]) + struct.pack(">H", 30) +
                _mqtt_str(client_id) + _mqtt_str(user) +
                _mqtt_str(pwd), 1))
            rc = -1
            while True:
                p = _mqtt_read_packet(sock)
                if p is None:
                    break  # broker 断开：本槽失败，换下一槽
                ptype, _, body = p
                if ptype == 2:  # CONNACK
                    rc = body[1] if len(body) > 1 else -1
                    break
            if rc == 0:
                GCU_LAST_CLIENT_ID = client_id
                return sock, ""
            rejected.append(f"{client_id}(rc={rc})")
        except OSError:
            rejected.append(f"{client_id}(连接中断)")
        try:
            sock.close()
        except OSError:
            pass
    return None, ("broker 拒绝连接：三槽均被拒 [" + "、".join(rejected) +
                  "]——官方控制中心/L-Mechrevo 可能占用全部槽位")


GCU_LAST_CLIENT_ID = ""  # 最近一次成功连接的槽位（诊断展示用；one-shot 场景无并发）


def gcu_mqtt_command(payload: str, listen_secs: float = 8.0,
                     stop_keys: tuple[str, ...] = ()
                     ) -> tuple[str, list[tuple[str, str]]]:
    """连 GCU 控制通道：订阅状态回报 → 发布命令到 Setting/Control → 收 listen_secs 秒。

    返回 (状态, [(topic, payload), ...])。状态 "ok" 或错误描述。
    连接走槽位回退链（_mqtt_connect_slot，方案 §2.1）；失败形态分级：
    连接失败 / 三槽全被拒在此返回，发送成功零回报由调用方判断 events 为空。
    stop_keys：任一回报 payload 含这些子串时立即结束收听——判据码已到手，等满
    listen_secs 是白等（2026-11-13 实测每次浪费 ~7s）。
    仅依赖标准库 socket/struct 手写 MQTT 3.1.1 最小客户端。
    """
    events: list[tuple[str, str]] = []
    sock, err = _mqtt_connect_slot()
    if sock is None:
        return err, events
    try:
        for i, t in enumerate(("Setting/Status", "GPUDevice/Status"), start=1):
            sock.sendall(_mqtt_packet(
                struct.pack(">H", i) + _mqtt_str(t) + bytes([0]), 8, flags=2))
        sock.sendall(_mqtt_packet(
            _mqtt_str("Setting/Control") + payload.encode("utf-8"), 3))
        deadline = time.monotonic() + listen_secs
        while time.monotonic() < deadline:
            try:
                p = _mqtt_read_packet(sock)
            except socket.timeout:
                continue
            if p is None:
                break
            ptype, flags, body = p
            if ptype == 3:  # PUBLISH
                item = _mqtt_parse_publish(flags, body)
                events.append(item)
                if any(k in item[1] for k in stop_keys):
                    break
        return "ok", events
    finally:
        if sock is not None:
            sock.close()


def merge_gcu_events(events: list[tuple[str, str]]) -> dict:
    """把回报事件里的 JSON 字段合并成一个 dict（先到先留，与 L-Mechrevo 累积口径一致）。"""
    raw: dict = {}
    for _topic, payload in events:
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if isinstance(data, dict):
            for k, v in data.items():
                raw.setdefault(k, v)
    return raw


def first_field(data: dict, *names: str):
    """别名族取第一个存在的字段值（键名忽略大小写，FirstField 移植）。"""
    lowered = {k.lower(): v for k, v in data.items()} if data else {}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


_FLEX_TRUE = {"OPEN", "TRUE", "YES", "Y", "ON", "ENABLE", "ENABLED",
              "SUPPORT", "AVAILABLE", "SUPPORTED", "SUPPORTS"}
_FLEX_FALSE = {"NONE", "OFF", "N", "NO", "CLOSED", "DISABLE", "UNSUPPORT",
               "DISABLED", "CLOSE", "FALSE", "NONSUPPORT"}


def parse_flexible_bool(v) -> bool | None:
    """能力位值解析（ParseFlexibleBool 移植）：bool 直用；int !=0；字符串清洗后
    匹配 true/yes/on/supported/enable/enabled 等词族；无法解析的字符串按 False。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(s) != 0
    except ValueError:
        pass
    cleaned = re.sub(r"[ \-._]", "", s).upper()
    if cleaned in _FLEX_TRUE:
        return True
    if cleaned in _FLEX_FALSE:
        return False
    return False


def first_optional_bool(data: dict, *names: str) -> bool | None:
    """别名族取第一个存在且可解析的 bool（FirstOptionalBool 移植）。"""
    v = first_field(data, *names)
    if v is None:
        return None
    return parse_flexible_bool(v)


def print_gcu_events(events: list[tuple[str, str]]) -> None:
    """打印 GCU 回报中与显示模式相关的关键字段。"""
    for topic, payload in events:
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        for key, label in (
                ("CheckDGpuStatusforIGpuOnlyOnSuccess",
                 "GCU 判据码（1=判「显卡使用中」拒绝；2=判据通过）"),
                ("CheckDGpuStatusforIGpuOnlySwitch",
                 "GCU 判据码备用字段（85=通过；170=拒绝，新版 GCU 使用）"),
                ("DiscreteGpuDirectConnectionSwitch_Status", "GCU 直连状态"),
                ("IGpuOnlyConnectionSwitch_Status", "GCU 核显省电状态"),
                ("currentSaveingMode", "GCU currentSaveingMode（不可信，仅参考）")):
            if key in data:
                print(f"  GCU 回报 {topic}: {key} = {data[key]}（{label}）")


def gcu_switch_result(events: list[tuple[str, str]]) -> int | None:
    """判据码解析（GpuSwitchResult 移植，方案 §2.7 双字段）。

    返回 1=判「显卡使用中」拒绝、2=判据通过、0=未知编码、None=无判据回报。
    主字段 CheckDGpuStatusforIGpuOnlyOnSuccess 字符串 Contains（'1'→1、'2'→2）；
    备用字段 CheckDGpuStatusforIGpuOnlySwitch 整数（85→2、170→1，新版 GCU）。
    """
    for _topic, payload in events:
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        v = first_field(data, "CheckDGpuStatusforIGpuOnlyOnSuccess")
        if v is not None and str(v).strip():
            s = str(v)
            if "1" in s:
                return 1
            if "2" in s:
                return 2
            return 0
        v2 = first_field(data, "CheckDGpuStatusforIGpuOnlySwitch")
        if v2 is not None:
            try:
                n = int(str(v2).strip())
            except ValueError:
                continue
            return 2 if n == 85 else (1 if n == 170 else 0)
    return None


def gcu_rejected(events: list[tuple[str, str]]) -> bool:
    """GCU 判据码是否为 1（判「显卡使用中」拒绝）。

    判据在驱动层，回落比进程退出/WDDM 计数器滞后——2026-11-11 实测：
    清场复查（+2s）已干净，命令仍被判据码 1 拒绝；隔 ~10s 重发即成功。
    """
    return gcu_switch_result(events) == 1


# ---------------------------------------------------------------------------
# GCU 探测层（方案 §三 A）：代际判定 + 词表 + 在线回报/注册表能力 + --gcu-info
# ---------------------------------------------------------------------------

# dGPU 代际词表（DisplayRouteMatrix.cs 实录的 ConsoleActions 证据矩阵，方案 §1.3）
# 运行时省电族（E11：30/40/50 三代 UI bytes 全有；30/40 常规机型的官方省电走这族，
# 与 IGPU_ONLY 族的三模硬件省电是两码事——不停 EC、只断输出+降刷新率+PnP 软禁用）
POWERSAVE_LITE_ACTIONS = frozenset({
    "GPU_POWERSAVEINGMODE",       # 省电总开关（toggle，E4）
    "GPU_DC_ONCE", "GPU_DC_HZ", "GPU_MODE_HZ",
    "GPU_DISCONNECTMONITOR", "GPU_HZSETTING",
})
GEN30_ACTIONS = frozenset({
    "DGPU_DIRECT_CONNECT_TOGGLE_ON", "DGPU_DIRECT_CONNECT_TOGGLE_OFF"}) | POWERSAVE_LITE_ACTIONS
# GEN40_DUAL / GEN40_TRI / GEN50 均继承 GEN30，自动带上 lite 族
GEN40_DUAL_ACTIONS = GEN30_ACTIONS | {
    "DGPU_DIRECT_CONNECT_TOGGLE_IGPU", "DGPU_DIRECT_CONNECT_RESTART"}
GEN40_TRI_ACTIONS = GEN40_DUAL_ACTIONS | {
    "IGPU_ONLY_CONNECT_RB_ON", "IGPU_ONLY_CONNECT_RB_OFF"}
GEN50_ACTIONS = GEN40_TRI_ACTIONS | {
    "IGPU_ONLY_CONNECT_RB_AUTO", "DGPU_DIRECT_CONNECT_RESTART",
    "GPU_HOTSWAP_ON", "GPU_HOTSWAP_OFF"}

GEN_LABELS = {
    "gen30": "30 系（Gen30：仅直连⇄混合两态，无重启命令）",
    "gen40": "40 系（Gen40：三模/双模档由能力位细分）",
    "gen50": "50 系（Gen50：词表全量，含热切换枚举）",
    "no_dgpu": "无 NVIDIA 独显",
    "unknown": "代际不可判定",
}

# 注册表能力位路径族（MechrevoDeviceCapabilities.cs 实录，方案 §2.2）：
# ItemSupport 三根 + MySetting\GpuConfig 三根，首见值优先，双视图都读
GCU_CAP_PATHS = (
    r"SOFTWARE\OEM\GamingCenter2\ItemSupport",
    r"SOFTWARE\OEM\GamingCenter\ItemSupport",
    r"SOFTWARE\OEM\ControlCenter\ItemSupport",
    r"SOFTWARE\OEM\GamingCenter2\MySetting\GpuConfig",
    r"SOFTWARE\OEM\GamingCenter\MySetting\GpuConfig",
    r"SOFTWARE\OEM\ControlCenter\MySetting\GpuConfig",
)

BIOS_IDENTITY_KEYS = ("SystemManufacturer", "SystemProductName", "SystemFamily",
                      "SystemSKU", "BaseBoardManufacturer", "BaseBoardProduct", "BIOSVersion")


def _gen_from_device_id(v: int) -> str | None:
    """PCI DeviceId 数值 → 代际（FromDeviceId 高字节规则）。"""
    hi = (v >> 8) & 0xFF
    if 0x20 <= hi <= 0x25:
        return "gen30"
    if 0x26 <= hi <= 0x28:
        return "gen40"
    if 0x2B <= hi <= 0x30:
        return "gen50"
    return None


def _generation_from_pnp_registry() -> tuple[str, str] | None:
    r"""注册表 PnP 实例名推代际（省电态通道）。

    省电态（EC 级断开）独显从 DXGI 图形栈消失，但注册表 Enum\PCI 的实例名
    （含幻影设备）仍留着 VEN_10DE&DEV_XXXX——FromDeviceId 本就解析这种字符串，
    从 DEV_ 推代际是同一规则，保证 eco off（省电→混合）不被误判 no_dgpu 挡住。
    """
    for inst, _state in nvidia_pnp_states():
        m = re.search(r"DEV_([0-9A-Fa-f]{4})", inst)
        if m:
            gen = _gen_from_device_id(int(m[1], 16))
            if gen:
                return gen, f"注册表 PnP 实例 {inst}"
    return None


def detect_dgpu_generation(adapters: list[Adapter] | None = None) -> tuple[str, str]:
    """dGPU 代际判定（DgpuGenerationProbe 移植，方案 §2.3）。

    第一优先：显卡名正则 \\bRTX\\s*(\\d{4})\\b 取前两位；第二优先：PCI DeviceId
    高字节（0x20–0x25→Gen30、0x26–0x28→Gen40、0x2B–0x30→Gen50）；
    DXGI 无 NVIDIA（省电态 EC 断开属预期）时第三优先：注册表 PnP 实例名推代际。
    返回 (gen30/gen40/gen50/no_dgpu/unknown, 判定依据描述)。
    """
    try:
        adapters = enumerate_adapters() if adapters is None else adapters
    except OSError:
        reg = _generation_from_pnp_registry()
        return (reg if reg else ("unknown", "DXGI 枚举不可用"))
    nv = [a for a in adapters if a.vendor_id == VENDOR_NVIDIA and not a.software]
    if not nv:
        reg = _generation_from_pnp_registry()
        if reg:
            return reg
        return "no_dgpu", "无 NVIDIA 适配器（DXGI 与注册表 PnP 均无）"
    for a in nv:
        m = re.search(r"\bRTX\s*(\d{4})\b", a.name, re.IGNORECASE)
        if m:
            gen = {"30": "gen30", "40": "gen40", "50": "gen50"}.get(m[1][:2])
            if gen:
                return gen, f"显卡名「{a.name}」"
    for a in nv:
        gen = _gen_from_device_id(a.device_id)
        if gen:
            return gen, f"PCI DeviceId 0x{a.device_id:04X}"
    return "unknown", f"NVIDIA 适配器「{nv[0].name}」名字与 DeviceId 均未命中代际规则"


def read_gcu_registry_caps() -> dict:
    """注册表能力位（ItemSupport + GpuConfig 六路径 × 双视图，首见值优先 TryAdd）。"""
    values: dict = {}
    for path in GCU_CAP_PATHS:
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                                    winreg.KEY_READ | view) as k:
                    i = 0
                    while True:
                        try:
                            name, v, _t = winreg.EnumValue(k, i)
                        except OSError:
                            break
                        i += 1
                        values.setdefault(name.lower(), v)
            except OSError:
                continue
    return values


def read_machine_identity() -> dict:
    """机器身份（HARDWARE\\DESCRIPTION\\System\\BIOS，IsMechrevo 判定用）。"""
    out: dict = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\BIOS", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            for key in BIOS_IDENTITY_KEYS:
                try:
                    out[key], _ = winreg.QueryValueEx(k, key)
                except OSError:
                    pass
    except OSError:
        pass
    return out


def is_mechrevo_name(*names: str) -> bool:
    """IsMechrevoName 移植：名字含 MECHREVO 或 MECH REV（带空格形态，忽略大小写）。"""
    for n in names:
        u = (n or "").upper()
        if "MECHREVO" in u or "MECH REV" in u:
            return True
    return False


@dataclass
class GcuInfo:
    connected: bool                 # MQTT GETSTATUS 是否拿到回报
    connect_note: str               # 失败形态分级（连接失败/三槽全被拒/零回报）
    generation: str                 # gen30/gen40/gen50/no_dgpu/unknown
    gen_source: str                 # 代际判定依据
    vocab: set[str]                 # 本机代际词表（词表外命令不发）
    three_mode: bool | None         # Gen40 档位（True=三模/False=双模/None=未定或非 Gen40）
    igpu_only: bool | None          # 核显省电能力（None = 探测不可得）
    dgpu_direct: bool | None        # 独显直连能力
    igpu_source: str                # 能力位来源（在线回报/注册表/不可得）
    dgpu_source: str
    hot_swap_note: str              # Gen50 热切换词表在但 GCU no-op，如实标注
    is_mechrevo: bool | None        # 机器身份判定（None = 身份不可读）
    identity: dict                  # BIOS 身份字段
    direct_status: str              # GCU 意图回报原文
    igpu_status: str
    raw: dict                       # GETSTATUS 回报原始字段留档
    client_id: str = ""             # 实际连上的槽位（回退链观测，R4）
    credential_prefix: str = ""     # MQTT 凭据前缀（E2 分线；空=未探测）
    credential_source: str = ""     # 前缀判定依据描述（默认族/Intel 线/STD 线…）


def probe_gcu(listen_secs: float = 4.0) -> GcuInfo:
    """GCU 全量探测：代际 + 词表 + 能力（在线回报优先、注册表回落）+ 意图回报。

    SupportsDgpuDirect 三级回落（方案 §2.7）：在线回报 → 注册表 → 不可得
    （UEFI MUX 变量存在性一级不做，列为可选增强）。
    SupportsIgpuOnly 以在线回报优先——Gen40 注册表位不可靠（HasReliableIgpuOnlyCapability）。
    """
    identity = read_machine_identity()
    mech = (is_mechrevo_name(str(identity.get("SystemManufacturer", "")),
                             str(identity.get("BaseBoardManufacturer", "")))
            if identity else None)
    try:
        adapters = enumerate_adapters()
        gen, gen_source = detect_dgpu_generation(adapters)
    except OSError:
        gen, gen_source = "unknown", "DXGI 枚举不可用"

    status, events = gcu_mqtt_command(GCU_GETSTATUS_CMD, listen_secs=listen_secs)
    connected = status == "ok"
    note = "在线" if connected else status
    raw = merge_gcu_events(events) if connected else {}

    direct_status = str(first_field(raw, *DIRECT_STATUS_KEYS) or "") if raw else ""
    igpu_status = str(first_field(raw, *IGPU_STATUS_KEYS) or "") if raw else ""

    # 能力位：在线回报优先（FirstOptionalBool 别名族）
    dgpu_direct = first_optional_bool(
        raw, "DiscreteGpuDirectConnectionSwitch_Support",
        "DGpuDirectConnectionSwitch_Support", "DGPU_DIRECT_SUPPORT") if raw else None
    igpu_only = first_optional_bool(
        raw, "IGpuOnlyConnectionSwitch_Support",
        "IGPUOnlyConnectionSwitch_Support", "IGPU_ONLY_SUPPORT") if raw else None
    # 状态字段实证 > Support 位（IsRecognized* 实录提级）：GCU 活跃回报某功能的
    # 状态字段 = 功能存在的直接证据，Support 位 False 不再否决——Gen40 能力位
    # 误报实录（E8 注册表位不可靠；2026-11-20 40 系三模真机反馈：在线
    # Support=0 而 IGPU 状态字段活跃回报，eco 按钮被误禁、点击零反应零日志）。
    # 双模机不受影响：无该功能的 GCU 不回报对应状态字段，Support=False 维持。
    dgpu_by_status = resolve_direct_status(direct_status) is not None
    igpu_by_status = resolve_igpu_status(igpu_status) is not None
    if dgpu_by_status:
        dgpu_direct = True
    if igpu_by_status:
        igpu_only = True
    dgpu_source = ("在线回报（状态字段实证）" if dgpu_by_status
                   else "在线回报" if dgpu_direct is not None else "")
    igpu_source = ("在线回报（状态字段实证）" if igpu_by_status
                   else "在线回报" if igpu_only is not None else "")

    # 回落：注册表能力位（三根双视图别名族）
    reg = read_gcu_registry_caps()
    if dgpu_direct is None:
        dgpu_direct = parse_flexible_bool(first_field(
            reg, "DGpuDirectConnectionSupport",
            "DiscreteGpuDirectConnectionSupport", "MuxSwitchSupport"))
        dgpu_source = "注册表" if dgpu_direct is not None else "不可得"
    if igpu_only is None:
        igpu_only = parse_flexible_bool(first_field(
            reg, "iGPUModeOnlySupport", "IGpuOnlyModeSupport",
            "IntegratedGpuOnlySupport", "IGpuOnlyConnectionSwitch_Support"))
        igpu_source = "注册表" if igpu_only is not None else "不可得"

    # Gen40 三模档判定：在线回报 iGPUModeOnlySupport 优先，回落注册表
    #（TierFromCapability 实录：mqtt 有值用 mqtt，否则 persisted 候选）。
    # 两源皆空 = 未定（None），不硬判双模——40 系 GCUService 无在线能力字段、
    # 注册表位在 Gen40 被上游标注不可靠（E8），硬判 False 会误拒三模真机
    three_mode = None
    if gen == "gen40":
        tri = first_optional_bool(raw, "iGPUModeOnlySupport") if raw else None
        if tri is None:
            tri = parse_flexible_bool(first_field(reg, "iGPUModeOnlySupport"))
        if tri is not None:
            three_mode = bool(tri)

    if gen == "gen50":
        vocab = set(GEN50_ACTIONS)
    elif gen == "gen40":
        # None=未定 → 给三模全集：宁可多给词表，由能力门禁与实测判据把关，不空手拒绝
        vocab = set(GEN40_TRI_ACTIONS if three_mode is not False else GEN40_DUAL_ACTIONS)
    elif gen == "gen30":
        vocab = set(GEN30_ACTIONS)
    else:
        vocab = set()  # 未知代际：词表为空 = 词表外命令一律不发（硬门禁）

    hot_swap_note = ""
    if "GPU_HOTSWAP_ON" in vocab:
        hot_swap_note = ("词表含 GPU_HOTSWAP_ON/OFF，但 GCU 处理器为 log-only no-op"
                         "（未证实可用）——不提供热切换路径，仅如实标注")

    prefix, cred_src = gcu_credential_prefix()
    return GcuInfo(connected=connected, connect_note=note, generation=gen,
                   gen_source=gen_source, vocab=vocab, three_mode=three_mode,
                   igpu_only=igpu_only, dgpu_direct=dgpu_direct,
                   igpu_source=igpu_source, dgpu_source=dgpu_source,
                   hot_swap_note=hot_swap_note, is_mechrevo=mech,
                   identity=identity, direct_status=direct_status,
                   igpu_status=igpu_status, raw=raw,
                   client_id=(GCU_LAST_CLIENT_ID if connected else ""),
                   credential_prefix=prefix, credential_source=cred_src)


def print_gcu_info(info: GcuInfo) -> None:
    """--gcu-info 诊断输出：探测结果全量打印（README 截图 / 真机反馈口径）。"""
    print("=== GCU 探测结果 ===")
    mech = {True: "是（机械革命 GCU 机型）", False: "否（不在通用化范围）",
            None: "身份不可读"}[info.is_mechrevo]
    print(f"机器身份: {mech}")
    for k in BIOS_IDENTITY_KEYS:
        if k in info.identity:
            print(f"  {k} = {info.identity[k]}")
    print(f"dGPU 代际: {GEN_LABELS.get(info.generation, info.generation)}（{info.gen_source}）")
    tier = ""
    if info.generation == "gen40":  # 档位行只对 Gen40 有意义；None=未定也如实报
        tier = {True: "三模档（iGPUModeOnlySupport=1）",
                False: "双模档（无 IGPU_ONLY_*）",
                None: "未定（在线无回报且注册表无线索，按三模词表尝试）"}.get(info.three_mode, "")
    if tier:
        print(f"  Gen40 档位: {tier}")
    print(f"本机词表（{len(info.vocab)} 条，词表外命令不发）:")
    for a in sorted(info.vocab):
        print(f"    {a}")
    print(f"核显省电能力: {_cap_str(info.igpu_only)}（来源: {info.igpu_source}）")
    print(f"独显直连能力: {_cap_str(info.dgpu_direct)}（来源: {info.dgpu_source}）")
    if info.hot_swap_note:
        print(f"热切换: {info.hot_swap_note}")
    slot = f"（槽位 {info.client_id}，前缀 {info.credential_prefix}" \
           f"：{info.credential_source}）" if info.client_id else ""
    print(f"GCU 通道: {info.connect_note}{slot}")
    if info.connected:
        if info.direct_status or info.igpu_status:
            print(f"GCU 意图回报: 直连={info.direct_status or '—'}"
                  f"  核显省电={info.igpu_status or '—'}")
        if not info.raw:
            print("（命令送达但零回报：listen 窗口内 Setting/Status 未出现数据）")
        keys = [k for k in info.raw
                if any(t in k.lower() for t in ("gpu", "display", "mode", "mux", "switch"))]
        if keys:
            print("回报原始字段（显示相关，留档核对）:")
            for k in sorted(keys):
                print(f"    {k} = {info.raw[k]}")


def _cap_str(v: bool | None) -> str:
    return {True: "支持 ✓", False: "不支持 ✗", None: "探测不可得"}[v]


def enrich_with_gcu_status(st: DisplayModeState,
                           listen_secs: float = 4.0) -> str:
    """MQTT GETSTATUS 把 GCU 意图回报（凭证 B）填进 st；返回通道状态。

    --status 与切换编排收尾用；GUI 常规采样不查（每秒轮询会互踢常驻客户端）。
    """
    status, events = gcu_mqtt_command(GCU_GETSTATUS_CMD, listen_secs=listen_secs)
    if status != "ok":
        return status
    raw = merge_gcu_events(events)
    st.gcu_direct_status = str(first_field(raw, *DIRECT_STATUS_KEYS) or "")
    st.gcu_igpu_status = str(first_field(raw, *IGPU_STATUS_KEYS) or "")
    return "ok"


def wait_for_mode(target: str, timeout_sec: float = 180.0,
                  interval: float = 2.0) -> DisplayModeState:
    """轮询直到达到目标模式；始终返回最后一次状态（调用方按需判定）。"""
    start = time.monotonic()
    deadline = start + timeout_sec
    st = read_display_mode_state()
    while st.mode != target and time.monotonic() < deadline:
        elapsed = int(time.monotonic() - start)
        ps = [p for _, p in st.problems]
        cur = STATE_LABELS.get(ps[0], ps[0]) if ps else "不可读"
        print(f"  … 等待切换生效（已等 {elapsed}s，当前 PnP: {cur}）")
        time.sleep(interval)
        st = read_display_mode_state()
    return st


# ---------------------------------------------------------------------------
# 核显省电切换编排（--eco）：检测独显持有 → 清场 → 弹窗引导官方切换 → 凭证验证
# ---------------------------------------------------------------------------

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
    # GCU 后台：MQTT 命令的接收与执行者（--eco 切换段依赖它），杀了断控制通道
    # （2026-11-11 实测被误列驱动层候选强杀，靠服务自愈才没断；NVAPI 模块口径对它必中）
    "gcuservice.exe", "gcubridge.exe", "ccuwinui.exe",
    # GCU UI 家族（Install-Gcu.ps1 VendorProcessNames 实录，方案 §2.8）：
    # 控制中心各代 UI 外壳与托盘组件，同属杀了自愈/断 UI 的通道组件
    "controlcenteru.exe", "gamingcentercu.exe", "gcuui.exe", "systraycomponent.exe",
}

# NVIDIA 自家组件（PROTECTED_PROCESSES 的 NV 子集）：GCU 接到切换指令后联动停止的
# 对象（2026-11-16 嗅探实测：指令后 1s 内开始退出，nvcontainer 服务正常停止
# exit code 0，非裸杀）。判据码 1 期间它们正在退场，退光后驱动层判据才回落——
# 所以重发前等它们退场，不赌固定秒数（官方 UI 就是定时重发赌回落，要连发 4 次）。
NV_COMPONENT_NAMES = {
    "nvcontainer.exe", "nvdisplay.container.exe", "nvidia overlay.exe",
    "nvidia share.exe", "nvidia web helper.exe",
}


def nv_components_alive() -> list[str]:
    """NV 自家组件存活名单（去重进程名，按名排序）。tasklist 查不动返回空。"""
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                             capture_output=True, timeout=8
                             ).stdout.decode("gbk", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return []  # 查不动当退光——不 block 重发，后面还有循环兜底
    alive = set()
    for line in out.splitlines():
        name = line.split('","', 1)[0].strip('"').lower()
        if name in NV_COMPONENT_NAMES:
            alive.add(name)
    return sorted(alive)


def wait_nv_components_gone(timeout_sec: float = 40.0) -> bool:
    """等 NV 自家组件退场：全退返回 True，超时返回 False。

    每 2s 快照一次，每 5s 打一行进度（在场还剩几个）。
    """
    deadline = time.monotonic() + timeout_sec
    next_note = time.monotonic() + 5.0
    while True:
        left = nv_components_alive()
        if not left:
            return True
        now = time.monotonic()
        if now >= deadline:
            return False
        if now >= next_note:
            print(f"    … NV 组件仍在场 {len(left)} 个（{'、'.join(left)}）")
            next_note = now + 5.0
        time.sleep(2.0)


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


# ---------------------------------------------------------------------------
# 驱动层持有者检测：NVAPI/NVML 模块加载者（GCU「显卡使用中」判据的观察口径）
# ---------------------------------------------------------------------------
# 2026-11-11 实测：HWiNFO 轮询 GPU 传感器不创建 WDDM 图形上下文，WDDM 计数器与
# nvidia-smi 都抓不到它，但它加载着 nvapi64.dll/nvml.dll 直连驱动，控制台判据
# 因此拒绝切换（关闭 HWiNFO 后同操作一次成功）。此口径补上这个盲区。
# 注意：需提权才能读到 SYSTEM/提权进程的模块；进程用完即卸载的临时加载抓不到。

NV_MODULE_MARKS = ("nvapi", "nvml", "nvspcaps")  # 模块名包含即算 NVIDIA 用户态接口


class _MEW(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


_psapi = None


def _load_psapi():
    """psapi 惰性单例（同 _load_pdh/_load_kernel32 的模式）。"""
    global _psapi
    if _psapi is None:
        psapi = ctypes.WinDLL("psapi")
        psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        _psapi = psapi
    return _psapi


def _list_pids() -> list[int]:
    """psapi EnumProcesses 全量 pid。"""
    psapi = _load_psapi()
    arr = (wintypes.DWORD * 2048)()
    needed = wintypes.DWORD(0)
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return []
    return [arr[i] for i in range(needed.value // ctypes.sizeof(wintypes.DWORD))]


def nv_modules_of(pid: int) -> set[str]:
    """pid 加载的 NVIDIA 用户态接口模块（nvapi/nvml/nvspcaps）；快照打不开返回空集。"""
    k = _load_kernel32()
    INVALID = ctypes.c_void_p(-1).value
    msnap = k.CreateToolhelp32Snapshot(0x8 | 0x10, pid)  # MODULE | MODULE32
    if not msnap or msnap == INVALID:
        return set()
    try:
        me = _MEW()
        me.dwSize = ctypes.sizeof(_MEW)
        out: set[str] = set()
        ok = k.Module32FirstW(msnap, ctypes.byref(me))
        while ok:
            mod = me.szModule.lower()
            if any(m in mod for m in NV_MODULE_MARKS):
                out.add(me.szModule)
            ok = k.Module32NextW(msnap, ctypes.byref(me))
        return out
    finally:
        k.CloseHandle(msnap)


def nv_module_holders(exclude_names: set[str]) -> list[tuple[int, str, set[str]]]:
    """全系统扫描 NVAPI/NVML 模块持有者，返回 [(pid, 进程名, 模块集)]。

    exclude_names 按映像名小写排除（保护名单——系统/NVIDIA 自家组件杀了会被拉回）。
    """
    out: list[tuple[int, str, set[str]]] = []
    for pid in _list_pids():
        mods = nv_modules_of(pid)
        if not mods:
            continue
        image = process_image(pid)
        name = proc_name(image) if not image.startswith("<") else image
        if name.lower() in exclude_names:
            continue
        out.append((pid, name, mods))
    return out


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


def kill_by_name(name: str) -> tuple[bool, str]:
    """taskkill /F /T /IM：按映像名强杀全部同名进程及各自的进程树。

    必须提权调用——否则对以管理员运行的软件（HwMonitor/GamePP 等）直接拒绝访问。
    返回码 128 = 该名进程已不在：常见于父进程先被杀、子进程已随树退出（2026-11-13
    实测 HwMonitor64 随 GamePP 树退出后按名再杀报此码），按已结束算成功，复查段会兜底验证。
    """
    r = subprocess.run(["taskkill", "/F", "/T", "/IM", name],
                       capture_output=True)
    if r.returncode == 128:
        return True, ""
    msg = (r.stderr or r.stdout or b"").decode("gbk", "replace").strip().replace("\n", " | ")
    return r.returncode == 0, msg


def relaunch_elevated_eco(extra_args: str = "") -> bool:
    """UAC 提权重启 --eco 自身；强杀以管理员运行的占用软件需要管理员。"""
    if getattr(sys, "frozen", False):
        params = "--eco" + extra_args  # PyInstaller exe：重启 exe 自身，不能传 .py 路径
    else:
        params = f'"{os.path.abspath(__file__)}" --eco{extra_args}'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    return rc > 32


class _Tee:
    """stdout 双写：终端 + 日志文件（提权新窗口跑完即关，输出必须留档）。"""

    def __init__(self, path: str):
        # 行缓冲：进程被强杀/崩溃时已打印内容仍在盘上（2026-11-11 实测教训：
        # 块缓冲下被 taskkill，整轮失败诊断 0 字节丢失）
        self._f = open(path, "w", encoding="utf-8", errors="replace", buffering=1)
        self._out = sys.stdout  # windowed exe 下为 None：只写文件，不写终端

    def write(self, s):
        if self._out is not None:
            self._out.write(s)
        self._f.write(s)
        self._f.flush()
        return len(s)

    def flush(self):
        if self._out is not None:
            self._out.flush()
        self._f.flush()

    def close(self):
        self._f.close()


def app_base_dir() -> str:
    # onefile 冻结后 __file__ 指向临时解压目录（退出即焚），落盘文件
    #（logs/、settings.json）必须放在 exe 旁
    return os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                           else os.path.abspath(__file__))


def eco_log_path(prefix: str = "eco") -> str:
    d = os.path.join(app_base_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, time.strftime(f"{prefix}_%Y%m%d_%H%M%S.txt"))


def cleanup_logs(retention_days: float) -> tuple[int, int]:
    """按保留天数清理 logs/ 下超龄的切换留档（*.txt，按 mtime 判龄）。

    返回 (删除数, 失败数)。单文件删除失败（被占用等 IO 边界）不中断，
    计入失败数交调用方如实展示；目录不存在视作无事可做。
    """
    d = os.path.join(app_base_dir(), "logs")
    deadline = time.time() - retention_days * 86400
    removed = failed = 0
    try:
        names = os.listdir(d)
    except OSError:
        return 0, 0
    for name in names:
        if not name.lower().endswith(".txt"):
            continue
        p = os.path.join(d, name)
        try:
            if os.path.getmtime(p) < deadline:
                os.remove(p)
                removed += 1
        except OSError:
            failed += 1
    return removed, failed


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
        note = f"  ← {r['_note']}" if r.get("_note") else ""
        print(f"  pid {r['pid']:<7} {proc_name(r['image']):<{name_w}}"
              f"  VRAM {r['local_mb']:>8.2f} MB  {eng}{note}")


def _eco_on_from_direct(st: DisplayModeState) -> int:
    """直连态/直连待重启态 → 核显省电（官方 SwitchGpuMode(mode=0) 三模档实录）。

    核显省电的前提是内屏由核显驱动；直连态内屏挂在独显上，必须先过 MUX
    （重启生效）。官方主命令是 DGPU_DIRECT_CONNECT_TOGGLE_IGPU——MUX 直达
    核显独占，重启后即省电态，全程不发 IGPU_ONLY 族（那是混合态省电开关
    专用通道，直连态发它判据永不可能满足）。2026-11-20 修正：此前无此分支，
    直连态误走混合态清场+发令路径，切换必然失败。
    不清场（内屏在独显上，杀持有者无意义）、无需提权。
    """
    enrich_with_gcu_status(st)  # 待重启态分流需要意图（mode property 动态重判）
    if st.mode == "direct_pending":
        # 进得来此函数的实况必是直连，pending 只能是「退出直连」方向（意图核显/混合）
        if st.gcu_intent == 0:
            print("\n「直连 → 核显省电」指令已生效待重启：重启后即到核显省电态"
                  "（--status / GUI 徽章显示待重启态）。")
            return 0
        print("\n检测到「直连回混合」待重启指令：重启后落混合模式，"
              "再跑 --eco 完成省电（混合态路径无需重启）。")
        return 0

    print("\n当前为独显直连态：核显省电须先退出直连（MUX 切换，重启生效）——"
          "与控制中心同款走 TOGGLE_IGPU，一次重启直达省电态。")
    info, rc = _direct_capability_gate("DGPU_DIRECT_CONNECT_TOGGLE_IGPU")
    if info is None:
        return rc

    print("\n--- 发送切换命令（GCU 控制通道，纯 Action 报文与控制中心同路线）---")
    status, events = gcu_mqtt_command(GCU_DIRECT_IGPU_CMD, listen_secs=2.0)
    if status != "ok":
        print(f"X GCU 控制通道不可用：{status}")
        print("  请确认机械革命控制中心后台（GCUBridge / GCUService）在运行后重试。")
        return 1
    print("  ✓ 命令已送达（DGPU_DIRECT_CONNECT_TOGGLE_IGPU）")
    print_gcu_events(events)

    print("\n--- 等待 GCU 确认（GETSTATUS 复查直连状态翻转，7×1.8s）---")
    ok, cur = wait_direct_status("TOGGLE_IGPU")
    if not ok:
        print(f"\nX GCU 未确认指令（当前直连状态: {cur or '无回报'}）。")
        res = read_display_mode_state()
        enrich_with_gcu_status(res)
        print_mode_state(res)
        print("建议：重启是 MUX pending 卡住的实测恢复手段（L-Mechrevo 同款实录）。")
        return 1
    print(f"  ✓ GCU 已确认（直连状态 = {cur}）——处于待重启态，"
          "重启后内屏改由核显驱动、独显断电。")

    choice = direct_restart_choice()
    if choice == "1":
        if not restart_countdown("仍会切换为核显省电"):
            runonce_set_reconcile()  # 倒计时取消：待重启态保持，重启后仍自动补账
            return 0
        runonce_set_reconcile()  # RESTART 后 1-2s 即断电，先登记补账再发令
        print("\n--- 发送重启命令（GCU 收到后自行执行 shutdown /r /t 0）---")
        rst, _ev = gcu_mqtt_command(GCU_DIRECT_RESTART_CMD, listen_secs=2.0)
        if rst != "ok":
            print(f"X 重启命令发送失败：{rst}")
            print("  机器未重启；已处于待重启态，可稍后手动重启或重跑 --eco。")
            return 1
        print("  ✓ 重启命令已发出，机器即将重启。")
        print("  （已登记重启后自动补账：登录时静默执行一次，官方控制台恢复识别省电态）")
        return 0
    if choice == "2":
        runonce_set_reconcile()
        print("  保持待重启态：稍后自行重启即完成切换（--status 显示待重启态）。")
        print("  （已登记重启后自动补账：登录时静默执行一次，官方控制台恢复识别省电态）")
        return 0
    # 取消撤回：发 TOGGLE_ON 翻回直连意图（IGPU 意图从没发过，无需撤）
    rc = _direct_cancel_revert(
        GCU_DIRECT_ON_CMD, "DGPU_DIRECT_CONNECT_TOGGLE_ON", "TOGGLE_ON",
        "保持独显直连", "可重跑 --direct on 撤回（其待重启分支会重发直连命令）")
    if rc == 0:
        runonce_clear_reconcile()  # 撤回成功：重启不会落省电，补账登记一并取消
    return rc


def cmd_eco_on(counters: GpuCounters, adapters_meta: list[Adapter],
               auto_yes: bool = False, keep_names: set[str] | None = None) -> int:
    """切核显省电（真切换）：检测独显持有 → 清场 → 发 GCU 同款 MQTT 命令 → 凭证验证。

    直连/直连待重启态走 _eco_on_from_direct（MUX 重启直达，官方三模档实录）。
    auto_yes：清场确认自动应答 1（无人值守跑法；GUI 不传，走问答框）。
    keep_names：清场豁免名单（小写进程名）；其余候选照杀、豁免者保留在场——
    受控实验用（如只留 steam.exe/Everything.exe，实测它们是否挡 GCU 判据）。
    """
    st = read_display_mode_state()
    print("=== 核显省电切换编排（检测 → 清场 → 发送官方同款命令 → 凭证验证）===")
    print_mode_state(st)
    if st.mode == "igpu_only":
        print("\n已在核显省电态（PnP 已断开 + DXGI 不可见），无需切换。")
        return 0
    if st.mode == "pnp_disabled":
        # lite 机型（词表含 GPU_POWERSAVEINGMODE 且无 MUX 省电能力）上这是官方
        # 运行时省电的正常落位（E7：PnP 禁卡是 lite 的副作用），不是假切换残留
        probe = probe_gcu()
        if "GPU_POWERSAVEINGMODE" in probe.vocab and probe.igpu_only is not True:
            print("\n已在 lite 省电态（PnP 软禁用，官方运行时省电）。恢复用 --eco lite off。")
            return 0
        print("\n检测到 PnP 软禁用残留（假切换形态）：设备仍焊在总线上。")
        print("请先在设备管理器启用 NVIDIA 设备（或 gpu_mode_switch 项目 gpu_switch.py off）")
        print("恢复混合模式后，再用本命令走官方真切换。")
        return 1
    if st.mode == "unknown":
        print("\n无法判定当前显示模式（PnP/DXGI 凭证异常，如上），不继续。")
        return 1
    # 直连/直连待重启态走 MUX 重启直达编排（官方 SwitchGpuMode(mode=0) 实录），
    # 不走下面的清场+IGPU 命令——那是混合态专用路径
    if st.mode in ("dgpu_direct", "direct_pending"):
        return _eco_on_from_direct(st)
    if st.mode == "hybrid":
        # 混合实况下可能挂着「进入直连」待重启指令——此刻切省电重启后会落直连白切
        enrich_with_gcu_status(st)
        if st.mode == "direct_pending":
            print("\n检测到「进入直连」待重启指令（与目标相反）：先重启落直连后重跑 --eco，")
            print("  或 --direct on 撤回回混合后再切省电。")
            return 1

    # 能力前置（通用化改造方案 §三 E「与 eco 的关系」）+ lite 路由（3040 方案 §三）：
    # 无 MUX 省电档（能力位/词表）但词表含官方运行时省电族 → 改走 lite，
    # 30 系 / 40 双模档在此自动进 lite；40 未定与 50 系走原路径
    info = probe_gcu()
    lite_ok = "GPU_POWERSAVEINGMODE" in info.vocab
    if info.igpu_only is False or "IGPU_ONLY_CONNECT_RB_ON" not in info.vocab:
        if lite_ok:
            print("\n本机无 MUX 核显省电档（能力位/词表），改走官方运行时省电（lite）…")
            return cmd_eco_lite(on=True)
        if info.igpu_only is False:
            print(f"\nX 本机不支持核显省电（能力位来源: {info.igpu_source}）。")
            return 1
        print(f"\nX rejected: action not in dGPU generation {info.generation} vocabulary")
        print(f"  本机词表不含 IGPU_ONLY_CONNECT_RB_ON"
              f"（{GEN_LABELS.get(info.generation, info.generation)}）。")
        return 1
    if info.igpu_only is None:
        print("\n! 核显省电能力探测不可得（GCU 离线且注册表无能力位），继续执行。")

    if not is_admin():
        print("\n关闭提权软件（HwMonitor/GamePP 等）需要管理员，提权继续…")
        extra = ((" --keep " + ",".join(sorted(keep_names))) if keep_names else "") + \
                (" --yes" if auto_yes else "")
        if relaunch_elevated_eco(extra):
            print("已在提权的新窗口继续，本窗口退出。")
            print(f"完整输出与日志见项目 logs/ 目录（{os.path.dirname(eco_log_path())}）")
            return 0
        print("提权被取消，退出。")
        return 1
    print("（已提权运行）")

    # ① 抓独显持有清单（collect_gpu_holders 两口径，清场与 reconcile 错位纠正共用）
    candidates, skipped, drv_holders, stale = collect_gpu_holders(counters, adapters_meta)
    if stale:
        print(f"（已过滤陈旧计数器实例 {stale} 个：进程已退出）")

    # ② 分类提示 + 选择；关闭 = 按进程名强杀全部同名实例（含子进程树）
    keep = {n.lower() for n in keep_names} if keep_names else set()
    candidates.sort(key=lambda r: (-r["local_mb"], -max(r["engines"].values(), default=0.0)))
    handled_pids: set[int] = set()
    handled_names: set[str] = set()
    if candidates or drv_holders:
        if candidates:
            print_eco_rows(candidates, f"图形上下文持有（{len(candidates)} 个，按显存排序）")
        if drv_holders:
            print(f"\n--- 驱动层持有（{len(drv_holders)} 个，NVAPI/NVML 模块加载者："
                  "不占图形上下文；轮询类实测挡判据，纯加载类实测不挡）---")
            name_w = max(len(n) for _, n, _ in drv_holders)
            for pid, name, mods in drv_holders:
                keep_mark = "（--keep 保留）" if name.lower() in keep else ""
                print(f"  pid {pid:<7} {name:<{name_w}}  [{', '.join(sorted(mods))}]{keep_mark}")
        print_eco_rows(skipped, f"系统/服务组件（{len(skipped)} 个，跳过不动：杀了被拉回或系统组件）")
        choice = ("1（--yes 自动应答）" if (auto_yes or keep) else
                  ask("\n[1] 结束以上候选（含全部同名实例与子进程）并继续"
                      "  [2] 不关闭直接继续  [回车/其他] 取消"))
        choice = "1" if (auto_yes or keep) else choice
        if choice == "1":
            names = sorted(n for n in ({proc_name(r["image"]) for r in candidates} |
                                       {name for _, name, _ in drv_holders})
                           if n.lower() not in keep)
            print(f"\n--- 强制结束（taskkill /F /T /IM，共 {len(names)} 个进程名）---")
            if keep:
                print(f"（--keep 豁免未杀: {', '.join(sorted(keep))}）")
            handled_pids = ({r["pid"] for r in candidates} | {p for p, _, _ in drv_holders}) - \
                {r["pid"] for r in candidates if proc_name(r["image"]).lower() in keep} - \
                {p for p, n, _ in drv_holders if n.lower() in keep}
            handled_names = ({proc_name(r["image"]).lower() for r in candidates} |
                             {name.lower() for _, name, _ in drv_holders}) - keep
            for name in names:
                ok, msg = kill_by_name(name)
                tail = "" if ok else f" —— {msg[:120]}"
                print(f"  {'✓' if ok else '✗'} {name}{tail}")
        elif choice != "2":
            print("已取消，未做任何改动。")
            return 1
    else:
        print("独显当前无用户软件持有 GPU 上下文或 NVAPI 句柄，无需关闭。")
        print_eco_rows(skipped, f"系统/服务组件（{len(skipped)} 个，不阻碍切换）")

    # ③ 复查：重新采样 + 模块重扫；同名新 pid / 重新加载 = 被守护/自启拉回
    print("\n--- 复查（重新采样）---")
    time.sleep(2.0)  # 给 WDDM 上下文销毁、计数器实例消失留时间
    remaining = []
    for gpu in sample_once(counters, adapters_meta, include_all=False):
        for r in gpu["processes"]:
            alive, _ = classify_pid(r["pid"])
            if not alive:
                continue
            if r["pid"] in handled_pids:
                r["_note"] = "杀失败残留"
            elif proc_name(r["image"]).lower() in keep:
                r["_note"] = "--keep 实验保留对象"
            elif proc_name(r["image"]).lower() in handled_names:
                r["_note"] = "被守护/自启拉回（新 pid），建议软件内退出或禁自启"
            remaining.append(r)
    drv_left = nv_module_holders(PROTECTED_PROCESSES)
    drv_notes = []
    for pid, name, mods in drv_left:
        if name.lower() in keep:
            note = "（--keep 实验保留对象）"
        elif name.lower() in handled_names:
            note = "（被守护/自启拉回），建议软件内退出或禁自启"
        else:
            note = ""
        drv_notes.append(f"  pid {pid:<7} {name}  [{', '.join(sorted(mods))}]{note}")
    if remaining or drv_notes:
        print_eco_rows(remaining, f"图形上下文剩余 {len(remaining)} 个（系统组件不阻碍切换）")
        if drv_notes:
            print(f"驱动层剩余 {len(drv_notes)} 个（轮询类挡判据建议处理；纯加载类实测不挡）：")
            for line in drv_notes:
                print(line)
        print("（若下面切换失败，优先怀疑又有软件扑上来挡判据）")
    else:
        print("  ✓ 独显已无任何存活持有（图形上下文 + 驱动层双口径）")

    # ④ 发送官方同款切换命令（控制中心 UI 的同一条 MQTT 通道，GCUService 执行）。
    #    判据码 1 不是拒绝而是中间态（2026-11-16 嗅探实测）：GCU 接指令后 1s 内联动
    #    停止 NV 自家组件，退场期间驱动层判据仍见其持有；等 NV 组件退光 + 5s 回落
    #    余量再重发，基本必中（官方 UI 是定时盲重发，连发 4 次耗 25s）。保留 3 次
    #    循环兜底；stop_keys 让判据码一到就返回，不傻等 listen 满 8s。
    print("\n--- 发送切换命令（GCU 控制通道，与控制中心点击同路线）---")
    events: list[tuple[str, str]] = []
    for attempt in range(1, 4):
        status, events = gcu_mqtt_command(GCU_ECO_ON_CMD, listen_secs=8.0,
                                          stop_keys=("CheckDGpuStatusforIGpuOnlyOnSuccess",))
        if status != "ok":
            print(f"X GCU 控制通道不可用：{status}")
            print("  请确认机械革命控制中心后台（GCUBridge / GCUService）在运行后重试。")
            return 1
        print(f"  ✓ 命令已送达（IGPU_ONLY_CONNECT_RB_ON，第 {attempt} 次）")
        print_gcu_events(events)
        if not gcu_rejected(events):
            break
        if attempt < 3:
            print("  ! 判据码 1：GCU 正联动停止 NV 自家组件（预期中间态），"
                  "等它们退场后再重发…")
            if wait_nv_components_gone():
                print("    NV 组件已退光，再留 15s 判据回落余量")
                # 2026-11-16 两次实测：NV 退光后驱动层判据回落还需 10-15s+；
                # 5s 余量实测第 2 发仍被拒（退光后 ~6s 发），15s 才稳落回落完成窗
                time.sleep(15.0)
            else:
                print("    40s 内 NV 组件未退光，照常重发兜底")
    rejected = gcu_rejected(events)

    res = None
    if rejected:
        print("\nX 三次命令均被判据码 1（判「显卡使用中」），不再空等。当前状态：")
        res = read_display_mode_state()
        print_mode_state(res)
    else:
        print("\n--- 验证切换（轮询 PnP problem code / DXGI / IGPUonly，上限 100 秒）---")
        res = wait_for_mode("igpu_only", timeout_sec=100.0)
        if res.mode == "igpu_only":
            print("\n✓ 核显省电切换成功（官方路径，EC 级断开）：")
            print_mode_state(res)
            return 0
        print("\nX 100 秒内未检测到 EC 级断开，切换未完成。当前状态：")
        print_mode_state(res)
    st2, ev2 = gcu_mqtt_command('{"Action": "GETSTATUS"}', listen_secs=4.0)
    if st2 == "ok":
        print_gcu_events(ev2)
    print("\n常见原因：清场后有软件重新拉起独显（图形上下文或 NVAPI 轮询），"
          "GCU 判据仍判「显卡使用中」。复查当前独显持有（双口径）：")
    time.sleep(0.5)
    print_eco_rows([r for gpu in sample_once(counters, adapters_meta, include_all=False)
                    for r in gpu["processes"] if classify_pid(r["pid"])[0]],
                   "图形上下文持有（全部）")
    drv = nv_module_holders(PROTECTED_PROCESSES)
    print(f"--- 驱动层持有（{len(drv)} 个）---")
    for pid, name, mods in drv:
        print(f"  pid {pid:<7} {name}  [{', '.join(sorted(mods))}]")
    print("可处理后重跑 --eco 再试；或用 --status 随时查看当前模式。")
    return 1


def collect_gpu_holders(counters: GpuCounters, adapters_meta: list[Adapter]) \
        -> tuple[list[dict], list[dict], list[tuple[int, str, set[str]]], int]:
    """抓独显持有清单（WDDM 图形上下文 + NVAPI/NVML 驱动层两口径）。

    返回 (candidates, skipped, drv_holders, stale)：可关闭候选（用户软件）、
    系统/服务/受保护组件、驱动层模块持有者、已退出的陈旧实例数。
    eco on 清场与 reconcile 错位纠正共用同一口径。
    驱动层口径备注：传感器轮询类软件（HWiNFO 等）不创建 WDDM 图形上下文，
    上下文口径抓不到，但 GCU 判据看得到（2026-11-11 实测教训）；持续轮询类
    实测挡判据，仅加载 nvapi64 不轮询类（Steam/Everything）实测不挡。
    """
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
    wddm_pids = {r["pid"] for r in candidates} | {r["pid"] for r in skipped}
    drv_holders = [(pid, name, mods) for pid, name, mods in
                   nv_module_holders(PROTECTED_PROCESSES)
                   if pid not in wddm_pids]
    return candidates, skipped, drv_holders, stale


def _gcu_wait_ready(max_secs: float = 180.0, poll_secs: float = 2.0) -> bool:
    """等 GCU 控制通道就绪（GETSTATUS 有应答）——对 GCU 启动速度零假设。

    RunOnce 在登录时跑，GCUService（系统服务）可能尚未起来：轮询到它应答
    为止；180s 上限只是防御性封顶，正常服务几秒内就绪。就绪判定要求 events
    非空——MQTT 通了但 GCU 没应答视为未就绪（broker 常驻而 GCU 后起）。
    """
    deadline = time.time() + max_secs
    rnd = 0
    while time.time() < deadline:
        status, events = gcu_mqtt_command(GCU_GETSTATUS_CMD, listen_secs=poll_secs)
        if status == "ok" and events:
            return True
        rnd += 1
        if rnd % 5 == 1:
            print(f"  … 等待 GCU 控制通道就绪（已等 {rnd * poll_secs:.0f}s，"
                  "服务可能仍在启动）")
    return False


def _send_eco_on_command(context: str) -> bool:
    """发 IGPU_RB_ON（省电开关同款命令）并打回报。返回命令是否送达。

    判据被拒（码 1）不算送达失败——调用方后续 wait_for_mode 给最终判定。
    """
    for attempt in range(1, 3):
        status, events = gcu_mqtt_command(GCU_ECO_ON_CMD, listen_secs=8.0,
                                          stop_keys=("IGpuOnlyConnectionSwitch_Status",
                                                     "CheckDGpuStatusforIGpuOnlyOnSuccess"))
        if status != "ok":
            print(f"X GCU 控制通道不可用：{status}（{context}）")
            return False
        print(f"  ✓ 命令已送达（IGPU_ONLY_CONNECT_RB_ON，第 {attempt} 次）")
        print_gcu_events(events)
        if not gcu_rejected(events):
            break
    return True


def _is_toggle_igpu_ledger(st: DisplayModeState) -> bool:
    """账外省电态判定：直连账本挂着 TOGGLE_IGPU（直连直达落下的态）。

    该态物理上是标准省电（PnP 断开 + DXGI 消失），但两本账与官方 UI 的
    三态判定全对不上——控制台三态全灰（2026-11-20 真机实录）。
    """
    return "DGPU_DIRECT_CONNECT_TOGGLE_IGPU" in (st.gcu_direct_status or "").upper()


def _reconcile_misalign_recover() -> int:
    """错位纠正：账面省电 ON、物理独显在线（重启过渡期对齐被占用软件挡住）。

    看证据不看时间：无人持有 → GCU 对齐应能自愈，等它；有持有 → 判据必被
    挡，清场（无人值守 auto 语义：杀可关闭候选与驱动层持有者，跳过系统组件）
    后补发省电命令并验证断电。杀不净如实记录，不弹窗打扰登录。
    """
    try:
        counters = GpuCounters()
    except OSError as e:
        print(f"X 计数器打开失败，无法清场纠正：{e}")
        return 1
    try:
        try:
            adapters = enumerate_adapters()
        except OSError:
            adapters = []
        candidates, _skipped, drv_holders, stale = collect_gpu_holders(counters, adapters)
        if stale:
            print(f"（已过滤陈旧计数器实例 {stale} 个）")

        if not candidates and not drv_holders:
            print("\n独显在线但无用户软件持有——GCU 对齐应能自愈，等它完成（上限 30s）…")
            res = wait_for_mode("igpu_only", timeout_sec=30.0)
            if res.mode == "igpu_only":
                print("  ✓ GCU 已自行完成省电对齐。")
                return 0
            print("  30s 后独显仍在线，复查持有、按被挡处理…")
            candidates, _skipped, drv_holders, _stale = collect_gpu_holders(counters, adapters)

        if candidates or drv_holders:
            print(f"\n--- 清场（无人值守：图形上下文 {len(candidates)} 个 + 驱动层 {len(drv_holders)} 个）---")
            if candidates:
                print_eco_rows(candidates, f"图形上下文持有（{len(candidates)} 个）")
            for pid, name, mods in drv_holders:
                print(f"  pid {pid:<7} {name}  [{', '.join(sorted(mods))}]")
            names = sorted({proc_name(r["image"]) for r in candidates} |
                           {name for _, name, _ in drv_holders})
            for name in names:
                ok, msg = kill_by_name(name)
                tail = "" if ok else f" —— {msg[:120]}"
                print(f"  {'✓' if ok else '✗'} {name}{tail}")
            time.sleep(2.0)  # 给 WDDM 上下文销毁留时间
            # 复查拉回（守护/自启）：如实记录不重杀
            c2, _s2, drv2, _st2 = collect_gpu_holders(counters, adapters)
            if c2 or drv2:
                print("  ! 有持有者被拉回（守护/自启），如下——切换可能仍被挡：")
                for r in c2:
                    print(f"    pid {r['pid']:<7} {proc_name(r['image'])}")
                for pid, name, mods in drv2:
                    print(f"    pid {pid:<7} {name}  [{', '.join(sorted(mods))}]")

        if not _send_eco_on_command("错位纠正"):
            return 1
        print("\n--- 验证省电落位（轮询 PnP/DXGI，上限 60 秒）---")
        res = wait_for_mode("igpu_only", timeout_sec=60.0)
        if res.mode == "igpu_only":
            print("\n✓ 错位已纠正：独显断电，回到核显省电态。")
            return 0
        print("\nX 60 秒内独显未断电，错位纠正未完成（占用者可能杀不掉）。当前状态：")
        print_mode_state(res)
        print("可稍后手动跑 --eco on（交互清场）或 --status 查看当前持有。")
        return 1
    finally:
        counters.close()


def cmd_eco_reconcile() -> int:
    """省电对账与错位纠正（直连直达省电后的 RunOnce 配套，无人值守幂等）。

    官方控制台是二态选择器（独显直连/混合模式）+ 混合下的省电子开关
    （咩咩 2026-11-20 纠正的心智模型）。标准省电组合 = 直连账本 OFF +
    省电账本 RB_ON。三分流：
    ① 已在省电态：两本账各自独立补齐（RB_ON / TOGGLE_OFF 归位 IGPU 位）；
    ② 账面 ON 而独显在线（重启过渡期对齐被自启软件抢占挡住）：错位纠正
       ——等 GCU 就绪后看持有证据决定等自愈还是清场补发；
    ③ 其他状态：已切走，退出。
    对 GCU 启动速度零假设：先轮询 GETSTATUS 到应答（上限 180s）再动手。
    """
    print("=== 省电对账与错位纠正（直连直达省电后的官方控制台识别补齐）===")
    if not _gcu_wait_ready():
        print("X GCU 控制通道 180s 内未就绪（GCUService 可能被禁或异常）。")
        print("  省电开关由 GCU 执行，它不在就无人断电——请检查控制中心后台后手动 --eco on。")
        return 1
    st = read_display_mode_state()
    enrich_with_gcu_status(st)
    print_mode_state(st)

    if st.base_mode == "igpu_only":
        ledger_ok = resolve_igpu_status(st.gcu_igpu_status) == 0
        direct_off_needed = _is_toggle_igpu_ledger(st)
        if ledger_ok and not direct_off_needed:
            print("\n✓ 已在省电态且两本账对齐（省电 ON + 直连 OFF），无事可做。")
            return 0
        info = probe_gcu()

        # ① 省电账本 → ON（混合大类下的省电子开关勾上；独显已下线判据必过）
        if not ledger_ok:
            if info.igpu_only is False or "IGPU_ONLY_CONNECT_RB_ON" not in info.vocab:
                print("\nX 本机不支持省电账本命令，无法补账（能力位/词表如上）。")
                return 1
            print("\n--- 补账 ① 省电账本翻 ON（独显已下线，判据必过）---")
            if not _send_eco_on_command("账本补齐"):
                return 1
            print("\n--- 等待省电账本翻 ON（GETSTATUS 复查，5×1.8s）---")
            ok, cur = wait_igpu_status("IGPU_ONLY_CONNECT_RB_ON")
            if not ok:
                print(f"X 省电账本未翻转（当前: {cur or '无回报'}）——可重跑 --eco reconcile。")
                return 1
            print(f"  ✓ 省电账本已翻 ON（{cur}）。")

        # ② 直连账本 IGPU 位 → OFF 位（UI 的「混合模式」选中判定认的就是它）
        if direct_off_needed:
            if info.dgpu_direct is False or "DGPU_DIRECT_CONNECT_TOGGLE_OFF" not in info.vocab:
                print("\nX 本机词表不含 TOGGLE_OFF，直连账本无法归位（省电开关账已对齐）。")
                return 1
            print("\n--- 补账 ② 直连账本 IGPU 位归位 OFF（控制台混合模式恢复选中）---")
            status, events = gcu_mqtt_command(GCU_DIRECT_OFF_CMD, listen_secs=2.0)
            if status != "ok":
                print(f"X GCU 控制通道不可用：{status}——省电账已对齐，可重跑 --eco reconcile 补。")
                return 1
            print("  ✓ 命令已送达（DGPU_DIRECT_CONNECT_TOGGLE_OFF）")
            print_gcu_events(events)
            ok, cur = wait_direct_status("TOGGLE_OFF")
            if not ok:
                print(f"X 直连账本未翻转（当前: {cur or '无回报'}）——可重跑 --eco reconcile 补。")
                return 1
            print(f"  ✓ 直连账本已归位 OFF（{cur}）——两本账对齐，官方控制台恢复显示")
            print("    （混合选中 + 省电勾上；重启后过渡几十秒独显在线，GCU 起来会拉回省电，")
            print("     被自启软件抢占则由本命令的错位纠正段兜底）")
        return 0

    if st.base_mode == "hybrid" and resolve_igpu_status(st.gcu_igpu_status) == 0:
        print("\n检测到错位：省电开关记 ON 但独显在线（重启过渡期对齐被挡？）——纠正。")
        return _reconcile_misalign_recover()

    print("\n当前不在省电相关状态（可能已切换走），无账可补，退出。")
    return 0


def _eco_off_from_toggle_igpu(st: DisplayModeState) -> int:
    """账外省电态 → 混合（官方同款回程 TOGGLE_OFF）。

    官方 UI 在账外态点「混合模式」发的就是 TOGGLE_OFF（MechrevoService.
    CreateGpuSwitchPayload(1) 实录）——直连账本从 IGPU 位翻回 OFF 位；
    省电开关通道（IGPU_RB_OFF）对这本没参与的账不保证有效。若此前补过账
    （省电账本 RB_ON），翻回后补发 IGPU_RB_OFF 把它归位，UI 三态全正常。
    TOGGLE 族属 MUX 命令，独显回线可能重启才兑现——如实验证、不猜。
    """
    info, rc = _direct_capability_gate("DGPU_DIRECT_CONNECT_TOGGLE_OFF")
    if info is None:
        return rc

    print("\n--- 发送切换命令（TOGGLE_OFF，与控制中心点混合模式同路线）---")
    status, events = gcu_mqtt_command(GCU_DIRECT_OFF_CMD, listen_secs=2.0)
    if status != "ok":
        print(f"X GCU 控制通道不可用：{status}")
        print("  请确认机械革命控制中心后台（GCUBridge / GCUService）在运行后重试。")
        return 1
    print("  ✓ 命令已送达（DGPU_DIRECT_CONNECT_TOGGLE_OFF）")
    print_gcu_events(events)

    print("\n--- 等待 GCU 确认（GETSTATUS 复查直连账本翻转，7×1.8s）---")
    ok, cur = wait_direct_status("TOGGLE_OFF")
    if not ok:
        print(f"\nX GCU 未确认指令（当前直连账本: {cur or '无回报'}）。")
        print("  仍在账外省电态；可重试 --hybrid，或 --status 查看当前账本。")
        return 1
    print(f"  ✓ GCU 已确认（直连账本 = {cur}）。")

    # 补过账的（省电账本 RB_ON）：顺手归位，官方 UI 三态全清
    if resolve_igpu_status(st.gcu_igpu_status) == 0:
        print("\n--- 省电账本归位（此前补过账，补发 IGPU_RB_OFF）---")
        status2, ev2 = gcu_mqtt_command(GCU_ECO_OFF_CMD, listen_secs=4.0,
                                        stop_keys=IGPU_STATUS_KEYS)
        if status2 == "ok":
            print("  ✓ 归位命令已送达（IGPU_ONLY_CONNECT_RB_OFF）")
        else:
            print(f"  ! 归位命令发送失败（{status2}）——不影响回混合，账本可稍后对齐。")

    print("\n--- 验证独显回线（PnP/DXGI，上限 60 秒；MUX 族命令可能重启才兑现）---")
    res = wait_for_mode("hybrid", timeout_sec=60.0)
    if res.mode == "hybrid":
        print("\n✓ 已切回混合模式（独显在线，两本账归位）：")
        print_mode_state(res)
        return 0
    print("\nX 60 秒内独显未回线——TOGGLE_OFF 可能需重启兑现。当前状态：")
    print_mode_state(res)
    print("处于待重启态：重启后落混合模式（--status 显示待重启态）。")
    return 1


def cmd_eco_off(adapters_meta: list[Adapter]) -> int:
    """切回混合模式：确认省电态 → 发送 GCU 同款 MQTT 命令 → 凭证验证。

    不杀进程、不动 PnP，无需提权。
    """
    st = read_display_mode_state()
    enrich_with_gcu_status(st)  # 账外态判定需要直连账本（GCU 意图回报）
    print("=== 显示模式切换：核显省电 → 混合模式 ===")
    print_mode_state(st)
    if st.mode == "hybrid":
        print("\n已在混合模式（独显在线），无需切换。")
        return 0
    if st.mode != "igpu_only":
        print(f"\n当前模式不是核显省电态（{MODE_LABELS.get(st.mode, st.mode)}），"
              "本命令只负责省电态 → 混合。")
        return 1
    # 账外省电态（直连直达落下的）：官方同款 TOGGLE_OFF 回程，不走省电开关通道
    if _is_toggle_igpu_ledger(st):
        print("\n检测到账外省电态（直连直达落下，官方控制台三态不识别的态）。")
        return _eco_off_from_toggle_igpu(st)

    # 能力前置（通用化改造方案 §三 E「与 eco 的关系」）：行为不变，仅头部插入
    info = probe_gcu()
    if info.igpu_only is False:
        print(f"\nX 本机不支持核显省电（能力位来源: {info.igpu_source}）。")
        return 1
    if "IGPU_ONLY_CONNECT_RB_OFF" not in info.vocab:
        print(f"\nX rejected: action not in dGPU generation {info.generation} vocabulary")
        print(f"  本机词表不含 IGPU_ONLY_CONNECT_RB_OFF"
              f"（{GEN_LABELS.get(info.generation, info.generation)}）。")
        return 1

    print("\n--- 发送切换命令（GCU 控制通道，与控制中心点击同路线）---")
    status, events = gcu_mqtt_command(
        GCU_ECO_OFF_CMD, listen_secs=8.0,
        stop_keys=("IGpuOnlyConnectionSwitch_Status", "CheckDGpuStatusforIGpuOnlyOnSuccess"))
    if status != "ok":
        print(f"X GCU 控制通道不可用：{status}")
        print("  请确认机械革命控制中心后台（GCUBridge / GCUService）在运行后重试。")
        return 1
    print("  ✓ 命令已送达（IGPU_ONLY_CONNECT_RB_OFF）")
    print_gcu_events(events)
    print("\n--- 验证切换（轮询 PnP problem code / DXGI / IGPUonly，上限 100 秒）---")
    res = wait_for_mode("hybrid", timeout_sec=100.0)
    if res.mode == "hybrid":
        print("\n✓ 已切回混合模式（独显重新在线）：")
        print_mode_state(res)
        return 0
    print("\nX 100 秒内独显未重新上线。当前状态：")
    print_mode_state(res)
    st2, ev2 = gcu_mqtt_command('{"Action": "GETSTATUS"}', listen_secs=4.0)
    if st2 == "ok":
        print_gcu_events(ev2)
    print("可稍后重试，或用 --status 随时查看当前模式。")
    return 1


# currentSaveingMode 枚举（E6：40 系 GCService5/GPUPORWERSAVING.cs）：
# 0=ENABLE / 1=DISABLE / 2=AUTO；回报在 GPUDevice/Status topic（E9 已订阅）
_LITE_MODE_LABELS = {0: "0=ENABLE（省电开）", 1: "1=DISABLE（省电关）", 2: "2=AUTO（自动）"}


def cmd_eco_lite(on: bool) -> int:
    """官方运行时省电（lite）：GPU_POWERSAVEINGMODE toggle（E4）。

    发令无值字段（toggle 语义，服务端按 currentSaveingMode 翻转）。
    确认三凭证：GPUDevice/Status 回报 currentSaveingMode 翻转（E6：0=ENABLE
    1=DISABLE）＋ 本地 nvidia_pnp_states 出现 disabled（E10，PnP 禁卡副作用）
    ＋ currentHZ 回报变化（240⇄60 联动）。回报事件由 gcu_mqtt_command 现有
    订阅收（E9，订阅列表已含 GPUDevice/Status）。
    on=False 即再发一次 toggle（对称恢复，E7 官方成对语义）。
    """
    target = 0 if on else 1
    print(f"=== 官方运行时省电（lite）：GPU_POWERSAVEINGMODE toggle → {'开省电' if on else '关省电'} ===")
    print("运行时省电 = 断显示输出 + 降刷新率 + 设备软禁用，非整卡断电"
          "（与三模机型的 EC 级断电不是一个体验）。")
    print("占用进程不影响 PnP 软禁用，无需清场；被禁前仍占用的软件会在启用后恢复占用。")

    # 发令 → 等 currentSaveingMode 翻转；超时 3 轮重发（对齐官方 UI 盲重发习惯）
    cur: int | None = None
    for attempt in range(1, 4):
        status, events = gcu_mqtt_command(GCU_ECO_LITE_CMD, listen_secs=8.0,
                                          stop_keys=("currentSaveingMode",))
        if status != "ok":
            print(f"X GCU 控制通道不可用：{status}")
            print("  请确认机械革命控制中心后台（GCUBridge / GCUService）在运行后重试。")
            return 1
        print(f"  ✓ 命令已送达（GPU_POWERSAVEINGMODE，第 {attempt} 次）")
        print_gcu_events(events)
        v = first_field(merge_gcu_events(events), "currentSaveingMode")
        if v is not None:
            try:
                cur = int(str(v).strip())
            except ValueError:
                cur = None
        if cur == target:
            break
        if attempt < 3:
            print(f"  ! currentSaveingMode 未翻到目标值"
                  f"（当前: {_LITE_MODE_LABELS.get(cur, cur) if cur is not None else '无回报'}），重发一次…")
            time.sleep(2.0)

    if cur == target:
        print(f"\n  ✓ GCU 账本确认：currentSaveingMode = {_LITE_MODE_LABELS[target]}")
    elif cur is not None:
        print(f"\nX 三次命令后 currentSaveingMode = {_LITE_MODE_LABELS.get(cur, cur)}"
            f"（目标 {_LITE_MODE_LABELS[target]}），账本未达预期。")
        print("  toggle 语义按当前值翻转；若当前值在意料外，先到控制中心 UI 核对省电模式设置。")
        return 1
    else:
        print("\nX 未收到 currentSaveingMode 回报（GPUDevice/Status 在收听窗口内无数据）。")
        print("  命令已送达但状态不可证；用控制中心 UI 或 --status 人工核对。")
        return 1

    # 本地凭证轮询：on 后 PnP 软禁用（mode=pnp_disabled）、off 后回在线（hybrid）。
    # 与回报双确认；只报事实不夸大——PnP 禁用 ≠ EC 断电。
    target_mode = "pnp_disabled" if on else "hybrid"
    print(f"\n--- 验证切换（轮询 PnP / DXGI 本地凭证，上限 60 秒）---")
    res = wait_for_mode(target_mode, timeout_sec=60.0)
    if res.mode == target_mode:
        print(f"\n✓ 运行时省电（lite）已{'开启' if on else '关闭、恢复混合'}："
              "GCU 账本回报 + 本地 PnP 双凭证确认。")
        print_mode_state(res)
        return 0
    print(f"\nX 60 秒内本地凭证未达预期（账本回报 currentSaveingMode = "
          f"{_LITE_MODE_LABELS[target]}）。当前状态：")
    print_mode_state(res)
    if on:
        print("PnP 软禁用可能滞后于账本回报（或该机型 lite 不做设备禁用，仅断输出）；")
        print("以控制中心 UI 显示与 --status 实况为准。")
    else:
        print("设备重新上线可能滞后；稍后用 --status 复查。")
    return 1


# ---------------------------------------------------------------------------
# 独显直连切换编排（--direct，方案 §三 E）：MUX 切换重启生效 + 待重启态
# ---------------------------------------------------------------------------

def wait_direct_status(token: str, rounds: int = 7,
                       round_secs: float = 1.8) -> tuple[bool, str]:
    """GETSTATUS 复查直连状态字段翻转（7×1.8s 超时上限与 L-Mechrevo 同款）。

    token 为期望出现的状态子串（TOGGLE_ON / TOGGLE_OFF）；
    stop_keys 节流与 eco 同款——状态字段一到就返回，不等满 listen 窗口。
    """
    cur = ""
    for i in range(rounds):
        status, events = gcu_mqtt_command(GCU_GETSTATUS_CMD, listen_secs=round_secs,
                                          stop_keys=DIRECT_STATUS_KEYS)
        if status != "ok":
            return False, f"GCU 控制通道不可用：{status}"
        cur = str(first_field(merge_gcu_events(events), *DIRECT_STATUS_KEYS) or "")
        if token in cur.upper():
            return True, cur
        print(f"  … 等待 GCU 确认（第 {i + 1}/{rounds} 轮，当前直连状态: {cur or '无回报'}）")
    return False, cur


def wait_igpu_status(token: str, rounds: int = 5,
                     round_secs: float = 1.8) -> tuple[bool, str]:
    """GETSTATUS 复查省电账本字段翻转（与 wait_direct_status 同构，轮数减配）。"""
    cur = ""
    for i in range(rounds):
        status, events = gcu_mqtt_command(GCU_GETSTATUS_CMD, listen_secs=round_secs,
                                          stop_keys=IGPU_STATUS_KEYS)
        if status != "ok":
            return False, f"GCU 控制通道不可用：{status}"
        cur = str(first_field(merge_gcu_events(events), *IGPU_STATUS_KEYS) or "")
        if token in cur.upper():
            return True, cur
        print(f"  … 等待账本翻转（第 {i + 1}/{rounds} 轮，当前省电账本: {cur or '无回报'}）")
    return False, cur


# --- RunOnce 补账登记（账外省电态对账，见 cmd_eco_reconcile） ---
RUNONCE_KEY = (r"Software\Microsoft\Windows\CurrentVersion\RunOnce")
RECONCILE_RUNONCE_NAME = "GpuModeSwitcherEcoReconcile"


def _reconcile_command_line() -> str:
    """RunOnce 里要执行的一条命令（exe 用自身；源码用 pythonw 静默跑脚本）。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --eco reconcile'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    runner = pyw if os.path.exists(pyw) else sys.executable
    return f'"{runner}" "{os.path.abspath(__file__)}" --eco reconcile'


def runonce_set_reconcile() -> bool:
    """登记 RunOnce：重启登录后自动跑一次补账（Windows 执行后自删条目）。"""
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUNONCE_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RECONCILE_RUNONCE_NAME, 0, winreg.REG_SZ,
                              _reconcile_command_line())
        return True
    except OSError as e:
        print(f"! RunOnce 登记失败（{e}）：重启后请手动跑 --eco reconcile 对账。")
        return False


def runonce_clear_reconcile() -> None:
    """取消切换时清掉补账登记（重启后不再自动对账）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUNONCE_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RECONCILE_RUNONCE_NAME)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"! RunOnce 清理失败（{e}）——重启后补账仍会自动跑一次，无妨（幂等）。")


def restart_countdown(effect: str, seconds: int = 5) -> bool:
    """RESTART 发送前的 5 秒可取消倒计时二次确认（CLI Ctrl+C 可中断）。

    effect 描述重启后的切换效果（「仍会切换为独显直连」/「仍会回到混合模式」）。
    """
    print(f"  {seconds} 秒后发送重启命令（Ctrl+C 取消）…")
    try:
        for i in range(seconds, 0, -1):
            print(f"  {i} …", flush=True)
            time.sleep(1.0)
        return True
    except KeyboardInterrupt:
        print(f"  已取消发送重启命令（保持待重启态，重启后{effect}）。")
        return False


def direct_restart_choice() -> str:
    """重启三选（方案 §三 E 步骤 5）：

    [1] 立即重启 = 发 DGPU_DIRECT_CONNECT_RESTART，GCU 自己执行 shutdown /r /t 0
        （发之前 5 秒可取消倒计时二次确认）
    [2] 稍后自行重启 = 保持待重启态，--status 显示 direct_pending
    [回车] 取消切换 = 发反向 TOGGLE 撤回意图（控制中心「再点一下开关」同款），
        GCU 确认翻回后恢复原模式，无需重启——取消必须真能取消动作
    """
    return ask("[1] 立即重启（5 秒可取消倒计时后由 GCU 执行重启）"
               "  [2] 稍后自行重启（保持待重启态）"
               "  [回车/其他] 取消切换（撤回指令恢复原模式，无需重启）")


def _direct_cancel_revert(revert_cmd: str, action_name: str, token: str,
                          back_desc: str, retry_hint: str) -> int:
    """三选「取消切换」= 撤回指令：发反向 TOGGLE 并等 GCU 确认翻回。

    与控制中心「再点一下开关」同款；撤回后意图与运行态一致，无需重启。
    撤回失败如实报错并停在待重启态（可按 retry_hint 重试撤回）。
    """
    print("\n--- 取消切换（发送撤回指令，与控制中心再点一下开关同款）---")
    status, events = gcu_mqtt_command(revert_cmd, listen_secs=2.0)
    if status != "ok":
        print(f"X 撤回指令发送失败：{status}——切换意图仍在，重启后仍会生效。")
        print(f"  {retry_hint}，或 --status 查看当前意图。")
        return 1
    print(f"  ✓ 撤回指令已送达（{action_name}）")
    print_gcu_events(events)

    print("\n--- 等待 GCU 确认撤回（GETSTATUS 复查状态翻回，7×1.8s）---")
    ok, cur = wait_direct_status(token)
    if not ok:
        print(f"X GCU 未确认撤回（当前直连状态: {cur or '无回报'}）——切换意图仍在，"
              "重启后仍会生效。")
        print(f"  {retry_hint}。")
        return 1
    print(f"  ✓ 切换已撤回（直连状态 = {cur}）——{back_desc}，无需重启。")
    return 0


def _direct_capability_gate(required_action: str) -> tuple[GcuInfo | None, int]:
    """直连编排的能力/词表前置（cmd_direct_on/off 共用）。

    返回 (GcuInfo, 0) 可继续；(None, 退出码) 已打印原因应退出。
    """
    info = probe_gcu()
    if info.dgpu_direct is False:
        print(f"\nX 本机不支持独显直连（能力位来源: {info.dgpu_source}）。")
        print("  宝龙达 Uniwill 模具（蛟龙 30 系部分/WUJIE14XA/星耀/UMI）不在支持范围。")
        return None, 1
    if required_action not in info.vocab or \
            "DGPU_DIRECT_CONNECT_RESTART" not in info.vocab:
        missing = required_action if required_action not in info.vocab \
            else "DGPU_DIRECT_CONNECT_RESTART"
        print(f"\nX rejected: action not in dGPU generation {info.generation} vocabulary")
        print(f"  本机词表不含 {missing}"
              f"（{GEN_LABELS.get(info.generation, info.generation)}）——"
              "30 系等无机种不发空命令。")
        return None, 1
    if info.dgpu_direct is None:
        print("\n! 独显直连能力探测不可得（GCU 离线且注册表无能力位），继续执行但不保证 GCU 接受。")
    return info, 0


def cmd_direct_on() -> int:
    """混合 → 独显直连（MUX 切换，重启生效；方案 §三 E cmd_direct_on）。

    省电态下自动串联：先 IGPU_ONLY_CONNECT_RB_OFF（复用 cmd_eco_off 发令与
    验证段）等独显回线，再继续直连——一次命令完成。
    """
    print("=== 独显直连切换编排（探测 → 发令 → GCU 确认 → 重启三选）===")
    info, rc = _direct_capability_gate("DGPU_DIRECT_CONNECT_TOGGLE_ON")
    if info is None:
        return rc

    st = read_display_mode_state()
    enrich_with_gcu_status(st)  # 凭证 B：直连待重启态判定需要 GCU 意图
    print_mode_state(st)
    if st.mode == "dgpu_direct":
        print("\n已在独显直连态（内屏由独显驱动），无需切换。")
        return 0
    if st.mode == "direct_pending" and st.gcu_intent == 2:
        print("\n直连指令已生效待重启：重启后即切换为独显直连"
              "（--status / GUI 徽章显示待重启态）。")
        return 0
    if st.mode in ("pnp_disabled", "unknown"):
        print(f"\n当前显示模式不干净（{MODE_LABELS.get(st.mode, st.mode)}）。")
        print("  请先恢复到干净的混合模式（--eco off，或设备管理器启用 NVIDIA 设备）再直连。")
        return 1
    if st.mode == "direct_pending":
        print("\n检测到「退出直连」的待重启指令——重发直连命令，回到直连目标。")
    if st.mode == "igpu_only":
        print("\n当前为核显省电态：自动串联「恢复混合」等独显回线，再继续直连…")
        try:
            adapters = enumerate_adapters()
        except OSError:
            adapters = []
        if cmd_eco_off(adapters) != 0:
            print("\nX 独显未回线（恢复混合未完成），中止直连编排。")
            return 1
        print("\n--- 独显已回线，继续直连 ---")

    print("\n--- 发送直连命令（GCU 控制通道，纯 Action 报文与控制中心同路线）---")
    status, events = gcu_mqtt_command(GCU_DIRECT_ON_CMD, listen_secs=2.0)
    if status != "ok":
        print(f"X GCU 控制通道不可用：{status}")
        print("  请确认机械革命控制中心后台（GCUBridge / GCUService）在运行后重试。")
        return 1
    print("  ✓ 命令已送达（DGPU_DIRECT_CONNECT_TOGGLE_ON）")
    print_gcu_events(events)

    print("\n--- 等待 GCU 确认（GETSTATUS 复查直连状态翻转，7×1.8s）---")
    ok, cur = wait_direct_status("TOGGLE_ON")
    if not ok:
        print(f"\nX GCU 未确认直连指令（当前直连状态: {cur or '无回报'}）。")
        print_gcu_events(events)
        res = read_display_mode_state()
        enrich_with_gcu_status(res)
        print_mode_state(res)
        print("建议：重启是 MUX pending 卡住的实测恢复手段"
              "（L-Mechrevo 同款实录）；「GPU output route unknown」场景同理。")
        return 1
    # 免三选：确认翻转后意图与运行态已一致（如重发直连撤掉「退出直连」待重启
    # 指令后实况本就是直连），重启无增益，直接收尾。意图直接复用刚确认的 cur
    res = read_display_mode_state()
    res.gcu_direct_status = cur
    if res.mode == "dgpu_direct":
        print(f"\n✓ GCU 已确认（直连状态 = {cur}）——意图与运行态已一致"
              "（内屏已由独显驱动），无需重启。")
        return 0
    print(f"  ✓ GCU 已确认（直连状态 = {cur}）——处于待重启态，重启后内屏改由独显驱动。")

    choice = direct_restart_choice()
    if choice == "1":
        if not restart_countdown("仍会切换为独显直连"):
            return 0  # 倒计时取消：命令已发，如实停在待重启态
        print("\n--- 发送重启命令（GCU 收到后自行执行 shutdown /r /t 0）---")
        rst, _ev = gcu_mqtt_command(GCU_DIRECT_RESTART_CMD, listen_secs=2.0)
        if rst != "ok":
            print(f"X 重启命令发送失败：{rst}")
            print("  机器未重启；已处于待重启态，可稍后手动重启或重跑 --direct on。")
            return 1
        print("  ✓ 重启命令已发出，机器即将重启。")
        return 0
    if choice == "2":
        print("  保持待重启态：稍后自行重启即完成直连（--status 显示待重启态）。")
        return 0
    return _direct_cancel_revert(
        GCU_DIRECT_OFF_CMD, "DGPU_DIRECT_CONNECT_TOGGLE_OFF", "TOGGLE_OFF",
        "保持混合模式", "可重跑 --direct off 撤回")


def cmd_direct_off() -> int:
    """直连 → 混合（MUX 切换，重启生效；方案 §三 E cmd_direct_off）。

    同构：TOGGLE_OFF → 确认翻转 → 重启三选。
    省去 300ms 补发序列（那是 GCU 侧收到的主状态机行为，客户端单命令即可；
    若真机嗅探发现需要补发再对齐——方案 §四 验证项 R2）。
    """
    print("=== 直连回混合切换编排（探测 → 发令 → GCU 确认 → 重启三选）===")
    info, rc = _direct_capability_gate("DGPU_DIRECT_CONNECT_TOGGLE_OFF")
    if info is None:
        return rc

    st = read_display_mode_state()
    enrich_with_gcu_status(st)
    print_mode_state(st)
    if st.mode == "hybrid":
        print("\n已在混合模式（独显在线、内屏由核显驱动），无需切换。")
        return 0
    if st.mode == "direct_pending" and st.gcu_intent == 1:
        print("\n「直连回混合」指令已生效待重启：重启后即回到混合模式。")
        return 0
    if st.mode not in ("dgpu_direct", "direct_pending"):
        print(f"\n当前不在直连态或直连待重启态（{MODE_LABELS.get(st.mode, st.mode)}），"
              "本命令只负责直连 → 混合。")
        return 1

    print("\n--- 发送切换命令（GCU 控制通道，纯 Action 报文与控制中心同路线）---")
    status, events = gcu_mqtt_command(GCU_DIRECT_OFF_CMD, listen_secs=2.0)
    if status != "ok":
        print(f"X GCU 控制通道不可用：{status}")
        print("  请确认机械革命控制中心后台（GCUBridge / GCUService）在运行后重试。")
        return 1
    print("  ✓ 命令已送达（DGPU_DIRECT_CONNECT_TOGGLE_OFF）")
    print_gcu_events(events)

    print("\n--- 等待 GCU 确认（GETSTATUS 复查直连状态翻转，7×1.8s）---")
    ok, cur = wait_direct_status("TOGGLE_OFF")
    if not ok:
        print(f"\nX GCU 未确认切换指令（当前直连状态: {cur or '无回报'}）。")
        res = read_display_mode_state()
        enrich_with_gcu_status(res)
        print_mode_state(res)
        print("建议：重启是 MUX pending 卡住的实测恢复手段（L-Mechrevo 同款实录）。")
        return 1
    # 免三选：确认翻转后意图与运行态已一致（如待重启态撤回直连指令后实况
    # 本就是混合），重启无增益，直接收尾。意图直接复用刚确认的 cur
    res = read_display_mode_state()
    res.gcu_direct_status = cur
    if res.mode == "hybrid":
        print(f"\n✓ GCU 已确认（直连状态 = {cur}）——意图与运行态已一致"
              "（混合模式，独显在线），无需重启。")
        return 0
    print(f"  ✓ GCU 已确认（直连状态 = {cur}）——处于待重启态，重启后回到混合模式。")

    choice = direct_restart_choice()
    if choice == "1":
        if not restart_countdown("仍会回到混合模式"):
            return 0
        print("\n--- 发送重启命令（GCU 收到后自行执行 shutdown /r /t 0）---")
        rst, _ev = gcu_mqtt_command(GCU_DIRECT_RESTART_CMD, listen_secs=2.0)
        if rst != "ok":
            print(f"X 重启命令发送失败：{rst}")
            print("  机器未重启；已处于待重启态，可稍后手动重启或重跑 --direct off。")
            return 1
        print("  ✓ 重启命令已发出，机器即将重启。")
        return 0
    if choice == "2":
        print("  保持待重启态：稍后自行重启即回到混合模式（--status 显示待重启态）。")
        return 0
    return _direct_cancel_revert(
        GCU_DIRECT_ON_CMD, "DGPU_DIRECT_CONNECT_TOGGLE_ON", "TOGGLE_ON",
        "保持独显直连", "可重跑 --direct on 撤回")


def cmd_hybrid() -> int:
    """统一「回混合」入口（--hybrid / GUI「混合模式」按钮）：按当前态自动路由。

    目标态式入口，与控制中心「选目标、路径自挑」同思路——两条回混合的路
    协议不同（省电态走 MQTT 让独显回线、无需重启；直连态走 MUX 切换、重启
    生效），但用户只关心目的地：
    hybrid → 无事可做；igpu_only → 复用 cmd_eco_off；
    dgpu_direct / direct_pending → 复用 cmd_direct_off；
    pnp_disabled / unknown → 拒绝（先恢复干净混合态再操作）。
    """
    st = read_display_mode_state()
    enrich_with_gcu_status(st)
    if st.mode == "igpu_only":
        print("=== 回混合模式：核显省电 → 混合（MQTT，独显回线，无需重启）===")
        try:
            adapters = enumerate_adapters()
        except OSError:
            adapters = []
        return cmd_eco_off(adapters)
    if st.mode in ("dgpu_direct", "direct_pending"):
        print("=== 回混合模式：直连/待重启 → 混合（MUX 切换，重启生效）===")
        return cmd_direct_off()
    print("=== 回混合模式 ===")
    print_mode_state(st)
    if st.mode == "hybrid":
        print("\n已在混合模式（独显在线、内屏由核显驱动），无需切换。")
        return 0
    print(f"\n当前显示模式不干净（{MODE_LABELS.get(st.mode, st.mode)}），无法自动路由。")
    print("  请先恢复到干净的混合模式（设备管理器启用 NVIDIA 设备）后重试。")
    return 1


def main() -> int:
    # pythonw / 无控制台运行（run_eco_yes.ps1 提权无人值守）：stdio 为 None，
    # print 直接炸——兜底为丢弃流；编排输出本就由 _Tee 双写 logs/ 留档
    for _name in ("stdout", "stderr"):
        if getattr(sys, _name, None) is None:
            setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
    ap = argparse.ArgumentParser(
        description="识别哪个进程在占用独立显卡（WDDM 性能计数器，任务管理器同款数据）")
    ap.add_argument("--list-gpus", action="store_true", help="列出所有显卡适配器后退出")
    ap.add_argument("--all", action="store_true", help="非独显适配器（核显等）也一并报告")
    ap.add_argument("--json", action="store_true", help="JSON 输出（一次性快照）")
    ap.add_argument("--watch", action="store_true", help="持续监视独显持有变化")
    ap.add_argument("--eco", nargs="*", choices=["on", "off", "lite", "reconcile"],
                    metavar="{on,off,lite,'lite off',reconcile}",
                    help="核显省电切换编排（全程自动，GCU 控制通道与控制中心同路线）："
                         "on = 检测独显占用并清场后发官方同款命令开启核显省电并验证（默认）；"
                         "off = 发命令切回混合模式并验证；"
                         "lite = 官方运行时省电（30/40 常规机型：断输出+降刷新率+设备软禁用，"
                         "非整卡断电）；lite off = 关闭运行时省电恢复混合；"
                         "无 MUX 省电档的机型 --eco 自动改走 lite；"
                         "reconcile = 账外省电态补账（直连直达落下后对齐官方控制台识别，"
                         "幂等，RunOnce 无人值守自动跑）")
    ap.add_argument("--direct", nargs="?", const="on", choices=["on", "off"],
                    metavar="{on,off}",
                    help="独显直连切换（MUX 切换，重启后生效）："
                         "on = 发直连命令并确认，重启三选（默认；核显省电态自动先串联恢复混合）；"
                         "off = 直连回混合，同构编排")
    ap.add_argument("--hybrid", action="store_true",
                    help="切回混合模式（目标态入口，按当前态自动路由）："
                         "核显省电态走 MQTT 恢复混合（无需重启）；直连/待重启态走 MUX 切换"
                         "（重启三选）；已混合则无事可做")
    ap.add_argument("--gcu-info", action="store_true",
                    help="打印 GCU 探测结果（代际/词表/能力位/意图回报，诊断入口）")
    ap.add_argument("--status", action="store_true",
                    help="报告当前显示模式与凭证（内屏路由 / PnP problem code / DXGI / "
                         "GCU 注册表与意图回报）")
    ap.add_argument("--interval", type=float, default=1.0, help="watch 间隔秒数（默认 1）")
    ap.add_argument("--yes", action="store_true",
                    help="eco on 清场确认自动应答 1（无人值守；会直接强杀候选进程）")
    ap.add_argument("--keep", metavar="NAMES",
                    help="eco on 清场豁免名单（逗号分隔进程名，不区分大小写）：其余候选照杀、"
                         "豁免者保留在场，用于实测豁免对象是否挡切换判据（无人值守）")
    args = ap.parse_args()
    # --eco 值归一化：nargs="*" 支持 "lite off" 双词形式，其余收敛回单值字符串
    if args.eco is not None:
        if not args.eco:
            args.eco = "on"                       # 裸 --eco 等同 --eco on
        elif args.eco == ["lite", "off"]:
            args.eco = "lite-off"
        elif len(args.eco) == 1:
            args.eco = args.eco[0]
        else:
            ap.error("参数 --eco 的形式为 on/off/lite/'lite off'/reconcile")

    if args.gcu_info:
        print_gcu_info(probe_gcu())
        return 0

    try:
        adapters_meta = enumerate_adapters()
    except OSError as e:
        print(f"DXGI 枚举失败: {e}", file=sys.stderr)
        return 1

    if args.list_gpus:
        print_list_gpus(adapters_meta)
        return 0

    if args.status:
        st = read_display_mode_state()
        if enrich_with_gcu_status(st) != "ok":
            print("（GCU 意图回报不可得——离线或通道被占；以下仅本地凭证判定）")
        print_mode_state(st)
        return 0

    if args.hybrid:
        log_path = eco_log_path("hybrid")
        log = _Tee(log_path)
        sys.stdout = log  # 双写：终端 + 日志
        try:
            print(f"（本次运行日志: {log_path}）")
            return cmd_hybrid()
        finally:
            sys.stdout = log._out
            log.close()
            # 跑完停窗：提权重启的新窗口在进程退出后即关闭，输出会丢；
            # 管道/重定向（无人值守）不停
            if sys.stdin and sys.stdin.isatty():
                try:
                    input("\n按回车关闭窗口…")
                except EOFError:
                    pass

    if args.direct == "off" or args.direct == "on":
        log_path = eco_log_path("direct")
        log = _Tee(log_path)
        sys.stdout = log  # 双写：终端 + 日志
        try:
            print(f"（本次运行日志: {log_path}）")
            return cmd_direct_off() if args.direct == "off" else cmd_direct_on()
        finally:
            sys.stdout = log._out
            log.close()
            # 跑完停窗：提权重启的新窗口在进程退出后即关闭，输出会丢；
            # 管道/重定向（无人值守）不停
            if sys.stdin and sys.stdin.isatty():
                try:
                    input("\n按回车关闭窗口…")
                except EOFError:
                    pass

    if args.eco == "reconcile":
        log_path = eco_log_path("eco_reconcile")
        log = _Tee(log_path)
        sys.stdout = log  # 双写：终端（有则）+ 日志；RunOnce 无人值守靠日志留档
        try:
            print(f"（本次运行日志: {log_path}）")
            return cmd_eco_reconcile()
        finally:
            sys.stdout = log._out
            log.close()

    if args.eco == "off":
        return cmd_eco_off(adapters_meta)

    if args.eco in ("lite", "lite-off"):
        return cmd_eco_lite(on=(args.eco == "lite"))

    if args.eco == "on":
        try:
            counters = GpuCounters()
        except OSError as e:
            print(f"PDH 计数器打开失败: {e}", file=sys.stderr)
            return 1
        log_path = eco_log_path()
        log = _Tee(log_path)
        sys.stdout = log  # 双写：终端 + 日志
        try:
            print(f"（本次运行日志: {log_path}）")
            keep = {s.strip() for s in args.keep.split(",") if s.strip()} if args.keep else set()
            return cmd_eco_on(counters, adapters_meta, auto_yes=args.yes, keep_names=keep)
        finally:
            counters.close()
            sys.stdout = log._out
            log.close()
            # 跑完停窗：提权重启的新窗口在进程退出后即关闭，输出会丢；
            # 管道/重定向（无人值守）不停。
            if sys.stdin and sys.stdin.isatty() and not (args.yes or args.keep):
                try:
                    input("\n按回车关闭窗口…")
                except EOFError:
                    pass

    try:
        counters = GpuCounters()
    except OSError as e:
        print(f"PDH 计数器打开失败: {e}", file=sys.stderr)
        return 1

    try:
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
