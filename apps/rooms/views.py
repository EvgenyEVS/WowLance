from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.core.absolute_uri import absolute_uri
from apps.pipeline.models import Task
from apps.users.models import User
from .forms import (
    AddFreelancerForm,
    AddToRoomForm,
    AssignTeamleadForm,
    ProjectCreateForm,
    RoomDocumentForm,
    TeamleadInviteRegisterForm,
)
from .models import Project, RoomActivity, RoomDocument, RoomMember, TeamleadInvite
from .onboarding import staffing_projects_for_user
from .presets import (
    ARCHITECTURE_PRESETS,
    apply_preset_to_form_initial,
    get_architecture_preset,
)
from .services import (
    TEST_LAUNCH_PAYMENT_AMOUNT_LABEL,
    accept_teamlead_invite,
    add_freelancer_to_room,
    assign_teamlead,
    create_teamlead_invite,
    ensure_room_for_project,
    handle_project_paid,
    launch_project,
    log_room_activity,
    user_can_access_project,
    user_can_manage_team,
)

SESSION_ARCH_KEY = 'architecture_preset'


def _require_director(user):
    if user.role != User.Roles.DIRECTOR:
        raise PermissionDenied('Только директор может выполнить это действие.')


def _get_accessible_project(user, project_id):
    project = get_object_or_404(
        Project.objects.select_related('owner', 'teamlead'),
        id=project_id,
    )
    if not user_can_access_project(user, project):
        raise PermissionDenied('Нет доступа к этому проекту.')
    return project


def _missing_launch_inputs(project):
    """Обязательные вводные, без которых проект нельзя запускать."""
    required = ('offer', 'audience', 'hot_criteria')
    return [key for key in required if not (project.input_data or {}).get(key)]


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


@login_required
def project_list(request):
    """Список проектов, доступных пользователю."""
    user = request.user
    if user.role == User.Roles.DIRECTOR:
        projects = Project.objects.filter(owner=user)
    elif user.role == User.Roles.TEAMLEAD:
        projects = Project.objects.filter(teamlead=user)
    elif user.role == User.Roles.FREELANCER:
        projects = Project.objects.filter(room__members__user=user).distinct()
    elif user.role == User.Roles.ADMIN:
        projects = Project.objects.all()
    else:
        projects = Project.objects.none()

    projects = projects.select_related('owner', 'teamlead').order_by('-created_at')
    return render(request, 'rooms/project_list.html', {
        'projects': projects,
        'empty_cta_url': (
            reverse('rooms:setup_wizard')
            if user.role == User.Roles.DIRECTOR
            else reverse('core:home')
        ),
        'empty_cta_label': (
            'Создать первый проект'
            if user.role == User.Roles.DIRECTOR
            else 'На дашборд'
        ),
    })


def apply_architecture(request):
    """
    Apply Architecture: сохраняет пресет и ведёт в 3-шаговый wizard
    или на регистрацию директора.
    """
    arch = request.GET.get('arch', '').strip()
    scale = request.GET.get('scale', '').strip()
    preset = get_architecture_preset(arch)
    if not preset:
        messages.error(request, 'Неизвестная архитектура.')
        return redirect('core:home')

    request.session[SESSION_ARCH_KEY] = arch
    if scale:
        request.session['architecture_scale'] = scale

    if not request.user.is_authenticated:
        return redirect(f"{reverse('users:register')}?role=director&arch={arch}")

    if request.user.role != User.Roles.DIRECTOR:
        messages.error(request, 'Архитектуру применяет директор.')
        return redirect('core:home')

    return redirect(f"{reverse('rooms:setup_wizard')}?step=2&arch={arch}")


@login_required
def setup_wizard(request):
    """Реальный wizard из 3 шагов: архитектура → вводные → запуск."""
    _require_director(request.user)
    step = request.GET.get('step') or request.POST.get('step') or '1'
    if step not in {'1', '2', '3'}:
        step = '1'

    arch_key = (
        request.GET.get('arch')
        or request.POST.get('arch')
        or request.session.get(SESSION_ARCH_KEY)
    )
    preset = get_architecture_preset(arch_key)

    if request.method == 'POST' and step == '1':
        arch_key = request.POST.get('arch', '').strip()
        preset = get_architecture_preset(arch_key)
        if not preset:
            messages.error(request, 'Выберите архитектуру.')
            return redirect(f"{reverse('rooms:setup_wizard')}?step=1")
        request.session[SESSION_ARCH_KEY] = arch_key
        return redirect(f"{reverse('rooms:setup_wizard')}?step=2&arch={arch_key}")

    if request.method == 'POST' and step == '2':
        form = ProjectCreateForm(request.POST)
        if form.is_valid():
            project = form.save(commit=False)
            project.owner = request.user
            project.status = Project.Status.DRAFT
            if preset:
                data = dict(project.input_data or {})
                data['architecture'] = preset['key']
                project.input_data = data
            project.save()
            request.session.pop(SESSION_ARCH_KEY, None)
            request.session['wizard_project_id'] = str(project.id)
            messages.success(request, 'Черновик проекта создан. Запустите комнату.')
            return redirect(f"{reverse('rooms:setup_wizard')}?step=3&project={project.id}")
    else:
        initial = apply_preset_to_form_initial(preset) if preset else None
        form = ProjectCreateForm(initial=initial) if step == '2' else ProjectCreateForm()

    project = None
    if step == '3':
        project_id = request.GET.get('project') or request.session.get('wizard_project_id')
        if project_id:
            project = get_object_or_404(Project, id=project_id, owner=request.user)

    if request.method == 'POST' and step == '3' and project:
        action = request.POST.get('action', 'launch')
        if action == 'launch':
            launch_project(project, actor=request.user)
            request.session.pop('wizard_project_id', None)
            messages.success(request, 'Комната запущена. Соберите команду.')
            return redirect('rooms:room_overview', project_id=project.id)
        request.session.pop('wizard_project_id', None)
        return redirect('rooms:project_detail', project_id=project.id)

    return render(request, 'rooms/setup_wizard.html', {
        'step': int(step),
        'presets': ARCHITECTURE_PRESETS.values(),
        'preset': preset,
        'arch_key': arch_key or '',
        'form': form if step == '2' else None,
        'project': project,
        'test_payment_amount': TEST_LAUNCH_PAYMENT_AMOUNT_LABEL,
    })


@login_required
def project_create(request):
    """Создание проекта директором (черновик) — с учётом пресета из сессии."""
    _require_director(request.user)
    arch_key = request.GET.get('arch') or request.session.get(SESSION_ARCH_KEY)
    preset = get_architecture_preset(arch_key)

    if request.method == 'POST':
        form = ProjectCreateForm(request.POST)
        if form.is_valid():
            project = form.save(commit=False)
            project.owner = request.user
            project.status = Project.Status.DRAFT
            if preset:
                data = dict(project.input_data or {})
                data['architecture'] = preset['key']
                project.input_data = data
            project.save()
            request.session.pop(SESSION_ARCH_KEY, None)
            messages.success(request, 'Проект создан. Заполните вводные и запустите его.')
            return redirect('rooms:project_detail', project_id=project.id)
    else:
        initial = apply_preset_to_form_initial(preset) if preset else None
        form = ProjectCreateForm(initial=initial)

    return render(request, 'rooms/project_create.html', {
        'form': form,
        'preset': preset,
    })


@login_required
def project_detail(request, project_id):
    """Карточка проекта → если есть комната, редирект в неё."""
    project = _get_accessible_project(request.user, project_id)
    if hasattr(project, 'room'):
        return redirect('rooms:room_overview', project_id=project.id)
    can_launch = (
        request.user.id == project.owner_id
        and project.status == Project.Status.DRAFT
    )
    return render(request, 'rooms/project_detail.html', {
        'project': project,
        'can_launch': can_launch,
        'test_payment_amount': TEST_LAUNCH_PAYMENT_AMOUNT_LABEL,
        'project_list_url': reverse('rooms:project_list'),
    })


@login_required
@require_POST
def project_launch(request, project_id):
    """Запуск проекта без оплаты (MVP): создаёт комнату, статус Staffing."""
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    if project.status != Project.Status.DRAFT:
        messages.error(request, 'Запустить можно только черновик.')
        return redirect('rooms:project_detail', project_id=project.id)

    required = ('offer', 'audience', 'hot_criteria')
    missing = [key for key in required if not (project.input_data or {}).get(key)]
    if missing:
        messages.error(request, 'Заполните обязательные вводные перед запуском.')
        return redirect('rooms:project_detail', project_id=project.id)

    launch_project(project, actor=request.user)
    messages.success(
        request,
        'Проект запущен. Комната создана, можно собирать команду. '
        '(Оплата будет подключена позже.)',
    )
    return redirect('rooms:room_overview', project_id=project.id)


@login_required
@require_POST
def project_pay(request, project_id):
    """
    Тестовая оплата запуска (stub, без Stripe и webhook).

    Проверяет доступ и черновик, отдаёт результат оплаты в rooms.services
    и уводит директора в комнату проекта.
    """
    project = get_object_or_404(Project, id=project_id, owner=request.user)
    if project.status != Project.Status.DRAFT:
        messages.error(request, 'Оплатить запуск можно только для черновика.')
        return redirect('rooms:project_detail', project_id=project.id)

    if _missing_launch_inputs(project):
        messages.error(request, 'Заполните обязательные вводные перед оплатой запуска.')
        return redirect('rooms:project_detail', project_id=project.id)

    room = handle_project_paid(project, actor=request.user)
    request.session.pop('wizard_project_id', None)
    messages.success(
        request,
        f'Тестовая оплата {TEST_LAUNCH_PAYMENT_AMOUNT_LABEL} прошла успешно. '
        'Комната открыта — соберите команду. (Реальный платёж не проводился.)',
    )
    return redirect('rooms:room_overview', project_id=room.project_id)


@login_required
def room_overview(request, project_id):
    """Hub комнаты: вводные + activity feed."""
    project = _get_accessible_project(request.user, project_id)
    room = ensure_room_for_project(project) if project.status != Project.Status.DRAFT else getattr(project, 'room', None)
    if room is None and project.status == Project.Status.DRAFT:
        return redirect('rooms:project_detail', project_id=project.id)

    room = ensure_room_for_project(project)
    members = room.members.select_related('user').all()
    my_membership = members.filter(user=request.user).first()
    activities = room.activities.select_related('actor').all()[:20]
    tasks = Task.objects.filter(project=project).select_related('assignee')[:50]

    return render(request, 'rooms/room_overview.html', {
        'project': project,
        'room': room,
        'members': members,
        'my_membership': my_membership,
        'activities': activities,
        'kanban_preview': _kanban_columns(tasks)[:3],
        'can_manage_team': user_can_manage_team(request.user, project),
        'can_launch': (
            request.user.id == project.owner_id
            and project.status == Project.Status.DRAFT
        ),
        'active_tab': 'overview',
    })


@login_required
def room_documents(request, project_id):
    """Документы / Dropbox-lite комнаты."""
    project = _get_accessible_project(request.user, project_id)
    room = ensure_room_for_project(project)
    documents = room.documents.select_related('uploaded_by').all()
    form = RoomDocumentForm()
    return render(request, 'rooms/room_documents.html', {
        'project': project,
        'room': room,
        'documents': documents,
        'form': form,
        'can_upload': user_can_access_project(request.user, project),
        'can_manage_team': user_can_manage_team(request.user, project),
        'active_tab': 'documents',
    })


@login_required
@require_POST
def room_document_upload(request, project_id):
    project = _get_accessible_project(request.user, project_id)
    room = ensure_room_for_project(project)
    form = RoomDocumentForm(request.POST, request.FILES)
    if form.is_valid():
        doc = form.save(commit=False)
        doc.room = room
        doc.uploaded_by = request.user
        if not doc.title and doc.file:
            doc.title = doc.file.name
        doc.save()
        log_room_activity(
            room,
            f'Документ «{doc.title}» загружен.',
            RoomActivity.EventType.DOCUMENT_UPLOADED,
            actor=request.user,
        )
        messages.success(request, 'Документ загружен.')
    else:
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
    return redirect('rooms:room_documents', project_id=project.id)


@login_required
@require_POST
def room_document_delete(request, project_id, document_id):
    project = _get_accessible_project(request.user, project_id)
    room = ensure_room_for_project(project)
    doc = get_object_or_404(RoomDocument, id=document_id, room=room)
    if not (
        user_can_manage_team(request.user, project)
        or doc.uploaded_by_id == request.user.id
    ):
        raise PermissionDenied('Нельзя удалить этот документ.')
    doc.delete()
    messages.success(request, 'Документ удалён.')
    return redirect('rooms:room_documents', project_id=project.id)


@login_required
def room_team(request, project_id):
    """Состав команды комнаты + invite тимлида."""
    project = _get_accessible_project(request.user, project_id)
    room = ensure_room_for_project(project)
    members = room.members.select_related('user').all()
    can_manage = user_can_manage_team(request.user, project)
    my_membership = members.filter(user=request.user).first()
    invite = (
        TeamleadInvite.objects.filter(project=project, is_active=True)
        .order_by('-created_at')
        .first()
    )
    invite_url = None
    if invite and invite.is_valid:
        invite_url = absolute_uri(
            request,
            reverse('rooms:teamlead_invite_accept', kwargs={'token': invite.token}),
        )

    return render(request, 'rooms/room_team.html', {
        'project': project,
        'room': room,
        'members': members,
        'can_manage_team': can_manage,
        'my_membership': my_membership,
        'teamlead_form': AssignTeamleadForm() if can_manage else None,
        'freelancer_form': AddFreelancerForm(room=room) if can_manage else None,
        'invite_url': invite_url,
        'active_tab': 'team',
    })


@login_required
@require_POST
def room_create_teamlead_invite(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    if request.user.id != project.owner_id and request.user.role != User.Roles.ADMIN:
        raise PermissionDenied('Создавать приглашение может только директор.')
    ensure_room_for_project(project)
    invite = create_teamlead_invite(project, request.user)
    url = absolute_uri(
        request,
        reverse('rooms:teamlead_invite_accept', kwargs={'token': invite.token}),
    )
    messages.success(request, f'Ссылка-приглашение для тимлида создана: {url}')
    return redirect('rooms:room_team', project_id=project.id)


def teamlead_invite_accept(request, token):
    """Публичная страница принятия invite тимлида."""
    invite = get_object_or_404(TeamleadInvite.objects.select_related('project'), token=token)
    if not invite.is_valid:
        messages.error(request, 'Приглашение недействительно или уже использовано.')
        return redirect('users:login')

    if request.user.is_authenticated:
        try:
            accept_teamlead_invite(invite, request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('core:home')
        messages.success(request, f'Вы тимлид проекта «{invite.project.name}».')
        return redirect('rooms:room_overview', project_id=invite.project_id)

    form = TeamleadInviteRegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        accept_teamlead_invite(invite, user)
        login(request, user)
        messages.success(request, f'Аккаунт тимлида создан. Проект: {invite.project.name}.')
        return redirect('rooms:room_overview', project_id=invite.project_id)

    return render(request, 'rooms/teamlead_invite.html', {
        'invite': invite,
        'project': invite.project,
        'form': form,
        'login_next': reverse('rooms:teamlead_invite_accept', kwargs={'token': token}),
    })


@login_required
@require_POST
def room_assign_teamlead(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    if not user_can_manage_team(request.user, project):
        raise PermissionDenied
    if request.user.id != project.owner_id and request.user.role != User.Roles.ADMIN:
        raise PermissionDenied('Назначать тимлида может только директор.')

    form = AssignTeamleadForm(request.POST)
    if form.is_valid():
        assign_teamlead(project, form.cleaned_data['teamlead'], actor=request.user)
        messages.success(request, 'Тимлид назначен.')
    else:
        messages.error(request, 'Не удалось назначить тимлида. Есть активные тимлиды?')
    return redirect('rooms:room_team', project_id=project.id)


@login_required
@require_POST
def room_add_freelancer(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    if not user_can_manage_team(request.user, project):
        raise PermissionDenied
    room = ensure_room_for_project(project)
    form = AddFreelancerForm(request.POST, room=room)
    if form.is_valid():
        add_freelancer_to_room(room, form.cleaned_data['freelancer'], actor=request.user)
        messages.success(request, 'Фрилансер добавлен в комнату.')
    else:
        messages.error(request, 'Не удалось добавить фрилансера.')
    return redirect('rooms:room_team', project_id=project.id)


@login_required
@require_POST
def catalog_add_to_room(request, user_id):
    """Добавить фрилансера из каталога/карточки в выбранный проект."""
    freelancer = get_object_or_404(User, id=user_id, role=User.Roles.FREELANCER)
    projects = staffing_projects_for_user(request.user)
    form = AddToRoomForm(request.POST, projects=projects)
    if not form.is_valid():
        messages.error(request, 'Выберите проект со статусом подбора или активный.')
        return redirect('profiles:detail', user_id=user_id)

    project = form.cleaned_data['project']
    if not user_can_manage_team(request.user, project):
        raise PermissionDenied
    room = ensure_room_for_project(project)
    add_freelancer_to_room(room, freelancer, actor=request.user)
    messages.success(
        request,
        f'{freelancer.full_name} добавлен в комнату «{project.name}».',
    )
    return redirect('rooms:room_team', project_id=project.id)


@login_required
@require_POST
def room_remove_member(request, project_id, member_id):
    project = get_object_or_404(Project, id=project_id)
    if not user_can_manage_team(request.user, project):
        raise PermissionDenied
    room = ensure_room_for_project(project)
    member = get_object_or_404(RoomMember, id=member_id, room=room)
    if member.role_in_room == RoomMember.RoleInRoom.DIRECTOR:
        messages.error(request, 'Нельзя удалить директора из комнаты.')
        return redirect('rooms:room_team', project_id=project.id)

    name = member.user.full_name
    if member.role_in_room == RoomMember.RoleInRoom.TEAMLEAD:
        project.teamlead = None
        project.save(update_fields=['teamlead', 'updated_at'])

    member.delete()
    log_room_activity(
        room,
        f'{name} удалён из команды.',
        RoomActivity.EventType.MEMBER_REMOVED,
        actor=request.user,
    )
    messages.success(request, 'Участник удалён из комнаты.')
    return redirect('rooms:room_team', project_id=project.id)


@login_required
@require_POST
def room_confirm_ready(request, project_id):
    """Фрилансер подтверждает готовность к работе."""
    project = _get_accessible_project(request.user, project_id)
    room = ensure_room_for_project(project)
    member = get_object_or_404(RoomMember, room=room, user=request.user)
    if member.role_in_room != RoomMember.RoleInRoom.FREELANCER:
        messages.error(request, 'Подтверждение готовности — для фрилансеров.')
        return redirect('rooms:room_overview', project_id=project.id)

    member.ready_status = RoomMember.ReadyStatus.READY
    member.save(update_fields=['ready_status'])
    log_room_activity(
        room,
        f'{request.user.full_name} готов к работе.',
        RoomActivity.EventType.READY,
        actor=request.user,
    )
    messages.success(request, 'Статус: готов к работе.')
    return redirect('rooms:room_overview', project_id=project.id)
