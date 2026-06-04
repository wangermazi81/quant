# 见微 Alpha 每日定时启动：同花顺 + web服务 + 桥接器(管理员)
# 由「任务计划程序」以最高权限、交易日早盘前触发；桥接器随之以管理员身份运行(按键不被UIPI拦)。
$ErrorActionPreference = 'SilentlyContinue'
$Base = 'D:\同花顺量化交易\ths_auto_trader'
$Py   = 'D:\Anaconda\python.exe'
$Ths  = 'D:\软件用\同花顺\同花顺\hexinlauncher.exe'
$Log  = Join-Path $Base 'logs\daily_start.log'

function W($m){ "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File -FilePath $Log -Append -Encoding utf8 }

W '=== daily_start begin ==='

# 1) 同花顺：未运行则启动（务必在同花顺里设好「记住密码/自动登录」，否则停在登录页桥接器无法下单）
$thsRunning = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'hexin*' }
if (-not $thsRunning) {
    Start-Process $Ths
    W '同花顺已启动，等待加载+自动登录...'
    Start-Sleep -Seconds 40
} else {
    W '同花顺已在运行'
}

# 2) web 服务（launcher.py 会先释放 5001 再起新的 main.py）
Start-Process -FilePath $Py -ArgumentList "`"$Base\launcher.py`"" -WorkingDirectory $Base -WindowStyle Hidden
W 'web(launcher.py) 已启动'
Start-Sleep -Seconds 15

# 3) 桥接器（bridge_toggle on：杀旧+自动确认+启新；本脚本以最高权限运行 => 桥接器=管理员）
& "$Base\tools\bridge_toggle.ps1" on
W 'bridge_toggle on 已执行'

W '=== daily_start done ==='

