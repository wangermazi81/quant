# bridge_toggle.ps1 - 桥接器自动确认开关 + 重启
# 用法:
#   .\tools\bridge_toggle.ps1 on       (全自动按 Y)
#   .\tools\bridge_toggle.ps1 off      (半自动等手动按 Y, 默认)
#   .\tools\bridge_toggle.ps1 status
#   .\tools\bridge_toggle.ps1 restart

param(
    [Parameter(Position=0)]
    [ValidateSet('on','off','status','restart')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$BaseDir  = Split-Path $PSScriptRoot -Parent
$Config   = Join-Path $BaseDir 'config.py'
$Anaconda = 'D:\Anaconda\python.exe'

function Show-Status {
    Set-Location $BaseDir
    Write-Host ""
    Write-Host "=== config.py ===" -ForegroundColor Cyan
    Select-String -Path $Config -Pattern '^(DRY_RUN|AUTO_CONFIRM_ORDER|MAX_ORDER_AMOUNT)' | Select-Object -ExpandProperty Line | ForEach-Object { Write-Host "  $_" }

    Write-Host ""
    Write-Host "=== Python real value ===" -ForegroundColor Cyan
    $env:PYTHONIOENCODING = 'utf-8'
    & $Anaconda -c "from config import AUTO_CONFIRM_ORDER, DRY_RUN, MAX_ORDER_AMOUNT; print(f'  DRY_RUN              = {DRY_RUN}'); print(f'  AUTO_CONFIRM_ORDER   = {AUTO_CONFIRM_ORDER}'); print(f'  MAX_ORDER_AMOUNT     = {MAX_ORDER_AMOUNT}')"

    Write-Host ""
    Write-Host "=== Bridge process ===" -ForegroundColor Cyan
    $found = $false
    Get-Process python -EA SilentlyContinue | ForEach-Object {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
        if ($cmd -like "*ths_bridge*") {
            $age = (Get-Date) - $_.StartTime
            Write-Host ("  PID {0} running {1:F1} min" -f $_.Id, $age.TotalMinutes) -ForegroundColor Green
            $found = $true
        }
    }
    if (-not $found) { Write-Host "  (no bridge running)" -ForegroundColor Yellow }
}

function Set-AutoConfirm($value) {
    $bytes = [System.IO.File]::ReadAllBytes($Config)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $bytes = $bytes[3..($bytes.Length - 1)]
    }
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    $text = $text -replace '(?m)^AUTO_CONFIRM_ORDER\s*=\s*\S+', "AUTO_CONFIRM_ORDER = $value"
    [System.IO.File]::WriteAllText($Config, $text, [System.Text.UTF8Encoding]::new($false))
    Write-Host "OK: AUTO_CONFIRM_ORDER = $value written to config.py" -ForegroundColor Green
}

function Restart-Bridge {
    Set-Location $BaseDir
    Get-Process python -EA SilentlyContinue | ForEach-Object {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
        if ($cmd -like "*ths_bridge*") {
            Stop-Process -Id $_.Id -Force
            Write-Host "  killed bridge PID $($_.Id)" -ForegroundColor Gray
        }
    }
    Start-Sleep 2
    $logFile = Join-Path $BaseDir 'logs\bridge.log'
    Remove-Item $logFile -EA SilentlyContinue
    $wsh = New-Object -ComObject WScript.Shell
    $cmdLine = "cmd /c cd /d `"$BaseDir`" && $Anaconda tools\ths_bridge.py > bridge_stdout.log 2>&1"
    $null = $wsh.Run($cmdLine, 0, $false)
    Start-Sleep 8

    Write-Host ""
    Write-Host "=== New bridge log ===" -ForegroundColor Cyan
    if (Test-Path $logFile) { Get-Content $logFile -Tail 6 -Encoding UTF8 }

    Write-Host ""
    Get-Process python -EA SilentlyContinue | ForEach-Object {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
        if ($cmd -like "*ths_bridge*") {
            Write-Host "OK: new bridge PID $($_.Id)" -ForegroundColor Green
        }
    }
}

switch ($Action) {
    'on'      { Set-AutoConfirm 'True';  Restart-Bridge; Show-Status }
    'off'     { Set-AutoConfirm 'False'; Restart-Bridge; Show-Status }
    'restart' { Restart-Bridge; Show-Status }
    'status'  { Show-Status }
}