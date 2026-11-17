# 提权跑 exe 冒烟测试（exe 带 uac-admin 清单，测试进程同权限才不被 UIPI 挡）
# 结果落 test_artifacts\exe_smoke_result.txt
#
# pythonw（windowed 子系统）：曾用 python + Start-Process——console 子系统
# 每次都弹出一个可见的提权控制台打断真机正在做的事（2026-11-20 咩咩拍桌）；
# pythonw 无控制台，结果全走文件回传（提权进程 stdout 本就不可见）
$ErrorActionPreference = "Stop"
Start-Process -Verb RunAs -Wait -FilePath "pythonw" -ArgumentList "`"$PSScriptRoot\exe_smoke.py`""
