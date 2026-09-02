# Обновление демо-VPS с Windows (нужен SSH: ключ или ssh-agent).
# Базу не сносит. Чистый стенд: -WipeDb
#
#   powershell -File scripts/deploy_from_windows.ps1
#   powershell -File scripts/deploy_from_windows.ps1 -SshTarget "ubuntu@195.19.209.121"
#   powershell -File scripts/deploy_from_windows.ps1 -WipeDb

param(
    [string]$SshTarget = "root@195.19.209.121",
    [string]$SshExe = "",
    [switch]$WipeDb
)

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "deploy_vps.sh"

if (-not $SshExe) {
    $candidates = @(
        "C:\Windows\System32\OpenSSH\ssh.exe",
        "C:\Program Files\Git\usr\bin\ssh.exe"
    )
    $SshExe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $SshExe) { throw "ssh.exe not found" }
if (-not (Test-Path $scriptPath)) { throw "Missing $scriptPath" }

$body = [System.IO.File]::ReadAllText($scriptPath) -replace "`r`n", "`n" -replace "`r", "`n"
$remote = if ($WipeDb) { "WIPE_DB=1 bash -s" } else { "bash -s" }

Write-Host "Deploying via $SshTarget (wipe_db=$WipeDb) ..."
$body | & $SshExe -o ConnectTimeout=20 $SshTarget $remote
if ($LASTEXITCODE -ne 0) { throw "SSH deploy failed with exit $LASTEXITCODE" }

Write-Host "OK. Open http://195.19.209.121/"
