from django.shortcuts import render

from apps.users.models import User


def about(request):
    return render(request, 'core/about.html')


def home(request):
    """Главная страница. Контент зависит от роли."""
    if not request.user.is_authenticated:
        return render(request, 'core/landing.html')

    if request.user.role == User.Roles.DIRECTOR:
        return render(request, 'core/director_dashboard.html')
    if request.user.role == User.Roles.FREELANCER:
        return render(request, 'core/freelancer_dashboard.html')

    return render(request, 'core/landing.html')
