# Деплой WowLance на VPS с Windows (нужен рабочий SSH: ключ или ssh-agent).
# Пример:
#   powershell -File scripts/deploy_from_windows.ps1
#   powershell -File scripts/deploy_from_windows.ps1 -SshTarget "ubuntu@195.19.209.121"

param(
    [string]$SshTarget = "root@195.19.209.121",
    [string]$SshExe = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
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

Write-Host "Deploying via $SshTarget ..."
Get-Content -Raw -Encoding UTF8 $scriptPath | & $SshExe -o ConnectTimeout=15 $SshTarget "bash -s"
if ($LASTEXITCODE -ne 0) { throw "SSH deploy failed with exit $LASTEXITCODE" }

Write-Host "OK. Open http://195.19.209.121/"
