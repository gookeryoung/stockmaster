<#
.SYNOPSIS
    Start StockMaster (serves API + built frontend on one port).

.EXAMPLE
    .\scripts\run.ps1
    .\scripts\run.ps1 -Port 9000 -Reload
#>
[CmdletBinding()]
param(
    [string]$Host_ = "127.0.0.1",
    [int]$Port = 8770,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "未找到虚拟环境 $python。请先运行 scripts\setup.ps1"
}

$distIndex = Join-Path $root "frontend\dist\index.html"
if (-not (Test-Path $distIndex)) {
    Write-Warning "前端未构建（frontend\dist 缺失），将以 API-only 模式启动。"
    Write-Warning "构建前端：cd frontend; pnpm install; pnpm build"
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = Join-Path $root "src"

$args = @("-m", "stockmaster.cli", "serve", "--host", $Host_, "--port", "$Port")
if ($Reload) { $args += "--reload" }

Write-Host "启动 StockMaster： http://${Host_}:${Port}" -ForegroundColor Cyan
& $python @args
