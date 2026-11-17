# build_core.ps1 —— 用 w64devkit 的 gcc 编译 pcepsg.dll
# 用法：pwsh -File build_core.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$src  = Join-Path $root "core"
$out  = Join-Path $root "pcepsg.dll"

$gcc = (Get-Command gcc -ErrorAction SilentlyContinue).Source
if (-not $gcc) { throw "找不到 gcc（需要 w64devkit 或 MinGW-w64）" }

& $gcc -O2 -std=c11 -ffast-math -shared -static-libgcc `
    -o $out (Join-Path $src "pcepsg.c") -lm

if ($LASTEXITCODE -ne 0) { throw "gcc 失败：$LASTEXITCODE" }
$len = (Get-Item $out).Length
"OK -> $out ($([math]::Round($len/1KB,1)) KB)"
