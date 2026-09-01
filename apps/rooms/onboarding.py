"""Онбординг-чеклисты и метрики дашбордов."""

from apps.pipeline.models import Lead, Report, Task
from apps.users.models import User

from .models import Project, RoomMember


def director_onboarding(user) -> list[dict]:
    projects = Project.objects.filter(owner=user)
    has_project = projects.exists()
    has_launched = projects.exclude(status=Project.Status.DRAFT).exists()
    has_freelancer = RoomMember.objects.filter(
        room__project__owner=user,
        role_in_room=RoomMember.RoleInRoom.FREELANCER,
    ).exists()
    return [
        {
            'key': 'create_project',
            'label': 'Создать проект по архитектуре',
            'done': has_project,
            'url_name': 'rooms:setup_wizard',
            'cta': 'Выбрать архитектуру',
        },
        {
            'key': 'launch_room',
            'label': 'Запустить комнату проекта',
            'done': has_launched,
            'url_name': 'rooms:project_list',
            'cta': 'К проектам',
        },
        {
            'key': 'add_freelancer',
            'label': 'Добавить фрилансера в команду',
            'done': has_freelancer,
            'url_name': 'profiles:catalog',
            'cta': 'Открыть каталог',
        },
    ]


def freelancer_onboarding(user) -> list[dict]:
    profile = getattr(user, 'freelancer_profile', None)
    skills_ok = bool(profile and profile.skills_list)
    country_ok = bool(profile and profile.country)
    in_room = RoomMember.objects.filter(
        user=user,
        role_in_room=RoomMember.RoleInRoom.FREELANCER,
    ).exists()
    return [
        {
            'key': 'fill_profile',
            'label': 'Заполнить страну и навыки',
            'done': skills_ok and country_ok,
            'url_name': 'profiles:edit',
            'cta': 'Редактировать профиль',
        },
        {
            'key': 'join_room',
            'label': 'Попасть в комнату проекта',
            'done': in_room,
            'url_name': 'rooms:project_list',
            'cta': 'Мои комнаты',
        },
        {
            'key': 'confirm_ready',
            'label': 'Подтвердить готовность в комнате',
            'done': RoomMember.objects.filter(
                user=user,
                role_in_room=RoomMember.RoleInRoom.FREELANCER,
                ready_status=RoomMember.ReadyStatus.READY,
            ).exists(),
            'url_name': 'rooms:project_list',
            'cta': 'К комнатам',
        },
    ]


def onboarding_progress(items: list[dict]) -> dict:
    total = len(items)
    done = sum(1 for i in items if i['done'])
    return {
        'items': items,
        'done': done,
        'total': total,
        'complete': total > 0 and done == total,
        'percent': int(round(100 * done / total)) if total else 100,
    }


def director_metrics(user) -> dict:
    from .director_stats import director_finance_metrics

    projects = Project.objects.filter(owner=user)
    active = projects.filter(
        status__in=[Project.Status.STAFFING, Project.Status.ACTIVE],
    ).count()
    hot_leads = Lead.objects.filter(
        project__owner=user,
        qualification_status=Lead.Qualification.HOT,
    ).count()
    metrics = {
        'projects_total': projects.count(),
        'rooms_active': active,
        'hot_leads': hot_leads,
    }
    metrics.update(director_finance_metrics(user))
    return metrics


def freelancer_metrics(user) -> dict:
    rooms = RoomMember.objects.filter(
        user=user,
        role_in_room=RoomMember.RoleInRoom.FREELANCER,
    ).count()
    open_tasks = Task.objects.filter(
        assignee=user,
    ).exclude(status=Task.Status.CLOSED).count()
    profile = getattr(user, 'freelancer_profile', None)
    rating = profile.rating if profile else 0
    return {
        'rooms_count': rooms,
        'open_tasks': open_tasks,
        'rating': rating,
    }


def freelancer_project_stats(user, project: Project) -> dict:
    """Свои цифры фрилансера на одном проекте — полоса Обзора комнаты.

    Не путать с `freelancer_metrics` на дашборде `/`: здесь только этот
    проект, без рейтинга и счётчика комнат. Лиды — свои Cold/Warm/Hot,
    не сводка по команде.
    """
    from django.db.models import Count, Q

    my_tasks = Task.objects.filter(project=project, assignee=user)
    lead_counts = Lead.objects.filter(project=project, creator=user).aggregate(
        leads_cold=Count(
            'id', filter=Q(qualification_status=Lead.Qualification.COLD)
        ),
        leads_warm=Count(
            'id', filter=Q(qualification_status=Lead.Qualification.WARM)
        ),
        leads_hot=Count(
            'id', filter=Q(qualification_status=Lead.Qualification.HOT)
        ),
    )
    return {
        'open_tasks': my_tasks.exclude(status=Task.Status.CLOSED).count(),
        'tasks_in_review': my_tasks.filter(
            status=Task.Status.READY_FOR_REVIEW,
        ).count(),
        'leads_cold': lead_counts['leads_cold'],
        'leads_warm': lead_counts['leads_warm'],
        'leads_hot': lead_counts['leads_hot'],
        'reports_approved': Report.objects.filter(
            task__project=project,
            author=user,
            review_status=Report.ReviewStatus.APPROVED,
        ).count(),
    }


def teamlead_metrics(user) -> dict:
    projects = Project.objects.filter(teamlead=user)
    review_tasks = Task.objects.filter(
        project__teamlead=user,
        status=Task.Status.READY_FOR_REVIEW,
    ).count()
    warm_hot = Lead.objects.filter(
        project__teamlead=user,
        qualification_status__in=[Lead.Qualification.WARM, Lead.Qualification.HOT],
    ).count()
    return {
        'projects_led': projects.count(),
        'tasks_in_review': review_tasks,
        'warm_hot_leads': warm_hot,
    }


def manager_metrics(user) -> dict:
    inbox = Task.objects.filter(
        assignee=user,
        task_type=Task.TaskType.MANAGER_HANDOFF,
    ).exclude(status=Task.Status.CLOSED)
    return {
        'handoff_open': inbox.count(),
        'handoff_new': inbox.filter(status=Task.Status.NEW).count(),
        'handoff_done': Task.objects.filter(
            assignee=user,
            task_type=Task.TaskType.MANAGER_HANDOFF,
            status=Task.Status.CLOSED,
        ).count(),
    }


def staffing_projects_for_user(user):
    """Проекты, куда директор/тимлид может добавить фрилансера."""
    if user.role == User.Roles.DIRECTOR:
        qs = Project.objects.filter(owner=user)
    elif user.role == User.Roles.TEAMLEAD:
        qs = Project.objects.filter(teamlead=user)
    elif user.role == User.Roles.ADMIN:
        qs = Project.objects.all()
    else:
        return Project.objects.none()
    return qs.exclude(
        status__in=[Project.Status.DRAFT, Project.Status.ARCHIVED, Project.Status.COMPLETED],
    ).select_related('owner', 'teamlead').order_by('-created_at')
