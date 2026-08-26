# WowLance

WowLance — B2B-платформа для запуска продаж и подбора команд.

## Локальный запуск и тестирование

1. Установите зависимости: `pip install -r requirements.txt`
2. Примените миграции: `python manage.py migrate`
3. Запустите сервер: `python manage.py runserver`

## Демо-сценарии

- **Регистрация фрилансера из WOW Talent (stub):**  
  Откройте ссылку [http://127.0.0.1:8000/register/?role=freelancer&ref=wowtalent_demo](http://127.0.0.1:8000/register/?role=freelancer&ref=wowtalent_demo)  
  *Ожидаемое поведение: отобразится баннер «Регистрация из WOW Talent», поля Имя, Фамилия и Email будут автоматически заполнены демо-данными.*

## Email / активация

По умолчанию письма идут в **консоль** (`console` backend).  
Чтобы слать реальные письма, задайте `EMAIL_HOST_USER` и `EMAIL_HOST_PASSWORD` (см. `.env.example`).

- **DEBUG или DEMO_MODE + console:** после регистрации на экране показывается ссылка активации.  
- **SMTP настроен:** письмо уходит на email.  
- **На демо-VPS:** `DJANGO_DEBUG=0` + `DEMO_MODE=1` — без Django-traceback, со ссылкой активации.

Демо-сценарий одной командой: `python manage.py seed_demo_scenario`  
Быстрый smoke тестов: `powershell -File scripts/test_lite.ps1`