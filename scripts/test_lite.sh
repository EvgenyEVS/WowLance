#!/usr/bin/env bash
# Быстрый локальный smoke: без полного apps.rooms.
# Usage: ./scripts/test_lite.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export DJANGO_TESTING=1
exec python -u manage.py test \
  apps.users \
  apps.profiles \
  apps.pipeline \
  apps.core \
  apps.rooms.tests \
  apps.rooms.tests_staffing_matching \
  -v 1 --keepdb
