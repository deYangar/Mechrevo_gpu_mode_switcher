# 提权跑 exe 冒烟测试（exe 带 uac-admin 清单，测试进程同权限才不被 UIPI 挡）
# 结果落 test_artifacts\exe_smoke_result.txt
$ErrorActionPreference = "Stop"
Start-Process -Verb RunAs -Wait -FilePath "python" -ArgumentList "`"$PSScriptRoot\exe_smoke.py`""
