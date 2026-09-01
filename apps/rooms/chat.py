"""Чат комнаты: отправка сообщения и чтение последних сообщений.

Отдельный маленький модуль, а не новая папка: чат — это одна операция записи
и один read-only селектор, ради которых заводить package вроде `staffing`
избыточно, но и растворять их в общем `services.py` незачем — там живут
проект, комната, команда и оплата.

Границы ответственности:

* здесь нет `request`, `messages`, шаблонов и редиректов — только модель;
* здесь нет правил ролей: кто имеет доступ к комнате / каналу, решает
  `services` во view. Дублировать RBAC в чате нельзя, иначе появится
  вторая, расходящаяся копия правил ROOM;
* `chat_enabled` проверяется и здесь тоже — это состояние самой комнаты,
  а не право пользователя, и защита не должна держаться только на шаблоне;
* `channel` обязателен: командный и директор↔тимлид чаты не смешиваются.

Отправка сообщения намеренно **не** пишет `RoomActivity`: иначе лента
комнаты превратится в дубликат переписки.
"""

from .models import Room, RoomChatMessage

__all__ = [
    'CHAT_HISTORY_LIMIT',
    'CHAT_MESSAGE_MAX_LENGTH',
    'ChatDisabledError',
    'post_chat_message',
    'recent_chat_messages',
]

#: Сколько последних сообщений отдаётся в UI. История комнаты целиком
#: не грузится: опрос идёт раз в несколько секунд, и полный queryset
#: со временем сделал бы каждый poll всё дороже.
CHAT_HISTORY_LIMIT = 50

#: Предел длины сообщения. Ограничение формы, а не колонки: `text` остаётся
#: TextField, чтобы менять лимит без миграции.
CHAT_MESSAGE_MAX_LENGTH = 2000


class ChatDisabledError(ValueError):
    """Чат комнаты выключен — писать в него нельзя."""


def post_chat_message(
    room: Room,
    author,
    text: str,
    *,
    channel: str = RoomChatMessage.Channel.TEAM,
) -> RoomChatMessage:
    """Создаёт сообщение в указанном канале чата комнаты.

    `chat_enabled` проверяется здесь ещё раз, поверх проверки во view:
    сервис нельзя вызвать в обход выключенного чата. `channel` всегда
    пишется явно — иначе командный и приватный контуры смешаются.
    """
    if not room.chat_enabled:
        raise ChatDisabledError('Чат комнаты выключен.')
    return RoomChatMessage.objects.create(
        room=room,
        author=author,
        text=text,
        channel=channel,
    )


def recent_chat_messages(
    room: Room,
    *,
    channel: str = RoomChatMessage.Channel.TEAM,
) -> list[RoomChatMessage]:
    """Последние `CHAT_HISTORY_LIMIT` сообщений канала, старые → новые.

    Срез берётся по убыванию времени (это последние сообщения, а не первые)
    и разворачивается в памяти уже после лимита — для UI, где новое внизу.
    `select_related('author')` убирает N+1: имя автора у каждого сообщения
    иначе стоило бы отдельного запроса на каждый poll.

    Фильтр по `channel` обязателен: без него poll одного контура отдавал бы
    чужие сообщения второго.
    """
    newest_first = (
        RoomChatMessage.objects
        .filter(room=room, channel=channel)
        .select_related('author')
        .order_by('-created_at')[:CHAT_HISTORY_LIMIT]
    )
    return list(reversed(newest_first))
