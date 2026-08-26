# Результаты сценариев fix/demo-hardening-risks

Дата прогона: 2026-08-26.

Команда: `python manage.py test apps.core.tests_demo_scenarios apps.core.tests_demo_hardening`

**Итого: 8/8 OK**

| # | Сценарий | Результат |
|---|---|---|
| 1 | `DEMO_MODE=1` + `DEBUG=0`: регистрация показывает ссылку активации | OK |
| 2 | Без DEMO: регистрация редиректит на login (ссылка не на экране) | OK |
| 3 | `seed_demo_scenario` → STAFFING, тимлид, слоты заполнены; кабинет директора/менеджера 200 | OK |
| 4 | Пустой пул кандидатов → `unfilled_opened_slots >= 1` | OK |
| 5 | Лендинг + login 200; шаблоны 400/403/404/500 на месте | OK |
| 6 | SQLite `OPTIONS.timeout=30`, флаг `DEMO_MODE` в settings | OK |
| 7 | Unit: activation без DEBUG (`tests_demo_hardening`) | OK |
| 8 | Unit: seed staffing project (`tests_demo_hardening`) | OK |

Вердикт: блокеров нет — ветка готова к merge и деплою на демо-VPS.
