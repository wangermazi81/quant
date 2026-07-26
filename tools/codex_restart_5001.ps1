$ErrorActionPreference = 'SilentlyContinue'
$root = 'D:\同花顺量化交易\ths_auto_trader'
$py = 'D:\Anaconda\python.exe'
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

$listeners = Get-NetTCPConnection -LocalPort 5001 -State Listen
foreach($item in $listeners){
  if($item.OwningProcess -and $item.OwningProcess -ne 0){
    & taskkill.exe /PID $item.OwningProcess /F /T | Out-Null
  }
}

Start-Sleep -Seconds 1
$left = Get-NetTCPConnection -LocalPort 5001 -State Listen
if($left){
  foreach($item in $left){
    $p = Get-Process -Id $item.OwningProcess
    Write-Host "5001 still occupied: pid=$($item.OwningProcess) proc=$($p.ProcessName) title=$($p.MainWindowTitle)"
  }
  exit 2
}

$schedPidFile = Join-Path $root 'data\scheduler.pid'
if(Test-Path $schedPidFile){
  $spid = Get-Content -LiteralPath $schedPidFile | Select-Object -First 1
  if($spid -match '^\d+$'){
    $p = Get-Process -Id ([int]$spid)
    if($p -and $p.ProcessName -like '*python*'){
      Stop-Process -Id ([int]$spid) -Force
    }
  }
}

$env:THS_EMBED_SCHEDULER = '0'
Start-Process -FilePath $py -ArgumentList @('-B', 'main.py') -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir "codex_web_$stamp.out.log") -RedirectStandardError (Join-Path $logDir "codex_web_$stamp.err.log")
Start-Process -FilePath $py -ArgumentList @('-B', 'scheduler_main.py') -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir "codex_scheduler_$stamp.out.log") -RedirectStandardError (Join-Path $logDir "codex_scheduler_$stamp.err.log")


