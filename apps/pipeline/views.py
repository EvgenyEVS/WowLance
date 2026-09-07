from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.rooms.models import Project, RoomActivity
from apps.rooms.services import (
    ensure_room_for_project,
    finalize_expired_termination_for_user,
    log_room_activity,
    require_can_work_in_room,
    require_non_archived_room_member,
    room_nav_context,
    user_can_access_lead,
    user_can_access_project,
    user_can_access_task,
    user_can_create_task,
    user_can_manage_team,
    user_can_view_tasks_tab,
)
from apps.users.models import User

from .forms import (
    LeadCreateForm,
    LeadDiscoveryForm,
    LeadQualifyForm,
    ReportReviewForm,
    ReportSubmitForm,
    TaskCreateForm,
    TeamleadPeriodReportForm,
)
from .discovery import (
    DISCOVERY_GROUPS,
    discovery_hint_key,
    discovery_hint_text,
    normalize_discovery_checks,
)
from .kanban import lead_columns, task_columns
from .models import FreelancerAccrual, Lead, Report, Task
from .teamlead_report import build_teamlead_period_report
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


def _can_edit_lead_discovery(user, lead) -> bool:
    """Чеклист пишет создатель-фрилансер или тимлид проекта."""
    if user_can_manage_team(user, lead.project):
        return True
    return (
        getattr(user, 'role', None) == User.Roles.FREELANCER
        and lead.creator_id == user.id
    )


def _get_project(user, project_id):
    project = get_object_or_404(
        Project.objects.select_related('owner', 'teamlead'),
        id=project_id,
    )
    if not user_can_access_project(user, project):
        raise PermissionDenied('Нет доступа к проекту.')
    # Срок расторжения закрывается до гейтов этого же запроса — см.
    # `apps.rooms.views._get_accessible_project`.
    finalize_expired_termination_for_user(user, project)
    ensure_room_for_project(project)
    return project


def _get_accessible_task(user, project_id, task_id):
    """Карточка задачи: доступ к проекту или assignee (менеджерский handoff)."""
    project = get_object_or_404(
        Project.objects.select_related('owner', 'teamlead'),
        id=project_id,
    )
    task = get_object_or_404(
        Task.objects.select_related('assignee', 'lead').prefetch_related('reports'),
        id=task_id,
        project=project,
    )
    if not user_can_access_task(user, task):
        raise PermissionDenied('Нет доступа к задаче.')
    finalize_expired_termination_for_user(user, project)
    ensure_room_for_project(project)
    return project, task


def _get_accessible_lead(user, project_id, lead_id):
    """Карточка лида: доступ к проекту или назначенный менеджер (Hot handoff).

    Узкий аналог `_get_accessible_task`: лид достаётся до проверки прав,
    потому что право открыть карточку зависит от самого лида
    (`assigned_manager`), а не только от проекта. Доступ ко всей комнате
    менеджеру это не даёт — см. `user_can_access_lead`.
    """
    project = get_object_or_404(
        Project.objects.select_related('owner', 'teamlead'),
        id=project_id,
    )
    lead = get_object_or_404(
        Lead.objects.select_related('creator', 'assigned_manager'),
        id=lead_id,
        project=project,
    )
    if not user_can_access_lead(user, lead):
        raise PermissionDenied('Нет доступа к лиду.')
    finalize_expired_termination_for_user(user, project)
    ensure_room_for_project(project)
    return project, lead


@login_required
def room_tasks(request, project_id):
    project = _get_project(request.user, project_id)
    require_non_archived_room_member(request.user, project)
    if not user_can_view_tasks_tab(request.user, project):
        messages.info(
            request,
            'Доска задач для операционной работы — у тимлида. На Обзоре доступно превью.',
        )
        return redirect('rooms:room_overview', project_id=project.id)
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
    nav = room_nav_context(request.user, project)
    task_list = list(tasks)
    period_report_form = None
    if project.teamlead_id == request.user.id:
        period_report_form = TeamleadPeriodReportForm(
            user=request.user,
            initial_project=project,
        )
    return render(request, 'pipeline/room_tasks.html', {
        'project': project,
        'tasks': task_list,
        'kanban_columns': task_columns(task_list),
        'can_manage_team': can_manage,
        'create_form': (
            TaskCreateForm(project=project) if nav['can_create_task'] else None
        ),
        'period_report_form': period_report_form,
        'active_tab': 'tasks',
        **nav,
    })


@login_required
@require_POST
def task_create(request, project_id):
    """Постановка задачи — право тимлида проекта (`user_can_create_task`).

    Проверка дублирует guard сервиса намеренно: view обязан ответить 403 до
    разбора формы, а сервис — не дать создать задачу в обход view.
    """
    project = _get_project(request.user, project_id)
    if not user_can_create_task(request.user, project):
        raise PermissionDenied('Задачи в комнате ставит тимлид проекта.')
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
    project, task = _get_accessible_task(request.user, project_id, task_id)
    # После получения задачи, а не вместо `user_can_access_task`: там есть
    # shortcut «я исполнитель», который сам по себе пропустил бы архивного.
    require_non_archived_room_member(request.user, project)

    reports = task.reports.select_related('author', 'reviewed_by').all()
    pending = reports.filter(review_status=Report.ReviewStatus.PENDING).first()
    can_manage = user_can_manage_team(request.user, project)
    is_assignee = task.assignee_id == request.user.id
    # Ссылка на лид — по праву на сам лид, а не на комнату: менеджеру
    # handoff-задачи связанная карточка открыта, доска лидов — нет.
    can_open_lead = task.lead_id is not None and user_can_access_lead(
        request.user, task.lead
    )
    # Вкладки комнаты и «← К задачам» — только у тех, кто уже в проекте.
    # Менеджер handoff задачу открывает, комнату — нет.
    show_room_chrome = user_can_access_project(request.user, project)
    context = {
        'project': project,
        'task': task,
        'reports': reports,
        'pending_report': pending,
        'can_manage_team': can_manage,
        'is_assignee': is_assignee,
        'can_open_lead': can_open_lead,
        'report_form': ReportSubmitForm() if is_assignee else None,
        'review_form': ReportReviewForm() if can_manage and pending else None,
        'can_close': task.can_be_closed(),
        'show_room_chrome': show_room_chrome,
        'active_tab': 'tasks',
    }
    if show_room_chrome:
        context.update(room_nav_context(request.user, project))
    return render(request, 'pipeline/task_detail.html', context)


@login_required
@require_POST
def task_start(request, project_id, task_id):
    project, task = _get_accessible_task(request.user, project_id, task_id)
    # До try: перехваченный ниже PermissionDenied стал бы flash-сообщением и
    # 302, а отстранённый расторжением исполнитель обязан получить 403.
    require_can_work_in_room(request.user, project)
    try:
        start_task(task, request.user)
        messages.success(request, 'Задача взята в работу.')
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, str(exc))
    return redirect('pipeline:task_detail', project_id=project.id, task_id=task.id)


@login_required
@require_POST
def task_submit_report(request, project_id, task_id):
    project, task = _get_accessible_task(request.user, project_id, task_id)
    require_can_work_in_room(request.user, project)
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
    # Review — только manage_team; доступ через карточку задачи не расширяет
    # право проверки (менеджер-assignee отчёт тимлиду не утверждает).
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
    project, task = _get_accessible_task(request.user, project_id, task_id)
    # До try, как в task_start: `close_task` разрешает закрытие исполнителю,
    # поэтому отстранённый расторжением фрилансер иначе закрывал бы свою
    # старую задачу прямым POST, а перехваченный ниже PermissionDenied стал
    # бы flash-сообщением вместо 403.
    require_can_work_in_room(request.user, project)
    try:
        close_task(task, request.user)
        messages.success(request, 'Задача закрыта.')
    except (PermissionDenied, TaskCloseError, ValidationError) as exc:
        messages.error(request, str(exc))
    return redirect('pipeline:task_detail', project_id=project.id, task_id=task.id)


@login_required
def room_leads(request, project_id):
    project = _get_project(request.user, project_id)
    require_non_archived_room_member(request.user, project)
    leads = Lead.objects.filter(project=project).select_related(
        'creator', 'assigned_manager',
    )
    if request.user.role == User.Roles.FREELANCER:
        leads = leads.filter(creator=request.user)

    can_create = request.user.role in {
        User.Roles.FREELANCER, User.Roles.TEAMLEAD, User.Roles.ADMIN,
    }
    lead_list = list(leads)
    return render(request, 'pipeline/room_leads.html', {
        'project': project,
        'leads': lead_list,
        # Cold / Warm / Hot раскладывает сервер по существующему
        # `Lead.Qualification`: шаблон не знает ни статусов, ни их порядка.
        'lead_columns': lead_columns(lead_list),
        'can_manage_team': user_can_manage_team(request.user, project),
        'can_create_lead': can_create and user_can_access_project(request.user, project),
        'create_form': LeadCreateForm() if can_create else None,
        'hot_criteria': (project.input_data or {}).get('hot_criteria', ''),
        'active_tab': 'leads',
        **room_nav_context(request.user, project),
    })


@login_required
@require_POST
def lead_create(request, project_id):
    project = _get_project(request.user, project_id)
    require_can_work_in_room(request.user, project)
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
    project, lead = _get_accessible_lead(request.user, project_id, lead_id)
    # После получения лида, а не вместо него: у `_get_accessible_lead` есть
    # ветка «я назначенный менеджер», архивного участника она не касается.
    require_non_archived_room_member(request.user, project)
    if (
        request.user.role == User.Roles.FREELANCER
        and lead.creator_id != request.user.id
    ):
        raise PermissionDenied

    history = lead.status_history.select_related('changed_by').all()
    can_manage = user_can_manage_team(request.user, project)
    can_edit_discovery = _can_edit_lead_discovery(request.user, lead)
    checks = normalize_discovery_checks(lead.discovery_checks)
    discovery_form = LeadDiscoveryForm(initial=checks)
    discovery_sections = [
        {
            'id': group_id,
            'label': group_label,
            'fields': [
                {
                    'key': key,
                    'caption': caption,
                    'checked': checks[key],
                }
                for key, caption in fields
            ],
        }
        for group_id, group_label, fields in DISCOVERY_GROUPS
    ]
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
        'can_edit_discovery': can_edit_discovery,
        'discovery_form': discovery_form,
        'discovery_sections': discovery_sections,
        'discovery_hint': discovery_hint_key(checks),
        'discovery_hint_text': discovery_hint_text(checks),
        # Менеджер handoff видит одну карточку, но не доску лидов: ссылка
        # «К лидам» ему привела бы на 403.
        'can_open_leads_board': user_can_access_project(request.user, project),
        'qualify_form': qualify_form,
        'hot_criteria': (project.input_data or {}).get('hot_criteria', ''),
        'active_tab': 'leads',
        **room_nav_context(request.user, project),
    })


@login_required
@require_POST
def lead_discovery(request, project_id, lead_id):
    """Сохранить чеклист фактов. Не трогает qualification_status / handoff."""
    project = _get_project(request.user, project_id)
    require_can_work_in_room(request.user, project)
    require_non_archived_room_member(request.user, project)
    lead = get_object_or_404(Lead, id=lead_id, project=project)
    if not _can_edit_lead_discovery(request.user, lead):
        raise PermissionDenied('Чеклист может сохранить создатель лида или тимлид.')

    form = LeadDiscoveryForm(request.POST)
    if form.is_valid():
        lead.discovery_checks = form.cleaned_checks()
        lead.save(update_fields=['discovery_checks', 'updated_at'])
        messages.success(request, 'Чеклист сохранён.')
    else:
        messages.error(request, 'Не удалось сохранить чеклист.')
    return redirect('pipeline:lead_detail', project_id=project.id, lead_id=lead.id)


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
        except ValidationError as exc:
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
    # НОВОЕ: есть ли в системе хотя бы один активный менеджер
    has_platform_manager = User.objects.filter(
        role=User.Roles.MANAGER,
        status=User.Status.ACTIVE,
    ).exists()
    return render(request, 'pipeline/manager_inbox.html', {
        'tasks': tasks,
        'has_platform_manager': has_platform_manager,
    })


@login_required
def teamlead_report(request):
    """Отчёт тимлида за период. Только чтение, только GET.

    View тонкий: разбор запроса — в `TeamleadPeriodReportForm`, все цифры —
    в `build_teamlead_period_report`. Здесь остаются ровно две вещи, которые
    формой не выражаются: ролевой guard и 403 за чужой `project_id`.

    Право даёт только роль TEAMLEAD. `user_can_access_project` сознательно не
    используется: он пропустил бы владельца-директора и любого участника
    комнаты, а отчёт — рабочее место тимлида.
    """
    if request.user.role != User.Roles.TEAMLEAD:
        raise PermissionDenied('Отчёт за период доступен только тимлиду.')

    # Чужой проект обязан давать 403, а не «Выберите корректный вариант»
    # от ModelChoiceField: иначе владелец чужого проекта по ответу формы
    # понимал бы, что проект существует.
    raw_project = (request.GET.get('project') or '').strip()
    if raw_project:
        try:
            requested = Project.objects.get(pk=raw_project)
        except (Project.DoesNotExist, ValidationError, ValueError):
            # Несуществующий или битый id — обычная ошибка формы (200),
            # а не 403 и тем более не 500.
            requested = None
        if requested is not None and requested.teamlead_id != request.user.id:
            raise PermissionDenied('Это не ваш проект.')

    # Форма связывается всегда, в том числе пустым QueryDict: по контракту
    # запрос без параметров означает период по умолчанию, а не пустой экран.
    form = TeamleadPeriodReportForm(request.GET, user=request.user)
    report = None
    if form.is_valid():
        report = build_teamlead_period_report(
            user=request.user,
            date_from=form.cleaned_data['date_from'],
            date_to=form.cleaned_data['date_to'],
            project=form.cleaned_data['project'],
        )

    return render(request, 'pipeline/teamlead_report.html', {
        'form': form,
        'report': report,
    })


@login_required
def freelancer_accruals(request):
    """Общая история начислений текущего фрилансера."""
    if request.user.role != User.Roles.FREELANCER:
        raise PermissionDenied('История начислений доступна только фрилансеру.')
    accruals = (
        FreelancerAccrual.objects.filter(freelancer=request.user)
        .select_related('project', 'report', 'report__task')
    )
    return render(request, 'pipeline/freelancer_accruals.html', {
        'accruals': accruals,
        'project': None,
    })


@login_required
def freelancer_project_accruals(request, project_id):
    """История начислений фрилансера на одном проекте (страница комнаты)."""
    if request.user.role != User.Roles.FREELANCER:
        raise PermissionDenied('История начислений доступна только фрилансеру.')
    project = _get_project(request.user, project_id)
    accruals = (
        FreelancerAccrual.objects.filter(
            freelancer=request.user,
            project=project,
        )
        .select_related('project', 'report', 'report__task')
    )
    return render(request, 'pipeline/freelancer_accruals.html', {
        'project': project,
        'accruals': accruals,
        'active_tab': '',
        **room_nav_context(request.user, project),
    })
