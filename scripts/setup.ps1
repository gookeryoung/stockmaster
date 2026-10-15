<#
.SYNOPSIS
    One-time environment setup: backend venv + frontend build.

.EXAMPLE
    .\scripts\setup.ps1
    .\scripts\setup.ps1 -SkipFrontend
#>
[CmdletBinding()]
param(
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "==> 检查 Python" -ForegroundColor Cyan
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Error "未找到 uv。请安装： https://docs.astral.sh/uv/"
}
& uv --version

Write-Host "==> 创建虚拟环境 .venv (Python 3.13)" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) {
    & uv venv .venv --python 3.13
} else {
    Write-Host "    .venv 已存在，跳过"
}

Write-Host "==> 安装后端依赖" -ForegroundColor Cyan
& uv pip install --python ".venv\Scripts\python.exe" -e ".[dev]"

Write-Host "==> 初始化数据库" -ForegroundColor Cyan
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = Join-Path $root "backend"
& ".venv\Scripts\python.exe" -m app.cli init

if (-not $SkipFrontend) {
    Write-Host "==> 安装并构建前端" -ForegroundColor Cyan
    Push-Location frontend
    try {
        if (Get-Command pnpm -ErrorAction SilentlyContinue) {
            & pnpm install
            & pnpm build
        } else {
            Write-Warning "未找到 pnpm；跳过前端构建。可稍后手动执行：cd frontend; pnpm install; pnpm build"
        }
    } finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "设置完成。启动： .\scripts\run.ps1" -ForegroundColor Green
Write-Host "首次使用请先同步数据： .venv\Scripts\python.exe -m app.cli sync-daily" -ForegroundColor Green
