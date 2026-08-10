"""Сервисные функции приложения profiles."""

from .models import FreelancerProfile, Portfolio


def get_or_create_freelancer_profile(user):
    """Создаёт профиль и портфолио фрилансера, если их ещё нет."""
    profile, _ = FreelancerProfile.objects.get_or_create(user=user)
    Portfolio.objects.get_or_create(profile=profile)
    return profile
