#!/usr/bin/env bash
# Обновление демо-VPS: код с origin/master, миграции, статика, демо-сид.
# Базу по умолчанию НЕ сносит. Чистый стенд: WIPE_DB=1 bash scripts/deploy_vps.sh
#
# На сервере (root/sudo):
#   bash scripts/deploy_vps.sh
# С Windows:
#   powershell -File scripts/deploy_from_windows.ps1
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/wowlance}"
REPO_URL="${REPO_URL:-https://github.com/EvgenyEVS/WowLance.git}"
BRANCH="${BRANCH:-master}"
PUBLIC_HOST="${PUBLIC_HOST:-195.19.209.121}"
SERVICE_NAME="${SERVICE_NAME:-wowlance}"
APP_USER="${APP_USER:-www-data}"
WIPE_DB="${WIPE_DB:-0}"

# Совпадает с /etc/systemd/system/wowlance.service
DJANGO_DEBUG="${DJANGO_DEBUG:-0}"
DEMO_MODE="${DEMO_MODE:-1}"
PUBLIC_SCHEME="${PUBLIC_SCHEME:-http}"
USE_HTTPS="${USE_HTTPS:-0}"

echo "==> App dir: $APP_DIR  branch: $BRANCH  host: $PUBLIC_HOST  wipe_db=$WIPE_DB"

if [[ ! -d "$APP_DIR/.git" ]]; then
  mkdir -p "$(dirname "$APP_DIR")"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
git fetch origin
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt

export DJANGO_DEBUG DEMO_MODE PUBLIC_HOST PUBLIC_SCHEME USE_HTTPS

mkdir -p media staticfiles
if [[ "$WIPE_DB" == "1" ]]; then
  echo "==> WIPE_DB=1: удаляю db.sqlite3"
  rm -f db.sqlite3
fi

if id "$APP_USER" >/dev/null 2>&1; then
  chown "$APP_USER:$APP_USER" db.sqlite3 2>/dev/null || true
  chown -R "$APP_USER:$APP_USER" media staticfiles
  run_django() {
    sudo -u "$APP_USER" env \
      DJANGO_DEBUG="$DJANGO_DEBUG" \
      DEMO_MODE="$DEMO_MODE" \
      PUBLIC_HOST="$PUBLIC_HOST" \
      PUBLIC_SCHEME="$PUBLIC_SCHEME" \
      USE_HTTPS="$USE_HTTPS" \
      HOME=/var/www \
      "$APP_DIR/.venv/bin/python" manage.py "$@"
  }
else
  echo "!! Пользователь $APP_USER не найден — manage.py от текущего UID"
  run_django() {
    python manage.py "$@"
  }
fi

echo "==> migrate / collectstatic / seed_demo_scenario"
run_django migrate --noinput
run_django collectstatic --noinput
run_django seed_demo_scenario

if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
  systemctl restart "$SERVICE_NAME"
  sleep 2
  systemctl --no-pager --lines=15 status "$SERVICE_NAME" || true
else
  echo "!! Unit ${SERVICE_NAME}.service не найден — перезапустите gunicorn вручную."
  echo "   gunicorn: --workers 1 (SQLite), env DJANGO_DEBUG=0 DEMO_MODE=1 PUBLIC_HOST=$PUBLIC_HOST"
fi

echo "==> Done. Open http://${PUBLIC_HOST}/"
echo "    DJANGO_DEBUG=$DJANGO_DEBUG DEMO_MODE=$DEMO_MODE  HEAD=$(git log -1 --format='%h %s')"
echo "    Логины: director@ / teamlead@ / manager@ / ivan.petrov@  wowlance.demo  / DemoPass123!"
