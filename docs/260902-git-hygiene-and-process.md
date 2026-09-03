# Задание: гигиена git и правила интеграции

| Поле | Значение |
|------|----------|
| **Статус** | К исполнению |
| **Дата** | 2026-09-02 |
| **Кто** | **Тимлид** делает A и C (ветки, stash, merge). Разработчики 1 и 2 только читают §B и кладут новый промпт в Cursor. ИИ **не** удаляет remote-ветки и **не** делает force-push |
| **Ветка** | Правки документов — `chore/git-process` от свежего `master`. Удаление веток — с `master`, отдельным шагом, не в том же PR что продукт |
| **Связано** | Разбор истории 02.09; [PROJECT_FOR_TEAM](PROJECT_FOR_TEAM.md) §7 и §9; [ADR-001](ADR-001-monolith-modules.md) |

**Зачем.** История после ADR читается, фичи идут ветками. Ломает сопровождение не «плохой код», а обряд вливания: на `master` лежат коммиты `18` / `19` / `20`, почти нет GitHub PR, ~40 мёртвых веток, 6 stash. Через месяц нельзя найти DT-чат по `git log --grep`. Два разработчика + ИИ будут конфликтовать о ветки-зомби и о простынях тестов в одном коммите.

**Не цель этого задания:** переписать уже смерженный `master`, урезать тесты ([отдельный тикет](260901-test-suite-hard-cut.md)), чинить продукт (обзор фрилансера, media, HTTPS).

**Жёсткий запрет:** `git push --force` в `master` / `main`. Нумерацию `1`…`20` в истории **не** переименовывать amend’ом уже запушенных коммитов. Правим процесс вперёд.

---

## A. Сейчас: убрать шум (тимлид, 0.5–1 час)

### A1. Stash — разобрать вручную, не пачкой

Сейчас 6 записей. Открыть каждую (`git stash show -p stash@{N}`).

| stash | Действие по умолчанию |
|-------|------------------------|
| `wip director-teamlead-comms` ×3 | DT-чат уже в master. Если diff не даёт нового — `git stash drop` |
| `wip profiles card + demo_content + 260901 comms task` | Если есть незакоммиченный кусок карточки/доков — вынести в ветку `chore/from-stash-…` или в существующую фичу. Иначе drop |
| `wip before merging intern PRs` | Скорее всего пусто после merge. Проверить, drop |
| `wip teamlead seed and labels` | Seed уже в master. Проверить, drop |

Не оставлять stash как «парковку» на дни. Не `git stash pop` на чужую ветку вслепую.

### A2. Удалить влитые ветки

Удалять **только** полностью влитые в `origin/master` (`git branch -r --merged origin/master`).

**Не удалять без отдельного решения:**

- `master` / `origin/master`
- `origin/dev_eubog` — ветка стажёра, в ней 8 уникальных коммитов (`update`, README, SMTP-черновик). Часть уже портирована, но это его архив
- локальную `fix/email-smtp-ready` и `dev_eubog`, пока стажёр не скажет «не нужна»
- любую ветку, на которой **сейчас** пишут фичу (не в списке `--merged`)

**Remote, можно снести** (на 02.09 все `--merged origin/master`; перед командой ещё раз проверить):

`Role_not_double_check`, `biz/verified-filter`, `biz/video`, `card_freelancer`, `chore/test-suite-hard-cut`, `feature/demo-manager-seed-and-moderation-admin`, `feature/director-overview-readonly-ops-to-teamlead`, `feature/dt-comms-header-all-tabs`, `feature/email-smtp-onto-master`, `feature/freelancer-accruals`, `feature/freelancer-overview-ui`, `feature/freelancer-project-stats`, `feature/freelancer-room-cleanup`, `feature/linkedin-package-kpi-target`, `feature/room-project-paid-stub`, `feature/room-rbac-by-role`, `feature/room-six-tabs`, `feature/staffing-auto-assign-on-slot-create`, `feature/staffing-data-foundation`, `feature/staffing-matching-engine`, `feature/staffing-workflow-ui`, `feature/teamlead-period-report`, `feature/wowtalent-ref-stub`, `fix/catalog-video-checkbox`, `fix/demo-hardening-risks`, `fix/freelancer-hide-economics`, `fix/logout-post-local-assets`, `fix/profiles-room-dependency`, `fix/teamlead-count-max-one`, `leads_metrics`, `light_color`, `profiles`, `tasks_leads`, `test/lite-optimize`, `tests`, `users`, `visual-1.0`

Плюс **дубли onto-master** (не `--merged`, но те же сообщения уже на master — `Add room chat`, `Complete room tabs…`). Перед удалением: `git log master..origin/<ветка> --oneline` — только старые «Add …». Если вдруг есть коммит без пары на master — **не удалять**, написать тимлиду.

Кандидаты-дубли: `origin/feature/functional-role-configurator`, `…-ui`, `…-slot-projection`, `origin/feature/room-automation-sla`, `origin/feature/room-comms`, `origin/feature/room-issue11-tabs-completion`.

Локально то же: `git branch --merged master`, минус `master` и минус `dev_eubog` / `fix/email-smtp-ready`. Ветки `frelance_*`, `develop` (висит на Initial commit), `review-*`, `room`, `https` — удалить, если `--merged`.

```powershell
# пример одной remote-ветки; повторить по списку
git push origin --delete feature/room-six-tabs
git branch -d feature/room-six-tabs
```

На origin — пачками по 5–10, не одной простынёй в 40 имён, чтобы легко откатить опечатку.

### A3. Имена веток вперёд

Только `feature/<short-name>`, `fix/<short-name>`, `chore/<short-name>`.  
Запрещены: `frelance_*`, `visual-1.0`, `Role_not_double_check`, голые `users` / `tests` / `room`, суффикс `-onto-master` (не копировать историю фичи второй раз — rebase/cherry-pick в рабочую ветку от свежего master).

---

## B. Правила интеграции (документировать и соблюдать)

Влить в `docs/PROJECT_FOR_TEAM.md` §7, заменив нынешний короткий список. В §9 (промпт ИИ) добавить блок «Git».

### B1. Что запрещено на `master`

- Прямой push разработчиков. Тимлид — только merge/PR.
- Subject из одних цифр (`20`) или `update` / `update views.py`.
- Fast-forward обезличенного чекпоинта Cursor. Перед вливанием: осмысленный squash или один merge-коммит с фразой «что и зачем».
- `git commit --amend` уже запушенного в `origin/master`.
- Фича + простыня из 50 тестов в **одном** коммите. Минимум два: код продукта, затем тесты (или наоборот), оба с понятным subject.

### B2. Как вливать

1. Ветка от `origin/master`: `feature/…` или `fix/…`.
2. Коммиты: английский или русский, но **одно предложение про смысл** (`Hide catalog from freelancer`, не `19`).
3. Открыть **GitHub Pull Request** (хотя бы draft). Review тимлида. Merge через GitHub **или** локально `git merge --no-ff -m "Merge …"` с тем же смыслом, что title PR.
4. После merge — удалить ветку на origin (галка GitHub или `git push origin --delete`).
5. Пока ветка не влита, не плодить вторую копию «onto-master».

Первый PR после этого тикета может быть как раз `chore/git-process` (только docs). Цель — чтобы у команды появился привычный URL review, не идеальный CI.

### B3. Промпт ИИ (добавить в §9)

```text
Git:
- Не коммить в master. Ветка feature/… или fix/… от свежего origin/master.
- Сообщение коммита — одно предложение, что изменилось для продукта. Запрещены: цифры (20), update, WIP, «fixes».
- Не делай git push --force, не переписывай origin/master.
- Продуктовый код и большая пачка тестов — разные коммиты.
- Не создавай второй репозиторий истории (ветка *-onto-master с копией тех же коммитов).
```

Стажёру в личный чеклист: не пушить `update views.py`; если Cursor предложил checkpoint `21` — переименовать до push.

### B4. Продуктовые решения не через три merge

Замечание про Обзор фрилансера (состав вернули и убрали в один день) — не git-баг. Правило: спорный UI фиксируется **абзацем в docs** (срез ролей / тикет) **до** второй ветки на ту же поверхность. Тимлид не мержит третью правку «как на созвоне показалось», пока в docs не одна формулировка.

Это одна строка в §7, без нового ADR.

---

## C. Приёмка

- На origin нет влитых `feature/*` / `fix/*` / опечаточных веток из списка A2 (кроме явно сохранённых).
- Локально `git branch` — `master` + текущие незакрытые задачи + `dev_eubog`, пока стажёр не отпустит.
- `git stash list` пустой **или** в нём только то, что тимлид письменно оставил с датой «зачем».
- `PROJECT_FOR_TEAM.md` §7 и §9 содержат запрет цифр на master, PR, два коммита на фичу+тесты, запрет force-push.
- `git log origin/master -5 --oneline` после следующих фич — ни одного subject вида `21`.
- История `master` **не** переписана (старые `18`/`19`/`20` остаются).

---

## D. Промпт тимлиду (документы)

```text
Прочитай docs/260902-git-hygiene-and-process.md.
Сделай ветку chore/git-process только под правки docs/PROJECT_FOR_TEAM.md (§7 Git-процесс и §9 промпт).
Не трогай Python, шаблоны, тесты. Не делай force-push. Не удаляй ветки из этой ветки — удаление веток тимлид делает руками по разделу A.
После: diff только markdown, как проверить: открыть §7 и увидеть запрет коммитов-цифр и шаг PR.
```

Удаление веток и stash **не** поручать ИИ одним промптом «почисти git».
