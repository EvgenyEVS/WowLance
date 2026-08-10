"""Сервисы для проектов и комнат."""

from django.db import transaction

from apps.users.models import User
from .models import Project, Room, RoomMember


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
def launch_project(project: Project) -> Project:
    """
    Запуск проекта без платёжного шлюза (MVP / DEBUG).
    Статус → Staffing, создаётся комната.
    """
    if project.status == Project.Status.DRAFT:
        project.status = Project.Status.STAFFING
        project.save(update_fields=['status', 'updated_at'])
    ensure_room_for_project(project)
    return project


@transaction.atomic
def assign_teamlead(project: Project, teamlead: User) -> RoomMember:
    """Назначает тимлида проекту и добавляет в комнату."""
    project.teamlead = teamlead
    project.save(update_fields=['teamlead', 'updated_at'])
    room = ensure_room_for_project(project)

    # Снять старую роль teamlead у других участников (кроме нового)
    RoomMember.objects.filter(
        room=room,
        role_in_room=RoomMember.RoleInRoom.TEAMLEAD,
    ).exclude(user=teamlead).update(role_in_room=RoomMember.RoleInRoom.FREELANCER)

    member, _ = RoomMember.objects.update_or_create(
        room=room,
        user=teamlead,
        defaults={'role_in_room': RoomMember.RoleInRoom.TEAMLEAD},
    )
    return member


@transaction.atomic
def add_freelancer_to_room(room: Room, freelancer: User) -> RoomMember:
    """Добавляет фрилансера в комнату."""
    member, _ = RoomMember.objects.get_or_create(
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
