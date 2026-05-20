# bundle_private.ps1
# Pack sensitive files (user DB / AI config / paper trading state / .env) into a zip
# for manual upload to cloud server.
#
# Usage (in ths_auto_trader/ folder):
#   powershell -ExecutionPolicy Bypass -File .\bundle_private.ps1
#
# Output: _private_bundle_YYYYMMDD-HHmm.zip in the same folder.

$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot
$Stamp     = Get-Date -Format "yyyyMMdd-HHmm"
$ZipName   = "_private_bundle_$Stamp.zip"
$ZipPath   = Join-Path $ScriptDir $ZipName

# Relative paths to include (only if they exist)
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

$Existing = @()
foreach ($t in $Targets) {
    $p = Join-Path $ScriptDir $t
    if (Test-Path -LiteralPath $p) { $Existing += $p }
}

if ($Existing.Count -eq 0) {
    Write-Host "[!] No sensitive files found, nothing to pack." -ForegroundColor Yellow
    exit 0
}

Write-Host "[*] Found $($Existing.Count) sensitive files, packing to $ZipName ..." -ForegroundColor Cyan
foreach ($f in $Existing) {
    $rel = $f.Substring($ScriptDir.Length).TrimStart('\')
    Write-Host "    + $rel"
}

Push-Location $ScriptDir
try {
    if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
    $RelPaths = $Existing | ForEach-Object { $_.Substring($ScriptDir.Length).TrimStart('\') }
    Compress-Archive -LiteralPath $RelPaths -DestinationPath $ZipPath -CompressionLevel Optimal
}
finally {
    Pop-Location
}

$Size   = (Get-Item -LiteralPath $ZipPath).Length
$SizeKB = [Math]::Round($Size / 1KB, 1)
$KbStr  = "$SizeKB KB"
Write-Host ""
Write-Host "[OK] Pack complete: $ZipName ($KbStr)" -ForegroundColor Green
Write-Host "[>>] Next steps:" -ForegroundColor Green
Write-Host "       scp $ZipName root@<server-ip>:/tmp/"
Write-Host "       ssh root@<server-ip> 'cd /opt/quant && unzip -o /tmp/$ZipName && rm /tmp/$ZipName && systemctl restart quant-platform'"
