#!/usr/bin/env bash
# Совместимый алиас полного suite: запускает ровно `python manage.py test`.
# Быстрого subset больше нет — DoD требует полного прогона перед каждым PR,
# и весь suite укладывается в ~4 секунды.
# Usage: ./scripts/test_lite.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export DJANGO_TESTING=1
exec python -u manage.py test -v 1
