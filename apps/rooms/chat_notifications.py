"""Колокольчик чата: непрочитанные разговоры и тосты без сокетов.

Рядом с `chat.py`: запись/чтение ленты остаются там, а курсоры прочтения и
опрос шапки — здесь. RBAC каналов не копируется: комнаты и DT берутся через
`user_can_access_director_teamlead_comms` и ролевые queryset'ы владельца /
тимлида / активного фрилансера-члена.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q, QuerySet
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.users.models import User

from .models import ChatReadCursor, Room, RoomChatMessage, RoomMember
from .services import user_can_access_director_teamlead_comms

__all__ = [
    'SESSION_LAST_POLL_KEY',
    'build_bell_context',
    'mark_channel_read',
    'mark_comms_page_read',
    'user_can_see_chat_bell',
]

CHAT_BELL_ROLES = frozenset({
    User.Roles.DIRECTOR,
    User.Roles.TEAMLEAD,
    User.Roles.FREELANCER,
})

SESSION_LAST_POLL_KEY = 'chat_alerts_last_poll'


def user_can_see_chat_bell(user) -> bool:
    """Колокольчик только у ролей с чатом; менеджер и гость — нет."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'role', None) not in CHAT_BELL_ROLES:
        return False
    return accessible_chat_rooms_qs(user).exists()


def accessible_chat_rooms_qs(user) -> QuerySet[Room]:
    """Комнаты, для которых пользователю есть смысл показывать колокольчик."""
    role = getattr(user, 'role', None)
    if role == User.Roles.DIRECTOR:
        qs = Room.objects.filter(project__owner=user)
    elif role == User.Roles.TEAMLEAD:
        qs = Room.objects.filter(project__teamlead=user)
    elif role == User.Roles.FREELANCER:
        qs = Room.objects.filter(
            members__user=user,
            members__role_in_room=RoomMember.RoleInRoom.FREELANCER,
            members__is_active=True,
        ).distinct()
    else:
        return Room.objects.none()
    return qs.select_related('project')


def channels_for_user(user, room: Room) -> list[str]:
    channels = [RoomChatMessage.Channel.TEAM]
    if user_can_access_director_teamlead_comms(user, room.project):
        channels.append(RoomChatMessage.Channel.DIRECTOR_TEAMLEAD)
    return channels


def ensure_cursor(user, room: Room, channel: str) -> ChatReadCursor:
    """Первый курсор = now(): старая история не считается непрочитанной."""
    cursor, _created = ChatReadCursor.objects.get_or_create(
        user=user,
        room=room,
        channel=channel,
        defaults={'last_read_at': timezone.now()},
    )
    return cursor


def mark_channel_read(user, room: Room, channel: str) -> None:
    now = timezone.now()
    cursor, created = ChatReadCursor.objects.get_or_create(
        user=user,
        room=room,
        channel=channel,
        defaults={'last_read_at': now},
    )
    if not created:
        cursor.last_read_at = now
        cursor.save(update_fields=['last_read_at'])


def mark_comms_page_read(user, room: Room, *, show_dt: bool) -> None:
    """GET «Коммуникации»: team всегда; DT — если блок в DOM для зрителя."""
    mark_channel_read(user, room, RoomChatMessage.Channel.TEAM)
    if show_dt:
        mark_channel_read(user, room, RoomChatMessage.Channel.DIRECTOR_TEAMLEAD)


@dataclass(frozen=True)
class UnreadConversation:
    room: Room
    channel: str
    latest_message: RoomChatMessage

    @property
    def href(self) -> str:
        url = reverse('rooms:room_comms', args=[self.room.project_id])
        if self.channel == RoomChatMessage.Channel.DIRECTOR_TEAMLEAD:
            return f'{url}#comms-dt-chat'
        return f'{url}#comms-team'

    def channel_label_for(self, viewer) -> str:
        if self.channel == RoomChatMessage.Channel.TEAM:
            return 'Команда'
        if self.room.project.owner_id == getattr(viewer, 'id', None):
            return 'С тимлидом'
        return 'С директором'


def unread_conversations(user) -> list[UnreadConversation]:
    """Разговоры (комната+канал) с хотя бы одним чужим непрочитанным."""
    result: list[UnreadConversation] = []
    for room in accessible_chat_rooms_qs(user):
        for channel in channels_for_user(user, room):
            cursor = ensure_cursor(user, room, channel)
            latest = (
                RoomChatMessage.objects
                .filter(
                    room=room,
                    channel=channel,
                    created_at__gt=cursor.last_read_at,
                )
                .exclude(author=user)
                .select_related('author', 'room__project')
                .order_by('-created_at')
                .first()
            )
            if latest is not None:
                result.append(
                    UnreadConversation(
                        room=room,
                        channel=channel,
                        latest_message=latest,
                    )
                )
    result.sort(key=lambda item: item.latest_message.created_at, reverse=True)
    return result


def fallback_comms_href(user) -> str | None:
    """Ноль непрочитанных: командный чат комнаты с последним видимым сообщением."""
    rooms = list(accessible_chat_rooms_qs(user))
    if not rooms:
        return None
    room_ids = [room.id for room in rooms]
    dt_ids = [
        room.id
        for room in rooms
        if user_can_access_director_teamlead_comms(user, room.project)
    ]
    channel_q = Q(channel=RoomChatMessage.Channel.TEAM)
    if dt_ids:
        channel_q |= Q(
            channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD,
            room_id__in=dt_ids,
        )
    latest = (
        RoomChatMessage.objects
        .filter(room_id__in=room_ids)
        .filter(channel_q)
        .select_related('room__project')
        .order_by('-created_at')
        .first()
    )
    room = latest.room if latest is not None else rooms[0]
    return reverse('rooms:room_comms', args=[room.project_id]) + '#comms-team'


def _conversation_payload(user, conversations: list[UnreadConversation]) -> list[dict]:
    return [
        {
            'href': conv.href,
            'project_name': conv.room.project.name,
            'channel': conv.channel,
            'channel_label': conv.channel_label_for(user),
            'latest_message': conv.latest_message,
        }
        for conv in conversations
    ]


def _mark_viewing_read(user, viewing_project_id: str | None, viewing_comms: bool) -> None:
    if not viewing_comms or not viewing_project_id:
        return
    room = (
        accessible_chat_rooms_qs(user)
        .filter(project_id=viewing_project_id)
        .first()
    )
    if room is None:
        return
    show_dt = user_can_access_director_teamlead_comms(user, room.project)
    mark_comms_page_read(user, room, show_dt=show_dt)


def _session_last_poll(request):
    raw = request.session.get(SESSION_LAST_POLL_KEY)
    if not raw:
        return None
    parsed = parse_datetime(str(raw))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def build_bell_context(
    request,
    *,
    emit_toasts: bool = False,
    viewing_project_id: str | None = None,
    viewing_comms: bool = False,
) -> dict:
    """Контекст partial колокольчика: SSR и HTMX poll делят одну сборку."""
    user = request.user
    poll_parts = []
    if viewing_project_id:
        poll_parts.append(f'viewing_project={viewing_project_id}')
    if viewing_comms:
        poll_parts.append('viewing_comms=1')
    poll_query = '&'.join(poll_parts)

    empty = {
        'show_chat_bell': False,
        'conversations': [],
        'conversation_count': 0,
        'toasts': [],
        'fallback_href': None,
        'poll_query': poll_query,
    }
    if not user_can_see_chat_bell(user):
        return empty

    _mark_viewing_read(user, viewing_project_id, viewing_comms)
    conversations = unread_conversations(user)
    payloads = _conversation_payload(user, conversations)

    toasts: list[dict] = []
    now = timezone.now()
    if emit_toasts:
        last_poll = _session_last_poll(request)
        if last_poll is not None:
            for item in payloads:
                message = item['latest_message']
                if message.created_at > last_poll:
                    toasts.append(item)
        request.session[SESSION_LAST_POLL_KEY] = now.isoformat()
    elif SESSION_LAST_POLL_KEY not in request.session:
        # Первый SSR: бейдж по курсорам есть, тосты истории — нет.
        request.session[SESSION_LAST_POLL_KEY] = now.isoformat()

    return {
        'show_chat_bell': True,
        'conversations': payloads,
        'conversation_count': len(payloads),
        'toasts': toasts,
        'fallback_href': fallback_comms_href(user),
        'poll_query': poll_query,
    }
