"""Транзакционные операции подбора: назначение, авто-подбор, замена, готовность.

Разделение обязанностей внутри `apps.rooms.staffing`:

* `matching.py` — read-only движок подбора (hard filters + ranking). Единственный
  источник истины о том, кто подходит слоту. Здесь фильтры **не дублируются**:
  модуль только вызывает `get_best_candidate` / `get_next_candidate` /
  `get_ranked_candidates`.
* `services.py` (этот файл) — все записи в БД: `RoomMember`, `RoomSlotCandidate`,
  лента комнаты и переход статуса проекта.
* `selectors.py` — сборка read-only данных для шаблонов.

Границы модулей (ADR-001): staffing читает BIZ только через `matching`,
`apps.pipeline` здесь не импортируется, обратной зависимости `profiles → rooms`
не появляется. Бизнес-логика не размазывается по views: views вызывают эти
сервисы и переводят результат в сообщения/редиректы.
"""

from dataclasses import dataclass

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from ..models import Project, RoomActivity, RoomFunctionSlot, RoomMember, RoomSlotCandidate
from ..services import log_room_activity, user_can_manage_team
from . import matching

__all__ = [
    'STAFFING_MUTABLE_STATUSES',
    'StaffingError',
    'StaffingOutcome',
    'assign_candidate_to_slot',
    'auto_assign_best_candidate',
    'confirm_freelancer_readiness',
    'is_functional_team_ready',
    'replace_slot_member',
    'slot_label',
    'sync_project_activation',
]

#: Статусы проекта, в которых команда ещё формируется и staffing разрешён.
#: Всё остальное (ACTIVE / ON_HOLD / COMPLETED / ARCHIVED) закрыто в том числе
#: для прямого POST: проверка живёт в сервисе, а не только в шаблоне.
STAFFING_MUTABLE_STATUSES = frozenset({
    Project.Status.DRAFT,
    Project.Status.STAFFING,
})


class StaffingError(ValueError):
    """Операция подбора невозможна в текущем состоянии данных.

    Права — отдельный случай: они поднимают `PermissionDenied`, чтобы view
    отдал 403 существующим механизмом, а не превращал отказ в сообщение.
    """


@dataclass(frozen=True)
class StaffingOutcome:
    """Итог операции подбора для UI.

    `code` — машинное состояние (`assigned`, `replaced`, `no_candidates`),
    `message` — готовый русский текст для flash/partial. Пустой пул кандидатов
    это нормальный исход, а не ошибка: он возвращается результатом.
    """

    code: str
    message: str
    member: RoomMember | None = None
    previous_member_name: str = ''

    @property
    def assigned(self) -> bool:
        return self.member is not None


def slot_label(slot: RoomFunctionSlot) -> str:
    """Человекочитаемое имя слота для ленты комнаты и сообщений."""
    return f'{slot.role_key} #{slot.slot_index}'


def _current_member(slot: RoomFunctionSlot) -> RoomMember | None:
    """Кто занимает слот прямо сейчас — по данным БД, а не по кэшу объекта.

    `slot.assigned_member` кэширует обратную связь на экземпляре: после
    удаления участника внутри одной операции («Другой сейлер») он вернул бы
    уже несуществующего человека. Для записи всегда спрашиваем БД.
    """
    return (
        RoomMember.objects.select_related('user')
        .filter(function_slot=slot)
        .first()
    )


def _guard_staffing(slot: RoomFunctionSlot, actor) -> Project:
    """Общие проверки любой мутации слота: права, статус проекта, слот активен."""
    project = slot.room.project
    if not user_can_manage_team(actor, project):
        raise PermissionDenied('Управлять составом команды может директор или тимлид.')
    if project.status not in STAFFING_MUTABLE_STATUSES:
        raise StaffingError(
            'Изменить состав команды можно только пока проект набирает команду.'
        )
    if not slot.is_active:
        raise StaffingError('Слот закрыт и в подборе не участвует.')
    return project


@transaction.atomic
def assign_candidate_to_slot(slot: RoomFunctionSlot, candidate, actor) -> RoomMember:
    """Назначает кандидата на функциональный слот комнаты.

    Единственная точка создания `RoomMember` для функционального слота:
    авто-подбор и «Другой сейлер» проходят через неё же, поэтому проверки
    и запись истории не расходятся между сценариями.

    Назначение **не** активирует проект: статус меняет только подтверждение
    готовности всей команды (`sync_project_activation`).
    """
    _guard_staffing(slot, actor)

    if _current_member(slot) is not None:
        raise StaffingError('Слот уже занят. Используйте замену кандидата.')

    if RoomMember.objects.filter(room_id=slot.room_id, user=candidate).exists():
        raise StaffingError('Кандидат уже участвует в этой комнате.')

    # Право кандидата занять слот перепроверяется здесь всегда — в том числе
    # при прямом POST и при повторной отправке формы со старого GET-списка.
    # Условия берутся из Matching Engine, а не переписываются заново.
    if not matching.get_ranked_candidates(slot).filter(user=candidate).exists():
        raise StaffingError('Кандидат больше не подходит под требования слота.')

    try:
        # Вложенная точка сохранения: гонка двух быстрых POST упирается в
        # уникальные constraint базы (`function_slot` OneToOne и `room+user`),
        # и второй запрос получает понятную ошибку, а не дубль участника.
        with transaction.atomic():
            member = RoomMember.objects.create(
                room=slot.room,
                user=candidate,
                role_in_room=RoomMember.RoleInRoom.FREELANCER,
                ready_status=RoomMember.ReadyStatus.PENDING,
                function_slot=slot,
            )
    except IntegrityError as exc:
        raise StaffingError('Слот уже занят другим запросом. Обновите страницу.') from exc

    RoomSlotCandidate.objects.update_or_create(
        slot=slot,
        candidate=candidate,
        defaults={
            'outcome': RoomSlotCandidate.Outcome.ASSIGNED,
            'actor': actor,
        },
    )
    log_room_activity(
        slot.room,
        f'{candidate.full_name} назначен на функцию «{slot_label(slot)}».',
        RoomActivity.EventType.MEMBER_ADDED,
        actor=actor,
    )
    return member


@transaction.atomic
def auto_assign_best_candidate(slot: RoomFunctionSlot, actor) -> StaffingOutcome:
    """Auto top-1: сажает на пустой слот лучшего кандидата ranking.

    Применяется только к фрилансерским функциональным слотам — тимлид приходит
    своим существующим invite/manual-потоком, подбор его не назначает.

    Сервис вызывается явно (кнопка «Подобрать лучшего», в будущем — создание
    слота функциональным конфигуратором). Ни signal, ни `post_save` за это не
    отвечают: неявное назначение участника скрыло бы бизнес-логику.
    """
    _guard_staffing(slot, actor)
    if _current_member(slot) is not None:
        raise StaffingError('Слот уже занят. Используйте замену кандидата.')

    # Пустая история — слот только создан: берём top-1 всего пула.
    # Если по слоту уже кого-то смотрели или пропускали, повторно предлагать
    # того же человека нельзя, поэтому запрашивается следующий по ranking.
    profile = (
        matching.get_next_candidate(slot)
        if RoomSlotCandidate.objects.filter(slot=slot).exists()
        else matching.get_best_candidate(slot)
    )
    if profile is None:
        return StaffingOutcome(
            code='no_candidates',
            message='Подходящие кандидаты не найдены.',
        )

    member = assign_candidate_to_slot(slot, profile.user, actor)
    return StaffingOutcome(
        code='assigned',
        message=f'{member.user.full_name} назначен на слот.',
        member=member,
    )


@transaction.atomic
def replace_slot_member(slot: RoomFunctionSlot, actor) -> StaffingOutcome:
    """«Другой сейлер»: меняет текущего исполнителя слота на следующего по ranking.

    Порядок принципиален: следующий кандидат ищется **до** снятия текущего.
    Если пул исчерпан, текущий участник остаётся в комнате со своим
    `ready_status` — операция ничего не меняет и сообщает об этом.
    """
    _guard_staffing(slot, actor)

    current = _current_member(slot)
    if current is None:
        raise StaffingError('Слот пуст — заменять некого.')

    # Сначала следующий кандидат, только потом любые удаления.
    profile = matching.get_next_candidate(slot)
    if profile is None:
        return StaffingOutcome(
            code='no_candidates',
            message='Подходящие кандидаты закончились — текущий исполнитель оставлен.',
        )

    previous_user = current.user
    previous_name = previous_user.full_name

    RoomSlotCandidate.objects.update_or_create(
        slot=slot,
        candidate=previous_user,
        defaults={
            'outcome': RoomSlotCandidate.Outcome.SKIPPED,
            'actor': actor,
        },
    )
    # Участник удаляется целиком: доступ к комнате снимается существующим
    # RBAC (`user_can_access_project` смотрит на RoomMember), а `ready_status`
    # нового исполнителя стартует с нуля, а не наследуется от снятого.
    current.delete()
    log_room_activity(
        slot.room,
        f'{previous_name} снят с функции «{slot_label(slot)}» (замена исполнителя).',
        RoomActivity.EventType.MEMBER_REMOVED,
        actor=actor,
    )

    member = assign_candidate_to_slot(slot, profile.user, actor)
    return StaffingOutcome(
        code='replaced',
        message=f'{previous_name} заменён: назначен {member.user.full_name}.',
        member=member,
        previous_member_name=previous_name,
    )


def is_functional_team_ready(room) -> bool:
    """Собрана ли требуемая функциональная команда на 100%.

    Условие этапа: есть хотя бы один активный слот, каждый активный слот занят,
    и каждый занимающий его участник подтвердил готовность. Директор и тимлид,
    не занимающие функциональные слоты, на результат не влияют — проверяются
    только слоты.
    """
    slots = room.function_slots.filter(is_active=True).select_related('member')
    has_slots = False
    for slot in slots:
        has_slots = True
        member = slot.assigned_member
        if member is None or member.ready_status != RoomMember.ReadyStatus.READY:
            return False
    return has_slots


@transaction.atomic
def sync_project_activation(project: Project, actor=None) -> bool:
    """Переводит проект в ACTIVE, когда функциональная команда готова.

    Идемпотентна: переход возможен только из STAFFING, поэтому повторный вызов
    (повторное подтверждение готовности) не создаёт второй переход и второе
    событие в ленте. Возвращает True, только если статус изменился именно сейчас.
    """
    if project.status != Project.Status.STAFFING:
        return False
    room = getattr(project, 'room', None)
    if room is None or not is_functional_team_ready(room):
        return False

    project.status = Project.Status.ACTIVE
    project.save(update_fields=['status', 'updated_at'])
    log_room_activity(
        room,
        f'Команда собрана и подтвердила готовность. Проект «{project.name}» активен.',
        RoomActivity.EventType.READY,
        actor=actor,
    )
    return True


@transaction.atomic
def confirm_freelancer_readiness(member: RoomMember, actor) -> bool:
    """Подтверждение готовности фрилансера + пересчёт активации проекта.

    Готовность подтверждает сам участник (или суперпользователь). Повторное
    подтверждение безопасно: статус уже READY, событие в ленту второй раз не
    пишется, активация проекта пересчитывается идемпотентно.

    Возвращает True, если готовность зафиксирована именно этим вызовом.
    """
    if member.role_in_room != RoomMember.RoleInRoom.FREELANCER:
        raise StaffingError('Подтверждение готовности — для фрилансеров.')
    if actor.id != member.user_id and not actor.is_superuser:
        raise PermissionDenied('Подтвердить готовность может только сам участник.')

    changed = member.ready_status != RoomMember.ReadyStatus.READY
    if changed:
        member.ready_status = RoomMember.ReadyStatus.READY
        member.save(update_fields=['ready_status'])
        log_room_activity(
            member.room,
            f'{member.user.full_name} готов к работе.',
            RoomActivity.EventType.READY,
            actor=actor,
        )

    sync_project_activation(member.room.project, actor=actor)
    return changed
