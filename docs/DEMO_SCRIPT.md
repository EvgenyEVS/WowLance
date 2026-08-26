# Шпаргалка для демо WowLance

Короткая инструкция, как QA или тимлид может самостоятельно провести демо.

## 1. Подготовка базы

```powershell
python manage.py migrate
python manage.py seed_demo_scenario
python manage.py runserver
```

`seed_demo_scenario` создаёт роли, фрилансеров и проект «Демо для стейкхолдеров»
(STAFFING + пакет quick_start + тимлид).

## 2. Логины

| Роль | Email | Пароль |
|------|--------|--------|
| Директор | director@wowlance.demo | DemoPass123! |
| Тимлид | teamlead@wowlance.demo | DemoPass123! |
| Менеджер | manager@wowlance.demo | DemoPass123! |

## 3. Демо-VPS (systemd)

```
Environment="DJANGO_DEBUG=0"
Environment="DEMO_MODE=1"
Environment="PUBLIC_HOST=195.19.209.121"
```

Так стейкхолдеры не видят traceback Django, но ссылка активации при console-email
остаётся на экране (`DEBUG` или `DEMO_MODE`).
