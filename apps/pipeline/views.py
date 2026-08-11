from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.rooms.models import Project, RoomActivity
from apps.rooms.services import (
    ensure_room_for_project,
    log_room_activity,
    user_can_access_project,
    user_can_manage_team,
)
from apps.users.models import User

from .forms import (
    LeadCreateForm,
    LeadQualifyForm,
    ReportReviewForm,
    ReportSubmitForm,
    TaskCreateForm,
)
from .models import Lead, Report, Task
from .services import (
    TaskCloseError,
    close_task,
    create_lead,
    create_task,
    review_report,
    set_lead_qualification,
    start_task,
    submit_report,
)


def _kanban_columns(tasks):
    review_statuses = {Task.Status.READY_FOR_REVIEW}
    done_statuses = {Task.Status.APPROVED, Task.Status.CLOSED}
    columns = [
        {'key': 'todo', 'title': 'К работе', 'tasks': []},
        {'key': 'review', 'title': 'На проверке', 'tasks': []},
        {'key': 'done', 'title': 'Готово', 'tasks': []},
    ]
    for task in tasks:
        if task.status in review_statuses:
            columns[1]['tasks'].append(task)
        elif task.status in done_statuses:
            columns[2]['tasks'].append(task)
        else:
            columns[0]['tasks'].append(task)
    return columns


def _get_project(user, project_id):
    project = get_object_or_404(
        Project.objects.select_related('owner', 'teamlead'),
        id=project_id,
    )
    if not user_can_access_project(user, project):
        raise PermissionDenied('Нет доступа к проекту.')
    ensure_room_for_project(project)
    return project


@login_required
def room_tasks(request, project_id):
    project = _get_project(request.user, project_id)
    tasks = (
        Task.objects.filter(project=project)
        .select_related('assignee', 'created_by', 'lead')
        .prefetch_related('reports')
    )
    if request.user.role == User.Roles.FREELANCER:
        tasks = tasks.filter(assignee=request.user)
    elif request.user.role == User.Roles.MANAGER:
        tasks = tasks.filter(assignee=request.user)

    can_manage = user_can_manage_team(request.user, project)
    task_list = list(tasks)
    return render(request, 'pipeline/room_tasks.html', {
        'project': project,
        'tasks': task_list,
        'kanban_columns': _kanban_columns(task_list),
        'can_manage_team': can_manage,
        'create_form': TaskCreateForm(project=project) if can_manage else None,
        'active_tab': 'tasks',
    })


@login_required
@require_POST
def task_create(request, project_id):
    project = _get_project(request.user, project_id)
    if not user_can_manage_team(request.user, project):
        raise PermissionDenied
    form = TaskCreateForm(request.POST, project=project)
    if form.is_valid():
        task = create_task(
            project=project,
            assignee=form.cleaned_data['assignee'],
            created_by=request.user,
            title=form.cleaned_data['title'],
            description=form.cleaned_data.get('description', ''),
            deadline=form.cleaned_data.get('deadline'),
            checklist=form.cleaned_checklist(),
            report_required=form.cleaned_data.get('report_required', True),
        )
        room = ensure_room_for_project(project)
        log_room_activity(
            room,
            f'Задача «{task.title}» создана.',
            RoomActivity.EventType.TASK_CREATED,
            actor=request.user,
        )
        messages.success(request, 'Задача создана.')
    else:
        messages.error(request, 'Не удалось создать задачу. Проверьте поля.')
    return redirect('pipeline:room_tasks', project_id=project.id)


@login_required
def task_detail(request, project_id, task_id):
    project = _get_project(request.user, project_id)
    task = get_object_or_404(
        Task.objects.select_related('assignee', 'lead').prefetch_related('reports'),
        id=task_id,
        project=project,
    )
    if (
        request.user.role == User.Roles.FREELANCER
        and task.assignee_id != request.user.id
    ):
        raise PermissionDenied

    reports = task.reports.select_related('author', 'reviewed_by').all()
    pending = reports.filter(review_status=Report.ReviewStatus.PENDING).first()
    can_manage = user_can_manage_team(request.user, project)
    is_assignee = task.assignee_id == request.user.id

    return render(request, 'pipeline/task_detail.html', {
        'project': project,
        'task': task,
        'reports': reports,
        'pending_report': pending,
        'can_manage_team': can_manage,
        'is_assignee': is_assignee,
        'report_form': ReportSubmitForm() if is_assignee else None,
        'review_form': ReportReviewForm() if can_manage and pending else None,
        'can_close': task.can_be_closed(),
        'active_tab': 'tasks',
    })


@login_required
@require_POST
def task_start(request, project_id, task_id):
    project = _get_project(request.user, project_id)
    task = get_object_or_404(Task, id=task_id, project=project)
    try:
        start_task(task, request.user)
        messages.success(request, 'Задача взята в работу.')
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, str(exc))
    return redirect('pipeline:task_detail', project_id=project.id, task_id=task.id)


@login_required
@require_POST
def task_submit_report(request, project_id, task_id):
    project = _get_project(request.user, project_id)
    task = get_object_or_404(Task, id=task_id, project=project)
    form = ReportSubmitForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            submit_report(
                task=task,
                author=request.user,
                content_text=form.cleaned_data['content_text'],
                attachment=form.cleaned_data['attachment'],
            )
            messages.success(request, 'Отчёт отправлен на проверку.')
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, str(exc))
    else:
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
    return redirect('pipeline:task_detail', project_id=project.id, task_id=task.id)


@login_required
@require_POST
def task_review_report(request, project_id, task_id, report_id):
    project = _get_project(request.user, project_id)
    task = get_object_or_404(Task, id=task_id, project=project)
    report = get_object_or_404(Report, id=report_id, task=task)
    form = ReportReviewForm(request.POST)
    if form.is_valid():
        try:
            review_report(
                report=report,
                reviewer=request.user,
                approve=form.cleaned_data['action'] == 'approve',
                comment=form.cleaned_data.get('comment', ''),
            )
            messages.success(
                request,
                'Отчёт утверждён.' if form.cleaned_data['action'] == 'approve' else 'Отчёт отклонён.',
            )
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, 'Некорректные данные проверки.')
    return redirect('pipeline:task_detail', project_id=project.id, task_id=task.id)


@login_required
@require_POST
def task_close(request, project_id, task_id):
    project = _get_project(request.user, project_id)
    task = get_object_or_404(Task, id=task_id, project=project)
    try:
        close_task(task, request.user)
        messages.success(request, 'Задача закрыта.')
    except (PermissionDenied, TaskCloseError, ValidationError) as exc:
        messages.error(request, str(exc))
    return redirect('pipeline:task_detail', project_id=project.id, task_id=task.id)


@login_required
def room_leads(request, project_id):
    project = _get_project(request.user, project_id)
    leads = Lead.objects.filter(project=project).select_related(
        'creator', 'assigned_manager',
    )
    if request.user.role == User.Roles.FREELANCER:
        leads = leads.filter(creator=request.user)

    can_create = request.user.role in {
        User.Roles.FREELANCER, User.Roles.TEAMLEAD, User.Roles.ADMIN,
    }
    return render(request, 'pipeline/room_leads.html', {
        'project': project,
        'leads': leads,
        'can_manage_team': user_can_manage_team(request.user, project),
        'can_create_lead': can_create and user_can_access_project(request.user, project),
        'create_form': LeadCreateForm() if can_create else None,
        'hot_criteria': (project.input_data or {}).get('hot_criteria', ''),
        'active_tab': 'leads',
    })


@login_required
@require_POST
def lead_create(request, project_id):
    project = _get_project(request.user, project_id)
    form = LeadCreateForm(request.POST)
    if form.is_valid():
        try:
            create_lead(
                project=project,
                creator=request.user,
                contact_info=form.contact_info(),
                source=form.cleaned_data['source'],
                notes=form.cleaned_data.get('notes', ''),
                qualification_status=form.cleaned_data['qualification_status'],
            )
            messages.success(request, 'Лид создан.')
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, 'Проверьте контакты лида.')
    return redirect('pipeline:room_leads', project_id=project.id)


@login_required
def lead_detail(request, project_id, lead_id):
    project = _get_project(request.user, project_id)
    lead = get_object_or_404(
        Lead.objects.select_related('creator', 'assigned_manager'),
        id=lead_id,
        project=project,
    )
    if (
        request.user.role == User.Roles.FREELANCER
        and lead.creator_id != request.user.id
    ):
        raise PermissionDenied

    history = lead.status_history.select_related('changed_by').all()
    can_manage = user_can_manage_team(request.user, project)
    qualify_form = None
    if can_manage:
        qualify_form = LeadQualifyForm(initial={
            'qualification_status': lead.qualification_status,
            'matched_hot_criteria': '\n'.join(lead.matched_hot_criteria or []),
        })

    return render(request, 'pipeline/lead_detail.html', {
        'project': project,
        'lead': lead,
        'history': history,
        'can_manage_team': can_manage,
        'qualify_form': qualify_form,
        'hot_criteria': (project.input_data or {}).get('hot_criteria', ''),
        'active_tab': 'leads',
    })


@login_required
@require_POST
def lead_qualify(request, project_id, lead_id):
    project = _get_project(request.user, project_id)
    lead = get_object_or_404(Lead, id=lead_id, project=project)
    form = LeadQualifyForm(request.POST)
    if form.is_valid():
        try:
            set_lead_qualification(
                lead=lead,
                new_status=form.cleaned_data['qualification_status'],
                changed_by=request.user,
                comment=form.cleaned_data.get('comment', ''),
                matched_hot_criteria=form.cleaned_criteria_list(),
            )
            messages.success(request, 'Квалификация обновлена.')
            if form.cleaned_data['qualification_status'] == Lead.Qualification.HOT:
                messages.info(
                    request,
                    'Создана задача менеджеру: связаться в течение 24 часов.',
                )
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, 'Некорректные данные квалификации.')
    return redirect('pipeline:lead_detail', project_id=project.id, lead_id=lead.id)


@login_required
def manager_inbox(request):
    """Список задач менеджера по горячим лидам."""
    if request.user.role not in {User.Roles.MANAGER, User.Roles.ADMIN}:
        raise PermissionDenied('Только для менеджера платформы.')
    tasks = (
        Task.objects.filter(
            assignee=request.user,
            task_type=Task.TaskType.MANAGER_HANDOFF,
        )
        .exclude(status=Task.Status.CLOSED)
        .select_related('project', 'lead')
        .order_by('deadline', '-created_at')
    )
    return render(request, 'pipeline/manager_inbox.html', {'tasks': tasks})
