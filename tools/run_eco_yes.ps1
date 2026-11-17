# 提权无人值守跑 --eco --yes（清场自动应答 1）
# pythonw（windowed 子系统）：曾用 cmd.exe /c python——console 子系统每次
# 弹出可见的提权控制台打断真机正在做的事（2026-11-20 拍桌，与冒烟同根源）；
# pythonw 无控制台，编排输出由引擎 _Tee 双写 logs/eco_*.txt，终端不需要。
# pythonw 下 sys.stdin=None，CLI 尾部「按回车关窗」的 isatty 防护自动跳过
# 用法: pwsh -NoProfile -File tools/run_eco_yes.ps1
$ErrorActionPreference = "Stop"
$pyw = "C:\Users\Yang\AppData\Local\Programs\Python\Python312\pythonw.exe"
$root = "C:\Users\Yang\.zcode\workspace\default\projects\dgpu_detective"
$before = @(Get-ChildItem "$root\logs\eco_*.txt" -ErrorAction SilentlyContinue | ForEach-Object Name)
$p = Start-Process -FilePath $pyw `
    -ArgumentList "`"$root\dgpu_detective.py`" --eco --yes" `
    -Verb RunAs -PassThru -Wait
Write-Host ("exitcode={0}" -f $p.ExitCode)
Get-ChildItem "$root\logs\eco_*.txt" | Where-Object { $_.Name -notin $before } |
    ForEach-Object { Write-Host ("newlog={0}" -f $_.FullName) }
