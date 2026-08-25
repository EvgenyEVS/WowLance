# Email/SMTP port onto master

| Поле | Значение |
|------|----------|
| **Источник** | `fix/email-smtp-ready` (polish `dev_eubog`) |
| **Ветка порта** | `feature/email-smtp-onto-master` |
| **Дата** | 2026-08-25 |

## Что перенесено

- SMTP при `EMAIL_HOST_USER` + `EMAIL_HOST_PASSWORD`, иначе console
- `DEFAULT_FROM_EMAIL` всегда задан (SMTP → user, console → `WowLance <noreply@…>`)
- Экран успеха регистрации: console vs SMTP в DEBUG
- `.env.example`, `requirements.txt` (UTF-8), краткий README

## Что сознательно не переносили

- **`python-decouple`** — на master конфиг уже через `os.environ`; обязательный import ломал бы старт без `pip install`
- Замену логики `PUBLIC_HOST` / `TESTING` MD5 hasher / staffing apps — оставлены как на master

## Проверка

```powershell
python manage.py check
python manage.py test apps.users -v 1 --keepdb
```
