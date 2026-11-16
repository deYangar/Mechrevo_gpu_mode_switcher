# 提权无人值守跑 --eco --yes（清场自动应答 1）
# stdin=NUL：跳过结尾「按回车关窗」（否则提权控制台 isatty 挂住等输入）
# stdout=NUL：编排自身 _Tee 双写 logs/eco_*.txt，终端不需要
# 用法: pwsh -NoProfile -File tools/run_eco_yes.ps1
$ErrorActionPreference = "Stop"
$py = "C:\Users\Yang\AppData\Local\Programs\Python\Python312\python.exe"
$root = "C:\Users\Yang\.zcode\workspace\default\projects\dgpu_detective"
$before = @(Get-ChildItem "$root\logs\eco_*.txt" -ErrorAction SilentlyContinue | ForEach-Object Name)
$p = Start-Process cmd.exe -ArgumentList "/c $py `"$root\dgpu_detective.py`" --eco --yes < NUL > NUL 2>&1" `
    -Verb RunAs -PassThru -Wait
Write-Host ("exitcode={0}" -f $p.ExitCode)
Get-ChildItem "$root\logs\eco_*.txt" | Where-Object { $_.Name -notin $before } |
    ForEach-Object { Write-Host ("newlog={0}" -f $_.FullName) }
