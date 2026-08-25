"""Read-only сборка данных подбора для шаблонов ROOM.

Здесь нет ни одной записи в БД и ни одного правила подбора: hard filters и
ranking живут только в `matching.py`, транзакции — только в `services.py`.
Модуль отвечает за то, чтобы вкладка «Команда» и «Обзор» получали готовые
карточки слотов одним набором запросов, без N+1 на участника и его профиль.

SLA подбора (MVP)
-----------------

Пустой активный слот показывает обратный отсчёт «сколько осталось на подбор».
**Якорь отсчёта — `RoomFunctionSlot.created_at`**: отдельного
`search_started_at` в модели сейчас нет, а заводить его — миграция и
отдельное продуктовое решение (когда именно «начался поиск»: создание слота,
освобождение слота, первый показанный кандидат). Пока честно: слот появился —
подбор пошёл.

Дедлайн вычисляется при каждом чтении и **нигде не сохраняется**: GET не
пишет в БД и не трогает слот. Просрочка считается на сервере, поэтому
страница остаётся правдивой при выключенном JavaScript, а таймер в браузере
только перерисовывает уже полученное значение.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from .. import functional_roles
from ..models import RoomFunctionSlot, RoomMember

__all__ = [
    'SEARCH_SLA',
    'SEARCH_SLA_OVERDUE_LABEL',
    'SEARCH_SLA_PREFIX',
    'SLOT_STATUS_LABELS',
    'SlotCard',
    'format_countdown',
    'slot_card_for',
    'slot_cards',
    'staffing_summary',
]

#: Сколько времени MVP отводит на подбор исполнителя в пустой активный слот.
#: Не путать с SLA стартовой задачи проекта (24 часа,
#: `apps.pipeline.services.START_CALLS_SLA`): это разные сроки разных
#: сущностей, и общей константы у них быть не должно.
SEARCH_SLA = timedelta(hours=1)

#: Подписи таймера. Константы, а не литералы шаблона: их проверяют тесты.
SEARCH_SLA_PREFIX = 'SLA'
SEARCH_SLA_OVERDUE_LABEL = 'SLA 1ч просрочен'


def format_countdown(seconds: int) -> str:
    """Остаток секунд → `HH:MM:SS`.

    Собственное форматирование, а не `timeuntil`: продукту нужен именно
    секундный отсчёт («00:42:17»), а фильтр Django округляет до минут и
    часов. Отрицательный остаток сюда не попадает — просрочка это отдельная
    подпись, а не «-00:00:05».
    """
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}'


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

    @property
    def role_label(self) -> str:
        """Публичное название функции слота вместо машинного `role_key`.

        Берётся из структурного каталога (`functional_roles.role_label`),
        поэтому подписи слота и строки состава не расходятся, а исторический
        ключ старой комнаты не роняет страницу — он показывается как есть.
        """
        return functional_roles.role_label(self.slot.role_key)

    @property
    def grade_label(self) -> str:
        """Подпись требуемого грейда слота — из его же enum, не из каталога.

        Требования зафиксированы в самом слоте (`required_level`), и именно
        по ним идёт подбор; каталог мог измениться после создания слота.
        """
        return self.slot.get_required_level_display()

    # --- SLA подбора --------------------------------------------------
    #
    # Показывается только пустому активному слоту: занятый слот уже
    # подобран, а закрытый в подборе не участвует. Ни одно из свойств ниже
    # ничего не пишет и не меняет слот.

    @property
    def is_searching(self) -> bool:
        """Идёт ли по этому слоту подбор прямо сейчас."""
        return self.member is None and self.slot.is_active

    @property
    def search_deadline(self):
        """Абсолютный дедлайн SLA подбора или None.

        `created_at + SEARCH_SLA`. Значение вычисляемое: в БД дедлайна нет
        и появляться от чтения страницы он не должен.
        """
        if not self.is_searching:
            return None
        return self.slot.created_at + SEARCH_SLA

    @property
    def search_seconds_left(self) -> int:
        """Остаток SLA в секундах (не меньше нуля). Считает сервер."""
        deadline = self.search_deadline
        if deadline is None:
            return 0
        return max(int((deadline - timezone.now()).total_seconds()), 0)

    @property
    def search_is_overdue(self) -> bool:
        """Истёк ли час на подбор. Вычисляется на сервере, а не в браузере."""
        deadline = self.search_deadline
        return deadline is not None and deadline <= timezone.now()

    @property
    def search_sla_prefix(self) -> str:
        """Подпись «SLA» для разметки — из константы, не из шаблона."""
        return SEARCH_SLA_PREFIX

    @property
    def search_sla_overdue_label(self) -> str:
        """Текст просрочки для разметки и для визуального отсчёта.

        Отдаётся в data-атрибут, поэтому скрипт в браузере не содержит
        собственного написания той же фразы.
        """
        return SEARCH_SLA_OVERDUE_LABEL

    @property
    def search_sla_display(self) -> str:
        """Готовая подпись таймера: `SLA: 00:42:17` либо `SLA 1ч просрочен`.

        Собирается на сервере, поэтому строка видна и без JavaScript —
        скрипт в браузере только перерисовывает её раз в секунду.
        """
        if not self.is_searching:
            return ''
        if self.search_is_overdue:
            return SEARCH_SLA_OVERDUE_LABEL
        return f'{SEARCH_SLA_PREFIX}: {format_countdown(self.search_seconds_left)}'


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
