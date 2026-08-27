"""Состав функциональных ролей проекта и его юнит-экономика.

Три источника истины и их границы
---------------------------------

1. **Структура** — `apps.rooms.functional_roles` (код): какие функции
   существуют, их грейд, канал и `is_fixed`.
2. **Актуальные бизнес-значения** — `rooms.FunctionalRoleConfig` (БД):
   цена, часы, продуктивность, Hot-лиды. Их правит администратор.
3. **Экономика конкретного проекта** — снапшот в
   `Project.input_data['functional_roles']`.

Зачем снапшот
-------------

Если бы summary считалась по текущему каталогу, завтрашняя правка цены в
админке молча переписала бы экономику всех уже согласованных проектов.
Поэтому при каждом явном сохранении состава актуальные бизнес-значения
копируются в проект, а summary считается **только** из этой копии.
Новые значения попадут в проект при следующем явном сохранении состава
(или применении пакета). Массового ретроактивного пересчёта нет и не должно
быть.

Чего здесь нет
--------------

* `RoomFunctionSlot` не создаётся и не меняется **этим модулем**: слоты
  приводит к составу `apps.rooms.staffing.projection`, а держит их в одной
  транзакции с составом `apps.rooms.services.save_functional_roles_and_sync_slots`.
  Проецируются только `seller_middle`, `seller_senior` и `linkedin_leadgen`;
  `teamlead` (ручной инвайт) и `database_assistant` (канал `base`, которого
  нет в enum слота) слотов не получают — см. docstring проекции;
* числовой «охват / производительность» не считается: `productivity_text`
  остаётся текстом до переработки этой математики Product Owner;
* побочных записей в БД при чтении summary не происходит.
"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.users.models import User

from . import functional_roles, presets
from .functional_roles import FunctionalRole
from .models import FunctionalRoleConfig, Project

__all__ = [
    'COMPOSITION_EDITABLE_STATUSES',
    'FUNCTIONAL_ROLES_KEY',
    'FunctionalRolesError',
    'UnitEconomicsRow',
    'UnitEconomicsSummary',
    'apply_package_to_project',
    'get_project_composition',
    'get_unit_economics_summary',
    'update_project_functional_roles',
    'user_can_edit_functional_roles',
    'user_can_view_unit_economics_finance',
]

#: Ключ снапшота внутри `Project.input_data`.
#:
#: Значение по этому ключу — **список** строк состава (контракт Issue #11),
#: без объемлющего объекта с версией: каждая строка самодостаточна и несёт
#: собственный `id`, поэтому обёртка ничего бы не добавляла.
FUNCTIONAL_ROLES_KEY = 'functional_roles'

#: Статусы, в которых состав команды ещё можно менять.
#:
#: Набор намеренно объявлен отдельно от `staffing.STAFFING_MUTABLE_STATUSES`,
#: хотя значения сейчас совпадают: это разные бизнес-правила (что покупает
#: директор vs кого назначает тимлид), и их совпадение — не инвариант.
COMPOSITION_EDITABLE_STATUSES = frozenset({
    Project.Status.DRAFT,
    Project.Status.STAFFING,
})

#: Денежная точность: рубли с копейками, без float.
MONEY_QUANTUM = Decimal('0.01')


class FunctionalRolesError(ValueError):
    """Состав нельзя сохранить: данные или состояние проекта не позволяют.

    Права — отдельный случай: они поднимают `PermissionDenied`, чтобы
    будущий view отдал 403 существующим механизмом.
    """


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def user_can_edit_functional_roles(user, project: Project) -> bool:
    """Кто вправе менять состав функциональных ролей проекта.

    Оба условия обязательны: пользователь — владелец **этого** проекта
    и его роль — директор. Отсюда следует, что чужой директор состав не
    правит, а владелец с не-директорской ролью — тоже.

    `user_can_manage_team` здесь сознательно **не** переиспользуется: он
    разрешает операции тимлиду, а тимлид управляет назначением людей на уже
    купленные слоты, но не тем, что и на какую сумму куплено. Менеджер и
    фрилансер состав только читают.

    `User.Roles.ADMIN` продуктового права на состав **не даёт**: платформенная
    роль обслуживает систему, а покупает команду директор. Доступ
    администратора к Django admin (в том числе к каталогу
    `FunctionalRoleConfig`) это правило не затрагивает — там действуют
    обычные права `is_staff` / `is_superuser`.

    Статус проекта здесь не проверяется: это отдельное правило, и сервис
    различает «нет прав» (403) и «сейчас нельзя менять» (ошибка операции).
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    if project.owner_id != user.id:
        return False
    return user.role == User.Roles.DIRECTOR


def user_can_view_unit_economics_finance(user) -> bool:
    """Кто видит стоимость, часы, бюджет, CPL и прогноз в конфигураторе.

    Фрилансеру нужны состав, производительность, Hot leads и подбор —
    без денежных и «директорских» метрик. Остальные роли комнаты
    (директор, тимлид, менеджер, admin) финансы читают как раньше.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    return getattr(user, 'role', None) != User.Roles.FREELANCER


# ---------------------------------------------------------------------------
# Снапшот состава
# ---------------------------------------------------------------------------


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def get_project_composition(project: Project) -> list[dict]:
    """Сохранённый снапшот состава проекта (может быть пустым).

    Чтение не мутирует проект: отсутствующий состав — это пустой список,
    а не повод создать состав по умолчанию побочным эффектом.
    """
    raw = (project.input_data or {}).get(FUNCTIONAL_ROLES_KEY)
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def _parse_int_string(text: str) -> int | None:
    """`'2'` / `'-2'` → int, всё остальное → None.

    Строки принимаются потому, что состав придёт из формы, где `count`
    всегда строка. Разбор при этом свой, а не `int()` в `try`: `str.isdigit()`
    пропускает не-ASCII цифры («٥», «²»), на которых `int()` падает или,
    хуже, даёт неожиданное число.
    """
    text = text.strip()
    negative = text.startswith('-')
    digits = text[1:] if negative else text
    if not digits or not (digits.isascii() and digits.isdigit()):
        return None
    value = int(digits)
    return -value if negative else value


def _normalize_count(raw, role_key: str) -> int:
    """Строгое приведение count к неотрицательному int.

    `bool` отсекается явно: в Python `True` — это `int`, и «count: true»
    молча превратился бы в одного сотрудника. `float` не принимается тоже:
    «полтора сейлера» — не количество, а ошибка вызывающего кода.
    """
    if isinstance(raw, bool):
        raise FunctionalRolesError(
            f'Количество для «{role_key}» должно быть целым числом.'
        )
    if isinstance(raw, int):
        count = raw
    elif isinstance(raw, str) and (parsed := _parse_int_string(raw)) is not None:
        count = parsed
    else:
        raise FunctionalRolesError(
            f'Количество для «{role_key}» должно быть целым числом.'
        )
    if count < 0:
        raise FunctionalRolesError(
            f'Количество для «{role_key}» не может быть отрицательным.'
        )
    return count


def _normalize_requested_counts(roles_data) -> dict[str, int]:
    """Вход клиента → {role_key: count}. Экономика из входа игнорируется.

    Принимается список словарей `[{'role_key': ..., 'count': ...}]` или
    отображение `{role_key: count}`. Любые прочие ключи входа
    (`monthly_cost`, `hot_leads_per_month`, …) **отбрасываются**: клиент
    выбирает только состав, а деньги и KPI берутся с сервера.
    """
    if isinstance(roles_data, dict):
        items = list(roles_data.items())
    elif isinstance(roles_data, (list, tuple)):
        items = []
        for entry in roles_data:
            if not isinstance(entry, dict):
                raise FunctionalRolesError(
                    'Каждая строка состава должна быть объектом.'
                )
            if 'role_key' not in entry:
                raise FunctionalRolesError('В строке состава не указан role_key.')
            items.append((entry.get('role_key'), entry.get('count')))
    else:
        raise FunctionalRolesError('Некорректный формат состава команды.')

    counts: dict[str, int] = {}
    for role_key, raw_count in items:
        if not functional_roles.is_known_role_key(role_key):
            raise FunctionalRolesError(f'Неизвестная функция: {role_key!r}.')
        if role_key in counts:
            raise FunctionalRolesError(f'Функция «{role_key}» указана дважды.')
        counts[role_key] = _normalize_count(raw_count, role_key)
    return counts


def _validate_fixed_roles(counts: dict[str, int]) -> None:
    """Обязательные функции нельзя ни убрать, ни обнулить.

    Проверка живёт в сервисе, а не в UI: правило должно выдерживать прямой
    POST и вызов из shell, а не только нажатие кнопки «−» в таблице.
    """
    for role_key in functional_roles.FUNCTIONAL_ROLE_KEYS:
        role = functional_roles.FUNCTIONAL_ROLES[role_key]
        if not role.is_fixed:
            continue
        if role_key not in counts:
            raise FunctionalRolesError(
                f'Функцию «{role.label}» нельзя убрать из состава команды.'
            )
        if counts[role_key] < 1:
            raise FunctionalRolesError(
                f'Функция «{role.label}» обязательна: минимум 1.'
            )


def _load_business_values() -> dict[str, FunctionalRoleConfig]:
    return {config.role_key: config for config in FunctionalRoleConfig.objects.all()}


def _snapshot_entry(
    role: FunctionalRole, count: int, config: FunctionalRoleConfig
) -> dict:
    """Одна строка снапшота в публичных именах контракта Issue #11.

    Имена полей здесь и в модели различаются намеренно. Модель описывает
    ставку функции (`monthly_cost` — сколько стоит одна такая роль в месяц),
    контракт описывает строку состава (`cost_per_unit` — цена за единицу,
    которую UI умножает на `count`). Переименовывать поля модели ради
    совпадения имён смысла нет: это была бы миграция без изменения данных.

    Деньги сериализуются строкой, а не float: `json` округлил бы `62000.00`
    к двоичной дроби, и сумма бюджета начала бы «плавать» в последних знаках.

    `title` и `is_fixed` кладутся в снапшот по контракту, хотя это
    структурные поля: сводка всё равно берёт их из каталога кода
    (см. `_row_from_snapshot`), а в JSON они нужны потребителю, который
    каталога не видит. `grade` и `channel` в снапшот не пишутся —
    контракт их не требует, а сводка добавляет их server-side.
    """
    return {
        'id': functional_roles.role_snapshot_id(role.role_key),
        'role_key': role.role_key,
        'title': role.label,
        'count': count,
        'cost_per_unit': str(_money(config.monthly_cost)),
        'hours_per_unit': int(config.monthly_hours),
        'productivity_text': config.productivity_text,
        'kpi_leads_per_unit': int(config.hot_leads_per_month),
        'is_fixed': role.is_fixed,
    }


def _build_snapshot(counts: dict[str, int]) -> list[dict]:
    """Снапшот в каноническом порядке каталога, нулевые позиции отброшены."""
    configs = _load_business_values()
    missing = [key for key in counts if key not in configs]
    if missing:
        raise FunctionalRolesError(
            'Бизнес-параметры функций не настроены: ' + ', '.join(sorted(missing))
        )
    return [
        _snapshot_entry(
            functional_roles.FUNCTIONAL_ROLES[role_key],
            counts[role_key],
            configs[role_key],
        )
        for role_key in functional_roles.FUNCTIONAL_ROLE_KEYS
        if counts.get(role_key, 0) > 0
    ]


# ---------------------------------------------------------------------------
# Запись состава
# ---------------------------------------------------------------------------


@transaction.atomic
def update_project_functional_roles(project: Project, roles_data, user):
    """Сохраняет состав функциональных ролей проекта, его бюджет и KPI.

    Единственная точка записи `input_data['functional_roles']`.

    Что делает:

    * проверяет права (владелец / администратор) и статус проекта;
    * нормализует вход: только `role_key` и `count`, дубли запрещены;
    * берёт цену, часы, продуктивность и Hot **из серверного каталога**,
      а не из запроса;
    * пишет снапшот, **сохраняя остальные ключи** `input_data`
      (offer / utp / audience / hot_criteria / architecture);
    * приводит `Project.budget` к рассчитанному `total_budget`, а
      `Project.kpi_target` — к прогнозу `forecast_hot_leads`; оба поля
      уходят в БД одним `save` вместе со снапшотом.

    Слоты комнаты (`RoomFunctionSlot`) здесь не трогаются: их приводит
    к составу проекция `apps.rooms.staffing.projection`. Продуктовые
    write-path обязаны вызывать не этот сервис напрямую, а оркестрацию
    `apps.rooms.services.save_functional_roles_and_sync_slots` — она
    держит состав, бюджет и слоты в одной транзакции.

    Возвращает `UnitEconomicsSummary` по сохранённому снапшоту.
    """
    if not user_can_edit_functional_roles(user, project):
        raise PermissionDenied('Менять состав команды может только директор проекта.')
    if project.status not in COMPOSITION_EDITABLE_STATUSES:
        raise FunctionalRolesError(
            'Состав команды можно менять только в черновике '
            'или во время набора команды.'
        )

    counts = _normalize_requested_counts(roles_data)
    _validate_fixed_roles(counts)
    snapshot = _build_snapshot(counts)

    # Слияние, а не присваивание: `input_data` — общий словарь вводных проекта,
    # и запись состава не должна уносить с собой оффер и критерии Hot-лида.
    input_data = dict(project.input_data or {})
    input_data[FUNCTIONAL_ROLES_KEY] = snapshot
    project.input_data = input_data

    summary = _summary_from_snapshot(snapshot)
    # Бюджет перестаёт быть вторым независимым ручным источником истины:
    # он всегда равен сумме сохранённого состава.
    project.budget = summary.total_budget
    # KPI проекта — тот же прогноз Hot, что показан директору в сводке, а не
    # отдельно введённое число: покупая состав, он покупает и его прогноз.
    # Значение приходит из сводки по **сохранённому** снапшоту, поэтому из
    # браузера его подменить нельзя — там же, где и бюджет.
    project.kpi_target = Decimal(summary.forecast_hot_leads)
    # Один `save` на все три поля: состав, его сумма и его прогноз — одно
    # состояние проекта, и «бюджет обновился, KPI нет» не должно существовать.
    project.save(
        update_fields=['input_data', 'budget', 'kpi_target', 'updated_at']
    )
    return summary


def apply_package_to_project(project: Project, package_key: str, user):
    """Применяет готовый пакет как обычное сохранение состава.

    Пакет нигде не запоминается: после применения это просто состав, который
    директор дальше правит вручную.

    Слоты комнаты, как и в `update_project_functional_roles`, не трогаются:
    продуктовый путь пакета — `apps.rooms.services.apply_package_and_sync_slots`.
    """
    return update_project_functional_roles(
        project,
        presets.functional_role_package_composition(package_key),
        user,
    )


# ---------------------------------------------------------------------------
# Чтение экономики
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitEconomicsRow:
    """Строка таблицы юнит-экономики проекта.

    Финансовые поля — из снапшота проекта, структурные (`label`, `grade`,
    `channel`, `is_fixed`) — из каталога кода: у сохранённого проекта своя
    экономика, но не своя структура ролей.
    """

    role_key: str
    label: str
    count: int
    monthly_cost: Decimal
    monthly_hours: int
    productivity_text: str
    hot_leads_per_month: int
    subtotal_cost: Decimal
    subtotal_hours: int
    subtotal_hot_leads: int
    grade: str | None
    channel: str | None
    is_fixed: bool


@dataclass(frozen=True)
class UnitEconomicsSummary:
    """Итог по составу проекта.

    `cpl is None` означает «CPL не рассчитывается» (прогноз Hot-лидов равен
    нулю), а не «CPL равен нулю».

    Числового суммарного «охвата» здесь нет: `productivity_text` остаётся
    текстом до отдельной проработки этой математики.
    """

    rows: list[UnitEconomicsRow] = field(default_factory=list)
    total_budget: Decimal = Decimal('0.00')
    total_hours: int = 0
    forecast_hot_leads: int = 0
    cpl: Decimal | None = None

    @property
    def is_empty(self) -> bool:
        return not self.rows

    @property
    def composition(self) -> list[dict]:
        """Нормализованный состав `[{'role_key', 'count'}]` для UI и тестов."""
        return [{'role_key': row.role_key, 'count': row.count} for row in self.rows]


def _row_from_snapshot(entry: dict) -> UnitEconomicsRow | None:
    """Строка снапшота (публичные имена) → строка сводки (имена ставки).

    Деньги и KPI читаются только из снапшота — в этом весь смысл снапшота.
    Структурные `grade` / `channel` / `is_fixed` берутся из каталога кода:
    они не покупаются вместе с составом и всегда актуальны.
    """
    role_key = entry.get('role_key')
    if not isinstance(role_key, str):
        return None
    try:
        count = int(entry.get('count', 0))
        cost_per_unit = _money(entry.get('cost_per_unit', 0))
        hours_per_unit = int(entry.get('hours_per_unit', 0))
        kpi_per_unit = int(entry.get('kpi_leads_per_unit', 0))
    except (TypeError, ValueError, ArithmeticError):
        return None

    # Роль, исчезнувшая из каталога кода, всё равно показывается: её деньги
    # уже зафиксированы в проекте, и молча вычесть их из бюджета нельзя.
    # Тогда структурных данных взять неоткуда — падаем на то, что лежит
    # в снапшоте, а грейд и канал остаются пустыми.
    role: FunctionalRole | None = functional_roles.get_structural_role(role_key)
    return UnitEconomicsRow(
        role_key=role_key,
        label=role.label if role else str(entry.get('title', '') or role_key),
        count=count,
        monthly_cost=cost_per_unit,
        monthly_hours=hours_per_unit,
        productivity_text=str(entry.get('productivity_text', '')),
        hot_leads_per_month=kpi_per_unit,
        subtotal_cost=_money(cost_per_unit * count),
        subtotal_hours=hours_per_unit * count,
        subtotal_hot_leads=kpi_per_unit * count,
        grade=role.grade if role else None,
        channel=role.channel if role else None,
        is_fixed=role.is_fixed if role else bool(entry.get('is_fixed', False)),
    )


def _summary_from_snapshot(snapshot: list[dict]) -> UnitEconomicsSummary:
    rows = [row for row in (_row_from_snapshot(entry) for entry in snapshot) if row]
    total_budget = _money(sum((row.subtotal_cost for row in rows), Decimal('0')))
    total_hours = sum(row.subtotal_hours for row in rows)
    forecast_hot_leads = sum(row.subtotal_hot_leads for row in rows)
    # Деление только при ненулевом прогнозе: нулевой Hot — это «CPL не
    # рассчитывается», а не ноль рублей за лид.
    cpl = (
        (total_budget / Decimal(forecast_hot_leads)).quantize(
            MONEY_QUANTUM, rounding=ROUND_HALF_UP
        )
        if forecast_hot_leads > 0
        else None
    )
    return UnitEconomicsSummary(
        rows=rows,
        total_budget=total_budget,
        total_hours=total_hours,
        forecast_hot_leads=forecast_hot_leads,
        cpl=cpl,
    )


def get_unit_economics_summary(project: Project) -> UnitEconomicsSummary:
    """Юнит-экономика проекта по **сохранённому** снапшоту.

    Read-only: ничего не пишет в БД и не мутирует проект. Проект без
    сохранённого состава отдаёт пустую сводку (`total_* == 0`, `cpl is None`),
    а не создаёт состав по умолчанию — данные не должны появляться
    побочным эффектом GET-запроса.

    Актуальные значения админского каталога здесь **не** подмешиваются:
    экономика согласованного проекта меняется только явным сохранением
    состава.
    """
    return _summary_from_snapshot(get_project_composition(project))
