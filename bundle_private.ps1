# -*- coding: utf-8 -*-
# 一键打包项目隐私文件（用户库、AI 配置、模拟盘状态、推送配置）
# 生成 _private_bundle_<时间戳>.zip 供手动上传云端
#
# 用法（在 ths_auto_trader/ 目录下）：
#   powershell -ExecutionPolicy Bypass -File .\bundle_private.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot
$Stamp = Get-Date -Format "yyyyMMdd-HHmm"
$ZipName = "_private_bundle_$Stamp.zip"
$ZipPath = Join-Path $ScriptDir $ZipName

# 待打包的相对路径列表
$Targets = @(
    ".env",
    "data\ai_config.json",
    "data\users.db",
    "data\users.db-wal",
    "data\users.db-shm",
    "data\paper_trading.db",
    "data\paper_trading.db-wal",
    "data\paper_trading.db-shm",
    "data\search_config.json",
    "data\notify_config.json",
    "secrets.json",
    "api_keys.json"
)

# 实际存在的文件
$Existing = @()
foreach ($t in $Targets) {
    $p = Join-Path $ScriptDir $t
    if (Test-Path -LiteralPath $p) { $Existing += $p }
}

if ($Existing.Count -eq 0) {
    Write-Host "未找到任何隐私文件，无需打包。" -ForegroundColor Yellow
    exit 0
}

Write-Host "发现 $($Existing.Count) 个隐私文件，正在打包到 $ZipName ..." -ForegroundColor Cyan
foreach ($f in $Existing) {
    $rel = $f.Substring($ScriptDir.Length).TrimStart('\')
    Write-Host "  + $rel"
}

# 用 Compress-Archive，保留相对路径结构
# 临时切到 ScriptDir，让 zip 内的路径以 data/... 开头
Push-Location $ScriptDir
try {
    if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
    $RelPaths = $Existing | ForEach-Object { $_.Substring($ScriptDir.Length).TrimStart('\') }
    Compress-Archive -LiteralPath $RelPaths -DestinationPath $ZipPath -CompressionLevel Optimal
}
finally {
    Pop-Location
}

$Size = (Get-Item -LiteralPath $ZipPath).Length
$SizeKB = [Math]::Round($Size / 1KB, 1)
Write-Host "`n✅ 打包完成: $ZipName ($SizeKB KB)" -ForegroundColor Green
Write-Host "上传到云端后，在目标 ths_auto_trader/ 目录解压即可恢复。" -ForegroundColor Green
