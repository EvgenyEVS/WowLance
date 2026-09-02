# WowLance — описание проекта для команды

Документ для разработчиков и для составления промптов в Cursor/ChatGPT.  
Перед задачей: прочитай нужный раздел и приложи в промпт путь к файлам/app.

| | |
|---|---|
| **Репозиторий** | https://github.com/EvgenyEVS/WowLance |
| **Основная ветка** | `master` |
| **Демо** | http://195.19.209.121/ |
| **Язык UI** | русский |
| **Архитектура (ADR)** | [`docs/ADR-001-monolith-modules.md`](ADR-001-monolith-modules.md) |

**Обязательно к прочтению:** [ADR-001 — монолит и границы модулей](ADR-001-monolith-modules.md).  
Этот файл — онбординг и шпаргалка; ADR — зафиксированные архитектурные решения. ADR **не заменяет** этот документ.

---

## 1. Что это за продукт

**WowLance** — B2B-платформа запуска продаж «в несколько кликов». С точки зрения пользователя — **два микропродукта** в одной платформе:

| Микропродукт | Смысл | Apps |
|---|---|---|
| **WOWLANCE BIZ** (биржа) | Профили и каталог фрилансеров-сейлов, регистрация, referral | `users`, `profiles` |
| **WOWLANCE ROOM** (комната) | Проект, команда, задачи, отчёты, лиды, материалы | `rooms`, `pipeline` |

> **BIZ** = биржа (marketplace / talent), **не** «блок бизнес-логики».

Продуктовая спецификация UX/потоков — правки Светланы (2026-08-17). Реализация — **modular monolith** (Django), без отдельных микросервисов на текущем этапе. Подробности — в ADR-001.

### Типовой путь

1. **Директор** выбирает архитектуру продаж (пресет) / проходит wizard → оплата (stub → staffing).  
2. Создаются **Project** и **Room**.  
3. **Тимлид** набирает команду из каталога BIZ (invite → подтверждение).  
4. **Задачи → отчёты (со скринами) → лиды** (Cold/Warm/Hot).  
5. **Hot-лид** → менеджер платформы (`/manager/inbox/`).

Оплата в текущем коде — заглушка запуска комнаты; целевая точка входа по ADR — `rooms.services.handle_project_paid` (без брокера сообщений).  
Почта в демо — `console` backend (ссылка активации на экране при `DEBUG=True`).

### Роли (`apps.users.models.User.Roles`)

| Роль | Код | Основное |
|------|-----|----------|
| Директор | `director` | проекты, wizard, оплата/запуск комнаты, обзор |
| Тимлид | `teamlead` | команда, invite, задачи, review отчётов, квалификация лидов |
| Менеджер | `manager` | inbox Hot-лидов |
| Фрилансер | `freelancer` | профиль (видео), задачи, отчёты, лиды |
| Админ | `admin` | админка Django |

Статусы аккаунта: `pending` → `active` / `blocked`.  
Логин по **email** (`USERNAME_FIELD = 'email'`).

### Главный пользовательский путь

```
Лендинг → «Применить архитектуру»
  → регистрация директора (если гость)
  → /setup/ (wizard)
  → Project + оплата/launch (stub → handle_project_paid) → Room (staffing)
  → тимлид приглашает фрилансеров из /freelancers/
  → задачи / отчёты / лиды
  → Hot → /manager/inbox/
```

Фрилансер: регистрация (прямая или `?ref=wowtalent_…`) → профиль с **видео** → каталог → invite в комнату.

---

## 2. Архитектура для разработчика (кратко)

Полный текст: **[ADR-001](ADR-001-monolith-modules.md)**.

### Модули

| Модуль | Apps | Не делать |
|--------|------|-----------|
| **BIZ** | `users`, `profiles` | Не импортировать `rooms` / `pipeline` |
| **ROOM** | `rooms` + `pipeline` (один контекст в MVP) | Не сплитить на микросервисы |
| **Shell** | `core` | Не класть доменную логику BIZ/ROOM |
| **Payments** | внутри `rooms` (или app `payments` позже) | Не поднимать RabbitMQ/Kafka |

### Правила зависимостей

```text
profiles / users  ──X──▶  rooms / pipeline     ЗАПРЕЩЕНО
pipeline          ────▶  rooms.services        доступ к проекту/правам — через фасад
core              ────▶  users / rooms / …     UI composition root
все               ────▶  users.User
```

- Invite «в комнату» из каталога — **view в `apps.rooms`** (например `rooms:catalog_add_to_room` + `freelancer_id`), не логика в `profiles`.
- FK `Task`/`Lead` → `rooms.Project` — ок; правила доступа не дублировать, использовать `rooms.services`.
- WOW Talent: клиент-stub в BIZ (`wowtalent_client.py`).  
- Чат MVP: HTMX polling; видео MVP: ссылка Jitsi (`meet.jit.si/wowlance-room-<uuid>` и аналоги).

### DoD для PR (дополнительно к коду)

- Не нарушены границы модулей (BIZ ↛ ROOM).  
- Бизнес-логика в `services.py`, UI на русском.  
- **Зелёный полный `python manage.py test`.** Прогон части suite (в том числе через `scripts/test_lite.*`) не делает PR готовым.
- Есть тест или явный ручной чеклист для QA.  
- В описании PR — как проверить локально.

### Политика тестов (против test sprawl)

Suite держим маленьким и быстрым: 15 файлов `tests*.py`, 161 тест, ~4 с.

- **Максимум 2 теста на фичу**: один на доменный инвариант (service) плюс один на HTTP-доступ/видимость — и только если нужны оба.
- **Матрицу ролей** проверяем одним методом через `subTest`, а не отдельным тестом на каждую роль.
- **Новый `tests_*.py`** заводится только под новый bounded context.
- **ROOM UI/RBAC** идут в `apps/rooms/tests_room_rbac.py`; новые `issue*`/`ui`/`tabs`-файлы не создаём.
- **Третий тест на тот же POST** не добавляем, если он не защищает новый доменный инвариант.

Запрещённые regression-паттерны:

- `inspect.getsource` / `inspect.signature`;
- разбор AST тела функций (в т.ч. «импорт лежит внутри функции»);
- `makemigrations --check` как продуктовый тест;
- `assertNumQueries`, `CaptureQueriesContext`;
- SQL-строки: `str(qs.query)`, проверки `WHERE` / `NOT EXISTS`;
- assertions по CSS-классам и парсинг разметки регулярками;
- «плейсхолдер исчез»;
- точное `HH:MM:SS` и отрендеренная дата (`strftime('%d.%m.%Y %H:%M')`);
- точные подписи кнопок и длинные `assertContains` по русской копии — если то же самое проверяется через URL, `response.context` или доменное состояние;
- проверки содержимого docstring.

AST остаётся допустим только для границ импорта модулей — `apps/profiles/tests_boundaries.py` (`ast.Import` / `ast.ImportFrom`).

---

## 3. Технологии (стек)

### Backend
- **Python 3.12+** (на демо-сервере встречается 3.14)
- **Django 6.1**
- **SQLite** (`db.sqlite3`) — локально и на демо-VPS
- Auth: кастомный `User` (`AUTH_USER_MODEL = 'users.User'`)
- UUID primary keys у доменных сущностей
- Forms + server-rendered views (не DRF/React)
- Management command: `python manage.py seed_freelancers`

### Frontend
- Django Templates (`templates/` + `apps/*/templates/`)
- CSS: один файл `static/css/style.css` (CSS-переменные, светлая тема)
- Шрифты: Google Fonts — **Syne** + **DM Sans**
- **HTMX** 2.x (подключён в `templates/base.html`; использовать точечно, в т.ч. polling чата в MVP)
- Без React/Vue/SPA (без нового ADR)

### Инфра демо
- Ubuntu VPS, **nginx** → **gunicorn** (`127.0.0.1:8000`)
- systemd unit: `wowlance.service`
- Код на сервере: `/var/www/wowlance`
- Сейчас сайт по **HTTP** (TLS нет). Ссылки активации должны быть `http://…`
- Env-флаги в `wowlance/settings.py`:
  - `PUBLIC_HOST` (по умолчанию `195.19.209.121`)
  - `PUBLIC_SCHEME` (`http` / `https`)
  - `USE_HTTPS` (`0`/`1`)
  - `DJANGO_DEBUG`, `DJANGO_SECRET_KEY`

### Зависимости (типичный набор)
```text
Django==6.1
gunicorn
whitenoise   # на сервере для static (может быть только на VPS)
```
Отдельного `requirements.txt` в корне может не быть — при онбординге зафиксировать и добавить.

### Тесты
- Django TestCase в `apps/*/tests*.py` — 15 файлов, 161 тест, ~4 с
- Хелперы: `apps/test_helpers.py`
- Запуск: `python manage.py test` — всегда целиком
- `scripts/test_lite.ps1` / `scripts/test_lite.sh` — совместимые алиасы того же полного прогона; быстрого subset больше нет
- Что можно и чего нельзя писать в тестах — раздел «Политика тестов»

---

## 4. Структура репозитория

```text
WowLance/
├── manage.py
├── db.sqlite3
├── docs/
│   ├── PROJECT_FOR_TEAM.md      # этот файл
│   └── ADR-001-monolith-modules.md
├── wowlance/                    # project package
│   ├── settings.py
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
├── apps/
│   ├── core/                    # Shell: лендинг, legal, дашборды, absolute_uri
│   ├── users/                   # BIZ: регистрация, логин, активация
│   ├── profiles/                # BIZ: профиль, каталог, портфолио
│   ├── rooms/                   # ROOM: Project, Room, команда, wizard, оплата-событие
│   ├── pipeline/                # ROOM: Task, Report, Lead, manager inbox
│   └── test_helpers.py
├── templates/
│   └── base.html
├── static/
│   └── css/style.css
├── media/
└── deploy/
    └── nginx-https.example.conf
```

### Apps — ответственность

#### `apps.core` (Shell)
- `/` — лендинг с архитектурами продаж  
- `/about/`, `/privacy/`, `/terms/`  
- Ролевые дашборды (director / teamlead / freelancer / manager)  
- `apps/core/absolute_uri.py` — абсолютные URL (**уважает `PUBLIC_SCHEME`**)

#### `apps.users` (BIZ)
- `/register/`, `/login/`, `/logout/`  
- `/activate/<uidb64>/<token>/`, `/resend-activation/`  
- Модель `User`; далее — referral / `wowtalent_client` (stub)

#### `apps.profiles` (BIZ)
- `/freelancers/` — каталог  
- `/freelancer/<uuid>/` — карточка (видео, skills, LinkedIn)  
- `/profile/edit/`, портфолио  
- `seed_freelancers`, `card.py`  
- **Не** содержит логику добавления в комнату (только ссылка/form action на URL в `rooms`)

#### `apps.rooms` (ROOM)
- `/apply-architecture/`, `/setup/` — wizard  
- `/projects/…`, `/projects/<id>/room/` — обзор, документы, команда  
- Invite тимлида; **добавление фрилансера в комнату** (в т.ч. из каталога)  
- `handle_project_paid` (целевая точка после оплаты)  
- Модели: `Project`, `Room`, `RoomMember`, `RoomDocument`, `RoomActivity`, `TeamleadInvite`  
- `presets.py`, `services.py`

#### `apps.pipeline` (ROOM)
- Задачи и отчёты  
- Лиды Cold/Warm/Hot, `/manager/inbox/`  
- Модели: `Task`, `Report`, `Lead`, `LeadStatusHistory`  
- Доступ к проекту/правам — через `rooms.services`

---

## 5. Доменная модель (кратко)

```text
User (role)
  └─ FreelancerProfile ── Portfolio ── PortfolioItem     ← BIZ

User(director) ── owns ── Project ── 1:1 ── Room          ← ROOM
                              │
                              ├─ RoomMember
                              ├─ RoomDocument
                              ├─ RoomActivity
                              ├─ TeamleadInvite
                              ├─ Task ── Report
                              └─ Lead ── LeadStatusHistory
                                         (Hot → inbox менеджеру)
```

Бизнес-логику класть в `services.py` app’а, не раздувать views.

---

## 6. Как поднять локально

```powershell
git clone https://github.com/EvgenyEVS/WowLance.git
cd WowLance
python -m venv .venv
.\.venv\Scripts\activate
pip install "Django==6.1"
python manage.py migrate
python manage.py seed_freelancers
python manage.py runserver
```

Открыть: http://127.0.0.1:8000/

**Важно для локалки:**  
`PUBLIC_HOST` по умолчанию указывает на IP демо. Для абсолютных ссылок активации локально:

```powershell
$env:PUBLIC_HOST="127.0.0.1:8000"
$env:PUBLIC_SCHEME="http"
```

---

## 7. Git-процесс команды

1. Collaborators с правом **Write** на репозиторий.  
2. Не пушить напрямую в `master`.  
3. Ветка: `feature/<short-name>`, `fix/<short-name>`.  
4. Перед стартом: `git checkout master && git pull origin master`.  
5. PR → review тимлида → merge в `master`.  
6. Деплой на VPS — только по согласованию с тимлидом (сейчас ручной `git pull` на сервере).  
7. Архитектурные споры («микросервисы?», «новый стек?») — сначала ADR, не «втихаря в PR».

---

## 8. Известные ограничения / долг

- Нет реального SMTP (activation через DEBUG-ссылку).  
- Нет HTTPS на демо-IP (ссылки должны быть `http://`).  
- SQLite + gunicorn: на проде предпочтительно `--workers 1`.  
- Оплата: stub; целевой API — `handle_project_paid` (см. ADR).  
- В коде ещё возможна связь `profiles → rooms` при invite из каталога — **техдолг фазы A** (перенос в `rooms`).  
- Полноценные Deal / SLA-таймеры / realtime-чат / боевой WOW Talent / live Stripe — не текущий минимум без явного scope.  
- `STATIC_ROOT` / WhiteNoise могут отличаться local vs server.  
- На сервере возможны локальные правки `settings.py` — перед `git pull` проверять конфликты.

---

## 9. Шаблон промпта для AI (копировать)

```text
Ты работаешь в репозитории WowLance (Django 6.1, server-rendered templates + static/css/style.css).

Обязательный контекст:
- Прочитай docs/ADR-001-monolith-modules.md и docs/PROJECT_FOR_TEAM.md.
- Modular monolith: BIZ = users+profiles (биржа), ROOM = rooms+pipeline (комната).
- Запрещено: profiles/users импортируют rooms/pipeline; микросервисы; React/DRF без явной просьбы.
- Invite в комнату — только через views/services apps.rooms.
- Оплата/ProjectPaid — rooms.services.handle_project_paid (без брокера); WOW Talent — stub-клиент в BIZ.
- UI на русском. CSS variables в static/css/style.css.

Задача:
<чётко что сделать>

Ограничения:
- Меняй только нужные файлы.
- Бизнес-логику в apps/<app>/services.py.
- Не коммить секреты; не ломай PUBLIC_SCHEME=http для демо без TLS.
- После изменений: какие файлы тронул, как протестировать вручную.

Релевантные пути (заполни):
- ...
```

### Примеры коротких промптов

**Фича в комнате**
> В `apps/rooms` (и при необходимости `pipeline`) добавь … Соблюдай ADR-001. Не импортируй rooms из profiles. Обнови шаблон и тест.

**Карточка / каталог фрилансера**
> Правь только `apps/profiles` (+ CSS). Кнопка «Пригласить» — form/link на URL в `rooms`, без импорта моделей Room/Project.

**Оплата / staffing**
> Реализуй или вызови `handle_project_paid` в `apps.rooms.services`. Без RabbitMQ/Kafka.

**Активация / URL**
> Абсолютные ссылки только через `apps.core.absolute_uri.absolute_uri`. Уважай `PUBLIC_SCHEME` и `PUBLIC_HOST`.

---

## 10. Полезные URL (демо/локально)

| URL | Назначение |
|-----|------------|
| `/` | Лендинг / архитектуры |
| `/register/?role=director` | Регистрация директора |
| `/register/?role=freelancer` | Регистрация фрилансера |
| `/setup/` | Wizard (только director) |
| `/projects/` | Список проектов |
| `/freelancers/` | Каталог (BIZ) |
| `/manager/inbox/` | Hot-лиды (manager) |
| `/admin/` | Django admin |

Демо-фрилансеры после seed: `anna.sokolova@wowlance.demo` и др., пароль `DemoPass123!`.

---

## 11. Контакты процесса

| Роль | Зона |
|------|------|
| **Тимлид / PM** | задачи, scope MVP, review PR, merge, деплой, ADR |
| **Python (BIZ)** | `users`, `profiles`, referral/видео/каталог |
| **Python (ROOM)** | `rooms`, `pipeline`, оплата-событие, invite, вкладки комнаты |
| **QA** | чеклисты ролей, смоук, регресс перед демо |

Разработчики — ветки + PR, без прямого пуша в `master` и без правок продакшен-сервера без согласования.

---

*Обновлено: 2026-08-17 — модули BIZ/ROOM, ссылка на ADR-001, правила зависимостей, промпты под монолит. При смене архитектуры обновлять этот файл и ADR в одном PR.*
