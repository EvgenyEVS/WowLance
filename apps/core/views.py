from django.shortcuts import render

from apps.pipeline.forms import TeamleadPeriodReportForm
from apps.rooms.onboarding import (
    director_metrics,
    director_onboarding,
    freelancer_metrics,
    freelancer_onboarding,
    manager_metrics,
    onboarding_progress,
    teamlead_metrics,
)
from apps.users.models import User


def about(request):
    return render(request, 'core/about.html')


def privacy(request):
    return render(request, 'core/privacy.html')


def terms(request):
    return render(request, 'core/terms.html')


def home(request):
    """Главная страница. Контент зависит от роли."""
    if not request.user.is_authenticated:
        return render(request, 'core/landing.html')

    context = {'user': request.user}

    if request.user.role == User.Roles.DIRECTOR:
        context['metrics'] = director_metrics(request.user)
        context['onboarding'] = onboarding_progress(director_onboarding(request.user))
        return render(request, 'core/director_dashboard.html', context)

    if request.user.role == User.Roles.TEAMLEAD:
        context['metrics'] = teamlead_metrics(request.user)
        # Незаполненная форма отчёта за период: даты уже проставлены
        # дефолтом (последние 7 дней), проект пуст = все проекты.
        # Считает и рендерит отчёт отдельная страница pipeline.
        context['report_form'] = TeamleadPeriodReportForm(user=request.user)
        return render(request, 'core/teamlead_dashboard.html', context)

    if request.user.role == User.Roles.MANAGER:
        context['metrics'] = manager_metrics(request.user)
        return render(request, 'core/manager_dashboard.html', context)

    if request.user.role == User.Roles.FREELANCER:
        context['metrics'] = freelancer_metrics(request.user)
        context['onboarding'] = onboarding_progress(freelancer_onboarding(request.user))
        return render(request, 'core/freelancer_dashboard.html', context)

    return render(request, 'core/landing.html')
