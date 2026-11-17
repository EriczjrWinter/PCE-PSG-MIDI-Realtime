# build_exe.ps1
# 打包成单文件 EXE。
#
#   .\build_exe.ps1                # 窗口版（默认，无控制台）
#   .\build_exe.ps1 -Console       # 带控制台，排错用
#   .\build_exe.ps1 -Name MyPlayer # 改名字
#
# 只打一个原生件：pcepsg.dll。没有音色库文件 —— 16 组波表音色是代码里的
# 32 步常量（见 pcew.py），所以 EXE 是自包含的。
param(
    [string]$Name = "PCEPSG-RT",
    [switch]$Console
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "PATH 里找不到 python"
}
python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "安装 PyInstaller…" -ForegroundColor Yellow
    python -m pip install -q pyinstaller
}

# 先保证 DLL 是最新的
& (Join-Path $PSScriptRoot "build_core.ps1")

$mode = if ($Console) { "--console" } else { "--windowed" }

$piArgs = @(
    "--noconfirm", "--clean", "--onefile", $mode,
    "--name", $Name,
    # 原生核心：打包后会被解到 _MEIPASS，pcepsg_core.py 用 __file__ 就能找到
    "--add-binary", "pcepsg.dll;.",
    # PortAudio 的 DLL 在 sounddevice 包里，不 collect 会漏
    "--collect-data", "sounddevice",
    "app_pce.py"
)

Write-Host "构建 $Name ($mode)…" -ForegroundColor Cyan
python -m PyInstaller @piArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$exe = Join-Path $PSScriptRoot "dist\$Name.exe"
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "完成: $exe ($mb MB)" -ForegroundColor Green
    Write-Host "自检: & '$exe' --selfcheck   → 结果写进 EXE 同目录 selfcheck.log" -ForegroundColor Green
} else {
    throw "没有生成 EXE"
}
