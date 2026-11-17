# -*- mode: python ; coding: utf-8 -*-
# 独显占用侦探单文件打包：dgpu_detective.exe（onefile + windowed + uac_admin）
# 双击即 UAC 提权运行，界面为 dgpu_gui.py（CustomTkinter 深色）；构建入口：pwsh -NoProfile -File build_exe.ps1

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# CTk 的主题 JSON/字体是运行时数据文件，不进包 = 启动即崩；子模块全收防动态 import 漏
ctk_datas = collect_data_files("customtkinter")
ctk_hidden = collect_submodules("customtkinter")

a = Analysis(
    ['dgpu_gui.py'],
    pathex=[],
    binaries=[],
    datas=ctk_datas,
    hiddenimports=ctk_hidden,
    # PIL 是 CTk 的可选依赖（仅 CTkImage 图片路径用到，try/except 包着）；
    # 本 GUI 全文字零图片，排除省 ~17MB（29.0 → 12.1 MB 实测）
    excludes=['PIL'],
    hookspath=[],
    runtime_hooks=[],
    noarchive=False,
)

# 这些属于操作系统/显卡驱动（ctypes 运行时动态加载），不进包——gputaskman 打包经验
driver_libs = ("dxgi.dll", "pdh.dll", "cfgmgr32.dll", "shell32.dll",
               "nvapi64.dll", "nvml.dll", "nvcuda.dll")


def is_driver_lib(name: str) -> bool:
    name = name.lower()
    return any(name == lib or name.startswith(lib + ".") for lib in driver_libs)


a.binaries = [e for e in a.binaries if not is_driver_lib(Path(e[0]).name)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,   # 全部内嵌 = onefile 单文件
    name='dgpu_detective',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed：不闪控制台
    disable_windowed_traceback=False,
    uac_admin=True,         # 双击即请求管理员（杀提权进程/切换省电都需要）
    uac_uiaccess=False,
    version='version.txt',
    icon=None,
)
