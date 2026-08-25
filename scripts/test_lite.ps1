# Быстрый локальный smoke: без полного apps.rooms (десятки минут).
# Полный suite: python manage.py test
# Usage: powershell -File scripts/test_lite.ps1

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

$env:DJANGO_TESTING = '1'
$modules = @(
    'apps.users',
    'apps.profiles',
    'apps.pipeline',
    'apps.core',
    'apps.rooms.tests',
    'apps.rooms.tests_staffing_matching'
)

Write-Host "test_lite:" ($modules -join ' ')
python -u manage.py test @modules -v 1 --keepdb
exit $LASTEXITCODE
