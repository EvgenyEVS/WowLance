"""Read-only сборка данных подбора для шаблонов ROOM.

Здесь нет ни одной записи в БД и ни одного правила подбора: hard filters и
ranking живут только в `matching.py`, транзакции — только в `services.py`.
Модуль отвечает за то, чтобы вкладка «Команда» и «Обзор» получали готовые
карточки слотов одним набором запросов, без N+1 на участника и его профиль.
"""

from dataclasses import dataclass

from ..models import RoomFunctionSlot, RoomMember

__all__ = [
    'SLOT_STATUS_LABELS',
    'SlotCard',
    'slot_card_for',
    'slot_cards',
    'staffing_summary',
]

#: Статус слота для UI. Ключи стабильны (используются в шаблоне и тестах),
#: подписи — русские, как и остальной интерфейс.
SLOT_STATUS_LABELS = {
    'empty': 'Свободен',
    'assigned': 'Кандидат назначен',
    'ready': 'Готов к работе',
    'declined': 'Отказался',
}


@dataclass(frozen=True)
class SlotCard:
    """Всё, что карточка слота показывает, посчитано заранее."""

    slot: RoomFunctionSlot
    member: RoomMember | None
    profile: object | None
    status: str

    @property
    def status_label(self) -> str:
        return SLOT_STATUS_LABELS[self.status]

    @property
    def is_filled(self) -> bool:
        return self.member is not None

    @property
    def assigned_at(self):
        """Момент назначения текущего исполнителя.

        Отдельной колонки под SLA-таймер не заводим: `RoomMember.joined_at`
        уже фиксирует, когда человек занял слот, и обнуляется при замене,
        потому что «Другой сейлер» создаёт нового участника.
        """
        return self.member.joined_at if self.member else None


def _status_for(member: RoomMember | None) -> str:
    if member is None:
        return 'empty'
    if member.ready_status == RoomMember.ReadyStatus.READY:
        return 'ready'
    if member.ready_status == RoomMember.ReadyStatus.DECLINED:
        return 'declined'
    return 'assigned'


def _card_queryset():
    """Слоты вместе с участником и его профилем — без N+1 на карточку."""
    return RoomFunctionSlot.objects.select_related(
        'member',
        'member__user',
        'member__user__freelancer_profile',
    )


def _build_card(slot: RoomFunctionSlot) -> SlotCard:
    member = slot.assigned_member
    profile = None
    if member is not None:
        # Reverse OneToOne уже в select_related: обращение не делает запрос,
        # а у участника без профиля атрибут просто отсутствует.
        profile = getattr(member.user, 'freelancer_profile', None)
    return SlotCard(slot=slot, member=member, profile=profile, status=_status_for(member))


def slot_cards(room) -> list[SlotCard]:
    """Карточки активных функциональных слотов комнаты.

    Один запрос на все слоты: участник и его профиль подтягиваются
    `select_related`, поэтому число слотов не превращается в число запросов.
    Профиль может отсутствовать (участник добавлен старым ручным потоком) —
    тогда карточка просто показывает данные пользователя без метрик.
    """
    slots = (
        _card_queryset()
        .filter(room=room, is_active=True)
        .order_by('role_key', 'slot_index')
    )
    return [_build_card(slot) for slot in slots]


def slot_card_for(slot: RoomFunctionSlot) -> SlotCard:
    """Свежая карточка одного слота — для HTMX-ответа после операции подбора."""
    return _build_card(_card_queryset().get(pk=slot.pk))


def staffing_summary(cards: list[SlotCard]) -> dict:
    """Компактная сводка для «Обзора»: всего / заполнено / готовы / в поиске.

    Считается по уже собранным карточкам, поэтому Обзор не делает второй
    независимый набор запросов и не заводит вторую копию бизнес-правил.
    """
    total = len(cards)
    filled = sum(1 for card in cards if card.is_filled)
    ready = sum(1 for card in cards if card.status == 'ready')
    declined = sum(1 for card in cards if card.status == 'declined')
    return {
        'total': total,
        'filled': filled,
        'ready': ready,
        'searching': total - filled,
        'declined': declined,
        'complete': total > 0 and ready == total,
    }
