# bridge_test.ps1 - 手动插入测试信号 (走完整桥接器闭环)
# 用法:
#   .\tools\bridge_test.ps1                                       # 默认: BUY 000001 平安银行 @10.50 x 100
#   .\tools\bridge_test.ps1 -Code 600519                          # 换股票
#   .\tools\bridge_test.ps1 -Code 600519 -Price 1800              # 换股+价
#   .\tools\bridge_test.ps1 -Action SELL                          # 改成卖出
#   .\tools\bridge_test.ps1 -Code 600519 -Price 1800 -Shares 200  # 全参数
#
# 流程:
#   1. 自检 Flask / 桥接器 / xiadan 三个进程
#   2. 显示 AUTO_CONFIRM_ORDER 状态 (True 会真成交 + 二次确认)
#   3. 插入 trade_signals 一行
#   4. 等 5 秒打印桥接器日志末尾

param(
    [string]$Code = '000001',
    [string]$Name = '',
    [double]$Price = 10.50,
    [int]$Shares = 100,
    [ValidateSet('BUY','SELL')]
    [string]$Action = 'BUY',
    [string]$Msg = '手动测试'
)

$ErrorActionPreference = 'Stop'
$BaseDir = Split-Path $PSScriptRoot -Parent
$Anaconda = 'D:\Anaconda\python.exe'

Write-Host ""
Write-Host "=== 1. 测试前自检 ===" -ForegroundColor Cyan
$flask = $false; $bridge = $false; $xiadan = $false
Get-Process python -EA SilentlyContinue | ForEach-Object {
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
    if ($cmd -like "*main.py*")    { Write-Host "  [OK] Flask PID $($_.Id)" -ForegroundColor Green; $flask = $true }
    if ($cmd -like "*ths_bridge*") { Write-Host "  [OK] 桥接器 PID $($_.Id)" -ForegroundColor Green; $bridge = $true }
}
$x = Get-Process xiadan -EA SilentlyContinue
if ($x) { Write-Host "  [OK] xiadan PID $($x.Id) ($($x.MainWindowTitle))" -ForegroundColor Green; $xiadan = $true }

$warns = @()
if (-not $flask)  { $warns += "Flask 没跑, 桥接器无法登录云端" }
if (-not $bridge) { $warns += "桥接器没跑, 信号不会被拉取. 运行: .\tools\bridge_toggle.ps1 restart" }
if (-not $xiadan) { $warns += "xiadan 没跑, 桥接器按键按到空气" }
foreach ($w in $warns) { Write-Host "  [WARN] $w" -ForegroundColor Yellow }
if ($warns.Count -gt 0) {
    Write-Host ""
    $continue = Read-Host "上面有警告, 仍要插信号吗? (y/N)"
    if ($continue -notin @('y','Y')) { Write-Host "取消"; exit 1 }
}

Write-Host ""
Write-Host "=== 2. AUTO_CONFIRM_ORDER 状态 ===" -ForegroundColor Cyan
$env:PYTHONIOENCODING = 'utf-8'
Push-Location $BaseDir
try {
    $autoVal = & $Anaconda -c "from config import AUTO_CONFIRM_ORDER; print(AUTO_CONFIRM_ORDER)"
} finally { Pop-Location }
$autoVal = $autoVal.Trim()
Write-Host "  AUTO_CONFIRM_ORDER = $autoVal"
$est = [Math]::Round($Price * $Shares, 0)
if ($autoVal -eq 'True') {
    Write-Host "  [DANGER] 桥接器会自动按 Y 真成交: $Action $Code ¥$Price x $Shares = ¥$est" -ForegroundColor Red
    $continue = Read-Host "  确认插入信号? (y/N)"
    if ($continue -notin @('y','Y')) { Write-Host "  取消"; exit 1 }
} else {
    Write-Host "  [SAFE] xiadan 弹委托确认框, 您手动按 Y 才真成交" -ForegroundColor Green
}

Write-Host ""
Write-Host "=== 3. 插入信号 ===" -ForegroundColor Cyan
Write-Host "  $Action $Code $Shares 股 @ ¥$Price (预估 ¥$est)"
if (-not $Name) { $Name = $Code }

$tmpPy = Join-Path $env:TEMP "_bridge_test_$(Get-Random).py"
$pyContent = @"
# -*- coding: utf-8 -*-
import sys, io, sqlite3, datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
con = sqlite3.connect(r'$BaseDir\data\users.db')
con.execute(
    "INSERT INTO trade_signals(user_id, code, name, action, price, shares, status, msg, created_at) "
    "VALUES (1, '$Code', '$Name', '$Action', $Price, $Shares, 'pending', '$Msg', ?)",
    (datetime.datetime.now().isoformat(),)
)
con.commit()
nid = con.execute('SELECT id FROM trade_signals ORDER BY id DESC LIMIT 1').fetchone()[0]
print(f'[OK] inserted trade_signal id = {nid}')
con.close()
"@
Set-Content -Path $tmpPy -Value $pyContent -Encoding UTF8
& $Anaconda $tmpPy
Remove-Item $tmpPy

Write-Host ""
Write-Host "=== 4. 等 5 秒看桥接器反应 (logs/bridge.log) ===" -ForegroundColor Cyan
Start-Sleep 5
Get-Content "$BaseDir\logs\bridge.log" -Tail 10 -Encoding UTF8

Write-Host ""
Write-Host "提示: 实时跟看日志可单开 PowerShell 跑: " -ForegroundColor Gray
Write-Host "  Get-Content `"$BaseDir\logs\bridge.log`" -Wait -Tail 5 -Encoding UTF8" -ForegroundColor Gray