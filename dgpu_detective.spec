# -*- mode: python ; coding: utf-8 -*-
# 独显占用侦探单文件打包：dgpu_detective.exe（onefile + windowed + uac_admin）
# 双击即 UAC 提权运行，界面为 dgpu_gui.py；构建入口：pwsh -NoProfile -File build_exe.ps1

from pathlib import Path

a = Analysis(
    ['dgpu_gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
