from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.users.models import User
from .forms import (
    AddFreelancerForm,
    AssignTeamleadForm,
    ProjectCreateForm,
    RoomDocumentForm,
)
from .models import Project, RoomDocument, RoomMember
from .services import (
    add_freelancer_to_room,
    assign_teamlead,
    ensure_room_for_project,
    launch_project,
    user_can_access_project,
    user_can_manage_team,
)


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
    return render(request, 'rooms/project_list.html', {'projects': projects})


@login_required
def project_create(request):
    """Создание проекта директором (черновик)."""
    _require_director(request.user)

    if request.method == 'POST':
        form = ProjectCreateForm(request.POST)
        if form.is_valid():
            project = form.save(commit=False)
            project.owner = request.user
            project.status = Project.Status.DRAFT
            project.save()
            messages.success(request, 'Проект создан. Заполните вводные и запустите его.')
            return redirect('rooms:project_detail', project_id=project.id)
    else:
        form = ProjectCreateForm()

    return render(request, 'rooms/project_create.html', {'form': form})


@login_required
def project_detail(request, project_id):
    """Карточка проекта → если есть комната, редирект в неё."""
    project = _get_accessible_project(request.user, project_id)
    if hasattr(project, 'room'):
        return redirect('rooms:room_overview', project_id=project.id)
    return render(request, 'rooms/project_detail.html', {
        'project': project,
        'can_launch': (
            request.user.id == project.owner_id
            and project.status == Project.Status.DRAFT
        ),
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

    launch_project(project)
    messages.success(
        request,
        'Проект запущен. Комната создана, можно собирать команду. '
        '(Оплата будет подключена позже.)',
    )
    return redirect('rooms:room_overview', project_id=project.id)


@login_required
def room_overview(request, project_id):
    """Overview комнаты проекта."""
    project = _get_accessible_project(request.user, project_id)
    room = ensure_room_for_project(project) if project.status != Project.Status.DRAFT else getattr(project, 'room', None)
    if room is None and project.status == Project.Status.DRAFT:
        return redirect('rooms:project_detail', project_id=project.id)

    room = ensure_room_for_project(project)
    members = room.members.select_related('user').all()
    my_membership = members.filter(user=request.user).first()

    return render(request, 'rooms/room_overview.html', {
        'project': project,
        'room': room,
        'members': members,
        'my_membership': my_membership,
        'can_manage_team': user_can_manage_team(request.user, project),
        'can_launch': (
            request.user.id == project.owner_id
            and project.status == Project.Status.DRAFT
        ),
        'active_tab': 'overview',
    })


@login_required
def room_documents(request, project_id):
    """Документы / вижен комнаты."""
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
    """Состав команды комнаты."""
    project = _get_accessible_project(request.user, project_id)
    room = ensure_room_for_project(project)
    members = room.members.select_related('user').all()
    can_manage = user_can_manage_team(request.user, project)
    my_membership = members.filter(user=request.user).first()

    return render(request, 'rooms/room_team.html', {
        'project': project,
        'room': room,
        'members': members,
        'can_manage_team': can_manage,
        'my_membership': my_membership,
        'teamlead_form': AssignTeamleadForm() if can_manage else None,
        'freelancer_form': AddFreelancerForm(room=room) if can_manage else None,
        'active_tab': 'team',
    })


@login_required
@require_POST
def room_assign_teamlead(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    if not user_can_manage_team(request.user, project):
        raise PermissionDenied
    # Назначать тимлида может директор (или admin)
    if request.user.id != project.owner_id and request.user.role != User.Roles.ADMIN:
        raise PermissionDenied('Назначать тимлида может только директор.')

    form = AssignTeamleadForm(request.POST)
    if form.is_valid():
        assign_teamlead(project, form.cleaned_data['teamlead'])
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
        add_freelancer_to_room(room, form.cleaned_data['freelancer'])
        messages.success(request, 'Фрилансер добавлен в комнату.')
    else:
        messages.error(request, 'Не удалось добавить фрилансера.')
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

    if member.role_in_room == RoomMember.RoleInRoom.TEAMLEAD:
        project.teamlead = None
        project.save(update_fields=['teamlead', 'updated_at'])

    member.delete()
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
    messages.success(request, 'Статус: готов к работе.')
    return redirect('rooms:room_overview', project_id=project.id)
