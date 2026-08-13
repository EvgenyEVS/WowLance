# WowLance — описание проекта для команды

Документ для разработчиков и для составления промптов в Cursor/ChatGPT.  
Перед задачей: прочитай нужный раздел и приложи в промпт путь к файлам/app.

| | |
|---|---|
| **Репозиторий** | https://github.com/EvgenyEVS/WowLance |
| **Основная ветка** | `master` |
| **Демо** | http://195.19.209.121/ |
| **Язык UI** | русский |

---

## 1. Что это за продукт

**WowLance** — B2B-платформа запуска продаж «в несколько кликов»:

1. **Директор** выбирает архитектуру продаж (пресет) или собирает проект с нуля.  
2. Создаётся **проект** и **комната** (рабочее пространство).  
3. В комнату нанимаются **тимлид** и **фрилансеры-сейлы**.  
4. Идут **задачи → отчёты (со скринами) → лиды** (Cold/Warm/Hot).  
5. **Hot-лид** уходит менеджеру платформы.

Оплата в MVP — заглушка (запуск комнаты без платёжного шлюза).  
Почта в демо — `console` backend (ссылка активации на экране при `DEBUG=True`).

### Роли (`apps.users.models.User.Roles`)

| Роль | Код | Основное |
|------|-----|----------|
| Директор | `director` | проекты, wizard, запуск комнаты, staffing |
| Тимлид | `teamlead` | команда, задачи, review отчётов |
| Менеджер | `manager` | inbox Hot-лидов |
| Фрилансер | `freelancer` | профиль, задачи, отчёты, лиды |
| Админ | `admin` | админка Django |

Статусы аккаунта: `pending` → `active` / `blocked`.  
Логин по **email** (`USERNAME_FIELD = 'email'`).

### Главный пользовательский путь

```
Лендинг → «Применить архитектуру»
  → регистрация директора (если гость)
  → /setup/ (wizard 3 шага)
  → черновик Project → launch → Room
  → команда (тимлид + фрилансеры из каталога)
  → задачи / отчёты / лиды
  → Hot → /manager/inbox/
```

---

## 2. Технологии (стек)

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
- **HTMX** 2.x (подключён в `templates/base.html`; использовать точечно)
- Без React/Vue/SPA

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
- Django TestCase в `apps/*/tests*.py`
- Хелперы: `apps/test_helpers.py`
- Запуск: `python manage.py test`

---

## 3. Структура репозитория

```text
WowLance/
├── manage.py
├── db.sqlite3                 # локальная БД (не коммитить секреты/прод-дампы)
├── wowlance/                  # project package
│   ├── settings.py
│   ├── urls.py                # include всех apps
│   ├── wsgi.py
│   └── asgi.py
├── apps/
│   ├── core/                  # лендинг, about/legal, absolute_uri, дашборды
│   ├── users/                 # регистрация, логин, активация email
│   ├── profiles/              # профиль/каталог/портфолио фрилансера
│   ├── rooms/                 # Project, Room, команда, документы, wizard
│   ├── pipeline/              # Task, Report, Lead, manager inbox
│   └── test_helpers.py
├── templates/
│   └── base.html              # общий layout
├── static/
│   └── css/style.css          # дизайн-система
├── media/                     # загруженные файлы (локально)
└── deploy/
    └── nginx-https.example.conf
```

### Apps — ответственность

#### `apps.core`
- `/` — лендинг с архитектурами продаж  
- `/about/`, `/privacy/`, `/terms/`  
- Ролевые дашборды (шаблоны director/teamlead/freelancer/manager)  
- `apps/core/absolute_uri.py` — абсолютные URL для писем/инвайтов (**уважает `PUBLIC_SCHEME`**)

#### `apps.users`
- `/register/`, `/login/`, `/logout/`  
- `/activate/<uidb64>/<token>/`  
- `/resend-activation/`  
- Модель `User`, токены активации, console-email  

#### `apps.profiles`
- `/freelancers/` — каталог (baseball-карточки)  
- `/freelancer/<uuid>/` — детальная карточка (видео, skills, LinkedIn)  
- `/profile/edit/`, портфолио upload/links  
- `seed_freelancers` — демо-аккаунты `*@wowlance.demo` / `DemoPass123!`  
- `card.py` — YouTube/Vimeo embed helpers  

#### `apps.rooms`
- `/apply-architecture/`, `/setup/` — Apply Architecture + 3-step wizard  
- `/projects/…` — список/создание/детали/launch  
- `/projects/<id>/room/` — overview, documents, team  
- Invite тимлида, add freelancer, «В комнату» из каталога  
- Модели: `Project`, `Room`, `RoomMember`, `RoomDocument`, `RoomActivity`, `TeamleadInvite`  
- Пресеты: `presets.py` (cold_calling, linkedin, scaleup)  

#### `apps.pipeline`
- Задачи и отчёты в комнате  
- Лиды + квалификация Cold/Warm/Hot  
- `/manager/inbox/`  
- Модели: `Task`, `Report`, `Lead`, `LeadStatusHistory`  

---

## 4. Доменная модель (кратко)

```text
User (role)
  └─ FreelancerProfile ── Portfolio ── PortfolioItem

User(director) ── owns ── Project ── 1:1 ── Room
                              │
                              ├─ RoomMember (director/teamlead/freelancer)
                              ├─ RoomDocument
                              ├─ RoomActivity
                              ├─ TeamleadInvite
                              ├─ Task ── Report
                              └─ Lead ── LeadStatusHistory
                                         (Hot → задача/inbox менеджеру)
```

Бизнес-логику по возможности класть в `services.py` app’а, не раздувать views.

---

## 5. Как поднять локально

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
`PUBLIC_HOST` по умолчанию указывает на IP демо. Для абсолютных ссылок активации локально либо не опираться на прод-хост, либо задать:

```powershell
$env:PUBLIC_HOST="127.0.0.1:8000"
$env:PUBLIC_SCHEME="http"
```

(при необходимости поправить `absolute_uri` / hosts под локальный сценарий).

---

## 6. Git-процесс команды

1. Collaborators с правом **Write** на репозиторий.  
2. Не пушить напрямую в `master`.  
3. Ветка: `feature/<short-name>`, `fix/<short-name>`.  
4. Перед стартом: `git checkout master && git pull origin master`.  
5. PR → review тимлида → merge в `master`.  
6. Деплой на VPS — только по согласованию с тимлидом (сейчас ручной `git pull` на сервере).

---

## 7. Известные ограничения / долг

- Нет реального SMTP (activation через DEBUG-ссылку).  
- Нет HTTPS на демо-IP (ссылки должны быть `http://`).  
- SQLite + gunicorn: на проде предпочтительно `--workers 1`.  
- Оплата / полноценные Deal / жёсткий SLA — ещё не «боевой» уровень по полному ТЗ.  
- `STATIC_ROOT` / WhiteNoise могут отличаться local vs server.  
- На сервере возможны локальные правки `settings.py` — перед `git pull` проверять конфликты.

---

## 8. Шаблон промпта для AI (копировать)

```text
Ты работаешь в репозитории WowLance (Django 6.1, server-rendered templates + static/css/style.css).

Контекст продукта:
- B2B sales marketplace: director → project/room → teamlead/freelancers → tasks/reports → leads Hot → manager.
- Apps: core, users, profiles, rooms, pipeline.
- Не вводи React/DRF без явной просьбы. UI на русском. Сохраняй текущий визуальный язык (CSS variables в static/css/style.css).

Задача:
<чётко что сделать>

Ограничения:
- Меняй только нужные файлы.
- Бизнес-логику по возможности в apps/<app>/services.py.
- Не коммить секреты, не ломай PUBLIC_SCHEME=http для демо без TLS.
- После изменений укажи: какие файлы тронул, как протестировать вручную.

Релевантные пути (заполни):
- ...
```

### Примеры коротких промптов

**Фича в комнате**
> В `apps/rooms` добавь … Не меняй pipeline. Обнови шаблон `room_overview.html` и при необходимости CSS. Напиши тест в `apps/rooms/tests.py`.

**Карточка фрилансера**
> Правь `apps/profiles` и `static/css/style.css`. Сохрани baseball/freelancer-card layout. Не трогай rooms/pipeline.

**Активация / URL**
> Абсолютные ссылки только через `apps.core.absolute_uri.absolute_uri`. Уважай `PUBLIC_SCHEME` и `PUBLIC_HOST` из settings.

---

## 9. Полезные URL (демо/локально)

| URL | Назначение |
|-----|------------|
| `/` | Лендинг / архитектуры |
| `/register/?role=director` | Регистрация директора |
| `/register/?role=freelancer` | Регистрация фрилансера |
| `/setup/` | Wizard (только director) |
| `/projects/` | Список проектов |
| `/freelancers/` | Каталог |
| `/manager/inbox/` | Hot-лиды (manager) |
| `/admin/` | Django admin |

Демо-фрилансеры после seed: `anna.sokolova@wowlance.demo` и др., пароль `DemoPass123!`.

---

## 10. Контакты процесса

- **Тимлид / PM** — постановка задач, review PR, merge в `master`, деплой.  
- Разработчики — ветки + PR, без прямого пуша в `master` и без правок продакшен-сервера без согласования.

*Документ актуален на момент создания под текущий `master`. При крупных изменениях архитектуры — обновлять этот файл в том же PR.*
