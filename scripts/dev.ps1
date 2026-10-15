<#
.SYNOPSIS
    Start (or stop) the StockMaster development environment.

.DESCRIPTION
    Start: launches the backend API and the Vite dev server in separate windows, so
    each keeps its own live log and Ctrl+C stops only that process. Vite binds the
    requested port strictly, so a conflict fails loudly instead of silently moving
    to another port (which would make the printed URL wrong).

    Stop: terminates the processes listening on the two configured ports. Use it
    when a previous run's processes are still holding a port.

.EXAMPLE
    .\scripts\dev.ps1
    .\scripts\dev.ps1 -BackendPort 9000 -FrontendPort 5174
    .\scripts\dev.ps1 -Stop
    .\scripts\dev.ps1 -NoFrontend      # backend only
#>
[CmdletBinding()]
param(
    [int]$BackendPort = 8770,
    [int]$FrontendPort = 5173,
    [switch]$NoFrontend,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Get-PortOwner {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return $null
}

function Stop-PortOwner {
    param([int]$Port, [string]$Label)
    $owner = Get-PortOwner -Port $Port
    if (-not $owner) {
        Write-Host "  $Label (端口 $Port)：未被占用" -ForegroundColor DarkGray
        return
    }
    $proc = Get-Process -Id $owner -ErrorAction SilentlyContinue
    Write-Host "  停止 $Label (端口 $Port, PID $owner $($proc.ProcessName))" -ForegroundColor Yellow
    Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
}

if ($Stop) {
    Write-Host "停止开发环境进程 ..." -ForegroundColor Cyan
    Stop-PortOwner -Port $BackendPort -Label "后端"
    if (-not $NoFrontend) { Stop-PortOwner -Port $FrontendPort -Label "前端" }
    Start-Sleep -Seconds 2
    exit 0
}

# --- preflight: report the conflict instead of failing with a bind error -----
$backendOwner = Get-PortOwner -Port $BackendPort
if ($backendOwner) {
    $proc = Get-Process -Id $backendOwner -ErrorAction SilentlyContinue
    Write-Host "端口 $BackendPort 已被占用（PID $backendOwner $($proc.ProcessName)）。" -ForegroundColor Red
    Write-Host "可先执行： .\scripts\dev.ps1 -Stop    或改用其它端口： -BackendPort 9000" -ForegroundColor Yellow
    exit 1
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Host "未找到 uv。请先安装： https://docs.astral.sh/uv/" -ForegroundColor Red
    exit 1
}

# --- backend -----------------------------------------------------------------
Write-Host "启动后端 API： http://127.0.0.1:$BackendPort" -ForegroundColor Cyan
$backend = Start-Process -FilePath "uv" `
    -ArgumentList @("run", "stockmaster", "serve", "--host", "127.0.0.1", "--port", "$BackendPort") `
    -WorkingDirectory $root -PassThru

# --- frontend ----------------------------------------------------------------
$frontend = $null
if (-not $NoFrontend) {
    if (-not (Test-Path (Join-Path $root "frontend\node_modules"))) {
        Write-Host "前端依赖未安装，正在执行 pnpm install ..." -ForegroundColor Yellow
        Push-Location (Join-Path $root "frontend")
        try { & pnpm install } finally { Pop-Location }
    }

    if (Get-PortOwner -Port $FrontendPort) {
        Write-Host "端口 $FrontendPort 已被占用，跳过前端启动（可先用 -Stop 清理）。" -ForegroundColor Yellow
    } else {
        # `pnpm` resolves to pnpm.ps1 in PowerShell, which Start-Process cannot
        # launch; the .cmd shim is the executable entry point.
        $pnpmCmd = (Get-Command "pnpm.cmd" -ErrorAction SilentlyContinue).Source
        if (-not $pnpmCmd) {
            Write-Host "未找到 pnpm.cmd，跳过前端启动。请先安装 pnpm。" -ForegroundColor Yellow
        } else {
            Write-Host "启动前端 Vite： http://127.0.0.1:$FrontendPort" -ForegroundColor Cyan
            $frontend = Start-Process -FilePath "cmd.exe" `
                -ArgumentList @("/c", "`"$pnpmCmd`"", "dev", "--host", "127.0.0.1", "--port", "$FrontendPort", "--strictPort") `
                -WorkingDirectory (Join-Path $root "frontend") -PassThru
        }
    }
}

Start-Sleep -Seconds 4

# --- verify the backend actually came up -------------------------------------
if (Get-PortOwner -Port $BackendPort) {
    Write-Host ""
    Write-Host "开发环境已启动：" -ForegroundColor Green
    Write-Host "  后端 API : http://127.0.0.1:$BackendPort/api/v1/health"
    Write-Host "  API 文档 : http://127.0.0.1:$BackendPort/docs"
    if ($frontend) { Write-Host "  前端界面 : http://127.0.0.1:$FrontendPort" }
    Write-Host ""
    $pids = "后端 PID=$($backend.Id)"
    if ($frontend) { $pids += "，前端 PID=$($frontend.Id)" }
    Write-Host "  进程：$pids"
    Write-Host "  停止：make dev-stop（或 .\scripts\dev.ps1 -Stop）" -ForegroundColor DarkGray
    exit 0
}

Write-Host "后端未能在 $BackendPort 上监听。请查看其窗口中的错误信息。" -ForegroundColor Red
if ($backend -and -not $backend.HasExited) { Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue }
if ($frontend -and -not $frontend.HasExited) { Stop-Process -Id $frontend.Id -Force -ErrorAction SilentlyContinue }
exit 1
