"""Общие хелперы для тестов."""

from dataclasses import dataclass
from decimal import Decimal

from django.contrib.auth import get_user_model

from apps.profiles.models import FreelancerProfile
from apps.profiles.services import get_or_create_freelancer_profile
from apps.rooms.models import Project, Room, RoomFunctionSlot, RoomMember
from apps.rooms.staffing.matching import CHANNEL_REQUIREMENTS

User = get_user_model()

#: Профиль без видео не проходит hard filters подбора — держим один адрес.
DEMO_VIDEO_URL = 'https://youtu.be/demo-presentation'


def make_user(
    email,
    role=User.Roles.FREELANCER,
    password='TestPass123!',
    status=User.Status.ACTIVE,
    first_name='Тест',
    last_name='Юзер',
    **extra,
):
    """Создаёт активного (или с указанным статусом) пользователя."""
    user = User(
        username=email,
        email=email,
        role=role,
        status=status,
        first_name=first_name,
        last_name=last_name,
        is_email_verified=(status == User.Status.ACTIVE),
        **extra,
    )
    user.set_password(password)
    user.save()
    return user


def make_freelancer(email='freelancer@test.com', **kwargs):
    user = make_user(email=email, role=User.Roles.FREELANCER, **kwargs)
    if user.status == User.Status.ACTIVE:
        profile = get_or_create_freelancer_profile(user)

        needs_save = False
        if not profile.video_url:
            profile.video_url = 'https://youtube.com/watch?v=test'
            needs_save = True

        if not profile.is_verified:
            profile.is_verified = True
            needs_save = True

        if needs_save:
            profile.save(update_fields=['video_url', 'is_verified'])

    return user


def make_director(email='director@test.com', **kwargs):
    return make_user(email=email, role=User.Roles.DIRECTOR, **kwargs)


def make_teamlead(email='teamlead@test.com', **kwargs):
    return make_user(email=email, role=User.Roles.TEAMLEAD, **kwargs)


@dataclass
class StaffedProject:
    """Комната в подборе как готовые данные — без действий и без выбора актора.

    Актора выбирает тест, а не фикстура:

    * состав / авто-подбор при покупке функции — `director` (владелец);
    * ручной подбор, замена, пул кандидатов — `teamlead` проекта.
    """

    project: Project
    room: Room
    director: User
    teamlead: User
    slots: list
    candidates: list


def make_staffed_project(
    *,
    slots=1,
    role_key='seller',
    grade=RoomFunctionSlot.Grade.MIDDLE,
    candidates=0,
    status=Project.Status.STAFFING,
    prefix='',
):
    """Проект в подборе: комната, директор, тимлид, слоты и пул кандидатов.

    Только прямое создание объектов: ни один сервис, поведение которого
    проверяется тестами, отсюда не вызывается. Хелпер никого не назначает
    на слот, не подтверждает готовность и не активирует проект — это
    предмет самих тестов.

    `candidates` создаются годными под слот (active / available / verified /
    video / грейд / оба канала) и с убывающим рейтингом: `candidates[0]` —
    top-1 ranking.
    """
    director = make_director(email=f'{prefix}director@staffed.test')
    teamlead = make_teamlead(email=f'{prefix}teamlead@staffed.test')

    project = Project.objects.create(
        owner=director,
        name=f'Проект подбора {prefix}'.strip(),
        status=status,
        teamlead=teamlead,
    )
    room = Room.objects.create(project=project)
    RoomMember.objects.create(
        room=room,
        user=director,
        role_in_room=RoomMember.RoleInRoom.DIRECTOR,
    )
    RoomMember.objects.create(
        room=room,
        user=teamlead,
        role_in_room=RoomMember.RoleInRoom.TEAMLEAD,
    )

    slot_list = [
        RoomFunctionSlot.objects.create(
            room=room,
            role_key=role_key,
            slot_index=index,
            required_level=grade,
        )
        for index in range(1, slots + 1)
    ]

    candidate_list = []
    for index in range(candidates):
        user = make_user(
            email=f'{prefix}candidate{index + 1}@staffed.test',
            role=User.Roles.FREELANCER,
            first_name=f'Кандидат{index + 1}',
        )
        fields = {
            'level': grade,
            'is_available': True,
            'is_verified': True,
            'video_url': DEMO_VIDEO_URL,
            'rating': Decimal('5.00') - index,
            'acceptance_rate': Decimal('90.00'),
            'experience_projects': 10,
        }
        # Признаки каналов берутся из таблицы Matching Engine, а не пишутся
        # строками: канал слота и поле профиля остаются связанными в одном месте.
        for channel_field in CHANNEL_REQUIREMENTS.values():
            fields[channel_field] = True
        FreelancerProfile.objects.create(user=user, **fields)
        candidate_list.append(user)

    return StaffedProject(
        project=project,
        room=room,
        director=director,
        teamlead=teamlead,
        slots=slot_list,
        candidates=candidate_list,
    )
