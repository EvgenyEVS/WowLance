"""Структурный каталог функциональных ролей.

Модуль сознательно **чистый Python**: он не импортирует модели Django и не
ходит в БД. Это позволяет `models.py` валидировать `FunctionalRoleConfig`
против каталога без циклического импорта, а тестам и будущему UI —
читать структуру ролей без поднятой базы.

Разделение источников истины
----------------------------

* **Структура** (этот файл): `role_key`, отображаемое имя, грейд, канал,
  признак `is_fixed`. Меняется только релизом кода: от неё зависят будущая
  проекция в `RoomFunctionSlot` и подбор, поэтому администратор не должен
  править её на проде.
* **Бизнес-значения** (`rooms.FunctionalRoleConfig` в БД): стоимость, часы,
  текст продуктивности, Hot-лиды. Их администратор меняет через Django admin
  без релиза.
* **Экономика конкретного проекта** (`Project.input_data['functional_roles']`):
  снапшот бизнес-значений на момент сохранения состава. См.
  `apps.rooms.unit_economics`.

Готовые составы («Быстрый старт», «Масштабирование», «Enterprise аутрич»)
живут в `apps.rooms.presets` рядом с остальными продуктовыми пресетами —
это продуктовая заготовка, а не структура ролей.

Про грейды и каналы
-------------------

Значения грейдов совпадают с `RoomFunctionSlot.Grade`, но продублированы
строками намеренно: каталог не должен тянуть модели, а `Teamlead` вообще не
имеет грейда (`None`) — такого варианта в enum слота нет и заводить его
ради каталога нельзя.

`CHANNEL_BASE` (`'base'`) в `RoomFunctionSlot.Channel` **отсутствует**.
Это осознанно: канал `base` (работа с базой / CRM / разметка) нужен
`database_assistant` на уровне продуктового каталога, но проекция
functional_roles → RoomFunctionSlot и подбор под этот канал — отдельное
решение следующего этапа. Matching в этом PR не расширяется.
"""

from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    'CHANNEL_ANY',
    'CHANNEL_BASE',
    'CHANNEL_COLD_CALLING',
    'CHANNEL_LINKEDIN',
    'FIXED_ROLE_KEYS',
    'FUNCTIONAL_ROLES',
    'FUNCTIONAL_ROLE_KEYS',
    'FUNCTIONAL_ROLE_KEY_CHOICES',
    'GRADE_JUNIOR',
    'GRADE_LABELS',
    'GRADE_MIDDLE',
    'GRADE_NOT_APPLICABLE',
    'GRADE_SENIOR',
    'FunctionalRole',
    'get_structural_role',
    'grade_display',
    'is_known_role_key',
    'role_grade_display',
    'role_label',
    'role_snapshot_id',
]

#: Грейды. Строки совпадают с `RoomFunctionSlot.Grade`, см. docstring модуля.
GRADE_JUNIOR = 'junior'
GRADE_MIDDLE = 'middle'
GRADE_SENIOR = 'senior'

#: Как грейд показывается пользователю. Английские подписи оставлены
#: намеренно: «Middle» / «Senior» — принятые в отрасли обозначения, и они уже
#: используются в публичных названиях функций («Сейлер Middle»), в
#: `RoomFunctionSlot.Grade` и в каталоге BIZ. Переводить их на русский только
#: в бейдже значило бы завести второе написание одного и того же уровня.
GRADE_LABELS: MappingProxyType = MappingProxyType({
    GRADE_JUNIOR: 'Junior',
    GRADE_MIDDLE: 'Middle',
    GRADE_SENIOR: 'Senior',
})

#: Подпись бейджа для функции, к которой грейд неприменим (Teamlead).
#: Это не «грейд неизвестен»: у тимлида грейда нет по структуре каталога.
GRADE_NOT_APPLICABLE = 'N/A'

#: Каналы. `CHANNEL_BASE` шире enum слота — см. docstring модуля.
CHANNEL_ANY = 'any'
CHANNEL_COLD_CALLING = 'cold_calling'
CHANNEL_LINKEDIN = 'linkedin'
CHANNEL_BASE = 'base'


@dataclass(frozen=True)
class FunctionalRole:
    """Структурное описание функции команды.

    Экономических полей здесь нет намеренно: цена, часы, продуктивность и
    Hot-лиды живут в `FunctionalRoleConfig` и правятся администратором.
    """

    role_key: str
    label: str
    #: Требуемый грейд исполнителя. `None` — грейд к функции неприменим
    #: (Teamlead в таблице руководителя помечен как N/A).
    grade: str | None
    channel: str
    #: True — функцию нельзя убрать из состава проекта (count всегда >= 1).
    is_fixed: bool = False


#: Полный структурный каталог MVP. Порядок определяет порядок строк
#: в снапшоте проекта и в summary — он стабильный, а не «как пришло из формы».
FUNCTIONAL_ROLES: MappingProxyType = MappingProxyType({
    role.role_key: role
    for role in (
        FunctionalRole(
            role_key='teamlead',
            label='Тимлид проекта',
            grade=None,
            channel=CHANNEL_ANY,
            is_fixed=True,
        ),
        FunctionalRole(
            role_key='seller_middle',
            label='Сейлер Middle',
            grade=GRADE_MIDDLE,
            channel=CHANNEL_COLD_CALLING,
        ),
        FunctionalRole(
            role_key='seller_senior',
            label='Сейлер Senior',
            grade=GRADE_SENIOR,
            channel=CHANNEL_COLD_CALLING,
        ),
        FunctionalRole(
            role_key='linkedin_leadgen',
            label='Лидген LinkedIn',
            grade=GRADE_MIDDLE,
            channel=CHANNEL_LINKEDIN,
        ),
        FunctionalRole(
            role_key='database_assistant',
            label='Ассистент базы',
            grade=GRADE_JUNIOR,
            channel=CHANNEL_BASE,
        ),
    )
})

#: Ключи каталога в каноническом порядке.
FUNCTIONAL_ROLE_KEYS: tuple[str, ...] = tuple(FUNCTIONAL_ROLES)

#: Роли, которые обязаны присутствовать в любом сохранённом составе.
FIXED_ROLE_KEYS: frozenset[str] = frozenset(
    key for key, role in FUNCTIONAL_ROLES.items() if role.is_fixed
)

#: `choices` для поля модели: администратор не может завести шестую роль
#: даже через shell, минуя запреты админки.
FUNCTIONAL_ROLE_KEY_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (key, role.label) for key, role in FUNCTIONAL_ROLES.items()
)


def is_known_role_key(role_key) -> bool:
    return isinstance(role_key, str) and role_key in FUNCTIONAL_ROLES


def get_structural_role(role_key: str) -> FunctionalRole | None:
    """Структурное описание функции или None для неизвестного ключа."""
    return FUNCTIONAL_ROLES.get(role_key)


def role_snapshot_id(role_key: str) -> str:
    """Стабильный публичный идентификатор строки состава: `role_<role_key>`.

    Выводится из `role_key`, а не хранится отдельно: два поля пришлось бы
    синхронизировать, и они бы разошлись. UI получает стабильный ключ для
    строк таблицы, не завися от порядка элементов в списке.
    """
    return f'role_{role_key}'


def grade_display(grade: str | None) -> str:
    """Грейд каталога → подпись бейджа. `None` → `N/A`.

    Единственное место, где грейд превращается в текст. Шаблоны и селекторы
    получают готовую подпись, поэтому второй копии соответствия
    «`middle` → Middle» в разметке не появляется, а функция без грейда
    (Teamlead) честно помечается `N/A`, а не пустым местом.

    Неизвестное историческое значение возвращается как есть: страница
    обязана отрисоваться, даже если каталог грейдов когда-то изменится.
    """
    if grade is None:
        return GRADE_NOT_APPLICABLE
    return GRADE_LABELS.get(grade, str(grade))


def role_grade_display(role_key: str) -> str:
    """Подпись грейда функции по её `role_key`.

    Для ключа, которого в каталоге нет, грейда тоже нет — отдаётся `N/A`:
    выдумывать уровень исчезнувшей функции нельзя.
    """
    role = get_structural_role(role_key)
    return grade_display(role.grade if role else None)


def role_label(role_key: str) -> str:
    """Публичное название функции по `role_key`; fallback — сам ключ.

    Нужен подбору и его UI: `RoomFunctionSlot.role_key` — машинный ключ,
    и показывать пользователю `seller_middle` нельзя. Каталог здесь
    единственный источник названий, поэтому подписи слотов и строк состава
    не расходятся.

    Исторический ключ, которого в каталоге уже (или ещё) нет, возвращается
    как есть: слот с таким ключом существует в БД реальных комнат, и
    страница обязана открыться, а не упасть на `KeyError`.
    """
    role = get_structural_role(role_key)
    return role.label if role else str(role_key)
