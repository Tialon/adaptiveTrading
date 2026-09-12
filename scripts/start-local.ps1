<#
.SYNOPSIS
    Level 1 本机一键启动 —— Windows + Python + SQLite,不需要 MySQL / Redis。

.DESCRIPTION
    V14 §4.1 要求「不要要求用户手工执行 `$env:DATABASE_URL=...` / `$env:REDIS_ENABLED=false`」。
    本脚本把这两步收进去: 一条命令起服务,直接打开 http://127.0.0.1:8800。

    它**不改 .env** —— 只在本次进程的环境里覆盖两个变量。原因:
    - `.env` 里的 `DATABASE_URL` 指向生产库, 且**含密码**, 不该因为本机开发被改写;
    - 环境变量优先级高于 `.env`(pydantic-settings), 所以进程内覆盖即可。

    环境模型(V14 §2 冻结):
        Level 1   Windows + Python + SQLite           ← 本脚本
        Level 2   Windows + Docker + MySQL + Redis
        Level 3   Pi      + Docker + MySQL + Redis

.PARAMETER OpenBrowser
    启动后自动打开浏览器(默认打开)。

.PARAMETER NoBrowser
    不自动打开浏览器。

.PARAMETER DbFile
    SQLite 文件路径(默认 .\data\local-dev.db)。

    **为什么默认不是仓库根的 adaptive.db**: 运行参数优先级是 **DB > env**, 所以一个
    老开发库里的 `runtime_config` 覆盖会**盖过**本脚本设的环境变量。实测踩到过:
    仓库根的 adaptive.db 里残留着早先模式切换测试写入的
    `TRADING_MODE=live` + `LIVE_TRADING_CONFIRM=true` + `MAINNET_API_SCOPE_CONFIRM=true`,
    于是「本机开发启动」直接把系统带到了**主网**上(靠主网守卫才没构成真实下单)。

    Level 1 的定位是「开发方便」, 状态应该是**可丢弃的**。所以默认用一个独立的开发库,
    与任何历史状态隔离; 想用旧库请显式 `-DbFile .\adaptive.db`, 届时本脚本会先做安全预检。

.EXAMPLE
    .\scripts\start-local.ps1
    .\scripts\start-local.ps1 -NoBrowser
    .\scripts\start-local.ps1 -DbFile .\data\local.db
#>
[CmdletBinding()]
param(
    [switch]$OpenBrowser = $true,
    [switch]$NoBrowser,
    [string]$DbFile = "data\local-dev.db"
)

$ErrorActionPreference = "Stop"

# 仓库根 = 脚本的上一级(脚本放在 scripts/ 下)
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step($text) { Write-Host "  $text" -ForegroundColor Gray }
function Write-OK($text)   { Write-Host "  [OK] $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }
function Write-Err($text)  { Write-Host "  [X]  $text" -ForegroundColor Red }

Write-Host ""
Write-Host "adaptiveTrading —— Level 1 本机启动 (Windows + SQLite)" -ForegroundColor Cyan
Write-Host ("─" * 56) -ForegroundColor DarkGray

# ---- 1. 解释器 ------------------------------------------------------------
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $Python = $VenvPython
    Write-OK "使用虚拟环境 .venv"
} else {
    Write-Warn "未找到 .venv —— 回退到 PATH 上的 python"
    Write-Step "首次使用建议先执行: uv sync  (或 python -m venv .venv; .venv\Scripts\pip install -e .)"
    $Python = "python"
}

# ---- 2. 端口占用检查(避免「起来了但访问的是上一个进程」) -------------------
$Port = 8800
try {
    $Busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
} catch {
    $Busy = $null   # 该 cmdlet 在部分环境不可用 —— 检查失败不该阻断启动
}
if ($Busy) {
    $Pids = ($Busy | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    Write-Err "端口 $Port 已被占用 (PID: $Pids)"
    Write-Step "本脚本不会自动杀进程。请先确认并停止它, 例如:"
    Write-Step "  Get-Process -Id $Pids | Stop-Process"
    Write-Step "否则新进程会因端口冲突而 web-server 退出, 你访问到的仍是旧进程。"
    exit 1
}

# ---- 3. 安全预检: 这个库里有没有会把本机开发带进实盘的覆盖? -----------------
# 运行参数优先级 DB > env, 所以库里的 runtime_config 会盖过下面的环境变量。
# Level 1 是开发环境, **绝不允许**因为一个老库而连上主网。
$DbPath = Join-Path $RepoRoot $DbFile
$env:DATABASE_URL = "sqlite+aiosqlite:///$DbFile"
$env:REDIS_ENABLED = "false"

if (Test-Path $DbPath) {
    # 预检逻辑放在 Python 里(scripts/local_preflight.py) —— 在 PowerShell 里拼
    # 多行 python -c 的引号极易出错, 而且那段逻辑值得单独测。
    $Preflight = Join-Path $RepoRoot "scripts\local_preflight.py"
    $ProbeOut = & $Python $Preflight $DbFile 2>&1
    $ProbeCode = $LASTEXITCODE
    foreach ($line in $ProbeOut) {
        if ($line -like "OVERRIDES=*") {
            Write-Warn "该库带有运行参数覆盖: $($line.Substring(10))"
            Write-Step "覆盖优先于环境变量, 会决定这次启动的模式。"
        } elseif ($line -like "DANGER=*") {
            Write-Host ""
            Write-Err "该库会让本机开发进入【实盘主网】: $($line.Substring(7))"
        } else {
            Write-Host "  $line"
        }
    }
    if ($ProbeCode -eq 2) { exit 2 }
}

Write-OK "数据库: SQLite ($DbFile)"
Write-OK "Redis : 关闭 (Level 1 不需要)"
Write-Host ""

# ---- 4. 提示浏览器 --------------------------------------------------------
$Url = "http://127.0.0.1:8800"
Write-Host "  启动后访问: $Url" -ForegroundColor White
Write-Host "  停止服务  : Ctrl+C" -ForegroundColor DarkGray
Write-Host ("─" * 56) -ForegroundColor DarkGray
Write-Host ""

if ($OpenBrowser -and -not $NoBrowser) {
    # 等几秒再开浏览器 —— 服务还没起来就打开会得到一个「无法访问」的页面。
    # 用独立的隐藏进程而不是 Start-Job: 后者要等前台命令结束才回收, 且嵌套引号容易踩坑。
    $DelayedOpen = "Start-Sleep -Seconds 8; Start-Process '$Url'"
    Start-Process powershell -ArgumentList "-NoProfile", "-Command", $DelayedOpen -WindowStyle Hidden
}

# ---- 5. 前台运行(日志直接打到本终端) --------------------------------------
& $Python run.py
exit $LASTEXITCODE
