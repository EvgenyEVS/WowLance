"""Сервисы для проектов и комнат."""

from django.db import transaction
from django.utils import timezone

from apps.users.models import User
from .models import Project, Room, RoomActivity, RoomMember, TeamleadInvite


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
    """Добавляет фрилансера в комнату."""
    member, created = RoomMember.objects.get_or_create(
        room=room,
        user=freelancer,
        defaults={
            'role_in_room': RoomMember.RoleInRoom.FREELANCER,
            'ready_status': RoomMember.ReadyStatus.PENDING,
        },
    )
    freelancers_count = room.members.filter(
        role_in_room=RoomMember.RoleInRoom.FREELANCER,
    ).count()
    project = room.project
    if project.status == Project.Status.STAFFING and freelancers_count >= 1:
        project.status = Project.Status.ACTIVE
        project.save(update_fields=['status', 'updated_at'])
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
