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