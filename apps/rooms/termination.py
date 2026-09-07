"""Расторжение с фрилансером: конечный автомат кейса и доменные операции.

Отдельный модуль, а не часть `services.py`, по той же причине, что и `chat.py`:
у расторжения свой небольшой набор операций и собственный инвариант («открытый
кейс на пару комната+фрилансер ровно один»), который нельзя растворять среди
проекта, комнаты, команды и оплаты.

Границы ответственности:

* здесь нет `request`, `messages`, шаблонов и редиректов — только модели;
* здесь нет RBAC вкладок и правил доступа к комнате: кто и что видит, решает
  `services`/view. Единственная проверка актора, которая живёт здесь, —
  «уведомление отправляет тимлид этого проекта»: это доменное правило самого
  расторжения, а не правило навигации;
* `apps.pipeline` не импортируется. Завершение кейса **не трогает** задачи,
  лиды, отчёты и начисления: работа архивного участника блокируется правами,
  а не переписыванием истории;
* `apps.rooms.services` не импортируется тоже. Публичным фасадом ROOM остаётся
  `services`, и он будет реэкспортировать эти функции — импорт в обратную
  сторону замкнул бы граф, поэтому событие ленты пишется локальным приватным
  хелпером в том же формате, что `services.log_room_activity`.

Состояния кейса и переходы описаны в `ALLOWED_TRANSITIONS`. Автомат строгий:
переход из терминального состояния — ошибка, а не тихий no-op, иначе двойное
нажатие «Отозвать» после архива выглядело бы как успех.

Чат расторжения живёт здесь же, на `TerminationMessage`, и с чатом комнаты не
пересекается: ни `RoomChatMessage`, ни его каналы (`team`,
`director_teamlead`) не задействованы. Из `chat.py` берутся только числовые
лимиты — общий предел длины сообщения не делает контуры одним чатом, а второе
значение «2000» в коде рано или поздно разошлось бы с первым.
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.utils import timezone

from .chat import CHAT_HISTORY_LIMIT, CHAT_MESSAGE_MAX_LENGTH
from .models import (
    OPEN_TERMINATION_STATUSES,
    FreelancerTermination,
    RoomActivity,
    RoomMember,
    TerminationMessage,
)

__all__ = [
    'ALLOWED_TRANSITIONS',
    'TERMINATION_NOTICE_DAYS',
    'TERMINATION_REASON_MIN_LENGTH',
    'InvalidTerminationTransition',
    'TerminationAlreadyOpen',
    'TerminationError',
    'appeal_termination',
    'complete_termination',
    'finalize_expired_termination_for',
    'finalize_expired_terminations',
    'has_open_freelancer_termination',
    'initiate_termination',
    'open_termination_for',
    'open_terminations_by_freelancer',
    'post_termination_message',
    'recent_termination_messages',
    'revoke_termination',
]

#: Сколько суток у фрилансера есть на ответ. По истечении срока молчание
#: приравнивается к уходу (`finalize_expired_terminations`).
TERMINATION_NOTICE_DAYS = 3

#: Нижняя граница длины причины. Дублируется формой позже, но проверяется и
#: здесь: сервис нельзя вызвать в обход валидации формы, а расторжение без
#: внятной причины — ровно то, ради чего этот процесс заводился.
TERMINATION_REASON_MIN_LENGTH = 20

_Status = FreelancerTermination.Status

#: Разрешённые переходы автомата. `completed` и `revoked` терминальны и в
#: таблице присутствуют с пустым набором — отсутствие ключа означало бы
#: «состояние неизвестно», а это другой случай.
#:
#: Переход `notice_sent → appeal_pending` выполняет только
#: `appeal_termination`, и он неотделим от письма в поддержку: статус
#: сохраняется после успешной отправки, в одной транзакции с ней.
ALLOWED_TRANSITIONS = {
    _Status.NOTICE_SENT: frozenset({
        _Status.APPEAL_PENDING,
        _Status.COMPLETED,
        _Status.REVOKED,
    }),
    _Status.APPEAL_PENDING: frozenset({
        _Status.COMPLETED,
        _Status.REVOKED,
    }),
    _Status.COMPLETED: frozenset(),
    _Status.REVOKED: frozenset(),
}


class TerminationError(Exception):
    """Расторжение невозможно в текущем состоянии данных."""


class TerminationAlreadyOpen(TerminationError):
    """На пару комната+фрилансер уже есть незакрытый кейс.

    Несёт сам кейс: вызывающему коду он нужен не для текста ошибки, а чтобы
    открыть существующее расторжение вместо создания второго.
    """

    def __init__(self, case, message=None):
        self.case = case
        super().__init__(message or 'По этому фрилансеру уже идёт расторжение.')


class InvalidTerminationTransition(TerminationError):
    """Переход не разрешён автоматом (в том числе из терминального статуса)."""

    def __init__(self, current, target, message=None):
        self.current = current
        self.target = target
        super().__init__(
            message
            or f'Переход {current} → {target} для кейса расторжения запрещён.'
        )


# ---------------------------------------------------------------------------
# Селекторы
# ---------------------------------------------------------------------------


def open_termination_for(room, freelancer):
    """Незакрытый кейс расторжения по паре комната+фрилансер или `None`.

    Только `notice_sent` / `appeal_pending`: завершённые и отозванные кейсы
    остаются в истории и на текущее состояние не влияют. Прав доступа функция
    не проверяет — это селектор, а не гейт.
    """
    return (
        FreelancerTermination.objects
        .filter(
            room=room,
            freelancer=freelancer,
            status__in=OPEN_TERMINATION_STATUSES,
        )
        .select_related('initiated_by', 'member')
        .first()
    )


def open_terminations_by_freelancer(room) -> dict:
    """`{user_id: кейс}` для всех незакрытых расторжений комнаты, один запрос.

    Нужен списочным экранам: состав команды показывает признак «идёт
    расторжение» у каждой строки, и спрашивать про каждого участника
    отдельно значило бы завести N+1 на вкладке «Команда».

    Ключ — `freelancer_id`, а не `member_id`: `member` у кейса `SET_NULL` и
    может быть пустым, а пользователь — никогда.
    """
    return {
        case.freelancer_id: case
        for case in FreelancerTermination.objects.filter(
            room=room,
            status__in=OPEN_TERMINATION_STATUSES,
        )
    }


def has_open_freelancer_termination(room, freelancer) -> bool:
    """Идёт ли по этому человеку расторжение в этой комнате.

    Предикат для будущих блокировок работы. Существование строки проверяется
    отдельным `exists()`, а не через `open_termination_for`: вызывающему нужен
    ответ «да/нет», а не объект со связями.
    """
    return (
        FreelancerTermination.objects
        .filter(
            room=room,
            freelancer=freelancer,
            status__in=OPEN_TERMINATION_STATUSES,
        )
        .exists()
    )


# ---------------------------------------------------------------------------
# Операции
# ---------------------------------------------------------------------------


@transaction.atomic
def initiate_termination(*, room, member, initiated_by, reason):
    """Отправляет фрилансеру уведомление о расторжении и заводит кейс.

    Членство и слот на этом шаге **не меняются**: место в команде остаётся
    занятым, пока фрилансер не ушёл или срок не истёк. Задачи, лиды, отчёты и
    начисления не затрагиваются вовсе.

    Второй открытый кейс на ту же пару не создаётся. Проверка на дубль есть и
    в Python (ради внятного ответа интерфейсу), и в БД (частичный уникальный
    индекс). Python-проверка первична для UX, индекс — защита от гонки, и её
    срабатывание переводится в тот же `TerminationAlreadyOpen`, а не в 500.
    """
    if member.room_id != room.id:
        raise TerminationError('Участник состоит в другой комнате.')
    if member.role_in_room != RoomMember.RoleInRoom.FREELANCER:
        raise TerminationError('Расторжение оформляется только с фрилансером.')
    if not member.is_active:
        raise TerminationError('Участник уже в архиве комнаты.')

    # Единственная проверка актора в модуле: уведомление отправляет тимлид
    # этого проекта. `PermissionDenied` — как в подборе (`staffing.services`):
    # отказ по правам обязан оставаться отказом по правам, а не сообщением.
    if initiated_by is None or initiated_by.id != room.project.teamlead_id:
        raise PermissionDenied(
            'Расторжение с фрилансером оформляет тимлид проекта.'
        )

    text = (reason or '').strip()
    if len(text) < TERMINATION_REASON_MIN_LENGTH:
        raise TerminationError(
            'Причина расторжения — минимум '
            f'{TERMINATION_REASON_MIN_LENGTH} символов.'
        )

    existing = open_termination_for(room, member.user)
    if existing is not None:
        raise TerminationAlreadyOpen(existing)

    initiated_at = timezone.now()
    try:
        # Вложенная точка сохранения: два быстрых запроса упираются в частичный
        # уникальный индекс, и второй обязан получить существующий кейс, а не
        # необработанную ошибку БД.
        with transaction.atomic():
            return FreelancerTermination.objects.create(
                room=room,
                freelancer=member.user,
                member=member,
                initiated_by=initiated_by,
                reason=text,
                status=_Status.NOTICE_SENT,
                initiated_at=initiated_at,
                deadline_at=initiated_at + timedelta(days=TERMINATION_NOTICE_DAYS),
            )
    except IntegrityError as exc:
        concurrent = open_termination_for(room, member.user)
        if concurrent is None:
            raise
        raise TerminationAlreadyOpen(concurrent) from exc


@transaction.atomic
def complete_termination(case, *, completed_at=None, actor=None):
    """Завершает расторжение: членство уходит в архив, слот освобождается.

    `RoomMember.delete()` не вызывается ни при каких условиях. Строка членства
    сохраняется целиком — вместе с ней остаются задачи, лиды, отчёты,
    начисления и сообщения командного чата этого человека. Роль в комнате тоже
    остаётся `freelancer`: архив описывается `is_active`, а не подменой роли.

    `ready_status` сознательно не сбрасывается: это историческое значение
    («он подтверждал готовность»), а повторное подтверждение — часть будущего
    сценария повторного найма, а не этой операции.

    Статус проекта не меняется.
    """
    timestamp = completed_at or timezone.now()
    locked = _lock_for_transition(case, _Status.COMPLETED)

    member = _member_for(locked)
    if member is not None:
        member.is_active = False
        member.left_at = timestamp
        member.function_slot = None
        member.save(update_fields=['is_active', 'left_at', 'function_slot'])

    locked.status = _Status.COMPLETED
    locked.completed_at = timestamp
    locked.save(update_fields=['status', 'completed_at'])

    # Причина в ленту не попадает: лента комнаты видна всей команде, а
    # формулировка тимлида адресована конкретному человеку и живёт в кейсе.
    name = locked.freelancer.full_name
    _log_member_removed(
        locked.room,
        f'Расторжение: {name} покинул проект.',
        actor=actor,
    )
    return locked


@transaction.atomic
def appeal_termination(case, *, admin_url, appealed_at=None):
    """Фрилансер оспаривает расторжение: письмо в поддержку и стоп таймера.

    Порядок шагов принципиален: блокировка и проверка автомата → отправка
    письма → сохранение статуса. Письмо уходит **до** записи и с
    `fail_silently=False`, поэтому упавшая отправка откатывает всю операцию:
    кейс остаётся `notice_sent`, `appealed_at` пустым, а фрилансер видит
    ошибку и может повторить. Обратный порядок оставил бы человека в статусе
    «ждём решения поддержки», о котором поддержка никогда не узнает.

    `transaction.on_commit()` здесь неприменим ровно поэтому: он выполнил бы
    отправку после фиксации, и сбой SMTP уже не смог бы отменить статус.

    `deadline_at` не меняется: таймер останавливает сам статус —
    `finalize_expired_terminations` отбирает только `notice_sent`. Членство и
    слот не трогаются: человек ещё в команде, просто работать не может.
    """
    url = (admin_url or '').strip()
    if not url:
        raise TerminationError(
            'Для протеста нужна ссылка на кейс: поддержке иначе некуда идти.'
        )

    timestamp = appealed_at or timezone.now()
    locked = _lock_for_transition(case, _Status.APPEAL_PENDING)

    _send_appeal_email(locked, admin_url=url)

    locked.status = _Status.APPEAL_PENDING
    locked.appealed_at = timestamp
    locked.save(update_fields=['status', 'appealed_at'])
    return locked


@transaction.atomic
def revoke_termination(case, *, revoked_at=None):
    """Отзывает расторжение: кейс закрывается, состав команды не менялся.

    Членство и слот здесь не трогаются намеренно — до завершения кейса они
    не архивировались, и «отозвать» означает просто снять уведомление.
    После `completed` отзыв запрещён: человек уже вышел из проекта.

    В ленту комнаты отзыв не пишется: `MEMBER_REMOVED` здесь был бы прямой
    неправдой (никого не удаляли), а заводить событие под отзыв — продуктовое
    решение уровня интерфейса, а не доменной операции.
    """
    timestamp = revoked_at or timezone.now()
    locked = _lock_for_transition(case, _Status.REVOKED)

    locked.status = _Status.REVOKED
    locked.revoked_at = timestamp
    locked.save(update_fields=['status', 'revoked_at'])
    return locked


def finalize_expired_termination_for(room, freelancer, *, now=None):
    """Завершает просроченное уведомление конкретного человека, если оно есть.

    Ленивая половина правила «три дня»: та же операция, что делает
    `finalize_expired_terminations` пакетно по cron, но для одной пары
    комната+фрилансер — чтобы срок срабатывал на первом же заходе в комнату,
    а не ждал команды.

    Берётся только `notice_sent`: у оспоренного кейса таймер остановлен, и
    автоматический уход по дедлайну для него не наступает никогда. Возвращает
    завершённый кейс или `None`, если завершать нечего.
    """
    moment = now or timezone.now()
    case = (
        FreelancerTermination.objects
        .filter(
            room=room,
            freelancer=freelancer,
            status=_Status.NOTICE_SENT,
            deadline_at__lte=moment,
        )
        .first()
    )
    if case is None:
        return None
    return complete_termination(case, completed_at=moment)


def finalize_expired_terminations(*, now=None):
    """Завершает кейсы, по которым истёк срок ответа. Возвращает их число.

    Берутся только `notice_sent`: у оспоренного кейса таймер остановлен, и
    автоматический уход по дедлайну для него не наступает никогда.

    Между выборкой и блокировкой строки статус мог измениться — фрилансер
    успел опротестовать, тимлид успел отозвать. Поэтому каждый кейс проходит
    через тот же `complete_termination` с перепроверкой под блокировкой, а
    отказ автомата здесь не ошибка, а нормальный исход гонки: такой кейс
    просто не считается завершённым.
    """
    moment = now or timezone.now()
    expired = (
        FreelancerTermination.objects
        .filter(status=_Status.NOTICE_SENT, deadline_at__lte=moment)
        .select_related('room__project', 'freelancer', 'member')
    )

    finalized = 0
    for case in expired:
        try:
            complete_termination(case, completed_at=moment)
        except InvalidTerminationTransition:
            continue
        finalized += 1
    return finalized


# ---------------------------------------------------------------------------
# Чат расторжения
# ---------------------------------------------------------------------------


@transaction.atomic
def post_termination_message(case, *, author, text):
    """Добавляет сообщение в приватный тред расторжения.

    Тред строго на двоих: фрилансер и тимлид, отправивший уведомление.
    Директор, другие тимлиды, остальные фрилансеры комнаты и менеджеры
    участниками не являются — у расторжения нет «наблюдателей».

    Кейс перечитывается под блокировкой: между открытием страницы и отправкой
    его могли завершить или отозвать, и сообщение не должно попадать в
    закрытый тред. Закрытый кейс — `TerminationError`, а не
    `InvalidTerminationTransition`: отправка сообщения не является переходом
    автомата, и второй смысл у этого исключения появляться не должен.

    Текст сохраняется как есть, обычной строкой. Ни `mark_safe`, ни какой-либо
    обработки HTML здесь нет и быть не может: экранирование — обязанность
    шаблона, а «подготовленная» разметка в БД превратила бы её в дыру.
    """
    locked = _lock_open_case(case)
    _assert_thread_participant(locked, author)

    body = (text or '').strip()
    if not body:
        raise TerminationError('Сообщение не может быть пустым.')
    if len(body) > CHAT_MESSAGE_MAX_LENGTH:
        raise TerminationError(
            f'Сообщение длиннее {CHAT_MESSAGE_MAX_LENGTH} символов.'
        )

    return TerminationMessage.objects.create(
        case=locked,
        author=author,
        text=body,
    )


def recent_termination_messages(case, *, limit=CHAT_HISTORY_LIMIT):
    """Последние сообщения треда, старые → новые.

    Срез берётся по убыванию времени (это последние сообщения, а не первые) и
    разворачивается в памяти уже после лимита — для ленты, где новое внизу.
    `select_related('author')` убирает N+1 при опросе.

    Статус кейса здесь не проверяется намеренно: переписка остаётся в БД и
    после завершения расторжения, а решение «показывать ли её закрытому кейсу»
    принимает слой доступа, а не селектор. RBAC чтения здесь тоже нет —
    это read-only выборка, а не гейт.
    """
    if limit <= 0:
        raise TerminationError('Размер выборки истории должен быть положительным.')

    newest_first = (
        TerminationMessage.objects
        .filter(case=case)
        .select_related('author')
        .order_by('-created_at')[:limit]
    )
    return list(reversed(newest_first))


# ---------------------------------------------------------------------------
# Внутреннее
# ---------------------------------------------------------------------------


def _lock_open_case(case):
    """Перечитывает кейс под блокировкой и требует, чтобы он был открыт."""
    locked = _locked_case(case)
    if locked.status not in OPEN_TERMINATION_STATUSES:
        raise TerminationError(
            'Кейс расторжения закрыт: переписка в нём больше не ведётся.'
        )
    return locked


def _assert_thread_participant(case, author):
    """Пускает в тред только фрилансера и инициатора расторжения.

    `initiated_by` — `SET_NULL`: если учётной записи тимлида больше нет,
    участником остаётся один фрилансер, а «пустой» инициатор не превращается
    в пропуск для анонима.
    """
    author_id = getattr(author, 'id', None)
    participants = {case.freelancer_id}
    if case.initiated_by_id:
        participants.add(case.initiated_by_id)

    if author_id is None or author_id not in participants:
        raise PermissionDenied(
            'Чат расторжения доступен только фрилансеру и тимлиду, '
            'отправившему уведомление.'
        )


def _locked_case(case):
    """Перечитывает кейс из БД под блокировкой строки.

    Статус берётся из БД, а не из переданного объекта: он мог устареть между
    чтением страницы и нажатием кнопки. На SQLite `select_for_update()` штатно
    вырождается в no-op, на PostgreSQL начинает сериализовать операции без
    правок кода; перепроверка состояния после блокировки обязательна в обоих
    случаях и является настоящей защитой от двойного завершения.
    """
    locked = (
        FreelancerTermination.objects
        .select_for_update()
        .select_related('room__project', 'freelancer', 'member')
        .filter(pk=case.pk)
        .first()
    )
    if locked is None:
        raise TerminationError('Кейс расторжения не найден.')
    return locked


def _lock_for_transition(case, target):
    """Блокирует кейс и проверяет допустимость перехода по `ALLOWED_TRANSITIONS`."""
    locked = _locked_case(case)
    if target not in ALLOWED_TRANSITIONS.get(locked.status, frozenset()):
        raise InvalidTerminationTransition(locked.status, target)
    return locked


def _member_for(case):
    """Строка членства, которую нужно архивировать, или `None`.

    `case.member` — `SET_NULL`, и замена исполнителя на слоте могла обнулить
    ссылку. Тогда участник ищется по паре комната+пользователь: `unique(room,
    user)` гарантирует, что таких строк не больше одной.
    """
    if case.member_id:
        return case.member
    return RoomMember.objects.filter(
        room_id=case.room_id, user_id=case.freelancer_id,
    ).first()


def _send_appeal_email(case, *, admin_url):
    """Письмо в поддержку о протесте против расторжения.

    Plain text и никакого шаблона: адресат внутренний, письмо читают глазами
    один раз и идут по ссылке в админку. HTML-версия добавила бы шаблон,
    который некому поддерживать.

    Паттерн повторяет `apps.users.activation` (`EmailMultiAlternatives` +
    `DEFAULT_FROM_EMAIL`), но модуль оттуда ничего не импортирует: ROOM не
    должен зависеть от BIZ ради отправки почты.

    `fail_silently=False` обязателен — на нём держится контракт отката в
    `appeal_termination`.
    """
    project = case.room.project
    freelancer = case.freelancer
    initiator = case.initiated_by

    initiator_line = (
        f'{initiator.full_name} <{initiator.email}>' if initiator else 'не указан'
    )
    body = (
        'Фрилансер оспаривает расторжение в комнате WowLance.\n\n'
        f'Проект: {project.name}\n'
        f'Фрилансер: {freelancer.full_name} <{freelancer.email}>\n'
        f'Инициатор расторжения (тимлид): {initiator_line}\n'
        f'Уведомление отправлено: {case.initiated_at:%d.%m.%Y %H:%M}\n\n'
        'Причина, указанная тимлидом:\n'
        f'{case.reason}\n\n'
        'Кейс в админке (оставить в силе / отклонить расторжение):\n'
        f'{admin_url}\n'
    )
    message = EmailMultiAlternatives(
        f'WowLance: протест против расторжения — {project.name}',
        body,
        settings.DEFAULT_FROM_EMAIL,
        [settings.SUPPORT_EMAIL],
    )
    message.send(fail_silently=False)
    return message


def _log_member_removed(room, message, *, actor=None):
    """Событие ленты комнаты в том же формате, что `services.log_room_activity`.

    Прямое создание записи, а не импорт сервиса: `services` — публичный фасад
    ROOM и будет реэкспортировать функции этого модуля, поэтому обратный
    импорт замкнул бы граф на уровне модулей.
    """
    return RoomActivity.objects.create(
        room=room,
        actor=actor,
        event_type=RoomActivity.EventType.MEMBER_REMOVED,
        message=message,
    )
