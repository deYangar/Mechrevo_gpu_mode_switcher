# 提权跑 exe --gcu-info（exe 带 uac_admin 清单，bash/普通进程启动被拒）：
# 提权 cmd /c 内完成 stdout 重定向（提权进程 stdout 对外不可见，须在提权上下文内落文件）；
# 提权进程 cwd=system32，先 cd /d 进项目目录（项目路径无空格，绕开 cmd /c 引号剥离）
$ErrorActionPreference = "Stop"
$proj = Split-Path -Parent $PSScriptRoot
$out = Join-Path $proj "test_artifacts\exe_gcuinfo.txt"
if (Test-Path $out) { Remove-Item $out -Force }
Start-Process -Verb RunAs -Wait -FilePath cmd.exe -ArgumentList `
    "/c cd /d $proj && dist\gpu_mode_switcher.exe --gcu-info > test_artifacts\exe_gcuinfo.txt 2>&1"
Get-Content $out
