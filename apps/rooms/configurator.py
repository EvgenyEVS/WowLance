"""Read-only сборка данных функциональных ролей для шаблонов ROOM.

Модуль — presentation-слой поверх `apps.rooms.unit_economics`, по образцу
`apps.rooms.staffing.selectors`: здесь нет ни одной записи в БД, ни одного
бизнес-правила и ни одной собственной цифры экономики.

Отсюда собираются два экрана одного и того же сохранённого состава:

* конфигуратор на «Обзоре» — что директор покупает и правит;
* блок «Требуется по плану проекта» на вкладке «Команда» — что заказано,
  как это видит тимлид (`build_planned_team_context`). Второй разбор
  `input_data` для него не заводится: используется та же сводка.

Границы
-------

* **Что покупает директор** — `unit_economics` (снапшот, валидация, бюджет).
  Единственная точка записи по-прежнему `update_project_functional_roles`.
* **Как это показать** — этот модуль: форматирование денег, «CPL не
  рассчитывается», список ещё не добавленных функций, подписи грейдов,
  read-only статус подбора.
* **Разметка** — `rooms/_unit_economics_table.html` и блок плана в
  `rooms/room_team.html`; копий у них нет.

Почему `counts` считает сервер
------------------------------

`build_counts` получает от клиента только `role_key` и намерение
(`inc` / `dec` / `set`), а текущее количество читает из **сохранённого**
состава проекта. Браузер не присылает ни арифметику, ни экономику: кнопка
«+» на устаревшей вкладке не может увеличить количество от старого значения,
а `count` для `set` уходит в сервис строкой, без собственного разбора, —
правила «целое, не отрицательное» остаются ровно в одном месте.
"""

from dataclasses import dataclass
from decimal import Decimal

from . import functional_roles, presets
from .functional_roles import FunctionalRole
from .models import FunctionalRoleConfig
from .staffing import selectors
from .unit_economics import (
    COMPOSITION_EDITABLE_STATUSES,
    FunctionalRolesError,
    UnitEconomicsRow,
    get_unit_economics_summary,
    user_can_edit_functional_roles,
    user_can_view_composition_staffing,
    user_can_view_unit_economics_finance,
)

__all__ = [
    'ACTION_DECREMENT',
    'ACTION_INCREMENT',
    'ACTION_SET',
    'CONFIGURATOR_ACTIONS',
    'EMPTY_VALUE',
    'EXECUTION_PENDING_LABEL',
    'HOT_UNIT',
    'HOURS_UNIT',
    'PLANNED_TEAM_TITLE',
    'REMOVE_BLOCKED_HINT',
    'SEARCHING_LABEL',
    'AvailableRole',
    'ConfiguratorRow',
    'PlannedRoleRow',
    'RoleStaffing',
    'build_configurator_context',
    'build_counts',
    'build_planned_team_context',
    'current_counts',
    'format_money',
    'user_can_configure_now',
]

#: Неразрывный пробел: «62 000 ₽» не должно переноситься по строкам.
NBSP = ' '
MONEY_SYMBOL = '₽'

#: Единый прочерк для «значения нет / не рассчитывается».
EMPTY_VALUE = '—'

#: Единицы измерения строк таблицы. Часы и Hot — целые числа, поэтому
#: собственного форматирования им не нужно, только подпись.
HOURS_UNIT = 'ч'
HOT_UNIT = 'Hot'

#: Знак умножения строки: `62 000 ₽ × 2`. Именно `×`, а не `x`.
TIMES_SIGN = '×'

#: Слот функции существует, но исполнителя в нём ещё нет.
SEARCHING_LABEL = 'Идёт подбор'

#: Почему кнопка «убрать функцию» недоступна. Полное удаление функции —
#: это `count → 0`, а проекция не закрывает слот с исполнителем и вернёт
#: `SlotProjectionError`. Кнопка, которая гарантированно упрётся в отказ,
#: вводит в заблуждение, поэтому она блокируется вместе с этой подсказкой.
REMOVE_BLOCKED_HINT = 'Сначала снимите исполнителя'

#: Заголовок display-only блока «что заказал директор» на вкладке «Команда».
PLANNED_TEAM_TITLE = 'Требуется по плану проекта'

#: Заказанная функция, у которой ещё нет ни одного слота комнаты.
#: Не «идёт подбор» и не прочерк: слот execution просто не создан —
#: у `teamlead` свой ручной поток, у `database_assistant` канал `base`,
#: которого нет в enum слота (см. `staffing.projection`).
EXECUTION_PENDING_LABEL = 'Execution slot ещё не создан'

#: Действия конфигуратора. `set` покрывает и «добавить» (count=1),
#: и «убрать» (count=0) — отдельные действия ничего бы не добавили.
ACTION_INCREMENT = 'inc'
ACTION_DECREMENT = 'dec'
ACTION_SET = 'set'
CONFIGURATOR_ACTIONS = frozenset({ACTION_INCREMENT, ACTION_DECREMENT, ACTION_SET})

def format_money(value) -> str:
    """`Decimal('62000.00')` → `'62 000 ₽'` (пробелы неразрывные).

    Форматирование своё, а не `humanize.intcomma` / `USE_THOUSAND_SEPARATOR`:
    первое потребовало бы нового приложения в `INSTALLED_APPS`, второе —
    глобально изменило бы вывод чисел на всех остальных страницах проекта.
    Копейки показываются только когда они есть: «62 000 ₽», но «1 234,50 ₽».
    """
    amount = Decimal(value or 0).quantize(Decimal('0.01'))
    whole = int(amount)
    kopecks = int((amount - whole) * 100)
    grouped = f'{whole:,}'.replace(',', NBSP)
    if kopecks:
        return f'{grouped},{kopecks:02d}{NBSP}{MONEY_SYMBOL}'
    return f'{grouped}{NBSP}{MONEY_SYMBOL}'


# ---------------------------------------------------------------------------
# Read-only статус подбора
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleStaffing:
    """Факты о слотах комнаты с таким же `role_key`. Только чтение.

    Показываются **только уже существующие** `RoomFunctionSlot`, и связь
    «функция состава → слот комнаты» здесь не изобретается: берутся ровно те
    активные слоты, у которых `role_key` буквально совпадает. У функции без
    слотов честный прочерк — ячейка не изображает, будто подбор уже запущен.
    Слотов не получают `teamlead` (ручной invite) и `database_assistant`
    (канал `base` вне enum слота), см. `staffing.projection`.

    Два разных SLA не смешиваются. SLA стартовой задачи проекта (24 часа)
    относится к проекту целиком, собирается во view «Обзора» из
    `apps.pipeline.services.get_start_calls_task` и живёт в отдельном
    баннере. SLA подбора (1 час) относится к **конкретному пустому слоту**
    и приходит готовым из `staffing.selectors.SlotCard`.
    """

    #: `staffing.selectors.SlotCard` существующих слотов этой функции.
    cards: tuple = ()

    @property
    def slots_total(self) -> int:
        return len(self.cards)

    @property
    def filled(self) -> int:
        return sum(1 for card in self.cards if card.is_filled)

    @property
    def ready(self) -> int:
        return sum(1 for card in self.cards if card.status == 'ready')

    @property
    def status(self) -> str:
        """Агрегированный статус — только для CSS-класса ячейки."""
        if not self.cards:
            return 'none'
        if self.filled == 0:
            return 'searching'
        if self.ready == self.slots_total:
            return 'ready'
        return 'assigned'

    @property
    def has_slots(self) -> bool:
        return bool(self.cards)

    @property
    def has_assigned(self) -> bool:
        """Есть ли уже назначенный человек в каком-либо слоте функции."""
        return self.filled > 0

    @property
    def remove_blocked(self) -> bool:
        """Упрётся ли полное удаление функции (`count → 0`) в исполнителя.

        Ровно то условие, по которому откажет `projection._decrease`: чтобы
        обнулить функцию, нужно закрыть все её активные слоты, а слот с
        участником проекция не закрывает никогда. Правило не переписывается,
        а зеркалится, и только для того, чтобы UI не предлагал операцию,
        которая заведомо закончится ошибкой. Настоящая защита остаётся в
        `SlotProjectionError`.
        """
        return self.has_assigned


def _staffing_by_role_key(room) -> dict[str, RoleStaffing]:
    """Существующие слоты комнаты, сгруппированные по `role_key`.

    Переиспользует `staffing.selectors.slot_cards`, поэтому второй копии
    определения «готов / назначен / свободен» не появляется, участник и его
    профиль приходят уже в `select_related`, а число слотов не превращается
    в число запросов.
    """
    if room is None:
        return {}
    grouped: dict[str, list] = {}
    for card in selectors.slot_cards(room):
        grouped.setdefault(card.slot.role_key, []).append(card)
    return {
        role_key: RoleStaffing(cards=tuple(cards))
        for role_key, cards in grouped.items()
    }


# ---------------------------------------------------------------------------
# Строки таблицы
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfiguratorRow:
    """Строка таблицы конфигуратора: экономика сервера + подготовленный вывод.

    Собственных вычислений экономики здесь нет — всё берётся из
    `UnitEconomicsRow`, посчитанного по сохранённому снапшоту проекта.
    """

    economics: UnitEconomicsRow
    staffing: RoleStaffing

    @property
    def role_key(self) -> str:
        return self.economics.role_key

    @property
    def label(self) -> str:
        return self.economics.label

    @property
    def count(self) -> int:
        return self.economics.count

    @property
    def is_fixed(self) -> bool:
        return self.economics.is_fixed

    @property
    def grade_display(self) -> str:
        """Подпись бейджа грейда рядом с названием функции.

        Значение приходит из **структурного** каталога через
        `UnitEconomicsRow.grade` (`_row_from_snapshot` берёт грейд из
        `functional_roles`, а не из снапшота), а текст — из
        `functional_roles.grade_display`. В шаблоне соответствия
        «`middle` → Middle» нет: иначе оно разошлось бы с каталогом.
        Teamlead грейда не имеет и честно помечается `N/A`.
        """
        return functional_roles.grade_display(self.economics.grade)

    @property
    def productivity_text(self) -> str:
        return self.economics.productivity_text

    # --- Норматив на одну единицу -------------------------------------

    @property
    def cost_per_unit_display(self) -> str:
        return format_money(self.economics.monthly_cost)

    @property
    def hours_per_unit(self) -> int:
        return self.economics.monthly_hours

    @property
    def hours_per_unit_display(self) -> str:
        return f'{self.economics.monthly_hours} {HOURS_UNIT}'

    @property
    def hot_leads_per_unit(self) -> int:
        return self.economics.hot_leads_per_month

    @property
    def hot_per_unit_display(self) -> str:
        return f'{self.economics.hot_leads_per_month} {HOT_UNIT}'

    # --- Итог строки: норматив × count ---------------------------------
    #
    # Умножение приходит из `UnitEconomicsRow` (сервер), а не считается в
    # шаблоне или в JS: показанный итог обязан совпадать с тем, из чего
    # сложился бюджет проекта.

    @property
    def subtotal_cost(self):
        return self.economics.subtotal_cost

    @property
    def subtotal_cost_display(self) -> str:
        return format_money(self.economics.subtotal_cost)

    @property
    def subtotal_hours(self) -> int:
        return self.economics.subtotal_hours

    @property
    def subtotal_hours_display(self) -> str:
        return f'{self.economics.subtotal_hours} {HOURS_UNIT}'

    @property
    def subtotal_hot_leads(self) -> int:
        return self.economics.subtotal_hot_leads

    @property
    def subtotal_hot_display(self) -> str:
        return f'{self.economics.subtotal_hot_leads} {HOT_UNIT}'

    @property
    def times_label(self) -> str:
        """«× 2» — множитель строки. Продуктивность им не умножается."""
        return f'{TIMES_SIGN} {self.economics.count}'

    @property
    def min_count(self) -> int:
        """Нижняя граница `input[type=number]`. Зеркалит правило сервиса."""
        return 1 if self.is_fixed else 0


@dataclass(frozen=True)
class AvailableRole:
    """Функция, которой ещё нет в составе, вместе с её базовыми нормативами.

    Ставка берётся из **актуального** админского каталога
    (`FunctionalRoleConfig`), а не из снапшота проекта: снапшота у ещё не
    добавленной функции нет, и в меню честно показывается цена, по которой
    её купят прямо сейчас. Зафиксируется она в проекте обычным сохранением
    состава — как и для всех остальных строк.

    Это только server-rendered display: в форму по-прежнему уходит один
    `role_key`, экономику из браузера сервис не принимает.
    """

    role: FunctionalRole
    config: FunctionalRoleConfig | None

    @property
    def role_key(self) -> str:
        return self.role.role_key

    @property
    def label(self) -> str:
        return self.role.label

    @property
    def cost_display(self) -> str:
        return format_money(self.config.monthly_cost) if self.config else EMPTY_VALUE

    @property
    def hours_display(self) -> str:
        if not self.config:
            return EMPTY_VALUE
        return f'{int(self.config.monthly_hours)} {HOURS_UNIT}'

    @property
    def hot_display(self) -> str:
        if not self.config:
            return EMPTY_VALUE
        return f'{int(self.config.hot_leads_per_month)} {HOT_UNIT}'

    @property
    def productivity_text(self) -> str:
        return self.config.productivity_text if self.config else ''

    @property
    def option_label(self) -> str:
        """Подпись пункта меню: название + базовые ставка, часы и Hot.

        Собирается на сервере одной строкой, потому что `<option>` не
        принимает разметку. Продуктивность в подпись не входит: она
        перегрузила бы строку выбора и остаётся текстом в таблице.
        """
        return (
            f'{self.label} — {self.cost_display} / мес · '
            f'{self.hours_display} · {self.hot_display}'
        )


# ---------------------------------------------------------------------------
# «Требуется по плану проекта» — display-only состав для вкладки «Команда»
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedRoleRow:
    """Одна заказанная директором функция — как её видит тимлид.

    Формальный AC Issue #11: тимлид должен видеть, **сколько и каких**
    функций и грейдов заказал директор, — включая те, у которых слотов
    execution нет и не будет на этом этапе (`teamlead`,
    `database_assistant`). Поэтому источник строки — сохранённый снапшот
    состава (`Project.input_data['functional_roles']` через
    `get_unit_economics_summary`), а не список слотов комнаты: иначе
    заказанная функция без слота просто исчезла бы с экрана.

    Блок строго read-only: он ничего не создаёт, не проецирует и не
    запускает подбор. Слоты по-прежнему появляются только из write-path
    сохранения состава.
    """

    economics: UnitEconomicsRow
    staffing: RoleStaffing

    @property
    def role_key(self) -> str:
        return self.economics.role_key

    @property
    def label(self) -> str:
        return self.economics.label

    @property
    def count(self) -> int:
        return self.economics.count

    @property
    def is_fixed(self) -> bool:
        return self.economics.is_fixed

    @property
    def grade_display(self) -> str:
        """Тот же серверный источник грейда, что и у конфигуратора."""
        return functional_roles.grade_display(self.economics.grade)

    @property
    def has_slots(self) -> bool:
        return self.staffing.has_slots

    @property
    def status(self) -> str:
        """CSS-статус строки: `none` — слотов нет, иначе агрегат подбора."""
        return self.staffing.status

    @property
    def execution_note(self) -> str:
        """Честная подпись для функции без единого слота комнаты.

        Не «идёт подбор» и не пустая строка: подбор по этой функции не
        запущен вообще. Функция с существующими слотами подписи не получает —
        её состояние показывают сами слоты.
        """
        return '' if self.has_slots else EXECUTION_PENDING_LABEL


def build_planned_team_context(project, room=None) -> dict:
    """Context display-only блока «Требуется по плану проекта».

    Собирается на сервере целиком: шаблон не разбирает `input_data` и не
    знает ни про снапшот, ни про слоты. Второго парсера `functional_roles`
    в разметке не появляется — используется та же сводка, что и у
    конфигуратора.
    """
    summary = get_unit_economics_summary(project)
    staffing = _staffing_by_role_key(room)
    return {
        'planned_team_title': PLANNED_TEAM_TITLE,
        'planned_roles': [
            PlannedRoleRow(
                economics=row,
                staffing=staffing.get(row.role_key, RoleStaffing()),
            )
            for row in summary.rows
        ],
        'planned_total_count': sum(row.count for row in summary.rows),
        # Подпись «идёт подбор» — та же константа, что и в конфигураторе:
        # второго написания одной фразы в проекте не появляется.
        'planned_searching_label': SEARCHING_LABEL,
    }


# ---------------------------------------------------------------------------
# Права
# ---------------------------------------------------------------------------


def user_can_configure_now(user, project) -> bool:
    """Один флаг для шаблона: и права, и статус проекта.

    Оба слагаемых берутся из backend (`user_can_edit_functional_roles` и
    `COMPOSITION_EDITABLE_STATUSES`), поэтому правила ролей не переписываются
    в шаблонах. Флаг влияет только на то, что видно: настоящая защита —
    та же пара проверок внутри `update_project_functional_roles`.
    """
    return (
        user_can_edit_functional_roles(user, project)
        and project.status in COMPOSITION_EDITABLE_STATUSES
    )


# ---------------------------------------------------------------------------
# Состав для сохранения
# ---------------------------------------------------------------------------


def current_counts(project) -> dict[str, int]:
    """Сохранённый состав как `{role_key: count}` по ключам каталога.

    Читается через сводку, а не через сырой `input_data`: нормализация строк
    снапшота уже есть в `unit_economics`, дублировать её незачем. Роль,
    исчезнувшая из каталога кода, в состав для перезаписи не попадает —
    сервис отверг бы её как неизвестную.
    """
    return {
        entry['role_key']: entry['count']
        for entry in get_unit_economics_summary(project).composition
        if functional_roles.is_known_role_key(entry['role_key'])
    }


def build_counts(project, role_key: str, action: str, raw_count=None) -> dict:
    """Полный состав для `update_project_functional_roles` по одному действию.

    `inc` / `dec` считаются от **сохранённого** состава: браузер присылает
    намерение, а не арифметику, поэтому устаревшая вкладка не может
    досчитать количество от старого значения.

    `set` кладёт присланное значение как есть — разбор («целое», «не
    отрицательное») остаётся единственной копией в сервисе.

    Обязательные функции досеиваются `setdefault(1)`, только если запрос их не
    трогает: так первое сохранение состава добавляет тимлида само (Issue #11),
    но явный `teamlead → 0` доходит до валидации сервиса и честно падает,
    а не подменяется молча единицей.
    """
    if not functional_roles.is_known_role_key(role_key):
        raise FunctionalRolesError(f'Неизвестная функция: {role_key!r}.')
    if action not in CONFIGURATOR_ACTIONS:
        raise FunctionalRolesError('Неизвестное действие конфигуратора.')

    counts: dict = dict(current_counts(project))
    if action == ACTION_INCREMENT:
        counts[role_key] = counts.get(role_key, 0) + 1
    elif action == ACTION_DECREMENT:
        counts[role_key] = max(counts.get(role_key, 0) - 1, 0)
    else:
        counts[role_key] = raw_count

    for fixed_key in functional_roles.FIXED_ROLE_KEYS:
        if fixed_key != role_key:
            counts.setdefault(fixed_key, 1)
    return counts


# ---------------------------------------------------------------------------
# Context шаблона
# ---------------------------------------------------------------------------


def build_configurator_context(user, project, room=None, *, error=None, notice=None):
    """Готовый context единственного partial конфигуратора.

    Собирается в одном месте, потому что partial отдаётся двумя путями —
    внутри «Обзора» и ответом на HTMX-POST. Если бы context собирали оба
    вызывающих, они бы со временем разошлись.
    """
    summary = get_unit_economics_summary(project)
    staffing = _staffing_by_role_key(room)
    present = {row.role_key for row in summary.rows}
    # Один запрос на весь каталог бизнес-значений: меню «Добавить функцию»
    # показывает базовые ставки, а не только названия.
    configs = {config.role_key: config for config in FunctionalRoleConfig.objects.all()}
    return {
        'project': project,
        'fr_rows': [
            ConfiguratorRow(
                economics=row,
                staffing=staffing.get(row.role_key, RoleStaffing()),
            )
            for row in summary.rows
        ],
        'fr_summary': summary,
        'fr_total_budget_display': format_money(summary.total_budget),
        # `cpl is None` — «не рассчитывается» (прогноз Hot нулевой),
        # а не ноль рублей за лид. Поэтому прочерк, а не «0 ₽».
        'fr_cpl_display': (
            format_money(summary.cpl) if summary.cpl is not None else EMPTY_VALUE
        ),
        'can_edit_functional_roles': user_can_configure_now(user, project),
        'can_view_unit_economics_finance': user_can_view_unit_economics_finance(user),
        'can_view_composition_staffing': user_can_view_composition_staffing(user),
        # Обязательные функции в «Добавить функцию» не предлагаются: убрать их
        # нельзя, а при первом сохранении состава они добавляются сами.
        'fr_available_roles': [
            AvailableRole(role=role, config=configs.get(role_key))
            for role_key, role in functional_roles.FUNCTIONAL_ROLES.items()
            if role_key not in present and not role.is_fixed
        ],
        # Обязательные функции нужны empty state: он объясняет, что при первом
        # сохранении они добавятся сами. Названия берутся из каталога, а не
        # пишутся в шаблоне.
        'fr_fixed_roles': [
            role for role in functional_roles.FUNCTIONAL_ROLES.values() if role.is_fixed
        ],
        'fr_packages': list(presets.FUNCTIONAL_ROLE_PACKAGES.values()),
        # Подписи ячейки подбора — константы модуля, а не литералы шаблона:
        # текст «идёт подбор» и прочерк проверяются тестами.
        'fr_searching_label': SEARCHING_LABEL,
        'fr_empty_value': EMPTY_VALUE,
        # Подсказка заблокированного «✕» — тоже константа модуля: текст
        # обещания «сначала снимите исполнителя» проверяется тестом.
        'fr_remove_blocked_hint': REMOVE_BLOCKED_HINT,
        'fr_error': error,
        'fr_notice': notice,
    }
