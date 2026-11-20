# 机革显卡模式切换妙妙小工具一键打包：dist\gpu_mode_switcher.exe（onefile + windowed + uac_admin）
# 用法：pwsh -NoProfile -File build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# 依赖检查：缺 pyinstaller / customtkinter 就装（清华镜像）
python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "安装 pyinstaller ..."
    python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller 安装失败" }
}

python -c "import customtkinter" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "安装 customtkinter ..."
    python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple customtkinter
    if ($LASTEXITCODE -ne 0) { throw "customtkinter 安装失败" }
}

python -m PyInstaller --noconfirm --clean --log-level WARN dgpu_detective.spec
if ($LASTEXITCODE -ne 0) { throw "打包失败" }

$out = Get-Item dist\gpu_mode_switcher.exe
Write-Host ("构建完成: dist\gpu_mode_switcher.exe  {0:N1} MB" -f ($out.Length / 1MB))
