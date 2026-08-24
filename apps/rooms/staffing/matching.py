"""Read-only Matching Engine: подбор фрилансеров на функциональный слот комнаты.

Модуль отвечает на четыре вопроса и больше ни на что:

* какие фрилансеры подходят конкретному `RoomFunctionSlot` (hard filters);
* в каком порядке их показывать (ranking);
* кто сейчас лучший кандидат (`get_best_candidate`);
* кто следующий лучший после уже показанных по этому слоту (`get_next_candidate`).

Движок **не пишет в БД**: он не назначает `RoomMember`, не создаёт и не меняет
`RoomSlotCandidate`, не трогает статус проекта и ленту комнаты. Историю
кандидатов он только читает. Назначение — отдельный этап и отдельный сервис.

Границы модулей (ADR-001): ROOM читает модели BIZ (`rooms.staffing → profiles.models`),
обратный импорт запрещён; `apps.pipeline` здесь не используется. Фильтры подбора
живут только тут — дублировать их во views, services или в `apps.profiles` нельзя.
"""

from django.db.models import Exists, OuterRef, QuerySet

from apps.profiles.models import FreelancerProfile
from apps.users.models import User

from ..models import RoomFunctionSlot, RoomMember, RoomSlotCandidate

__all__ = [
    'CANDIDATE_ORDERING',
    'CHANNEL_REQUIREMENTS',
    'get_ranked_candidates',
    'get_best_candidate',
    'get_next_candidate',
]

#: Порядок показа кандидатов. Бизнес-метрики берутся из профиля как есть:
#: собственной формулы рейтинга у подбора нет, `rating` и `acceptance_rate`
#: здесь не пересчитываются. Завершающий `pk` — детерминированный tie-breaker:
#: кандидаты с одинаковыми метриками не должны меняться местами между запросами.
CANDIDATE_ORDERING = ('-rating', '-acceptance_rate', '-experience_projects', 'pk')

#: Канал слота → обязательный структурированный признак профиля.
#: `Channel.ANY` в таблице отсутствует: такой слот каналом не ограничивает.
#: Свободный текст `FreelancerProfile.skills` в подборе не участвует вообще.
CHANNEL_REQUIREMENTS = {
    RoomFunctionSlot.Channel.COLD_CALLING: 'does_cold_calling',
    RoomFunctionSlot.Channel.LINKEDIN: 'does_linkedin_outreach',
}


def get_ranked_candidates(
    slot: RoomFunctionSlot,
    *,
    exclude_seen: bool = False,
) -> QuerySet:
    """Подходящие слоту профили фрилансеров в порядке показа.

    Единственный источник фильтрации подбора: `get_best_candidate` и
    `get_next_candidate` — тонкие обёртки над этой функцией.

    Hard filters (все выполняются в SQL, одновременно):

    * `User.status == active`;
    * `is_available=True`;
    * `is_verified=True`;
    * непустой `video_url`;
    * `level == slot.required_level`;
    * канал соответствует `slot.required_channel`;
    * пользователь ещё не `RoomMember` этой комнаты.

    При `exclude_seen=True` дополнительно исключаются кандидаты, по которым
    в **этом** слоте уже есть запись `RoomSlotCandidate` (любой outcome:
    shown / assigned / skipped / declined). История другого слота на выборку
    не влияет — связь идёт по конкретному `slot_id`.

    Возвращает ленивый `QuerySet` с `select_related('user')`: фильтрация,
    сортировка и выборка top-1 остаются на стороне БД, кандидаты не
    выгружаются в Python и не фильтруются списком.
    """
    if not slot.is_active:
        # Закрытый слот в подборе не участвует (см. RoomFunctionSlot.is_active).
        return FreelancerProfile.objects.none()

    queryset = (
        FreelancerProfile.objects.select_related('user')
        .filter(
            user__status=User.Status.ACTIVE,
            is_available=True,
            is_verified=True,
            # Значения RoomFunctionSlot.Grade и FreelancerProfile.Level совпадают
            # намеренно (см. модели): BIZ и ROOM не импортируют друг друга,
            # поэтому грейд сравнивается по значению, а не по общему enum.
            level=slot.required_level,
        )
        # NULL и '' одинаково не проходят: сравнение с NULL даёт unknown.
        .exclude(video_url='')
    )
    queryset = _apply_channel_requirement(queryset, slot)

    already_in_room = RoomMember.objects.filter(
        room_id=slot.room_id,
        user_id=OuterRef('user_id'),
    )
    queryset = queryset.filter(~Exists(already_in_room))

    if exclude_seen:
        seen_on_this_slot = RoomSlotCandidate.objects.filter(
            slot_id=slot.pk,
            candidate_id=OuterRef('user_id'),
        )
        queryset = queryset.filter(~Exists(seen_on_this_slot))

    return queryset.order_by(*CANDIDATE_ORDERING)


def get_best_candidate(slot: RoomFunctionSlot):
    """Лучший подходящий кандидат слота или `None`, если пул пуст.

    Ничего не записывает: показ кандидата фиксирует вызывающий код будущего
    этапа, а не сам подбор.
    """
    return get_ranked_candidates(slot).first()


def get_next_candidate(slot: RoomFunctionSlot):
    """Лучший кандидат из пула без уже учтённых в истории этого слота, или `None`.

    Отличается от `get_best_candidate` ровно одним условием — исключением
    существующих `RoomSlotCandidate` этого слота. Ничего не записывает.
    """
    return get_ranked_candidates(slot, exclude_seen=True).first()


def _apply_channel_requirement(queryset: QuerySet, slot: RoomFunctionSlot) -> QuerySet:
    """Добавляет фильтр по каналу; для `Channel.ANY` queryset не меняется.

    Признаки независимы: профиль с обоими `True` подходит и cold-, и
    LinkedIn-слоту.
    """
    required_field = CHANNEL_REQUIREMENTS.get(slot.required_channel)
    if required_field is None:
        return queryset
    return queryset.filter(**{required_field: True})
