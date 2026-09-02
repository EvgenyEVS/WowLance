# Совместимый алиас полного suite: запускает ровно `python manage.py test`.
# Быстрого subset больше нет — DoD требует полного прогона перед каждым PR,
# и весь suite укладывается в ~4 секунды.
# Usage: powershell -File scripts/test_lite.ps1

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

$env:DJANGO_TESTING = '1'

Write-Host 'test_lite: full suite -> python manage.py test'
python -u manage.py test -v 1
exit $LASTEXITCODE
