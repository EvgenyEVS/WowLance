"""Сервисы для проектов и комнат."""

from dataclasses import dataclass

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.users.models import User
from . import presets
from .models import (
    Project,
    Room,
    RoomActivity,
    RoomFunctionSlot,
    RoomMember,
    TeamleadInvite,
)
from .unit_economics import (  # noqa: F401  (публичный фасад модуля ROOM)
    apply_package_to_project,
    get_unit_economics_summary,
    update_project_functional_roles,
    user_can_edit_functional_roles,
    user_can_view_composition_staffing,
    user_can_view_unit_economics_finance,
)

# Состав функциональных ролей и юнит-экономика реализованы в
# `apps.rooms.unit_economics`, но публичной точкой входа модуля ROOM остаётся
# `apps.rooms.services` — как и для остальных операций над проектом.
# Реэкспорт, а не перенос кода: держать снапшот, каталог и расчёты рядом
# друг с другом полезнее, чем сваливать их в общий сервисный модуль.
# Цикла импорта нет: `unit_economics` зависит от `models` и `presets`,
# но не от `services`.

# Заглушка суммы тестовой оплаты запуска проекта.
# Без тарифной логики и расчётов: временная константа до боевого платёжного шлюза.
TEST_LAUNCH_PAYMENT_AMOUNT_LABEL = '₽1 000'

#: Публичный Jitsi для видеокомнаты MVP (ADR-001: «видео — ссылка Jitsi»).
#: Хост и префикс комнаты — константы кода: адрес встречи не приходит из
#: браузера и не настраивается пользователем.
JITSI_BASE_URL = 'https://meet.jit.si'
JITSI_ROOM_PREFIX = 'wowlance-room'
#: Отдельный префикс для приватной встречи директор↔тимлид: не должен
#: совпадать с командным `wowlance-room-<room.id>`.
JITSI_DIRECTOR_TEAMLEAD_PREFIX = 'wowlance-dt'


def room_video_call_url(room: Room) -> str:
    """Постоянная ссылка на видеокомнату проекта.

    Собирается сервером из `Room.id` (UUID) — ничего пользовательского в
    адрес не попадает, поэтому подменить комнату встречи через форму или
    query-параметр нельзя. Схема всегда `https`, даже пока сама платформа
    на демо работает по http: это внешний сервис со своим TLS.

    Хостинг сознательно публичный `meet.jit.si` и без JWT: приватный /
    self-hosted Jitsi и авторизация встреч — отдельное решение по ADR-001,
    вне этого этапа.
    """
    return f'{JITSI_BASE_URL}/{JITSI_ROOM_PREFIX}-{room.id}'


def director_teamlead_video_call_url(project: Project) -> str:
    """Приватная Jitsi-ссылка директор↔тимлид этого проекта.

    Ключ — `Project.id`, а не `Room.id`: контур не должен совпасть с
    командной встречей. JWT и self-hosted не используем (ADR-001, MVP).
    """
    return f'{JITSI_BASE_URL}/{JITSI_DIRECTOR_TEAMLEAD_PREFIX}-{project.id}'


# ---------------------------------------------------------------------------
# Публичный фасад готовности исполнителя
# ---------------------------------------------------------------------------


def confirm_freelancer_readiness(member: RoomMember, actor):
    """Публичная точка входа ROOM: подтверждение готовности исполнителя.

    Реализация живёт в `apps.rooms.staffing.services` рядом с остальными
    операциями подбора и активации проекта — переносить её сюда значило бы
    оторвать её от `sync_project_activation`, с которым она обязана быть в
    одной транзакции. Здесь только фасад модуля, как и для состава команды.

    Импорт **внутри функции** обязателен и не является стилевой мелочью:
    `apps.rooms.staffing.services` импортирует этот модуль на уровне модуля
    (`log_room_activity`, `user_can_manage_team`). Модульный
    `from .staffing.services import …` замкнул бы граф, и порядок импорта
    начал бы решать, поднимется ли приложение. Тот же приём уже применяется
    в `save_functional_roles_and_sync_slots`.

    Семантика не ослабляется: и проверка роли участника, и правило «готовность
    подтверждает сам участник (или суперпользователь)» остаются в сервисе.
    `actor` остаётся обязательным позиционным аргументом — необязательным его
    делать нельзя, иначе исчезнет тот, чьё право проверяется.
    """
    from .staffing.services import confirm_freelancer_readiness as _confirm

    return _confirm(member, actor)


# ---------------------------------------------------------------------------
# Вводные проекта («вижен») — редактирование директором
# ---------------------------------------------------------------------------

#: Четыре ключа вводных проекта («вижен»), которые правит директор.
#:
#: Это **точные** существующие ключи `Project.input_data`, которые уже
#: читают свойства модели (`Project.offer` / `utp` / `audience` /
#: `hot_criteria`) и «Обзор». Новых ключей эта операция не заводит:
#: иначе страница показывала бы одно, а форма писала бы другое.
VISION_INPUT_KEYS: tuple[str, ...] = ('offer', 'utp', 'audience', 'hot_criteria')


def user_can_edit_project_vision(user, project: Project) -> bool:
    """Кто вправе править вводные проекта: владелец **и** директор.

    Оба условия обязательны, поэтому чужой директор вводные не правит, а
    тимлид, фрилансер и менеджер видят их только на чтение.
    `user_can_manage_team` здесь не переиспользуется намеренно: он допускает
    тимлида, а оффер, УТП, ЦА и критерии Hot задаёт заказчик, а не
    исполнитель. `User.Roles.ADMIN` продуктового права тоже не даёт — как и
    для состава команды (`user_can_edit_functional_roles`).
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    if project.owner_id != user.id:
        return False
    return user.role == User.Roles.DIRECTOR


@transaction.atomic
def update_project_vision(project: Project, vision: dict, user) -> Project:
    """Сохраняет четыре вводных проекта, **не трогая остальной `input_data`**.

    `input_data` — общий словарь проекта: кроме вводных там лежат
    `architecture` и снапшот состава команды (`functional_roles`,
    см. `apps.rooms.unit_economics`). Поэтому здесь именно merge поверх
    прочитанного из БД словаря, а не присваивание: сохранение оффера не
    имеет права унести с собой купленный состав и бюджет.

    Из запроса принимаются **только** ключи `VISION_INPUT_KEYS`; всё
    остальное, что прислал бы браузер, игнорируется — общий `input_data`
    целиком клиент задавать не может.

    `Project.budget` не трогается: он равен сумме сохранённого состава и
    меняется только через `update_project_functional_roles`.
    """
    if not user_can_edit_project_vision(user, project):
        raise PermissionDenied('Менять вводные проекта может только директор проекта.')

    input_data = dict(project.input_data or {})
    for key in VISION_INPUT_KEYS:
        if key in vision:
            input_data[key] = vision[key]
    project.input_data = input_data
    project.save(update_fields=['input_data', 'updated_at'])
    return project


def log_room_activity(room: Room, message: str, event_type: str, actor=None) -> RoomActivity:
    return RoomActivity.objects.create(
        room=room,
        actor=actor,
        event_type=event_type,
        message=message,
    )


@transaction.atomic
def ensure_room_for_project(project: Project) -> Room:
    """Создаёт комнату и добавляет директора участником, если ещё нет.

    Новая комната открывается с включённым чатом: он часть комнаты, а не
    дополнительная опция, которую кто-то должен не забыть включить.
    Значение задаётся здесь, в `defaults`, а не в `Room.chat_enabled.default` —
    так у уже существующих комнат ничего не меняется молча, а выключить чат
    конкретной комнате через админку по-прежнему можно: `get_or_create`
    применяет `defaults` только при создании и не перезаписывает их потом.
    """
    room, created = Room.objects.get_or_create(
        project=project,
        defaults={'chat_enabled': True},
    )
    RoomMember.objects.get_or_create(
        room=room,
        user=project.owner,
        defaults={'role_in_room': RoomMember.RoleInRoom.DIRECTOR},
    )
    if project.teamlead_id:
        RoomMember.objects.get_or_create(
            room=room,
            user=project.teamlead,
            defaults={'role_in_room': RoomMember.RoleInRoom.TEAMLEAD},
        )
    return room


# ---------------------------------------------------------------------------
# Состав функциональных ролей + проекция в слоты комнаты
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompositionSaveResult:
    """Итог одного сохранения состава: экономика проекта и что стало со слотами.

    Две части одной операции возвращаются вместе, потому что вместе и
    происходят: `summary` — то, что купил директор, `projection` — то, во
    что это превратилось в комнате.
    """

    #: `apps.rooms.unit_economics.UnitEconomicsSummary`.
    summary: object
    #: `apps.rooms.staffing.projection.SlotProjectionResult`.
    projection: object
    #: Сколько только что открытых слотов остались без кандидата.
    unfilled_opened_slots: int = 0


def _auto_assign_opened_slots(project: Project, projection, actor) -> int:
    """Сажает лучшего кандидата на слоты, открытые **этой** синхронизацией.

    Возвращает число открытых слотов, для которых кандидат не найден
    (пустой пул / no_candidates) — чтобы UI мог предупредить директора.
    """
    from .staffing.services import StaffingError, auto_assign_best_candidate

    unfilled = 0
    for change in projection.changes:
        opened_indexes = change.created + change.reactivated
        if not opened_indexes:
            continue

        opened_slots = (
            RoomFunctionSlot.objects.filter(
                room__project=project,
                role_key=change.role_key,
                slot_index__in=opened_indexes,
                is_active=True,
                member__isnull=True,
            )
            .select_related('room__project')
            .order_by('slot_index')
        )

        for slot in opened_slots:
            try:
                outcome = auto_assign_best_candidate(
                    slot, actor, for_composition_autofill=True
                )
            except StaffingError:
                continue
            if not outcome.assigned:
                unfilled += 1

    return unfilled


@transaction.atomic
def save_functional_roles_and_sync_slots(project: Project, roles_data, user):
    """Единственная точка изменения состава команды: снапшот + слоты комнаты.

    Все write-path конфигуратора (числовой ввод, «+», «−», добавление и
    удаление функции, применение пакета) заканчиваются здесь. Иначе
    появился бы сценарий «кнопкой слоты создаются, пакетом — нет».

    Атомарность обязательна и в этом весь смысл сервиса. Состав, бюджет
    проекта и слоты комнаты — одно состояние: если проекция упирается в
    занятый слот, `Project.input_data` и `Project.budget` тоже обязаны
    остаться прежними. Поэтому `@transaction.atomic` стоит над **обоими**
    шагами, а не только внутри каждого из них.

    Обратите внимание: python-объект `project` после отката остаётся с
    новыми значениями в памяти — БД откатывается, а атрибуты нет. Тот, кто
    строит ответ после ошибки, обязан перечитать проект из БД
    (`_configurator_response` это делает).

    Третий шаг — авто-подбор на только что открытые слоты
    (`_auto_assign_opened_slots`). Он идёт после проекции и в той же
    транзакции: покупка функции и появление на ней исполнителя — одно
    событие для директора, и «состав сохранился, а комната пустая» не
    должно существовать как промежуточное состояние. Пустой пул
    кандидатов при этом не ошибка: слот остаётся пустым, состав
    сохраняется, исключения нет.

    Статус проекта авто-подбор не меняет: переход STAFFING → ACTIVE
    по-прежнему делает только подтверждённая готовность команды
    (`staffing.services.sync_project_activation`).

    Импорт проекции — функцией: `apps.rooms.staffing` импортирует
    `apps.rooms.services`, и модульный импорт обратно замкнул бы граф.
    """
    from .staffing.projection import sync_functional_roles_to_slots

    summary = update_project_functional_roles(project, roles_data, user)
    projection = sync_functional_roles_to_slots(project)
    unfilled = _auto_assign_opened_slots(project, projection, user)
    return CompositionSaveResult(
        summary=summary,
        projection=projection,
        unfilled_opened_slots=unfilled,
    )


def apply_package_and_sync_slots(project: Project, package_key: str, user):
    """Пакет — то же самое сохранение состава, той же оркестрацией.

    Состав пакета берётся из `apps.rooms.presets` (единственный источник) и
    дальше идёт общим путём: второй копии ни состава пакета, ни проекции
    здесь не появляется.
    """
    return save_functional_roles_and_sync_slots(
        project,
        presets.functional_role_package_composition(package_key),
        user,
    )


@transaction.atomic
def launch_project(project: Project, actor=None) -> Project:
    """
    Запуск проекта без платёжного шлюза (MVP / DEBUG).
    Статус → Staffing, создаётся комната.
    """
    if project.status == Project.Status.DRAFT:
        project.status = Project.Status.STAFFING
        project.save(update_fields=['status', 'updated_at'])
    room = ensure_room_for_project(project)
    log_room_activity(
        room,
        f'Проект «{project.name}» запущен. Комната открыта.',
        RoomActivity.EventType.PROJECT_LAUNCHED,
        actor=actor or project.owner,
    )
    return project


@transaction.atomic
def assign_teamlead(project: Project, teamlead: User, actor=None) -> RoomMember:
    """Назначает тимлида проекту и добавляет в комнату."""
    project.teamlead = teamlead
    project.save(update_fields=['teamlead', 'updated_at'])
    room = ensure_room_for_project(project)

    RoomMember.objects.filter(
        room=room,
        role_in_room=RoomMember.RoleInRoom.TEAMLEAD,
    ).exclude(user=teamlead).update(role_in_room=RoomMember.RoleInRoom.FREELANCER)

    member, _ = RoomMember.objects.update_or_create(
        room=room,
        user=teamlead,
        defaults={'role_in_room': RoomMember.RoleInRoom.TEAMLEAD},
    )
    log_room_activity(
        room,
        f'Тимлид {teamlead.full_name} назначен.',
        RoomActivity.EventType.TEAMLEAD_ASSIGNED,
        actor=actor or project.owner,
    )
    return member


@transaction.atomic
def add_freelancer_to_room(room: Room, freelancer: User, actor=None) -> RoomMember:
    """Добавляет фрилансера в комнату.

    Статус проекта здесь не меняется. Раньше первый добавленный фрилансер
    переводил проект `STAFFING → ACTIVE`; теперь активация — результат
    подтверждённой готовности всей функциональной команды
    (`apps.rooms.staffing.services.sync_project_activation`), а не факта
    появления одного участника.
    """
    member, created = RoomMember.objects.get_or_create(
        room=room,
        user=freelancer,
        defaults={
            'role_in_room': RoomMember.RoleInRoom.FREELANCER,
            'ready_status': RoomMember.ReadyStatus.PENDING,
        },
    )
    if created:
        log_room_activity(
            room,
            f'Фрилансер {freelancer.full_name} добавлен в команду.',
            RoomActivity.EventType.MEMBER_ADDED,
            actor=actor,
        )
    return member


@transaction.atomic
def create_teamlead_invite(project: Project, created_by: User) -> TeamleadInvite:
    """Деактивирует старые инвайты и создаёт новый."""
    TeamleadInvite.objects.filter(project=project, is_active=True).update(is_active=False)
    return TeamleadInvite.objects.create(project=project, created_by=created_by)


@transaction.atomic
def accept_teamlead_invite(invite: TeamleadInvite, user: User) -> RoomMember:
    if not invite.is_valid:
        raise ValueError('Приглашение недействительно или истекло.')
    if user.role not in {User.Roles.TEAMLEAD, User.Roles.ADMIN}:
        # При принятии инвайта повышаем роль до тимлида (только pending/active freelancer edge case)
        if user.role == User.Roles.FREELANCER:
            raise ValueError('Войдите аккаунтом тимлида или зарегистрируйтесь по ссылке.')
        if user.role == User.Roles.DIRECTOR:
            raise ValueError('Директор не может принять приглашение тимлида.')
    if user.role != User.Roles.TEAMLEAD and user.role != User.Roles.ADMIN:
        user.role = User.Roles.TEAMLEAD
        user.save(update_fields=['role'])

    member = assign_teamlead(invite.project, user, actor=user)
    invite.accepted_by = user
    invite.accepted_at = timezone.now()
    invite.is_active = False
    invite.save(update_fields=['accepted_by', 'accepted_at', 'is_active'])
    return member


def user_can_access_project(user, project: Project) -> bool:
    if not user.is_authenticated:
        return False
    if user.role == User.Roles.ADMIN:
        return True
    if project.owner_id == user.id:
        return True
    if project.teamlead_id == user.id:
        return True
    return RoomMember.objects.filter(room__project=project, user=user).exists()


def user_can_manage_team(user, project: Project) -> bool:
    """Операционка комнаты: подбор, review, квалификация лидов.

    Только тимлид проекта (и платформенный admin). Владелец-директор
    покупает состав на «Обзоре» и не управляет командой вручную: иначе
    операционные кнопки и вкладки «Команда»/«Задачи» снова сливаются
    с ролью заказчика.
    """
    if not user.is_authenticated:
        return False
    if project.teamlead_id == user.id:
        return True
    if user.role == User.Roles.ADMIN:
        return True
    return False


def user_can_appoint_teamlead(user, project: Project) -> bool:
    """Кто назначает/приглашает тимлида: владелец проекта (или admin).

    Это не операционка слотов, а старт комнаты: без тимлида операционки
    ещё нет. Живёт на «Обзоре», потому что вкладка «Команда» директору
    больше не показывается.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    if user.role == User.Roles.ADMIN:
        return True
    return project.owner_id == user.id


def user_can_create_task(user, project: Project) -> bool:
    """Кто ставит задачи в комнате: только тимлид ЭТОГО проекта.

    Задача — управленческий инструмент тимлида: он распределяет работу по
    команде и принимает отчёты. Директор покупает результат и смотрит
    «Обзор», поэтому в постановку задач не вмешивается; фрилансер задачи
    исполняет, а не выдаёт.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    return project.teamlead_id == user.id


def user_can_view_team_tab(user, project: Project) -> bool:
    """Вкладка «Команда» — только тимлид проекта.

    Директор видит состав и слоты на «Обзоре» (read-only + конфигуратор),
    операционный подбор — на вкладке у тимлида. Фрилансер и менеджер
    вкладку не получают.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    return project.teamlead_id == user.id


def user_can_view_tasks_tab(user, project: Project) -> bool:
    """Вкладка «Задачи»: тимлид и исполнители; директору — только превью на Обзоре."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if project.teamlead_id == user.id:
        return True
    if user.role == User.Roles.ADMIN:
        return True
    if user.role == User.Roles.FREELANCER:
        return RoomMember.objects.filter(
            room__project=project,
            user=user,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        ).exists()
    if user.role == User.Roles.MANAGER:
        return RoomMember.objects.filter(room__project=project, user=user).exists()
    return False


def user_can_access_director_teamlead_comms(user, project: Project) -> bool:
    """Приватный контур директор↔тимлид: только owner или teamlead ЭТОГО проекта.

    Платформенный `ADMIN` и «любой director» права не получают: иначе чужой
    директор или админ платформы видели бы переписку и ссылку на встречу.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    if project.owner_id == user.id:
        return True
    return project.teamlead_id == user.id


def room_nav_context(user, project: Project) -> dict:
    """Ролевая часть context для `rooms/_room_header.html`.

    Единственный источник ролевых флагов навигации комнаты: страницы `rooms`
    и `pipeline` подмешивают этот словарь, а не считают правила сами, —
    поэтому вкладки не расходятся между разделами. Глобального context
    processor здесь нет намеренно: флаги зависят от конкретного проекта.
    """
    is_owner = (
        getattr(user, 'is_authenticated', False) and project.owner_id == user.id
    )
    return {
        'show_team_tab': user_can_view_team_tab(user, project),
        'show_tasks_tab': user_can_view_tasks_tab(user, project),
        'can_create_task': user_can_create_task(user, project),
        'can_appoint_teamlead': user_can_appoint_teamlead(user, project),
        # Верхняя секция на «Коммуникациях» и доступ к приватным URL.
        'show_director_teamlead_comms': user_can_access_director_teamlead_comms(
            user, project
        ),
        # Кнопка в шапке «Обзора»: только владельцу и только когда тимлид уже есть.
        'show_teamlead_comms_button': bool(is_owner and project.teamlead_id),
    }


@transaction.atomic
def handle_project_paid(project: Project, actor=None) -> Room:
    """
    Единая точка входа события «проект оплачен» (ADR-001).

    Сейчас вызывается из stub тестовой оплаты (без Stripe, webhook и брокеров).
    Результат успешной оплаты:
    статус → Staffing, комната гарантированно существует (одна на проект),
    в ленту комнаты пишется событие запуска.

    Возвращает Room — по ней view делает redirect в комнату.
    """
    launched_now = project.status == Project.Status.DRAFT
    if launched_now:
        project.status = Project.Status.STAFFING
        project.save(update_fields=['status', 'updated_at'])

    room = ensure_room_for_project(project)

    if launched_now:
        log_room_activity(
            room,
            f'Оплата получена (тестовая). Проект «{project.name}» запущен, комната открыта.',
            RoomActivity.EventType.PROJECT_LAUNCHED,
            actor=actor or project.owner,
        )
        _after_project_paid(project, room, actor=actor or project.owner)

    return room


def handle_project_activated(project: Project, actor=None):
    """Шаги, которые запускает активация проекта. Точка входа ROOM → pipeline.

    Сейчас шаг ровно один: стартовая задача «Начать звонки» с SLA 24 часа.
    Вызывается из `apps.rooms.staffing.services.sync_project_activation` в
    ветке фактического перехода STAFFING → ACTIVE и внутри его транзакции,
    поэтому задача и новый статус проекта коммитятся вместе.

    Почему обращение к pipeline живёт здесь, а не в staffing: направление
    `rooms → pipeline` ADR-001 допускает осознанно, но именно через фасад
    модуля. Подбор команды о задачах знать не должен — это отдельная
    зафиксированная граница (`tests_staffing_matching.MatchingBoundaryTests`),
    и обходить её ради одного вызова нельзя. Тот же приём, что и у
    `_after_project_paid` ниже: расширение потока живёт в сервисах ROOM.

    Импорт функцией: `apps.pipeline.services` импортирует этот модуль, и
    модульный импорт обратно замкнул бы граф.
    """
    from apps.pipeline.services import ensure_start_calls_task

    task, _created = ensure_start_calls_task(project, actor=actor)
    return task


def _after_project_paid(project: Project, room: Room, actor=None) -> None:
    """
    Точка расширения потока оплаты: сюда добавляются шаги после запуска проекта.

    Вызывается один раз — при переходе проекта из черновика в Staffing,
    поэтому шаги здесь не дублируются при повторной обработке оплаты.

    TODO (вне текущего scope): автоматический тимлид, стартовые задачи,
    письма участникам. Добавлять их нужно здесь, без правок views и URL оплаты.
    """
    return None
