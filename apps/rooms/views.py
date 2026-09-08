import mimetypes

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from apps.core.absolute_uri import absolute_uri
from apps.pipeline.kanban import task_columns
from apps.pipeline.forms import TeamleadPeriodReportForm
from apps.pipeline.models import Task
from apps.pipeline.services import get_start_calls_task
from apps.users.models import User
from . import chat, configurator
from .chat_notifications import (
    build_bell_context,
    mark_channel_read,
    mark_comms_page_read,
)
from .forms import (
    AddFreelancerForm,
    AddToRoomForm,
    AssignTeamleadForm,
    ProjectCreateForm,
    ProjectVisionForm,
    RoomChatMessageForm,
    RoomDocumentForm,
    TeamleadInviteRegisterForm,
    TerminationAppealForm,
    TerminationNoticeForm,
)
from .models import (
    FreelancerTermination,
    Project,
    RoomActivity,
    RoomChatMessage,
    RoomDocument,
    RoomFunctionSlot,
    RoomMember,
    TeamleadInvite,
)
from .director_stats import project_overview_metrics
from .onboarding import freelancer_project_stats, staffing_projects_for_user
from .presets import (
    ARCHITECTURE_PRESETS,
    apply_preset_to_form_initial,
    get_architecture_preset,
)
from .services import (
    TERMINATION_BADGE_LABELS,
    TEST_LAUNCH_PAYMENT_AMOUNT_LABEL,
    accept_teamlead_invite,
    add_freelancer_to_room,
    apply_package_and_sync_slots,
    assign_teamlead,
    confirm_freelancer_readiness,
    create_teamlead_invite,
    ensure_room_for_project,
    handle_project_paid,
    launch_project,
    log_room_activity,
    director_teamlead_video_call_url,
    finalize_expired_termination_for_user,
    open_termination_for,
    open_terminations_by_freelancer,
    open_terminations_by_project,
    require_can_work_in_room,
    require_non_archived_room_member,
    room_nav_context,
    room_video_call_url,
    user_can_access_director_teamlead_comms,
    save_functional_roles_and_sync_slots,
    update_project_vision,
    user_can_access_project,
    user_can_appoint_teamlead,
    user_can_edit_functional_roles,
    user_can_edit_project_vision,
    user_can_manage_team,
    user_can_view_composition_staffing,
    user_can_view_tasks_tab,
    user_can_view_team_tab,
)
from .staffing import matching, selectors
from .termination import (
    CHAT_MESSAGE_MAX_LENGTH,
    SYSTEM_FREELANCER_LEFT_TEXT,
    TerminationAlreadyOpen,
    TerminationError,
    appeal_termination,
    complete_termination,
    finalize_expired_termination_for,
    initiate_termination,
    post_termination_message,
    recent_termination_messages,
    revoke_termination,
)
from .staffing.services import (
    SLOT_FILL_STATUSES,
    STAFFING_MUTABLE_STATUSES,
    StaffingError,
    assign_candidate_to_slot,
    auto_assign_best_candidate,
    replace_slot_member,
)
from .unit_economics import FunctionalRolesError

SESSION_ARCH_KEY = 'architecture_preset'

#: Сколько кандидатов показывать на одной странице пула «Выбрать из пула».
CANDIDATE_POOL_PAGE_SIZE = 20


def _require_director(user):
    if user.role != User.Roles.DIRECTOR:
        raise PermissionDenied('Только директор может выполнить это действие.')


def _get_accessible_project(user, project_id):
    """Проект комнаты с проверкой доступа и ленивым истечением расторжения.

    Срок «три дня» закрывается здесь, в общей точке входа всех страниц
    комнаты, а не в контексте шаблона: гейт архивного участника стоит сразу
    после этого вызова, и просроченное уведомление обязано превратиться в
    архив **на том же запросе**, иначе после дедлайна человек успел бы
    открыть операционную вкладку ещё один раз.
    """
    project = get_object_or_404(
        Project.objects.select_related('owner', 'teamlead'),
        id=project_id,
    )
    if not user_can_access_project(user, project):
        raise PermissionDenied('Нет доступа к этому проекту.')
    finalize_expired_termination_for_user(user, project)
    return project


def _staffing_is_open(project) -> bool:
    """Показывать ли кнопки изменения занятого состава («Другой сейлер»).

    Только про UI. Настоящая защита — та же проверка внутри сервисов подбора
    (`_guard_staffing`), поэтому прямой POST в закрытый по статусу проект не
    пройдёт.
    """
    return project.status in STAFFING_MUTABLE_STATUSES


def _slot_filling_is_open(project) -> bool:
    """Показывать ли кнопки заполнения **пустого** слота.

    Шире предыдущего ровно на `ACTIVE`: завершённое расторжение освобождает
    слот запущенного проекта, и место обязано заполняться без возврата
    всего проекта в подбор. Зеркалит серверный `_guard_slot_assignment`,
    чтобы интерфейс не показывал кнопку, которая гарантированно вернёт
    `StaffingError`, и не прятал разрешённую.
    """
    return project.status in SLOT_FILL_STATUSES


def _get_slot_for_staffing(request, project_id, slot_id):
    """Слот комнаты для операции подбора: доступ к проекту + права на команду.

    Права проверяются и здесь, и в сервисе. Дублирования бизнес-логики нет:
    view отвечает за 403 до выполнения операции, сервис — за то, что операцию
    нельзя выполнить в обход view.
    """
    project = _get_accessible_project(request.user, project_id)
    if not user_can_manage_team(request.user, project):
        raise PermissionDenied('Подбором кандидатов управляет тимлид проекта.')
    room = ensure_room_for_project(project)
    slot = get_object_or_404(RoomFunctionSlot, id=slot_id, room=room)
    return project, room, slot


def _members_with_termination(room):
    """Состав комнаты с проставленным открытым кейсом расторжения.

    Признак нужен строке участника на вкладке «Команда», а спрашивать про
    каждого отдельно означало бы N+1: кейсы комнаты берутся одним запросом
    и раскладываются по участникам в памяти.

    Список, а не queryset: на объекты проставляется `open_termination`, и
    ленивый queryset выполнился бы заново уже без этого атрибута.

    Берутся только активные членства. Архивная строка — история человека в
    комнате, а не участник команды: после завершения расторжения он не
    должен ни числиться в составе, ни получать кнопки управления. Строка при
    этом остаётся в БД — фильтр стоит здесь, а не в виде `delete()`.

    Хелпер один на оба пути (вкладка «Команда» и out-of-band обновление
    таблицы после действий подбора), поэтому фильтр не может разойтись
    между обычным и HTMX-рендером.

    Директора и тимлида фильтр не задевает: их членство `is_active=True`,
    а расторжение бывает только с фрилансером.
    """
    members = list(
        room.members.select_related('user').filter(is_active=True)
    )
    open_cases = open_terminations_by_freelancer(room)
    for member in members:
        member.open_termination = open_cases.get(member.user_id)
    return members


def _termination_form_response(
    request, project, room, member, *, form=None, case=None, status=200,
):
    """Страница расторжения: новая форма причины либо карточка открытого кейса.

    Один шаблон на оба состояния и на оба входа (GET страницы и невалидный
    POST уведомления) — иначе разметка формы и её ошибки разошлись бы между
    двумя файлами.
    """
    # Чат кейса показывается только его участнику со стороны команды —
    # инициатору расторжения. Текущий тимлид, если уведомление отправлял не
    # он, тред не читает: правило треда — участие в кейсе, а не роль.
    can_use_chat = bool(case and case.initiated_by_id == request.user.id)
    return render(request, 'rooms/room_termination_form.html', {
        'project': project,
        'room': room,
        'member': member,
        'case': case,
        'form': form,
        'can_use_termination_chat': can_use_chat,
        # Отзыв — право текущего тимлида проекта, а не инициатора кейса:
        # уведомление мог отправить прежний тимлид, а отвечает за команду
        # тот, кто ведёт её сейчас. Флаг считается отдельно от чата именно
        # поэтому, а не выводится из `can_use_termination_chat`.
        'can_revoke_termination': bool(
            case is not None
            and case.is_open
            and project.teamlead_id == request.user.id
        ),
        'termination_messages': (
            recent_termination_messages(case) if can_use_chat else []
        ),
        'termination_text_max_length': CHAT_MESSAGE_MAX_LENGTH,
        'active_tab': 'team',
        **room_nav_context(request.user, project),
    }, status=status)


def _slot_action_response(request, project, slot, message, is_error=False):
    """HTMX → свежая карточка слота **и** таблица участников, обычный POST → redirect.

    Fallback без HTMX обязателен: интерфейс должен работать и при выключенном
    JavaScript, поэтому кнопки подбора остаются обычными формами.

    Таблица участников отдаётся вместе с карточкой (out-of-band), потому что
    любое действие подбора меняет состав комнаты: «Другой сейлер» снимает
    прежнего исполнителя и сажает следующего, «Подобрать лучшего» добавляет
    нового. Форма целится в свою карточку, поэтому без второго блока таблица
    ниже оставалась бы с прошлым составом до перезагрузки страницы — карточка
    показывала бы нового исполнителя, а «Участники» снятого.

    Состав собирается `_members_with_termination` уже **после** операции
    подбора — она к этому моменту завершена, — поэтому таблица показывает
    итоговое состояние. Тем же хелпером состав собирает и вкладка «Команда»,
    так что признак «идёт расторжение» не теряется после HTMX-обновления.
    """
    if request.headers.get('HX-Request'):
        return render(request, 'rooms/_slot_action.html', {
            'project': project,
            'card': selectors.slot_card_for(slot),
            'can_staff_slots': _staffing_is_open(project),
            'action_note': message,
            'action_note_is_error': is_error,
            'members': _members_with_termination(slot.room),
            'can_manage_team': user_can_manage_team(request.user, project),
            'can_fill_slots': _slot_filling_is_open(project),
        })
    if is_error:
        messages.error(request, message)
    else:
        messages.success(request, message)
    return redirect('rooms:room_team', project_id=project.id)


def _missing_launch_inputs(project):
    """Обязательные вводные, без которых проект нельзя запускать."""
    required = ('offer', 'audience', 'hot_criteria')
    return [key for key in required if not (project.input_data or {}).get(key)]


#: Заголовок единственной пока группы материалов.
#: У RoomDocument нет поля категории, и заводить его миграцией — отдельный шаг.
#: Страница уже рендерит *список групп*, поэтому появление категорий сведётся
#: к другой реализации `_material_groups`, без переделки шаблона.
MATERIALS_DEFAULT_GROUP_KEY = 'all'
MATERIALS_DEFAULT_GROUP_TITLE = 'Все материалы'


def _material_groups(documents):
    """Материалы комнаты, сгруппированные для отображения.

    Сейчас группа одна: категорий у документа нет. Пустой список означает
    «материалов нет» — шаблон показывает empty state.
    """
    docs = list(documents)
    if not docs:
        return []
    return [{
        'key': MATERIALS_DEFAULT_GROUP_KEY,
        'title': MATERIALS_DEFAULT_GROUP_TITLE,
        'documents': docs,
    }]


def _document_content_type(filename: str) -> str:
    """MIME файла для открытия в браузере.

    Для текста явно ставим UTF-8: иначе Windows открывает .txt как CP1251
    и кириллица в скриптах звонка превращается в «РћС‚РєСЂС‹С‚РёРµ».
    """
    guessed, _encoding = mimetypes.guess_type(filename)
    content_type = guessed or 'application/octet-stream'
    textual = content_type.startswith('text/') or content_type in {
        'application/json',
        'application/javascript',
        'application/xml',
    }
    if textual and 'charset=' not in content_type:
        return f'{content_type}; charset=utf-8'
    return content_type


#: Сколько последних событий комнаты показывает «Обзор» (Issue #11).
#: Ограничивается только вывод: модель `RoomActivity` и запись событий
#: не меняются, лента остаётся полной в БД.
ROOM_ACTIVITY_FEED_LIMIT = 10

#: Сколько своих задач показывает «Обзор» фрилансеру.
#: Чужая доска ему не нужна: на «Обзоре» он видит короткий список
#: собственных задач, а полный список — на вкладке «Задачи», где
#: фильтрация по исполнителю уже есть и не меняется.
FREELANCER_TASK_PREVIEW_LIMIT = 5


def _freelancer_room_projects(user, *, is_active):
    """Комнаты фрилансера, разделённые по активности его членства.

    Три условия стоят в одном `filter()` намеренно: они должны выполниться
    на **одной и той же** строке членства. Цепочка `.filter().filter()`
    по обратной связи `room__members` разрешила бы им сойтись на разных
    строках, и чужое активное членство вернуло бы проект в оперативный
    список.

    `role_in_room` проверяется явно, хотя пользователь с ролью фрилансера
    иначе в комнату и не попадает: архив — свойство строки членства, и
    правило «архивная комната» должно читаться из самого запроса.

    `distinct()` сохраняется от прежней реализации: join по обратной
    связи остаётся, и защита от дублей не должна зависеть от того,
    сколько комнат у проекта сегодня.
    """
    return Project.objects.filter(
        room__members__user=user,
        room__members__role_in_room=RoomMember.RoleInRoom.FREELANCER,
        room__members__is_active=is_active,
    ).distinct()


def _with_termination_badges(projects, freelancer):
    """Рабочие комнаты фрилансера с подписью открытого кейса на строке.

    Список, а не queryset: на проекты проставляется `termination_badge`, и
    ленивый queryset выполнился бы заново уже без этого атрибута.

    Кейсы берутся одним запросом на весь список (`open_terminations_by_project`),
    а не по кейсу на строку: N+1 на первом же экране после входа — ровно то,
    ради чего этот хелпер и заведён.

    Открытый кейс не убирает комнату из рабочего списка: членство ещё
    активно, работать в проекте нельзя, но ответить на уведомление нужно
    именно отсюда. В архив она уходит только после `completed` — этим
    занимается `_freelancer_room_projects`, и здесь эта логика не
    дублируется.
    """
    rows = list(projects)
    open_cases = open_terminations_by_project(freelancer, projects=rows)
    for project in rows:
        case = open_cases.get(project.id)
        project.termination_badge = (
            TERMINATION_BADGE_LABELS.get(case.status) if case else None
        )
    return rows


@login_required
def project_list(request):
    """Список проектов, доступных пользователю.

    У фрилансера список раздваивается: оперативные комнаты и архивные —
    те, откуда он вышел по расторжению. Архив не удаляется и не прячется
    совсем: своя история, свои цифры и начисления остаются доступны через
    «Обзор» архивной комнаты.

    Ему же — и только ему — на строке рабочей комнаты проставляется
    подпись открытого расторжения: это сигнал исполнителю, что от него
    ждут ответа. Тимлид, директор, менеджер и платформенный admin такой
    пометки не получают, и лишнего запроса за кейсами им тоже не стоит.

    Для директора, тимлида, менеджера и платформенного admin запрос и
    семантика списка не меняются: архив — состояние членства фрилансера,
    а не проекта.
    """
    user = request.user
    archived_projects = Project.objects.none()
    if user.role == User.Roles.DIRECTOR:
        projects = Project.objects.filter(owner=user)
    elif user.role == User.Roles.TEAMLEAD:
        projects = Project.objects.filter(teamlead=user)
    elif user.role == User.Roles.FREELANCER:
        projects = _freelancer_room_projects(user, is_active=True)
        archived_projects = (
            _freelancer_room_projects(user, is_active=False)
            .select_related('owner', 'teamlead')
            .order_by('-created_at')
        )
    elif user.role == User.Roles.ADMIN:
        projects = Project.objects.all()
    else:
        projects = Project.objects.none()

    projects = projects.select_related('owner', 'teamlead').order_by('-created_at')
    if user.role == User.Roles.FREELANCER:
        projects = _with_termination_badges(projects, user)
    return render(request, 'rooms/project_list.html', {
        'projects': projects,
        'archived_projects': archived_projects,
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
        action = request.POST.get('action', 'later')
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
    """Hub комнаты: метрики, лента, вводные, канбан, состав."""
    project = _get_accessible_project(request.user, project_id)
    room = ensure_room_for_project(project) if project.status != Project.Status.DRAFT else getattr(project, 'room', None)
    if room is None and project.status == Project.Status.DRAFT:
        return redirect('rooms:project_detail', project_id=project.id)

    room = ensure_room_for_project(project)
    members = room.members.select_related('user').all()
    my_membership = members.filter(user=request.user).first()
    is_freelancer_task_preview = request.user.role == User.Roles.FREELANCER
    # Лента комнаты — операционка; фрилансеру на Обзоре её не показываем.
    if is_freelancer_task_preview:
        activities = []
    else:
        activities = (
            room.activities.select_related('actor').all()[:ROOM_ACTIVITY_FEED_LIMIT]
        )
    project_tasks = Task.objects.filter(project=project).select_related('assignee')
    if is_freelancer_task_preview:
        my_tasks_preview = list(
            project_tasks.filter(assignee=request.user)[:FREELANCER_TASK_PREVIEW_LIMIT]
        )
        kanban_preview = []
        my_project_stats = freelancer_project_stats(request.user, project)
    else:
        my_tasks_preview = []
        kanban_preview = task_columns(project_tasks[:50])
        my_project_stats = None
    cards = selectors.slot_cards(room)
    staffing_summary = selectors.staffing_summary(cards)
    # Метрики шапки — для управленческого контура (директор/тимлид),
    # не для исполнителя с превью «Мои задачи».
    overview_metrics = (
        None
        if is_freelancer_task_preview
        else project_overview_metrics(project, staffing_summary)
    )

    start_calls_task = get_start_calls_task(project)
    # Фрилансер видит SLA только по стартовой задаче, назначенной ему.
    if is_freelancer_task_preview and (
        start_calls_task is None
        or start_calls_task.assignee_id != request.user.id
    ):
        start_calls_task = None
    start_calls_deadline = start_calls_task.deadline if start_calls_task else None
    start_calls_is_done = bool(
        start_calls_task and start_calls_task.status == Task.Status.CLOSED
    )
    start_calls_is_overdue = bool(
        start_calls_deadline
        and not start_calls_is_done
        and start_calls_deadline <= timezone.now()
    )

    can_edit_vision = user_can_edit_project_vision(request.user, project)
    can_appoint = user_can_appoint_teamlead(request.user, project)
    period_report_form = None
    if project.teamlead_id == request.user.id:
        period_report_form = TeamleadPeriodReportForm(
            user=request.user,
            initial_project=project,
        )
    invite = None
    invite_url = None
    if can_appoint and not project.teamlead_id:
        invite = (
            TeamleadInvite.objects.filter(project=project, is_active=True)
            .order_by('-created_at')
            .first()
        )
        if invite and invite.is_valid:
            invite_url = absolute_uri(
                request,
                reverse('rooms:teamlead_invite_accept', kwargs={'token': invite.token}),
            )

    return render(request, 'rooms/room_overview.html', {
        'project': project,
        'room': room,
        'members': members,
        'my_membership': my_membership,
        'activities': activities,
        'slot_cards': cards,
        'staffing_summary': staffing_summary,
        'project_metrics': overview_metrics,
        'my_project_stats': my_project_stats,
        'start_calls_task': start_calls_task,
        'start_calls_deadline': start_calls_deadline,
        'start_calls_is_overdue': start_calls_is_overdue,
        'start_calls_is_done': start_calls_is_done,
        'kanban_preview': kanban_preview,
        'is_freelancer_task_preview': is_freelancer_task_preview,
        'my_tasks_preview': my_tasks_preview,
        'period_report_form': period_report_form,
        'can_manage_team': user_can_manage_team(request.user, project),
        'can_appoint_teamlead': can_appoint,
        'teamlead_form': AssignTeamleadForm() if can_appoint and not project.teamlead_id else None,
        'invite_url': invite_url,
        'can_launch': (
            request.user.id == project.owner_id
            and project.status == Project.Status.DRAFT
        ),
        'active_tab': 'overview',
        'can_edit_vision': can_edit_vision,
        'vision_form': (
            ProjectVisionForm.from_project(project) if can_edit_vision else None
        ),
        **configurator.build_configurator_context(request.user, project, room),
        **room_nav_context(request.user, project),
    })


@login_required
@require_POST
def room_vision_update(request, project_id):
    """Сохранение четырёх вводных проекта директором-владельцем.

    Только POST: `@require_POST` гарантирует, что GET страницы «Обзора»
    ничего не мутирует, а форма редактирования — обычный Django-`<form>`
    с `csrf_token`, без модалки, HTMX и JavaScript.

    Права проверяются дважды: здесь — чтобы отдать 403 до начала работы,
    и внутри `update_project_vision` — чтобы вводные нельзя было изменить
    в обход view. Доступ к самому проекту проверяет
    `_get_accessible_project`, поэтому посторонний получает 403 и на этом
    адресе.

    Из запроса берутся только поля формы (`ProjectVisionForm.vision`), а
    сервис кладёт их поверх прочитанного из БД `input_data` — состав
    команды, бюджет и `architecture` остаются на месте.
    """
    project = _get_accessible_project(request.user, project_id)
    if not user_can_edit_project_vision(request.user, project):
        raise PermissionDenied('Менять вводные проекта может только директор проекта.')

    form = ProjectVisionForm(request.POST)
    if not form.is_valid():
        messages.error(
            request,
            ' '.join(
                str(message)
                for errors in form.errors.values()
                for message in errors
            ),
        )
        return redirect('rooms:room_overview', project_id=project.id)

    update_project_vision(project, form.vision(), request.user)
    messages.success(request, 'Вводные проекта обновлены.')
    return redirect('rooms:room_overview', project_id=project.id)


def _configurator_response(request, project, *, error=None, notice=None):
    """HTMX → свежий partial конфигуратора, обычный POST → redirect с flash.

    Fallback без HTMX обязателен: таблица остаётся рабочей формой при
    выключенном JavaScript, как и кнопки подбора на вкладке «Команда».

    Ответ всегда строится по **свежему** состоянию из БД: проект
    перечитывается, потому что после отката транзакции (например, состав
    уменьшили ниже занятого слота) python-объект остался бы с несохранёнными
    значениями и partial показал бы состав, которого в базе нет.
    Ошибка операции отдаётся вместе с partial (статус 200) — тем же способом,
    что и `StaffingError` в `_slot_action_response`: ответ 4xx htmx по
    умолчанию не подставляет, и пользователь остался бы вообще без обратной
    связи. Нехватки прав это не касается: она поднимает `PermissionDenied`
    до начала работы и отдаётся существующим 403.
    """
    # `refresh_from_db` заодно сбрасывает кэш обратной связи `room`,
    # поэтому только что созданная проекцией комната и её слоты видны здесь,
    # а не подставляются из объекта, прочитанного до POST.
    project.refresh_from_db()
    room = getattr(project, 'room', None)
    context = configurator.build_configurator_context(
        request.user, project, room, error=error, notice=notice,
    )
    if request.headers.get('HX-Request'):
        return render(request, 'rooms/_unit_economics_table.html', context)
    if error:
        messages.error(request, error)
    elif notice:
        messages.success(request, notice)
    return redirect('rooms:room_overview', project_id=project.id)


def _project_for_configurator(request, project_id):
    """Проект для операции над составом: доступ к проекту + права на состав.

    Права проверяются и здесь, и в сервисе: view отдаёт 403 до начала работы,
    сервис гарантирует, что состав нельзя изменить в обход view. Статус
    проекта здесь **не** проверяется — «сейчас менять нельзя» это ошибка
    операции, а не отсутствие прав, и различает их сервис.
    """
    project = _get_accessible_project(request.user, project_id)
    if not user_can_edit_functional_roles(request.user, project):
        raise PermissionDenied('Состав команды меняет только директор проекта.')
    return project


@login_required
@require_POST
def room_functional_roles_update(request, project_id):
    """Одно изменение состава функциональных ролей: +1, −1 или точное число.

    Клиент присылает только `role_key` и намерение. Количество для «+»/«−»
    сервер досчитывает от сохранённого состава, а цену, часы, продуктивность
    и Hot берёт из своего каталога — экономика из запроса не принимается
    вообще (см. `update_project_functional_roles`).

    Состав и слоты комнаты меняются одной атомарной операцией
    `save_functional_roles_and_sync_slots`: логике проекции во view делать
    нечего, а «состав сохранился, слоты — нет» не должно существовать
    как состояние.
    """
    project = _project_for_configurator(request, project_id)
    try:
        counts = configurator.build_counts(
            project,
            request.POST.get('role_key', ''),
            request.POST.get('action', configurator.ACTION_SET),
            request.POST.get('count'),
        )
        result = save_functional_roles_and_sync_slots(project, counts, request.user)
    except FunctionalRolesError as exc:
        return _configurator_response(request, project, error=str(exc))
    notice = 'Состав команды обновлён.'
    if result.unfilled_opened_slots:
        notice = (
            f'{notice} Для {result.unfilled_opened_slots} слот(ов) не найден '
            'подходящий фрилансер — откройте вкладку «Команда» '
            'или расширьте каталог (видео + верификация).'
        )
    return _configurator_response(request, project, notice=notice)


@login_required
@require_POST
def room_functional_roles_apply_package(request, project_id):
    """Применение готового пакета: состав целиком заменяется серверным.

    В запросе только ключ пакета. Сам состав берётся из `apps.rooms.presets`,
    поэтому подменить количества или экономику пакета из браузера нельзя.

    Путь тот же самый, что и у ручного изменения: пакет доходит до
    `save_functional_roles_and_sync_slots`, поэтому слоты комнаты появляются
    и здесь, а не только у кнопок «+» / «−».
    """
    project = _project_for_configurator(request, project_id)
    try:
        result = apply_package_and_sync_slots(
            project, request.POST.get('package', ''), request.user
        )
    except KeyError:
        return _configurator_response(request, project, error='Неизвестный пакет.')
    except FunctionalRolesError as exc:
        return _configurator_response(request, project, error=str(exc))
    notice = 'Пакет применён к составу команды.'
    if result.unfilled_opened_slots:
        notice = (
            f'{notice} Для {result.unfilled_opened_slots} слот(ов) не найден '
            'подходящий фрилансер — проверьте каталог и вкладку «Команда».'
        )
    return _configurator_response(request, project, notice=notice)


@login_required
def room_documents(request, project_id):
    """Вкладка «Материалы»: файлы комнаты.

    Имя view и URL остаются documents — переименование ради подписи вкладки
    сломало бы reverse и существующие ссылки, ничего не дав пользователю.
    """
    project = _get_accessible_project(request.user, project_id)
    require_non_archived_room_member(request.user, project)
    room = ensure_room_for_project(project)
    documents = room.documents.select_related('uploaded_by').all()
    form = RoomDocumentForm()
    return render(request, 'rooms/room_documents.html', {
        'project': project,
        'room': room,
        'documents': documents,
        'material_groups': _material_groups(documents),
        'form': form,
        'can_upload': user_can_manage_team(request.user, project),
        'can_manage_team': user_can_manage_team(request.user, project),
        'active_tab': 'documents',
        **room_nav_context(request.user, project),
    })


@login_required
@require_safe
def room_document_file(request, project_id, document_id):
    """Отдаёт файл материала с кодировкой для текста.

    Прямой `/media/...` charset не ставит: браузер на русской Windows
    читает .txt как системную кодировку. PDF и картинки без charset.
    """
    project = _get_accessible_project(request.user, project_id)
    require_non_archived_room_member(request.user, project)
    room = ensure_room_for_project(project)
    doc = get_object_or_404(RoomDocument, id=document_id, room=room)
    if not doc.file:
        raise Http404
    return FileResponse(
        doc.file.open('rb'),
        as_attachment=False,
        filename=doc.file.name.rsplit('/', 1)[-1],
        content_type=_document_content_type(doc.file.name),
    )


@login_required
@require_POST
def room_document_upload(request, project_id):
    project = _get_accessible_project(request.user, project_id)
    if not user_can_manage_team(request.user, project):
        raise PermissionDenied('Загружать материалы может только тимлид проекта.')
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
            f'Материал «{doc.title}» загружен.',
            RoomActivity.EventType.DOCUMENT_UPLOADED,
            actor=request.user,
        )
        messages.success(request, 'Материал загружен.')
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
        raise PermissionDenied('Нельзя удалить этот материал.')
    doc.delete()
    messages.success(request, 'Материал удалён.')
    return redirect('rooms:room_documents', project_id=project.id)


@login_required
def room_comms(request, project_id):
    """Вкладка «Коммуникации»: два контура — директор↔тимлид и команда.

    Страница только читает состояние. Сообщения и опрос — отдельные endpoints
    по каналам (`team` / `director_teamlead`). Видео — внешние ссылки Jitsi
    без iframe/JWT (ADR-001, MVP).

    Верхнюю секцию видят только owner и тимлид этого проекта
    (`show_director_teamlead_comms` из `room_nav_context`).
    """
    project = _get_accessible_project(request.user, project_id)
    require_non_archived_room_member(request.user, project)
    room = getattr(project, 'room', None)
    if room is None:
        # Комнаты ещё нет (проект в черновике). Вкладка коммуникаций ничего
        # не создаёт, поэтому отправляем на карточку проекта, а не создаём
        # комнату побочным эффектом GET-запроса.
        return redirect('rooms:project_detail', project_id=project.id)

    nav = room_nav_context(request.user, project)
    show_dt = nav['show_director_teamlead_comms']
    chat_enabled = room.chat_enabled
    context = {
        'project': project,
        'room': room,
        'team_video_call_url': room_video_call_url(room),
        'team_chat_form': RoomChatMessageForm() if chat_enabled else None,
        'team_chat_messages': (
            chat.recent_chat_messages(room, channel=RoomChatMessage.Channel.TEAM)
            if chat_enabled
            else []
        ),
        'active_tab': 'comms',
        **nav,
    }
    if show_dt:
        context.update({
            'dt_video_call_url': director_teamlead_video_call_url(project),
            'dt_chat_form': RoomChatMessageForm() if chat_enabled else None,
            'dt_chat_messages': (
                chat.recent_chat_messages(
                    room, channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD
                )
                if chat_enabled
                else []
            ),
        })
    mark_comms_page_read(request.user, room, show_dt=show_dt)
    return render(request, 'rooms/room_comms.html', context)


@login_required
@require_safe
def chat_alerts(request):
    """HTMX-опрос колокольчика: бейдж разговоров + тосты новых сообщений."""
    viewing_project_id = (request.GET.get('viewing_project') or '').strip() or None
    viewing_comms = request.GET.get('viewing_comms') == '1'
    context = build_bell_context(
        request,
        emit_toasts=True,
        viewing_project_id=viewing_project_id,
        viewing_comms=viewing_comms,
    )
    if not context['show_chat_bell']:
        return HttpResponse('')
    return render(request, 'rooms/_header_chat_bell.html', context)


@login_required
@require_safe
def room_comms_teamlead(request, project_id):
    """Страница «Коммуникация с тимлидом»: только приватный видео+чат.

    Командный контур остаётся на вкладке `room_comms`. GET не создаёт
    комнату у черновика. Доступ — только owner или teamlead этого проекта.
    """
    project = _get_accessible_project(request.user, project_id)
    if not user_can_access_director_teamlead_comms(request.user, project):
        raise PermissionDenied(
            'Коммуникация с тимлидом доступна только владельцу и тимлиду проекта.'
        )
    room = getattr(project, 'room', None)
    if room is None:
        return redirect('rooms:project_detail', project_id=project.id)

    chat_enabled = room.chat_enabled
    mark_channel_read(
        request.user,
        room,
        RoomChatMessage.Channel.DIRECTOR_TEAMLEAD,
    )
    return render(request, 'rooms/room_comms_teamlead.html', {
        'project': project,
        'room': room,
        'dt_video_call_url': director_teamlead_video_call_url(project),
        'dt_chat_form': RoomChatMessageForm() if chat_enabled else None,
        'dt_chat_messages': (
            chat.recent_chat_messages(
                room, channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD
            )
            if chat_enabled
            else []
        ),
        # Отдельная страница: ни одна вкладка не подсвечивается как «текущая».
        'active_tab': '',
        **room_nav_context(request.user, project),
    })


def _get_chat_room(request, project_id):
    """Комната для chat endpoints: доступ по RBAC ROOM + включённый чат.

    Правила ролей не переписываются: доступ решает тот же
    `_get_accessible_project` / `user_can_access_project`, что и остальные
    вкладки. Поэтому удалённый из комнаты фрилансер теряет и чат — отдельного
    списка «кому можно писать» не существует.

    `chat_enabled=False` — это 403 на обоих endpoints, а не просто скрытый
    блок в шаблоне: иначе выключение чата не защищало бы от прямого запроса.
    """
    project = _get_accessible_project(request.user, project_id)
    room = getattr(project, 'room', None)
    if room is None:
        raise Http404('Комната проекта ещё не создана.')
    if not room.chat_enabled:
        raise PermissionDenied('Чат этой комнаты выключен.')
    return project, room


def _chat_partial(request, project, room, *, channel, error=None):
    """Свежий список сообщений канала — общий ответ для опроса и отправки."""
    return render(request, 'rooms/_chat_messages.html', {
        'project': project,
        'chat_messages': chat.recent_chat_messages(room, channel=channel),
        'chat_error': error,
        'chat_is_director_teamlead': (
            channel == RoomChatMessage.Channel.DIRECTOR_TEAMLEAD
        ),
    })


@login_required
@require_safe
def room_chat_messages(request, project_id):
    """Лента командного чата для HTMX-опроса.

    `require_safe` — не украшение: этот адрес вызывается каждые несколько
    секунд, и он обязан оставаться строго read-only. Ни одной записи в БД
    здесь нет, комната по пути тоже не создаётся.
    """
    project, room = _get_chat_room(request, project_id)
    # Только архивный гейт: при открытом кейсе ленту читать можно — человек
    # ещё в команде и заходит в комнату. Запись закрыта отдельно, в send.
    require_non_archived_room_member(request.user, project)
    return _chat_partial(
        request, project, room, channel=RoomChatMessage.Channel.TEAM
    )


@login_required
@require_POST
def room_chat_send(request, project_id):
    """Отправка сообщения в командный чат комнаты.

    Доступ проверяется здесь заново, на сервере: HTMX-запрос ничем не
    привилегированнее прямого POST, поэтому чужая комната отсекается до
    валидации формы.

    Ответ зависит от клиента: HTMX получает обновлённый список сообщений,
    обычная форма — redirect обратно на вкладку с flash-сообщением. Без
    JavaScript чат остаётся рабочим.
    """
    project, room = _get_chat_room(request, project_id)
    # Запись в командный чат — работа в проекте. Чтение при открытом кейсе
    # остаётся: человек заходит в комнату и видит уведомление о расторжении,
    # поэтому гейт стоит здесь, а не в общем `_get_chat_room`.
    require_can_work_in_room(request.user, project)
    form = RoomChatMessageForm(request.POST)
    error = None
    if form.is_valid():
        chat.post_chat_message(
            room,
            request.user,
            form.cleaned_data['text'],
            channel=RoomChatMessage.Channel.TEAM,
        )
    else:
        error = ' '.join(
            str(message) for errors in form.errors.values() for message in errors
        )

    if request.headers.get('HX-Request'):
        return _chat_partial(
            request,
            project,
            room,
            channel=RoomChatMessage.Channel.TEAM,
            error=error,
        )

    if error:
        messages.error(request, error)
    return redirect('rooms:room_comms', project_id=project.id)


def _get_dt_chat_room(request, project_id):
    """Комната для приватного канала директор↔тимлид + включённый чат."""
    project, room = _get_chat_room(request, project_id)
    if not user_can_access_director_teamlead_comms(request.user, project):
        raise PermissionDenied(
            'Приватный чат доступен только владельцу и тимлиду проекта.'
        )
    return project, room


@login_required
@require_safe
def room_dt_chat_messages(request, project_id):
    """Лента приватного чата директор↔тимлид для HTMX-опроса."""
    project, room = _get_dt_chat_room(request, project_id)
    return _chat_partial(
        request,
        project,
        room,
        channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD,
    )


@login_required
@require_POST
def room_dt_chat_send(request, project_id):
    """Отправка в приватный канал директор↔тимлид."""
    project, room = _get_dt_chat_room(request, project_id)
    form = RoomChatMessageForm(request.POST)
    error = None
    if form.is_valid():
        chat.post_chat_message(
            room,
            request.user,
            form.cleaned_data['text'],
            channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD,
        )
    else:
        error = ' '.join(
            str(message) for errors in form.errors.values() for message in errors
        )

    if request.headers.get('HX-Request'):
        return _chat_partial(
            request,
            project,
            room,
            channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD,
            error=error,
        )

    if error:
        messages.error(request, error)
    return redirect('rooms:room_comms_teamlead', project_id=project.id)


@login_required
def room_team(request, project_id):
    """Состав команды комнаты + invite тимлида.

    Вкладка «Команда» — зона тимлида (`user_can_view_team_tab`). Директор
    смотрит состав на «Обзоре»; прямой GET уводим туда же, а не в 403.
    """
    project = _get_accessible_project(request.user, project_id)
    require_non_archived_room_member(request.user, project)
    if not user_can_view_team_tab(request.user, project):
        # Владелец уходит на «Обзор»; остальные роли получают отказ —
        # иначе скрытая вкладка превратилась бы в тихий редирект.
        if project.owner_id == request.user.id:
            messages.info(
                request,
                'Управление командой доступно тимлиду. На Обзоре — состав и метрики.',
            )
            return redirect('rooms:room_overview', project_id=project.id)
        raise PermissionDenied('Вкладка «Команда» доступна только тимлиду проекта.')
    room = ensure_room_for_project(project)
    members = _members_with_termination(room)
    can_manage = user_can_manage_team(request.user, project)
    # `members` — уже готовый список, поэтому своя строка ищется в памяти:
    # второй запрос за тем, что только что выбрано, не нужен.
    my_membership = next(
        (item for item in members if item.user_id == request.user.id), None,
    )
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

    cards = selectors.slot_cards(room)

    return render(request, 'rooms/room_team.html', {
        'project': project,
        'room': room,
        'members': members,
        'slot_cards': cards,
        'staffing_summary': selectors.staffing_summary(cards),
        **configurator.build_planned_team_context(project, room),
        'can_staff_slots': can_manage and _staffing_is_open(project),
        'can_fill_slots': can_manage and _slot_filling_is_open(project),
        'can_manage_team': can_manage,
        'can_view_composition_staffing': user_can_view_composition_staffing(
            request.user
        ),
        'my_membership': my_membership,
        'teamlead_form': None,
        'freelancer_form': AddFreelancerForm(room=room) if can_manage else None,
        'invite_url': invite_url,
        'active_tab': 'team',
        **room_nav_context(request.user, project),
    })


@login_required
@require_POST
def room_create_teamlead_invite(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    if not user_can_appoint_teamlead(request.user, project):
        raise PermissionDenied('Создавать приглашение может только директор проекта.')
    ensure_room_for_project(project)
    invite = create_teamlead_invite(project, request.user)
    url = absolute_uri(
        request,
        reverse('rooms:teamlead_invite_accept', kwargs={'token': invite.token}),
    )
    messages.success(request, f'Ссылка-приглашение для тимлида создана: {url}')
    return redirect('rooms:room_overview', project_id=project.id)


@login_required
@require_POST
def room_assign_teamlead(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    if not user_can_appoint_teamlead(request.user, project):
        raise PermissionDenied('Назначать тимлида может только директор проекта.')

    form = AssignTeamleadForm(request.POST)
    if form.is_valid():
        assign_teamlead(project, form.cleaned_data['teamlead'], actor=request.user)
        messages.success(request, 'Тимлид назначен.')
    else:
        messages.error(request, 'Не удалось назначить тимлида. Есть активные тимлиды?')
    return redirect('rooms:room_overview', project_id=project.id)


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


def _get_termination_thread(
    request, project_id, case_id, *, allow_completed=False,
):
    """Кейс расторжения для обоих endpoint'ов его чата.

    Правило доступа — **участие в кейсе**, а не роль в комнате: тред читают
    и пишут только фрилансер и тимлид, отправивший уведомление. Директор,
    другой фрилансер, менеджер, платформенный admin и даже текущий тимлид,
    если инициатором был не он, сюда не попадают. Поэтому здесь нет ни
    `user_can_manage_team`, ни правил командного чата.

    `room__project_id` в запросе обязателен: подменив `project_id` в адресе,
    чужой кейс достать нельзя.

    `room.chat_enabled` не проверяется намеренно — это настройка командного
    чата комнаты, а переписка о расторжении обязательна и выключаться ею не
    может.

    Чтение и запись расходятся ровно в одном месте — `allow_completed`.
    Завершённый кейс новых сообщений не принимает (`post_termination_message`
    и без того откажет), но последняя системная строка треда — «Фрилансер
    покинул проект» — пишется **до** перехода в `completed` именно для того,
    чтобы её увидел тимлид. Отдав на опрос 404, интерфейс спрятал бы её
    навсегда: у тимлида открыта форма расторжения, и следующий HTMX-опрос
    обязан принести финал, а не «страницы нет». Поэтому GET ленты пускает и
    `completed`, а POST отправки — нет.

    `revoked` остаётся закрытым для обоих: там уведомление снято, человек
    работает дальше, и читать в этом треде нечего.

    Посторонний — `PermissionDenied` в любом статусе: круг участников
    завершение кейса не расширяет.
    """
    case = get_object_or_404(
        FreelancerTermination.objects.select_related(
            'room__project', 'freelancer', 'initiated_by',
        ),
        id=case_id,
        room__project_id=project_id,
    )
    # Лента опрашивается каждые 7 секунд, поэтому именно она закрывает срок
    # у человека, который держит вкладку открытой до дедлайна. Хелпер сам
    # отбирает только просроченный `notice_sent`, поэтому вызов безусловный
    # и `appeal_pending` не задевает.
    if finalize_expired_termination_for(case.room, case.freelancer):
        case.refresh_from_db()
    is_readable = case.is_open or (
        allow_completed
        and case.status == FreelancerTermination.Status.COMPLETED
    )
    if not is_readable:
        raise Http404('Расторжение закрыто: переписка по нему недоступна.')

    participants = {case.freelancer_id}
    if case.initiated_by_id:
        participants.add(case.initiated_by_id)
    if request.user.id not in participants:
        raise PermissionDenied(
            'Чат расторжения доступен только фрилансеру и тимлиду, '
            'отправившему уведомление.'
        )
    return case


def _termination_messages_partial(request, case, *, error=None, status=200):
    """Свежая лента треда — общий ответ для опроса и для отправки.

    Тот же partial отдаётся и форме тимлида, и будущей модалке фрилансера:
    ни имя шаблона, ни контекст к странице тимлида не привязаны.
    """
    return render(request, 'rooms/_termination_messages.html', {
        'termination_case': case,
        'termination_messages': recent_termination_messages(case),
        'termination_chat_error': error,
    }, status=status)


@login_required
@require_safe
def room_termination_messages(request, project_id, case_id):
    """Лента чата расторжения для HTMX-опроса.

    `require_safe` — не украшение: адрес опрашивается каждые несколько
    секунд и обязан оставаться строго read-only. Ровно поэтому здесь и
    можно пустить `completed` (`allow_completed=True`): опрос ничего не
    меняет, а участникам нужен финал переписки — системная строка ухода,
    записанная перед закрытием кейса. Отправка сообщений остаётся закрытой:
    у неё этого флага нет.
    """
    case = _get_termination_thread(
        request, project_id, case_id, allow_completed=True,
    )
    return _termination_messages_partial(request, case)


@login_required
@require_POST
def room_termination_send(request, project_id, case_id):
    """Отправка сообщения в чат расторжения.

    Проверки участия, статуса кейса и текста живут в домене
    (`post_termination_message`) и здесь не дублируются. `TerminationError`
    (пустой текст, перебор длины, закрытый кейс) — это 400 с той же лентой и
    текстом ошибки; `PermissionDenied` намеренно не перехватывается и
    остаётся 403.

    `allow_completed` здесь не передаётся сознательно: читать финал
    завершённого кейса можно, дописывать в него — нет, и завершение обратно
    не открывается.
    """
    case = _get_termination_thread(request, project_id, case_id)
    try:
        post_termination_message(
            case, author=request.user, text=request.POST.get('text', ''),
        )
    except TerminationError as exc:
        return _termination_messages_partial(
            request, case, error=str(exc), status=400,
        )
    return _termination_messages_partial(request, case)


def _get_termination_case(request, project_id, case_id):
    """Кейс расторжения по адресу проекта — общий вход трёх действий.

    Тред-хелпер (`_get_termination_thread`) здесь не годится: у отзыва другой
    круг прав (текущий тимлид проекта, а не участник переписки), и правило
    «закрытый кейс — `Http404`» для действий неверно. Кейс существует и виден
    вызывающему; недопустим именно переход, а это 400, а не «страницы нет».

    `room__project_id` в фильтре обязателен: подменив проект в адресе, чужой
    кейс достать нельзя.

    Состояние здесь **не** проверяется намеренно: у каждого действия свой
    набор допустимых статусов, и, спрятав их в общий запрос, мы отвечали бы
    «нет такого кейса» там, где на самом деле «нельзя в этом статусе».
    """
    return get_object_or_404(
        FreelancerTermination.objects.select_related(
            'room__project', 'freelancer', 'member',
        ),
        id=case_id,
        room__project_id=project_id,
    )


def _termination_action_conflict(message):
    """Общий ответ «действие не подходит текущему статусу» для трёх действий.

    400 у всех трёх и у всех промахов автомата — и у `appeal_pending`, и у
    терминальных статусов. Разные коды на разные статусы заставляли бы
    клиента угадывать состояние кейса по коду ответа, а состояние он и так
    видит на странице.
    """
    return HttpResponseBadRequest(message)


def _close_expired_termination(case):
    """Закрывает просроченный `notice_sent` перед самим действием.

    Тот же ленивый срок, что на страницах комнаты и в опросе чата. Без него
    прямой POST мимо интерфейса открывал бы протест и отзыв уже после
    дедлайна — тогда как во всём остальном приложении кейс к этому моменту
    считается завершённым, а человек — вне проекта.

    Вызывается **после** проверки прав: посторонний POST не должен менять
    состояние, даже детерминированно.

    `appeal_pending` хелпер не трогает: там таймер остановлен статусом.
    """
    if finalize_expired_termination_for(case.room, case.freelancer):
        case.refresh_from_db()
    return case


def _require_case_freelancer(request, case):
    """Действия модалки выполняет только сам фрилансер этого кейса.

    Ни тимлид, ни директор, ни платформенный admin: «Покинуть проект» и
    «Опротестовать решение» — ответ на уведомление, и ответить за человека
    не может никто.
    """
    if request.user.id != case.freelancer_id:
        raise PermissionDenied(
            'Ответить на уведомление о расторжении может только фрилансер, '
            'которого оно касается.'
        )


@login_required
@require_POST
def room_termination_leave(request, project_id, case_id):
    """«Покинуть проект»: фрилансер соглашается с расторжением.

    Разрешено только из `notice_sent`. Из `appeal_pending` — сознательно
    нет: протест уже ушёл в поддержку письмом, исход определяет она, и
    добровольный выход закрыл бы кейс, по которому идёт разбирательство.
    Отказ здесь — 400, а не тихое завершение: домен переход
    `appeal_pending → completed` разрешает (им поддержка оставит расторжение
    в силе), поэтому статус проверяется до вызова, а не вместо него.

    Членство архивируется, а не удаляется — этим занимается
    `complete_termination`, и здесь его работа не дублируется. Ему же
    передаётся системная строка ухода: тред закрывается вместе с кейсом,
    поэтому запись обязана появиться до перехода, а не после него.

    Редирект в список проектов: комната для ушедшего осталась read-only
    «Обзором», и возвращать его туда сразу после выхода незачем.
    """
    case = _get_termination_case(request, project_id, case_id)
    _require_case_freelancer(request, case)
    _close_expired_termination(case)

    if case.status != FreelancerTermination.Status.NOTICE_SENT:
        return _termination_action_conflict(
            'Покинуть проект можно только по действующему уведомлению '
            'о расторжении.'
        )

    try:
        complete_termination(
            case,
            actor=request.user,
            system_message=SYSTEM_FREELANCER_LEFT_TEXT,
        )
    except TerminationError as exc:
        return _termination_action_conflict(str(exc))

    messages.success(
        request,
        f'Вы покинули проект «{case.room.project.name}». '
        'История работы по проекту сохранена.',
    )
    return redirect('rooms:project_list')


def _termination_modal_response(request, case, *, appeal_form, status=400):
    """Страница комнаты, свёрнутая до блокирующей модалки, с ошибками формы.

    Невалидный протест не может быть редиректом: 302 потерял бы ошибки
    поля, и фрилансер увидел бы ту же пустую форму без объяснения. Поэтому
    ответ — обычный render той же модалки с той же связанной формой, только
    со статусом 400.

    Отдельный тонкий шаблон, а не «Обзор» комнаты: за оверлеем всё равно не
    работает ни одна вкладка, а собирать ради ошибки поля метрики, канбан и
    состав команды не за чем. Шапка комнаты и глобальная навигация
    `base.html` при этом на месте — уйти из комнаты можно.
    """
    project = case.room.project
    return render(request, 'rooms/room_termination_notice.html', {
        'project': project,
        'room': case.room,
        'active_tab': 'overview',
        **room_nav_context(request.user, project),
        # После словаря навигации: он кладёт пустую форму, а здесь нужна
        # связанная — с введённым текстом и ошибкой.
        'termination_appeal_form': appeal_form,
    }, status=status)


@login_required
@require_POST
def room_termination_appeal(request, project_id, case_id):
    """«Опротестовать решение»: письмо в поддержку и остановка срока.

    Протест — не пустая кнопка: без текста разбирать поддержке нечего,
    поэтому POST проходит через `TerminationAppealForm`. Невалидная форма —
    400 с той же модалкой и ошибкой поля (`_termination_modal_response`);
    кейс при этом остаётся `notice_sent`, письма нет, и в треде не
    появляется ни одной ложной строки.

    Ссылка на кейс в админке собирается здесь, а не в домене: у домена нет
    `request`, а поддержке нужен абсолютный адрес. `absolute_uri` — тот же
    хелпер, что у приглашения тимлида и активации: на демо и проде хост
    берётся из `PUBLIC_HOST`, а не из заголовка запроса.

    Текст письма, системные строки треда, порядок «сообщения → письмо →
    статус» и откат при сбое отправки живут в `appeal_termination` и здесь
    не повторяются. Исключение отправки намеренно не проглатывается: тихий
    `except` превратил бы неушедшее письмо в статус «ждём ответа
    поддержки», о котором поддержка не узнает.

    Редирект на «Обзор» комнаты: там же, где фрилансер нажал кнопку, он
    сразу видит модалку с новым статусом. Внешний `next` не принимается —
    открытый редирект на POST-действии не нужен.
    """
    case = _get_termination_case(request, project_id, case_id)
    _require_case_freelancer(request, case)
    _close_expired_termination(case)

    if case.status != FreelancerTermination.Status.NOTICE_SENT:
        return _termination_action_conflict(
            'Опротестовать можно только действующее уведомление '
            'о расторжении.'
        )

    form = TerminationAppealForm(request.POST)
    if not form.is_valid():
        return _termination_modal_response(request, case, appeal_form=form)

    admin_url = absolute_uri(
        request,
        reverse('admin:rooms_freelancertermination_change', args=[case.pk]),
    )
    try:
        appeal_termination(
            case,
            admin_url=admin_url,
            appeal_reason=form.cleaned_data['appeal_reason'],
        )
    except TerminationError as exc:
        return _termination_action_conflict(str(exc))

    messages.success(
        request,
        'Решение оспорено: поддержка получила обращение. '
        'Работать в проекте нельзя, пока решение не принято.',
    )
    return redirect('rooms:room_overview', project_id=case.room.project_id)


@login_required
@require_POST
def room_termination_revoke(request, project_id, case_id):
    """«Отозвать увольнение»: тимлид снимает уведомление о расторжении.

    Право уже, чем `user_can_manage_team`: только **текущий** тимлид
    проекта. Ни директор, ни платформенный admin, ни прежний тимлид,
    отправивший уведомление, но уже снятый с проекта: за состав команды
    отвечает тот, кто ведёт её сейчас.

    Разрешены оба открытых статуса, `appeal_pending` в том числе: тимлид
    вправе передумать и до решения поддержки — это снимает вопрос, а не
    решает его в свою пользу.

    После `completed` отзыв запрещён: членство уже архивировано, и «отозвать»
    означало бы вернуть человека в команду. Повторный найм — отдельный
    сценарий, а не побочный эффект этой кнопки.

    Редирект в состав команды, а не обратно на страницу расторжения:
    открытого кейса больше нет, и та страница снова предложила бы форму
    нового уведомления — ровно то, чего тимлид только что не захотел.
    """
    case = _get_termination_case(request, project_id, case_id)
    project = case.room.project
    if project.teamlead_id != request.user.id:
        raise PermissionDenied(
            'Отозвать расторжение может только текущий тимлид проекта.'
        )
    _close_expired_termination(case)

    if not case.is_open:
        return _termination_action_conflict(
            'Расторжение уже закрыто: отзывать нечего.'
        )

    try:
        revoke_termination(case)
    except TerminationError as exc:
        return _termination_action_conflict(str(exc))

    messages.success(
        request,
        f'Расторжение с {case.freelancer.full_name} отозвано: '
        'человек остаётся в команде и снова может работать.',
    )
    return redirect('rooms:room_team', project_id=project.id)


def _get_freelancer_member_for_termination(request, project_id, member_id):
    """Общий вход страницы расторжения: права, комната, участник.

    Право здесь **уже**, чем `user_can_manage_team`: страницу открывает
    именно текущий тимлид проекта. Общий хелпер пускает и платформенного
    `ADMIN`, а расторжение оформляет тот, кто отвечает за команду, — то же
    правило проверяет домен в `initiate_termination`, и интерфейс не должен
    показывать форму тому, кому сервис откажет.

    Правило сужено только для этой страницы: удаление тимлида и запрет на
    удаление директора в `room_remove_member` работают как раньше.

    Не-фрилансер здесь `Http404`, а не 403: страницы расторжения для тимлида
    и директора не существует в принципе, и сообщать о её отсутствии
    отдельным кодом не за чем.

    Архивный участник — тоже `Http404`: человек уже вышел из проекта, и
    расторгать с ним больше нечего. Без этой проверки прямая ссылка на
    страницу снова предлагала бы форму новой причины по тому, кого нет в
    команде. История его кейсов при этом сохраняется — 404 закрывает вход
    в UI нового расторжения, а не удаляет данные.
    """
    project = _get_accessible_project(request.user, project_id)
    if project.teamlead_id != request.user.id:
        raise PermissionDenied(
            'Расторжение с фрилансером оформляет тимлид проекта.'
        )
    room = ensure_room_for_project(project)
    member = get_object_or_404(
        RoomMember.objects.select_related('user'), id=member_id, room=room,
    )
    if member.role_in_room != RoomMember.RoleInRoom.FREELANCER:
        raise Http404('Расторжение оформляется только с фрилансером.')
    if not member.is_active:
        raise Http404('Участник уже вышел из проекта: расторгать нечего.')
    return project, room, member


@login_required
@require_safe
def room_termination_form(request, project_id, member_id):
    """Форма причины расторжения либо карточка уже открытого кейса.

    Второй кейс отсюда не создаётся: если по паре комната+фрилансер уже есть
    незакрытое расторжение, страница показывает его — причину, сроки и
    статус, — а формы новой причины не выводит.
    """
    project, room, member = _get_freelancer_member_for_termination(
        request, project_id, member_id,
    )
    case = open_termination_for(room, member.user)
    return _termination_form_response(
        request,
        project,
        room,
        member,
        form=None if case else TerminationNoticeForm(),
        case=case,
    )


def _start_freelancer_termination(request, project, room, member):
    """POST «Удалить» для фрилансера: уведомление о расторжении, не delete.

    Пустая или слишком короткая причина — 400 с той же формой и её ошибками,
    а не молчаливое удаление участника: именно этот путь и заменяет прежний
    `confirm()` + `member.delete()`.
    """
    form = TerminationNoticeForm(request.POST)
    if not form.is_valid():
        return _termination_form_response(
            request, project, room, member, form=form, status=400,
        )

    try:
        initiate_termination(
            room=room,
            member=member,
            initiated_by=request.user,
            reason=form.cleaned_data['reason'],
        )
    except TerminationAlreadyOpen:
        # Не ошибка интерфейса: тимлид просто открыл форму дважды.
        # Второй кейс не создаётся, показываем существующий.
        messages.info(request, 'По этому фрилансеру уже идёт расторжение.')
    except TerminationError as exc:
        messages.error(request, str(exc))
        return redirect('rooms:room_team', project_id=project.id)
    else:
        messages.success(
            request, 'Уведомление о расторжении отправлено фрилансеру.',
        )

    return redirect(
        'rooms:room_termination_form',
        project_id=project.id,
        member_id=member.id,
    )


@login_required
@require_POST
def room_remove_member(request, project_id, member_id):
    """Удаление участника из команды.

    Для фрилансера это больше не удаление: путь ведёт в расторжение с
    письменной причиной, а строка членства и вся его история сохраняются
    (см. `apps.rooms.termination`). Для тимлида поведение прежнее.
    """
    project = get_object_or_404(Project, id=project_id)
    if not user_can_manage_team(request.user, project):
        raise PermissionDenied
    room = ensure_room_for_project(project)
    member = get_object_or_404(RoomMember, id=member_id, room=room)
    if member.role_in_room == RoomMember.RoleInRoom.DIRECTOR:
        messages.error(request, 'Нельзя удалить директора из комнаты.')
        return redirect('rooms:room_team', project_id=project.id)

    if member.role_in_room == RoomMember.RoleInRoom.FREELANCER:
        return _start_freelancer_termination(request, project, room, member)

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
    """Фрилансер подтверждает готовность к работе.

    Вся логика (готовность участника + пересчёт активации проекта) живёт в
    `staffing.services.confirm_freelancer_readiness`; view только показывает
    результат.
    """
    project = _get_accessible_project(request.user, project_id)
    room = ensure_room_for_project(project)
    member = get_object_or_404(
        RoomMember.objects.select_related('room__project', 'user'),
        room=room,
        user=request.user,
    )
    try:
        confirm_freelancer_readiness(member, request.user)
    except StaffingError as exc:
        messages.error(request, str(exc))
        return redirect('rooms:room_overview', project_id=project.id)

    messages.success(request, 'Статус: готов к работе.')
    return redirect('rooms:room_overview', project_id=project.id)


@login_required
@require_POST
def room_slot_auto_assign(request, project_id, slot_id):
    """«Подобрать лучшего»: auto top-1 на пустой функциональный слот."""
    project, _room, slot = _get_slot_for_staffing(request, project_id, slot_id)
    try:
        outcome = auto_assign_best_candidate(slot, request.user)
    except StaffingError as exc:
        return _slot_action_response(request, project, slot, str(exc), is_error=True)
    return _slot_action_response(
        request,
        project,
        slot,
        outcome.message,
        is_error=not outcome.assigned,
    )


@login_required
@require_POST
def room_slot_replace(request, project_id, slot_id):
    """«Другой сейлер»: следующий по ranking кандидат вместо текущего."""
    project, _room, slot = _get_slot_for_staffing(request, project_id, slot_id)
    try:
        outcome = replace_slot_member(slot, request.user)
    except StaffingError as exc:
        return _slot_action_response(request, project, slot, str(exc), is_error=True)
    return _slot_action_response(
        request,
        project,
        slot,
        outcome.message,
        is_error=not outcome.assigned,
    )


@login_required
def room_slot_candidates(request, project_id, slot_id):
    """«Выбрать из пула»: ROOM-страница подходящих кандидатов слота.

    Пул берётся из Matching Engine с теми же исключениями, что и «следующий
    кандидат»: уже рассмотренные по ЭТОМУ слоту не показываются, история
    других слотов на выборку не влияет. Страница ничего не пишет в БД —
    просмотр не проставляет `shown` кандидатам.

    Публичный каталог `/freelancers/` не при чём: это отдельная страница
    комнаты со своими правами (директор и тимлид).
    """
    project, _room, slot = _get_slot_for_staffing(request, project_id, slot_id)
    candidates = matching.get_ranked_candidates(slot, exclude_seen=True)
    page = Paginator(candidates, CANDIDATE_POOL_PAGE_SIZE).get_page(
        request.GET.get('page'),
    )
    return render(request, 'rooms/room_slot_candidates.html', {
        'project': project,
        'card': selectors.slot_card_for(slot),
        'slot': slot,
        'page_obj': page,
        'active_tab': 'team',
        **room_nav_context(request.user, project),
    })


@login_required
@require_POST
def room_slot_assign_candidate(request, project_id, slot_id, candidate_id):
    """Ручное назначение кандидата из пула.

    Eligibility перепроверяется сервером внутри сервиса: то, что кандидат
    подходил на момент GET страницы пула, ничего не гарантирует.
    """
    project, _room, slot = _get_slot_for_staffing(request, project_id, slot_id)
    candidate = get_object_or_404(User, id=candidate_id, role=User.Roles.FREELANCER)
    try:
        member = assign_candidate_to_slot(slot, candidate, request.user)
    except StaffingError as exc:
        messages.error(request, str(exc))
        return redirect(
            'rooms:room_slot_candidates',
            project_id=project.id,
            slot_id=slot.id,
        )

    messages.success(request, f'{member.user.full_name} назначен на слот.')
    return redirect('rooms:room_team', project_id=project.id)
