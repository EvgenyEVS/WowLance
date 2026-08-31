#!/usr/bin/env bash
# Деплой WowLance на демо-VPS «с нуля» (БД пересоздаётся).
# Запуск НА СЕРВЕРЕ от root/sudo:
#   bash scripts/deploy_vps.sh
# Или одной командой с локальной машины (нужен SSH-ключ):
#   ssh root@195.19.209.121 'bash -s' < scripts/deploy_vps.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/wowlance}"
REPO_URL="${REPO_URL:-https://github.com/EvgenyEVS/WowLance.git}"
BRANCH="${BRANCH:-master}"
PUBLIC_HOST="${PUBLIC_HOST:-195.19.209.121}"
SERVICE_NAME="${SERVICE_NAME:-wowlance}"

echo "==> App dir: $APP_DIR  branch: $BRANCH  host: $PUBLIC_HOST"

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
# WhiteNoise часто только на сервере
pip install 'whitenoise>=6.0' || true

export DJANGO_DEBUG="${DJANGO_DEBUG:-1}"
export PUBLIC_HOST
export PUBLIC_SCHEME=http
export USE_HTTPS=0

# Свежая БД (пользователь явно ок с потерей данных)
rm -f db.sqlite3
python manage.py migrate --noinput
python manage.py collectstatic --noinput || true

# Демо-данные, если команды есть
python manage.py seed_freelancers || true
python manage.py seed_demo_users 2>/dev/null || true

# systemd: обновить Environment, если unit уже есть
if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
  systemctl restart "$SERVICE_NAME"
  systemctl --no-pager --full status "$SERVICE_NAME" || true
else
  echo "!! Unit ${SERVICE_NAME}.service не найден — перезапустите gunicorn вручную."
fi

echo "==> Done. Open http://${PUBLIC_HOST}/"
echo "    DEBUG=$DJANGO_DEBUG — при регистрации ссылка активации на экране (console email)."
