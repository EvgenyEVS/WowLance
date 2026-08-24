"""Сервисы для проектов и комнат."""

from django.db import transaction
from django.utils import timezone

from apps.users.models import User
from .models import Project, Room, RoomActivity, RoomMember, TeamleadInvite
from .unit_economics import (  # noqa: F401  (публичный фасад модуля ROOM)
    apply_package_to_project,
    get_unit_economics_summary,
    update_project_functional_roles,
    user_can_edit_functional_roles,
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


def log_room_activity(room: Room, message: str, event_type: str, actor=None) -> RoomActivity:
    return RoomActivity.objects.create(
        room=room,
        actor=actor,
        event_type=event_type,
        message=message,
    )


@transaction.atomic
def ensure_room_for_project(project: Project) -> Room:
    """Создаёт комнату и добавляет директора участником, если ещё нет."""
    room, created = Room.objects.get_or_create(project=project)
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
    if not user.is_authenticated:
        return False
    if project.owner_id == user.id:
        return True
    if project.teamlead_id == user.id:
        return True
    if user.role == User.Roles.ADMIN:
        return True
    return False


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


def _after_project_paid(project: Project, room: Room, actor=None) -> None:
    """
    Точка расширения потока оплаты: сюда добавляются шаги после запуска проекта.

    Вызывается один раз — при переходе проекта из черновика в Staffing,
    поэтому шаги здесь не дублируются при повторной обработке оплаты.

    TODO (вне текущего scope): автоматический тимлид, стартовые задачи,
    письма участникам. Добавлять их нужно здесь, без правок views и URL оплаты.
    """
    return None
