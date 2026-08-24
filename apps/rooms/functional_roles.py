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
    'GRADE_MIDDLE',
    'GRADE_SENIOR',
    'FunctionalRole',
    'get_structural_role',
    'is_known_role_key',
    'role_snapshot_id',
]

#: Грейды. Строки совпадают с `RoomFunctionSlot.Grade`, см. docstring модуля.
GRADE_JUNIOR = 'junior'
GRADE_MIDDLE = 'middle'
GRADE_SENIOR = 'senior'

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
