# apps/rooms/chat_notifications.py

from django.utils import timezone
from django.db import transaction, OperationalError
from apps.users.models import User
from .models import Room, RoomChatMessage, ChatReadCursor
import time


def get_accessible_rooms(user):
    """Возвращает комнаты, доступные пользователю."""
    if user.role == 'director':
        return Room.objects.filter(project__owner=user)
    if user.role == 'teamlead':
        return Room.objects.filter(project__teamlead=user)
    return Room.objects.filter(members__user=user).distinct()


def can_access_dt_chat(user, room):
    """Проверяет доступ к DT чату."""
    if user.role not in ['director', 'teamlead']:
        return False
    if user.role == 'director':
        return room.project.owner_id == user.id
    return room.project.teamlead_id == user.id


def get_available_channels(user, room):
    """Возвращает доступные каналы."""
    channels = ['team']
    if can_access_dt_chat(user, room):
        channels.append('director_teamlead')
    return channels


def get_unread_conversations(user):
    """Возвращает непрочитанные разговоры."""
    rooms = get_accessible_rooms(user)
    conversations = []

    for room in rooms:
        channels = get_available_channels(user, room)
        for channel in channels:
            cursor, _ = ChatReadCursor.objects.get_or_create(
                user=user,
                room=room,
                channel=channel,
                defaults={'last_read_at': timezone.now()}
            )

            unread_qs = RoomChatMessage.objects.filter(
                room=room,
                channel=channel,
                created_at__gt=cursor.last_read_at
            ).exclude(author=user).select_related('author', 'room__project')

            count = unread_qs.count()
            if count > 0:
                latest_msg = unread_qs.order_by('-created_at').first()
                conversations.append({
                    'room': room,
                    'channel': channel,
                    'count': count,
                    'latest_message': latest_msg,
                    'project_name': room.project.name,
                })

    return conversations


def mark_channel_read(user, room, channel, retry_count=0):
    """Отмечает канал как прочитанный."""
    max_retries = 3
    try:
        with transaction.atomic():
            cursor, created = ChatReadCursor.objects.get_or_create(
                user=user,
                room=room,
                channel=channel,
                defaults={'last_read_at': timezone.now()}
            )
            if not created:
                cursor.last_read_at = timezone.now()
                cursor.save(update_fields=['last_read_at'])
    except OperationalError as e:
        if 'database is locked' in str(e) and retry_count < max_retries:
            time.sleep(0.1 * (retry_count + 1))
            return mark_channel_read(user, room, channel, retry_count + 1)
        else:
            try:
                ChatReadCursor.objects.filter(
                    user=user,
                    room=room,
                    channel=channel
                ).update(last_read_at=timezone.now())
            except:
                pass


def get_last_accessible_room(user):
    """Возвращает последнюю доступную комнату."""
    rooms = get_accessible_rooms(user)
    return rooms.first() if rooms else None


def chat_context_processor(request):
    """Добавляет last_accessible_room в контекст всех шаблонов."""
    if not request.user.is_authenticated or request.user.role == 'manager':
        return {'last_accessible_room': None}

    return {'last_accessible_room': get_last_accessible_room(request.user)}