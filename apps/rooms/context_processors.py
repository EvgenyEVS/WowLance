"""Context processors модуля ROOM.

BIZ-шаблоны (каталог и карточка фрилансера в ``apps.profiles``) показывают
кнопку «В комнату». Правила ROOM — какие проекты доступны для staffing и как
выглядит форма выбора проекта — остаются здесь, чтобы ``apps.profiles``
не импортировал ``apps.rooms`` (см. docs/ADR-001-monolith-modules.md).
"""

from django.utils.functional import SimpleLazyObject

from apps.users.models import User

from .forms import AddToRoomForm
from .onboarding import staffing_projects_for_user

#: Роли, которые в принципе могут добавлять фрилансеров в комнату.
STAFFING_ROLES = frozenset({
    User.Roles.DIRECTOR,
    User.Roles.TEAMLEAD,
    User.Roles.ADMIN,
})


def _resolve(request, cache):
    """Считает флаг и форму один раз на запрос (лениво)."""
    if 'can' in cache:
        return cache

    user = getattr(request, 'user', None)
    if (
        user is None
        or not user.is_authenticated
        or getattr(user, 'role', None) not in STAFFING_ROLES
    ):
        cache['can'] = False
        cache['form'] = None
        return cache

    projects = staffing_projects_for_user(user)
    can_staff = projects.exists()
    cache['can'] = can_staff
    cache['form'] = AddToRoomForm(projects=projects) if can_staff else None
    return cache


def add_to_room(request):
    """Отдаёт шаблонам ``can_add_to_room`` и ``add_to_room_form``.

    Значения ленивые: ROOM-запросы выполняются только если шаблон реально
    обращается к этим переменным, поэтому остальные страницы сайта
    не получают лишних запросов.
    """
    cache = {}
    return {
        'can_add_to_room': SimpleLazyObject(lambda: _resolve(request, cache)['can']),
        'add_to_room_form': SimpleLazyObject(lambda: _resolve(request, cache)['form']),
    }
