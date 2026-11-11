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
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
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
# 显示模式状态检测：PnP problem code 主判据 + DXGI 可见性 + GCU 注册表辅证
# ---------------------------------------------------------------------------
# 2026-11-11 实测定案：官方「核显省电」成功 = EC 级断开（problem=45，设备从 PCI 层
# 消失、DXGI 枚举不到）；而 gpu_switch 的 PnP 软禁用 = problem=22，设备仍焊在总线上，
# 是假切换。三态判据由此而来。

IGPUONLY_REG = (r"SOFTWARE\OEM\GamingCenter2\MySetting", "IGPUonly")

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
    "unknown": "无法判定",
}


def _devnode_state(instance_id: str) -> str:
    """cfgmgr32 判定 devnode 状态。

    NORMAL 定位成功 = 设备在场 → CM_Get_DevNode_Status 读 problem
    （0=online / 22=disabled / 其他原样报）；NORMAL 失败而 PHANTOM 定位成功 =
    幻影设备（不在总线上）= EC 级下线；两者都失败 = absent。
    """
    cfg = ctypes.WinDLL("cfgmgr32")
    cfg.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                       wintypes.LPCWSTR, wintypes.UINT]
    cfg.CM_Get_DevNode_Status.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                          ctypes.POINTER(wintypes.DWORD),
                                          wintypes.DWORD, wintypes.UINT]
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
    """GCU 亲手写的模式意图位（切换成功时 GCU 会更新它）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, IGPUONLY_REG[0], 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            v, _ = winreg.QueryValueEx(k, IGPUONLY_REG[1])
            return int(v)
    except OSError:
        return None


@dataclass
class DisplayModeState:
    problems: list[tuple[str, str]]
    dxgi_names: list[str]
    dxgi_error: str = ""
    igpu_only: int | None = None

    @property
    def mode(self) -> str:
        ps = [p for _, p in self.problems]
        if ps and all(p == "disconnected" for p in ps):
            return "igpu_only"
        if ps and all(p == "disabled" for p in ps):
            return "pnp_disabled"
        if ps and all(p == "online" for p in ps):
            return "hybrid"
        return "unknown"


def read_display_mode_state() -> DisplayModeState:
    dxgi_names: list[str] = []
    dxgi_error = ""
    try:
        dxgi_names = [a.name for a in enumerate_adapters()
                      if a.vendor_id == VENDOR_NVIDIA and not a.software]
    except OSError as e:
        dxgi_error = str(e)
    return DisplayModeState(problems=nvidia_pnp_states(),
                            dxgi_names=dxgi_names, dxgi_error=dxgi_error,
                            igpu_only=_read_igpuonly())


def print_mode_state(st: DisplayModeState) -> None:
    print(f"当前显示模式: {MODE_LABELS[st.mode]}")
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


def popup_confirm(text: str, title: str = "dgpu_detective 显示模式切换") -> bool:
    """置顶选择窗：确定=操作完成，取消=放弃。不依赖终端窗口是否有焦点。"""
    MB_OKCANCEL, MB_ICONINFORMATION = 0x1, 0x40
    MB_TOPMOST, MB_SETFOREGROUND = 0x40000, 0x10000
    r = ctypes.windll.user32.MessageBoxW(
        None, text, title,
        MB_OKCANCEL | MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND)
    return r == 1  # IDOK


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


def _list_pids() -> list[int]:
    """psapi EnumProcesses 全量 pid。"""
    psapi = ctypes.WinDLL("psapi")
    psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    arr = (wintypes.DWORD * 2048)()
    needed = wintypes.DWORD(0)
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return []
    return [arr[i] for i in range(needed.value // ctypes.sizeof(wintypes.DWORD))]


def nv_modules_of(pid: int) -> set[str]:
    """pid 加载的 NVIDIA 用户态接口模块（nvapi/nvml/nvspcaps）；快照打不开返回空集。"""
    k = _load_kernel32()
    for fn in ("Module32FirstW", "Module32NextW"):
        f = getattr(k, fn)
        f.restype = wintypes.BOOL
        f.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
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
    """
    r = subprocess.run(["taskkill", "/F", "/T", "/IM", name],
                       capture_output=True)
    msg = (r.stderr or r.stdout or b"").decode("gbk", "replace").strip().replace("\n", " | ")
    return r.returncode == 0, msg


def relaunch_elevated_eco() -> bool:
    """UAC 提权重启 --eco 自身；强杀以管理员运行的占用软件需要管理员。"""
    params = f'"{os.path.abspath(__file__)}" --eco'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    return rc > 32


class _Tee:
    """stdout 双写：终端 + 日志文件（提权新窗口跑完即关，输出必须留档）。"""

    def __init__(self, path: str):
        self._f = open(path, "w", encoding="utf-8", errors="replace")
        self._out = sys.stdout

    def write(self, s):
        self._out.write(s)
        self._f.write(s)
        return len(s)

    def flush(self):
        self._out.flush()
        self._f.flush()

    def close(self):
        self._f.close()


def eco_log_path() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, time.strftime("eco_%Y%m%d_%H%M%S.txt"))


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


def cmd_eco_on(counters: GpuCounters, adapters_meta: list[Adapter]) -> int:
    """切核显省电（真切换）：检测独显持有 → 清场 → 弹窗引导官方控制台操作 → 凭证验证。"""
    st = read_display_mode_state()
    print("=== 核显省电切换编排（检测 → 清场 → 官方切换 → 凭证验证）===")
    print_mode_state(st)
    if st.mode == "igpu_only":
        print("\n已在核显省电态（PnP 已断开 + DXGI 不可见），无需切换。")
        return 0
    if st.mode == "pnp_disabled":
        print("\n检测到 PnP 软禁用残留（假切换形态）：设备仍焊在总线上。")
        print("请先在设备管理器启用 NVIDIA 设备（或 gpu_mode_switch 项目 gpu_switch.py off）")
        print("恢复混合模式后，再用本命令走官方真切换。")
        return 1
    if st.mode == "unknown":
        print("\n无法判定当前显示模式（PnP/DXGI 凭证异常，如上），不继续。")
        return 1

    if not is_admin():
        print("\n关闭提权软件（HwMonitor/GamePP 等）需要管理员，提权继续…")
        if relaunch_elevated_eco():
            print("已在提权的新窗口继续，本窗口退出。")
            print(f"完整输出与日志见项目 logs/ 目录（{eco_log_path()[: -21]}…）")
            return 0
        print("提权被取消，退出。")
        return 1
    print("（已提权运行）")

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

    # ①b 驱动层口径：NVAPI/NVML 模块持有者。传感器轮询类软件（HWiNFO 等）不创建
    #     WDDM 图形上下文，① 抓不到它们，但 GCU 判据看得到（2026-11-11 实测教训）
    wddm_pids = {r["pid"] for r in candidates} | {r["pid"] for r in skipped}
    drv_holders = [(pid, name, mods) for pid, name, mods in
                   nv_module_holders(PROTECTED_PROCESSES)
                   if pid not in wddm_pids]

    # ② 分类提示 + 选择；关闭 = 按进程名强杀全部同名实例（含子进程树）
    candidates.sort(key=lambda r: (-r["local_mb"], -max(r["engines"].values(), default=0.0)))
    handled_pids: set[int] = set()
    handled_names: set[str] = set()
    if candidates or drv_holders:
        if candidates:
            print_eco_rows(candidates, f"图形上下文持有（{len(candidates)} 个，按显存排序）")
        if drv_holders:
            print(f"\n--- 驱动层持有（{len(drv_holders)} 个，NVAPI/NVML 模块加载者："
                  "不占图形上下文但挡切换判据）---")
            name_w = max(len(n) for _, n, _ in drv_holders)
            for pid, name, mods in drv_holders:
                print(f"  pid {pid:<7} {name:<{name_w}}  [{', '.join(sorted(mods))}]")
        print_eco_rows(skipped, f"系统/服务组件（{len(skipped)} 个，跳过不动：杀了被拉回或系统组件）")
        choice = ask("\n[1] 结束以上候选（含全部同名实例与子进程）并继续"
                     "  [2] 不关闭直接继续  [回车/其他] 取消")
        if choice == "1":
            names = sorted({proc_name(r["image"]) for r in candidates} |
                           {name for _, name, _ in drv_holders})
            print(f"\n--- 强制结束（taskkill /F /T /IM，共 {len(names)} 个进程名）---")
            handled_pids = {r["pid"] for r in candidates} | {p for p, _, _ in drv_holders}
            handled_names = ({proc_name(r["image"]).lower() for r in candidates} |
                             {name.lower() for _, name, _ in drv_holders})
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
            elif proc_name(r["image"]).lower() in handled_names:
                r["_note"] = "被守护/自启拉回（新 pid），建议软件内退出或禁自启"
            remaining.append(r)
    drv_left = nv_module_holders(PROTECTED_PROCESSES)
    drv_notes = []
    for pid, name, mods in drv_left:
        note = "（被守护/自启拉回），建议软件内退出或禁自启" \
            if name.lower() in handled_names else ""
        drv_notes.append(f"  pid {pid:<7} {name}  [{', '.join(sorted(mods))}]{note}")
    if remaining or drv_notes:
        print_eco_rows(remaining, f"图形上下文剩余 {len(remaining)} 个（系统组件不阻碍切换）")
        if drv_notes:
            print(f"驱动层剩余 {len(drv_notes)} 个（挡判据，切换前建议处理）：")
            for line in drv_notes:
                print(line)
        print("（若下面切换失败，优先怀疑又有软件扑上来挡判据）")
    else:
        print("  ✓ 独显已无任何存活持有（图形上下文 + 驱动层双口径）")

    # ④ 弹窗引导官方切换：占用杀绝后 GCU 判据可通过，控制台点击即真切换
    if not popup_confirm(
            "独显占用清场完成。\n\n"
            "请打开「机械革命控制中心」→ 显卡设置，开启「核显省电模式」。\n"
            "等控制台切换动作完成（不再转圈）后，点【确定】验证结果；\n"
            "点【取消】放弃本次切换（已结束的进程不会自动恢复）。",
            "请到控制中心开启核显省电"):
        print("已取消切换（本次已结束的进程不自动恢复）。")
        return 1

    print("\n--- 验证切换（轮询 PnP problem code / DXGI / IGPUonly，上限 3 分钟）---")
    res = wait_for_mode("igpu_only", timeout_sec=180.0)
    if res.mode == "igpu_only":
        print("\n✓ 核显省电切换成功（官方路径，EC 级断开）：")
        print_mode_state(res)
        return 0
    print("\nX 3 分钟内未检测到 EC 级断开，切换未完成。当前状态：")
    print_mode_state(res)
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
    print("可处理后再到控制台重试；或用 --status 随时查看当前模式。")
    return 1


def cmd_eco_off(adapters_meta: list[Adapter]) -> int:
    """切回混合模式：确认省电态 → 弹窗引导官方控制台操作 → 凭证验证。

    不杀进程、不动 PnP，无需提权。
    """
    st = read_display_mode_state()
    print("=== 显示模式切换：核显省电 → 混合模式 ===")
    print_mode_state(st)
    if st.mode == "hybrid":
        print("\n已在混合模式（独显在线），无需切换。")
        return 0
    if st.mode != "igpu_only":
        print(f"\n当前模式不是核显省电态（{MODE_LABELS[st.mode]}），本命令只负责省电态 → 混合。")
        return 1
    if not popup_confirm(
            "请打开「机械革命控制中心」→ 显卡设置，切回「混合模式」。\n\n"
            "等控制台切换动作完成（不再转圈）后，点【确定】验证独显已重新上线；\n"
            "点【取消】放弃。",
            "请到控制中心切回混合模式"):
        print("已取消，未做任何改动。")
        return 1
    print("\n--- 验证切换（轮询 PnP problem code / DXGI / IGPUonly，上限 3 分钟）---")
    res = wait_for_mode("hybrid", timeout_sec=180.0)
    if res.mode == "hybrid":
        print("\n✓ 已切回混合模式（独显重新在线）：")
        print_mode_state(res)
        return 0
    print("\nX 3 分钟内独显未重新上线。当前状态：")
    print_mode_state(res)
    print("可稍后重试，或用 --status 随时查看当前模式。")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="识别哪个进程在占用独立显卡（WDDM 性能计数器，任务管理器同款数据）")
    ap.add_argument("--list-gpus", action="store_true", help="列出所有显卡适配器后退出")
    ap.add_argument("--all", action="store_true", help="非独显适配器（核显等）也一并报告")
    ap.add_argument("--json", action="store_true", help="JSON 输出（一次性快照）")
    ap.add_argument("--watch", action="store_true", help="持续监视独显持有变化")
    ap.add_argument("--eco", nargs="?", const="on", choices=["on", "off"],
                    metavar="{on,off}",
                    help="显示模式切换编排：on = 检测独显占用并清场后，弹窗引导到官方控制中心"
                         "开启核显省电并自动验证（默认）；off = 弹窗引导切回混合模式并验证")
    ap.add_argument("--status", action="store_true",
                    help="报告当前显示模式与凭证（PnP problem code / DXGI / GCU 注册表）")
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

    if args.status:
        print_mode_state(read_display_mode_state())
        return 0

    if args.eco == "off":
        return cmd_eco_off(adapters_meta)

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
            return cmd_eco_on(counters, adapters_meta)
        finally:
            counters.close()
            sys.stdout = log._out
            log.close()
            # 跑完停窗：提权重启的新窗口在进程退出后即关闭，输出会丢；
            # 管道/重定向（无人值守）不停。
            if sys.stdin and sys.stdin.isatty():
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
