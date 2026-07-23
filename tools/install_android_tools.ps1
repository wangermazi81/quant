$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SdkDir = Join-Path $ProjectRoot '.android-sdk'
$Archive = Join-Path $env:TEMP 'platform-tools-latest-windows.zip'
$Python = 'D:\Anaconda\python.exe'

New-Item -ItemType Directory -Path $SdkDir -Force | Out-Null
Invoke-WebRequest -Uri 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip' -OutFile $Archive
Expand-Archive -LiteralPath $Archive -DestinationPath $SdkDir -Force

& $Python -m pip install --index-url 'https://pypi.org/simple' -r (Join-Path $ProjectRoot 'requirements-android.txt')
if ($LASTEXITCODE -ne 0) {
    throw "Android Python 依赖安装失败，退出码 $LASTEXITCODE"
}

$Adb = Join-Path $SdkDir 'platform-tools\adb.exe'
& $Adb version
if ($LASTEXITCODE -ne 0) {
    throw "ADB 启动失败，退出码 $LASTEXITCODE"
}

Write-Host "Android 工具链已安装：$Adb"
