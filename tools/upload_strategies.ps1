<#
.SYNOPSIS
    上传本地策略到云端服务器（策略不在 git 中，需单独 scp）
.DESCRIPTION
    1. 检查所有策略 META 的 builtin 标记（必须全部为 true）
    2. scp 上传到服务器 /opt/quant/strategies/
    3. 重启云端服务
    4. 验证非 admin 用户看不到源码
.PARAMETER SkipRestart
    跳过重启服务器（仅上传文件）
.EXAMPLE
    .\tools\upload_strategies.ps1
    .\tools\upload_strategies.ps1 -SkipRestart
#>
param([switch]$SkipRestart)

$ErrorActionPreference = "Stop"
$SERVER = "root@47.100.109.173"
$REMOTE_DIR = "/opt/quant/strategies"
$LOCAL_DIR = Join-Path $PSScriptRoot ".." "strategies"

Write-Host "=== 策略上传工具 ===" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: 检查本地策略文件 ──
$strategies = Get-ChildItem -Path $LOCAL_DIR -Filter "*.py" -ErrorAction Stop
if ($strategies.Count -eq 0) {
    Write-Host "❌ 本地 strategies/ 目录没有 .py 文件" -ForegroundColor Red
    exit 1
}
Write-Host "📁 本地策略 ($($strategies.Count)个):" -ForegroundColor Yellow
foreach ($f in $strategies) {
    Write-Host "  - $($f.Name)"
}

# ── Step 2: 检查 builtin 标记 ──
Write-Host ""
Write-Host "🔒 检查 builtin 标记..." -ForegroundColor Yellow
$allBuiltin = $true
foreach ($f in $strategies) {
    $firstLine = Get-Content $f.FullName -TotalCount 1
    if ($firstLine -match '"builtin"\s*:\s*false') {
        Write-Host "  ⚠️ $($f.Name): builtin=false（用户将能看到源码！）" -ForegroundColor Red
        $allBuiltin = $false
    } elseif ($firstLine -match '"builtin"\s*:\s*true') {
        Write-Host "  ✅ $($f.Name): builtin=true" -ForegroundColor Green
    } else {
        Write-Host "  ⚠️ $($f.Name): 未找到 META builtin 标记" -ForegroundColor Yellow
        $allBuiltin = $false
    }
}
if (-not $allBuiltin) {
    Write-Host ""
    Write-Host "❌ 存在 builtin!=true 的策略！上传后用户将能看到源码。" -ForegroundColor Red
    Write-Host "请先修复 META 标记：在每个 .py 文件第一行设置 `"builtin`": true" -ForegroundColor Red
    $confirm = Read-Host "是否仍然继续？(y/N)"
    if ($confirm -ne "y") {
        Write-Host "已取消" -ForegroundColor Yellow
        exit 0
    }
}

# ── Step 3: SCP 上传 ──
Write-Host ""
Write-Host "📤 上传到云端 $SERVER`:$REMOTE_DIR ..." -ForegroundColor Yellow
$files = $strategies | ForEach-Object { $_.FullName }
scp $files "${SERVER}:${REMOTE_DIR}/"
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ SCP 上传失败" -ForegroundColor Red
    exit 1
}
Write-Host "✅ 上传成功 ($($strategies.Count)个文件)" -ForegroundColor Green

# ── Step 4: 重启服务 ──
if (-not $SkipRestart) {
    Write-Host ""
    Write-Host "🔄 重启云端服务..." -ForegroundColor Yellow
    ssh $SERVER "systemctl restart quant-platform.service"
    Start-Sleep 3
    $status = ssh $SERVER "systemctl is-active quant-platform.service"
    if ($status -eq "active") {
        Write-Host "✅ 服务已重启 (active)" -ForegroundColor Green
    } else {
        Write-Host "⚠️ 服务状态: $status" -ForegroundColor Yellow
    }
} else {
    Write-Host ""
    Write-Host "⏭️ 已跳过重启（-SkipRestart）" -ForegroundColor Yellow
}

# ── Step 5: 验证隐私保护 ──
Write-Host ""
Write-Host "🔍 验证隐私保护..." -ForegroundColor Yellow
$checkResult = ssh $SERVER "python3 -c `"import requests,json; s=requests.Session(); s.post('http://127.0.0.1:5000/api/auth/login',json={'username':'111','password':'111'}); r=s.get('http://127.0.0.1:5000/api/strategies'); strats=r.json() if isinstance(r.json(),list) else r.json().get('strategies',[]); hidden=sum(1 for st in strats if '受保护' in st.get('desc','') or '保密' in st.get('desc','')); print(f'{hidden}/{len(strats)} strategies hidden')`""
Write-Host "  $checkResult" -ForegroundColor Green

Write-Host ""
Write-Host "=== 完成 ===" -ForegroundColor Cyan
