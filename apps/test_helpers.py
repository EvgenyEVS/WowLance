"""Общие хелперы для тестов."""

from django.contrib.auth import get_user_model

from apps.profiles.services import get_or_create_freelancer_profile

User = get_user_model()


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
        if not profile.video_url:
            profile.video_url = 'https://youtube.com/watch?v=test'
            profile.save(update_fields=['video_url'])
    return user


def make_director(email='director@test.com', **kwargs):
    return make_user(email=email, role=User.Roles.DIRECTOR, **kwargs)


def make_teamlead(email='teamlead@test.com', **kwargs):
    return make_user(email=email, role=User.Roles.TEAMLEAD, **kwargs)
