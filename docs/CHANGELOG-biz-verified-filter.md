# Ветка `biz/verified-filter` — Каталог показывает только верифицированных

## Цель задачи
Реализовать требование MVP: каталог фрилансеров по умолчанию показывает только верифицированных пользователей. Неверифицированные видны директору/админу по отдельному чекбоксу.

## Что сделано

### 1. `apps/profiles/views.py` — Фильтрация по умолчанию
В функции `freelancer_catalog`:
- По умолчанию (без параметра) queryset фильтруется по `is_verified=True`.
- Добавлен GET-параметр `show_unverified` (по умолчанию `'0'`).
- При `show_unverified='1'` фильтр снимается, показываются все профили.
- Флаг `selected_show_unverified` передаётся в шаблон для сохранения состояния чекбокса.

### 2. `apps/profiles/templates/profiles/catalog.html` — UI
- В блок фильтров добавлен чекбокс **«Показать неверифицированных»** (выключен по умолчанию).
- На карточку фрилансера добавлен бейдж **«На модерации»**, если `not profile.is_verified`.

### 3. `apps/profiles/tests.py` — Новые тесты
Добавлен класс `FreelancerVerifiedFilterTests` с 2 тестами:
- `test_catalog_shows_only_verified_by_default` — без параметров в каталоге нет неверифицированного Кирилла.
- `test_catalog_shows_all_with_show_unverified_flag` — с флагом `?show_unverified=1` Кирилл появляется в каталоге.

### 4. `apps/test_helpers.py` — Обновление тестового хелпера
Функция `make_freelancer()` теперь по умолчанию проставляет `is_verified=True` (аналогично `video_url` из ветки `biz/video`). Это предотвращает **регрессию существующих тестов** (543 теста в проекте), которые ожидают видимости фрилансеров в каталоге.

## Критерии готовности (DoD)
- [x] Тест: без параметров Кирилла (неверифицированного) нет в каталоге.
- [x] Тест: с флагом `show_unverified=1` Кирилл появляется.
- [x] `python manage.py test apps.profiles` — все 23 теста зелёные.
- [x] `python manage.py test` — все 543 теста проекта зелёные.
- [x] `python manage.py check` — `System check identified no issues (0 silenced)`.

## Что НЕ делалось (согласно ограничениям задачи)
- ❌ Не тронуты `apps/rooms`, `apps/pipeline`, `matching`.
- ❌ Не добавлены новые поля в БД.
- ❌ Не изменена логика подбора (staffing) или приглашений в комнату.
- ❌ Не тронут `wowlance/settings.py`.

## Как проверить локально

### Автоматические тесты
```powershell
python manage.py test apps.profiles -v 2
python manage.py test
```