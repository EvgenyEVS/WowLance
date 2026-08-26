# Шпаргалка для демо WowLance

Короткая инструкция, как QA или тимлид может самостоятельно провести демо без вопросов в Slack.

## 1. Подготовка базы

Выполнить в терминале (из корня проекта, где лежит `manage.py`):

```powershell
python manage.py migrate
python manage.py seed_freelancers
python manage.py seed_managers
python manage.py seed_teamleads   # если команда есть в репо
python manage.py runserver