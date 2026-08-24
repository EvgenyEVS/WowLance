"""Проекция состава функциональных ролей проекта в слоты комнаты.

Что это
-------

Директор покупает **функции** (`Project.input_data['functional_roles']`,
см. `apps.rooms.unit_economics`), а подбор работает со **слотами**
(`RoomFunctionSlot`). Этот модуль — единственный мост между ними: он
приводит слоты комнаты к сохранённому составу проекта.

Границы модуля (продолжение разделения внутри `apps.rooms.staffing`):

* `matching.py` — правила подбора. Здесь **не** трогается и не вызывается.
* `services.py` — назначения, замены, готовность. Проекция никого не
  назначает: после появления слота вкладка «Команда» предлагает
  «Подобрать лучшего», «Другой сейлер» и «Выбрать из пула», и решение
  остаётся за человеком.
* `selectors.py` — read-only карточки. Проекция ничего не показывает.
* `projection.py` (этот файл) — только жизненный цикл самих слотов:
  создать, вернуть из истории, закрыть.

Проекция запускается **только из write-path** (явное сохранение состава
директором) и никогда из selector'а или GET: слоты не должны появляться
побочным эффектом чтения страницы. Оркестрация —
`apps.rooms.services.save_functional_roles_and_sync_slots`.

Какие функции проецируются
--------------------------

Только те, чьи требования структурный каталог задаёт однозначно в терминах
контракта слота: грейд из `RoomFunctionSlot.Grade` и канал из
`RoomFunctionSlot.Channel`. Сегодня это `seller_middle`, `seller_senior` и
`linkedin_leadgen`. Набор **выводится** из каталога и enum модели, а не
переписывается вторым списком: расхождение каталога и модели не должно
молча создавать слот с выдуманными требованиями.

Осознанно **не** проецируются:

* **`teamlead`** — у тимлида собственный ручной поток (`TeamleadInvite` →
  `assign_teamlead`): он приходит в комнату по приглашению, а не подбором.
  Грейда у него в каталоге нет (`grade is None`), то есть требования слота
  для него неопределимы. Кроме того, слот тимлида попал бы в
  `is_functional_team_ready`, и проект перестал бы активироваться: ручной
  поток никого на слот не сажает. Это отдельное продуктовое решение.
* **`database_assistant`** — его канал в каталоге `base` (работа с базой /
  CRM / разметка), а в `RoomFunctionSlot.Channel` такого варианта нет и
  `matching.CHANNEL_REQUIREMENTS` под него правил не знает. Подставить
  `any` или завести `base` в enum значило бы расширить контракт
  execution/matching — это отдельный этап. Пока честно: слота нет.

Оба случая — явный продуктовый follow-up, а не забытый `else`.
"""

from dataclasses import dataclass

from django.db import transaction

from ..functional_roles import FUNCTIONAL_ROLES, get_structural_role
from ..models import Project, RoomFunctionSlot, RoomMember
from ..unit_economics import FunctionalRolesError, get_unit_economics_summary

__all__ = [
    'MANUAL_FLOW_ROLE_KEYS',
    'PROJECTED_ROLE_KEYS',
    'RoleSlotChange',
    'SlotProjectionError',
    'SlotProjectionResult',
    'is_projected_role',
    'sync_functional_roles_to_slots',
]

#: Функции, исполнителя которым приводит собственный ручной поток, а не подбор.
#: Слоты им не создаются, даже если требования формально выразимы.
MANUAL_FLOW_ROLE_KEYS: frozenset[str] = frozenset({'teamlead'})


def _is_projectable(role) -> bool:
    """Выразимы ли требования функции в контракте слота **без** допущений.

    Проверяются оба требования сразу: грейд и канал должны буквально
    существовать в enum модели. Ни `None`, ни «похожее» значение не
    подменяется: слот с выдуманными требованиями подобрал бы не того
    человека, а это хуже, чем отсутствие слота.
    """
    if role.role_key in MANUAL_FLOW_ROLE_KEYS:
        return False
    return (
        role.grade in RoomFunctionSlot.Grade.values
        and role.channel in RoomFunctionSlot.Channel.values
    )


#: Функции состава, которые проекция превращает в слоты комнаты.
#: Сейчас: `seller_middle`, `seller_senior`, `linkedin_leadgen`.
PROJECTED_ROLE_KEYS: frozenset[str] = frozenset(
    role_key for role_key, role in FUNCTIONAL_ROLES.items() if _is_projectable(role)
)


def is_projected_role(role_key: str) -> bool:
    """Появляются ли у этой функции слоты комнаты."""
    return role_key in PROJECTED_ROLE_KEYS


class SlotProjectionError(FunctionalRolesError):
    """Состав нельзя привести к слотам, не тронув живое назначение.

    Наследуется от `FunctionalRolesError` намеренно: конфигуратор уже ловит
    именно его и показывает текст пользователю тем же partial. Отдельный
    несовместимый класс потребовал бы второй ветки `except` в каждом
    write-path и однажды остался бы без неё.
    """


@dataclass(frozen=True)
class RoleSlotChange:
    """Что проекция сделала со слотами одной функции. Только факты."""

    role_key: str
    target: int
    #: `slot_index` затронутых слотов — по ним видно и что создано, и что
    #: вернулось из истории, без второго запроса в БД.
    created: tuple[int, ...] = ()
    reactivated: tuple[int, ...] = ()
    deactivated: tuple[int, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.created or self.reactivated or self.deactivated)


@dataclass(frozen=True)
class SlotProjectionResult:
    """Итог синхронизации по всем проецируемым функциям."""

    changes: tuple[RoleSlotChange, ...] = ()

    @property
    def changed(self) -> bool:
        """False — состояние уже совпадало с составом, в БД ничего не писалось."""
        return any(change.changed for change in self.changes)

    @property
    def created_count(self) -> int:
        return sum(len(change.created) for change in self.changes)

    @property
    def reactivated_count(self) -> int:
        return sum(len(change.reactivated) for change in self.changes)

    @property
    def deactivated_count(self) -> int:
        return sum(len(change.deactivated) for change in self.changes)


def _resolve_room(project: Project):
    """Комната проекта; при её отсутствии — штатный сервис создания.

    Импорт функцией, а не модулем: `apps.rooms.staffing.services` уже
    импортирует `apps.rooms.services`, и модульный импорт обратно замкнул бы
    граф. Тот же приём применяется в оркестрации.

    Создание комнаты здесь безопасно именно потому, что проекция вызывается
    только из write-path сохранения состава: к этому моменту комната по
    продуктовому потоку уже существует (её открывает оплата / запуск
    проекта), а `ensure_room_for_project` идемпотентен. Из selector'ов и GET
    проекция не вызывается, поэтому комнат «из ниоткуда» этот путь не делает.
    """
    room = getattr(project, 'room', None)
    if room is not None:
        return room
    from ..services import ensure_room_for_project

    return ensure_room_for_project(project)


def _target_counts(project: Project) -> dict[str, int]:
    """Сколько слотов каждой проецируемой функции требует состав проекта.

    Читается через сводку юнит-экономики — единственную нормализацию
    снапшота. Функция, которой в составе нет, получает 0: убранная из
    состава функция обязана закрыть свои слоты, а не остаться в подборе.
    """
    saved = {
        entry['role_key']: entry['count']
        for entry in get_unit_economics_summary(project).composition
    }
    return {
        role_key: saved.get(role_key, 0)
        for role_key in sorted(PROJECTED_ROLE_KEYS)
    }


def _assigned_slot_ids(room) -> set:
    """Слоты комнаты, за которыми закреплён участник, — по данным БД.

    Один запрос на всю комнату и никакого `slot.assigned_member`: обратная
    связь кэшируется на экземпляре, а здесь от ответа «слот пуст» зависит,
    можно ли слот закрыть, — значит читать нужно свежее состояние.
    """
    return set(
        RoomMember.objects.filter(
            room=room, function_slot__isnull=False
        ).values_list('function_slot_id', flat=True)
    )


def _increase(room, role, slots, active, target, assigned_ids) -> RoleSlotChange:
    """Слотов не хватает: сначала вернуть закрытые, потом создать новые.

    Порядок принципиален. Закрытый слот несёт историю кандидатов
    (`RoomSlotCandidate`): если вместо возврата создавать новый, подбор
    забыл бы, кого уже показывали и кто отказывался, и предложил бы их
    заново. Возвращаются слоты с наименьшим `slot_index` — нумерация
    функции остаётся плотной.
    """
    inactive = [slot for slot in slots if not slot.is_active]
    needed = target - len(active)
    reactivated: list[int] = []

    for slot in inactive:
        if needed == 0:
            break
        if slot.id in assigned_ids:
            # Закрытый слот с исполнителем — уже рассогласованное состояние:
            # человек числится в комнате за слотом, которого нет в требуемом
            # составе. Вернуть его в команду молча значит принять за директора
            # кадровое решение, поэтому операция останавливается целиком.
            raise SlotProjectionError(
                f'Закрытый слот функции «{role.label}» (№{slot.slot_index}) '
                'занят исполнителем. Освободите его, прежде чем увеличивать '
                'количество.'
            )
        slot.is_active = True
        # Требования подтягиваются к структурному каталогу: слот мог быть
        # закрыт до изменения каталога, а вернуться обязан актуальным.
        slot.required_level = role.grade
        slot.required_channel = role.channel
        slot.save(
            update_fields=[
                'is_active',
                'required_level',
                'required_channel',
                'updated_at',
            ]
        )
        reactivated.append(slot.slot_index)
        needed -= 1

    created: list[int] = []
    # Индекс считается по **всем** слотам функции, включая закрытые:
    # переиспользование номера закрытого слота упёрлось бы в
    # `unique_room_function_slot` и смешало бы две разные истории подбора.
    next_index = max((slot.slot_index for slot in slots), default=0) + 1
    for _ in range(needed):
        RoomFunctionSlot.objects.create(
            room=room,
            role_key=role.role_key,
            slot_index=next_index,
            required_level=role.grade,
            required_channel=role.channel,
        )
        created.append(next_index)
        next_index += 1

    return RoleSlotChange(
        role_key=role.role_key,
        target=target,
        created=tuple(created),
        reactivated=tuple(reactivated),
    )


def _decrease(role, active, target, assigned_ids) -> RoleSlotChange:
    """Слотов больше, чем куплено: закрываются только пустые, с конца.

    Слот не удаляется никогда: удаление унесло бы историю кандидатов и
    обнулило бы смысл повторного открытия функции. Закрытый слот остаётся
    в БД и возвращается тем же `slot_index` при следующем увеличении.
    """
    excess = len(active) - target
    empty = [slot for slot in active if slot.id not in assigned_ids]
    if len(empty) < excess:
        # Уменьшать состав за счёт занятого слота нельзя: это молча выбросило
        # бы человека из требуемой команды. Операция откатывается целиком —
        # вместе с составом и бюджетом (см. оркестрацию).
        raise SlotProjectionError(
            f'Сначала снимите исполнителя с функции «{role.label}».'
        )

    deactivated: list[int] = []
    # С наибольшего индекса: закрываются последние добавленные слоты, а не
    # первые — у них короче история подбора, и нумерация остаётся плотной.
    for slot in sorted(empty, key=lambda item: item.slot_index, reverse=True)[:excess]:
        slot.is_active = False
        slot.save(update_fields=['is_active', 'updated_at'])
        deactivated.append(slot.slot_index)

    return RoleSlotChange(
        role_key=role.role_key,
        target=target,
        deactivated=tuple(deactivated),
    )


def _sync_role(room, role_key: str, target: int, assigned_ids: set) -> RoleSlotChange:
    """Приводит слоты одной функции к `target`. Слоты никогда не удаляются."""
    role = get_structural_role(role_key)
    slots = list(
        RoomFunctionSlot.objects.select_for_update()
        .filter(room=room, role_key=role_key)
        .order_by('slot_index')
    )
    active = [slot for slot in slots if slot.is_active]

    if len(active) == target:
        # Состояние уже совпадает: ни одной записи в БД.
        return RoleSlotChange(role_key=role_key, target=target)
    if len(active) < target:
        return _increase(room, role, slots, active, target, assigned_ids)
    return _decrease(role, active, target, assigned_ids)


@transaction.atomic
def sync_functional_roles_to_slots(project: Project) -> SlotProjectionResult:
    """Приводит `RoomFunctionSlot` комнаты к сохранённому составу проекта.

    Идемпотентна: повторный вызов при совпадающем состоянии не делает ни
    одной записи в БД. Никого не назначает и не снимает — `RoomMember` и
    `RoomSlotCandidate` этот модуль не пишет вообще.

    Поднимает `SlotProjectionError` (это `FunctionalRolesError`), если
    привести слоты к составу можно было бы только тронув живое назначение.
    Ошибка обязана откатить и сам состав, поэтому вызывать сервис нужно
    внутри `apps.rooms.services.save_functional_roles_and_sync_slots`, а не
    после самостоятельного сохранения состава.
    """
    room = _resolve_room(project)
    assigned_ids = _assigned_slot_ids(room)
    changes = [
        _sync_role(room, role_key, target, assigned_ids)
        for role_key, target in _target_counts(project).items()
    ]
    return SlotProjectionResult(changes=tuple(changes))
