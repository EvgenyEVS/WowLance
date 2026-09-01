"""Отчёт тимлида за период: агрегация уже существующих данных ROOM.

Только чтение. Сервис не создаёт задач, не вызывает `ensure_start_calls_task`,
не трогает лиды, отчёты, участников, кандидатов и ленту комнаты — исключительно
SELECT. Новых моделей под отчёт нет и не требуется: все цифры собираются из
`Task`, `Report`, `Lead`, `RoomMember` и `RoomSlotCandidate`.

Модуль живёт в `pipeline`, потому что агрегирует преимущественно
pipeline-сущности и обращается к `rooms` за проектом и составом команды —
это разрешённое ADR-001 направление `pipeline -> rooms`. Обратной зависимости
(`rooms -> pipeline`) он не добавляет.

Границы периода — календарные дни в текущей таймзоне Django (см.
`period_bounds`), фильтры полуоткрытые: `__gte=start, __lt=end_exclusive`.
"""

from datetime import datetime, time, timedelta

from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.utils import timezone

from apps.rooms.models import Project, RoomMember, RoomSlotCandidate
from apps.users.models import User

from .models import Lead, Report, Task
from .services import get_start_calls_task

#: Сколько календарных дней покрывает период по умолчанию, включая сегодня.
DEFAULT_PERIOD_DAYS = 7

# ---------------------------------------------------------------------------
# Статусы строки SLA. Строки, а не enum модели: это вычисляемая характеристика
# отчёта, в БД она не хранится и миграции за собой не тянет.
# ---------------------------------------------------------------------------

#: Стартовой задачи «Начать звонки» у проекта нет (проект ещё не активирован).
SLA_NO_START_TASK = 'no_start_task'
#: Закрыта в срок либо закрыта при отсутствующем дедлайне.
SLA_ON_TIME = 'on_time'
#: Закрыта позже дедлайна либо ещё не закрыта, а дедлайн уже прошёл.
SLA_OVERDUE = 'overdue'
#: Закрыта, дедлайн известен, а времени закрытия нет: задача закрывалась до
#: появления `Task.closed_at`. Честный отдельный статус вместо догадки.
SLA_CLOSED_TIME_UNKNOWN = 'closed_time_unknown'
#: Не закрыта, дедлайн ещё не наступил (или его нет).
SLA_IN_PROGRESS = 'in_progress'


def default_report_period() -> tuple:
    """Период по умолчанию: сегодня и шесть предыдущих дней.

    Возвращает `(date_from, date_to)` — два `datetime.date`, ровно
    `DEFAULT_PERIOD_DAYS` календарных дней включительно.

    «Сегодня» берётся как `timezone.localdate()`, а не `timezone.now().date()`:
    второе вернуло бы дату по UTC и в ночные часы сдвинуло бы период на сутки
    назад относительно того, что видит пользователь.
    """
    date_to = timezone.localdate()
    date_from = date_to - timedelta(days=DEFAULT_PERIOD_DAYS - 1)
    return date_from, date_to


def period_bounds(date_from, date_to) -> tuple:
    """Две календарные даты -> полуоткрытый aware-диапазон `[start, end)`.

    `start` — `date_from` 00:00:00 в текущей таймзоне Django;
    `end_exclusive` — 00:00:00 **следующего** после `date_to` дня.

    Диапазон полуоткрытый намеренно. Вариант «до 23:59:59» терял бы всё, что
    произошло в последнюю секунду суток: `DateTimeField` хранит микросекунды,
    и `auto_now_add` их проставляет. `[start, end_exclusive)` покрывает день
    `date_to` целиком без исключений.

    Таймзона берётся из `timezone.get_current_timezone()`, а не прописывается
    константой: сервис обязан следовать настройке `TIME_ZONE` проекта.

    :raises ValueError: если `date_from > date_to`. Молча менять даты местами
        сервис не имеет права — пользователю нужна ошибка формы, а не тихо
        подменённый период (форму делает следующий этап).
    """
    if date_from > date_to:
        raise ValueError('Дата «с» не может быть позже даты «по».')
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    end_exclusive = timezone.make_aware(
        datetime.combine(date_to + timedelta(days=1), time.min), tz
    )
    return start, end_exclusive


def _projects_for_report(user, project):
    """Проекты, попадающие в отчёт, с проверкой прав тимлида.

    Право на отчёт даёт только роль TEAMLEAD и только на свои проекты.
    Платформенный ADMIN сюда сознательно не допускается: отчёт — рабочее место
    конкретного тимлида, а не инструмент администрирования.
    """
    if not getattr(user, 'is_authenticated', False):
        raise PermissionDenied('Требуется вход.')
    if user.role != User.Roles.TEAMLEAD:
        raise PermissionDenied('Отчёт за период доступен только тимлиду.')

    if project is not None:
        if project.teamlead_id != user.id:
            raise PermissionDenied('Это не ваш проект.')
        return [project]

    # Без фильтра по статусу: завершённый или архивный проект из отчёта за
    # прошедший период выпадать не должен.
    return list(Project.objects.filter(teamlead=user).order_by('created_at'))


def _task_counts(projects, start, end_exclusive, now) -> dict:
    """Задачи, созданные в периоде. `manager_handoff` исключены.

    Задачи передачи горячего лида менеджеру — служебные, они создаются
    автоматикой на менеджера платформы и к исполнению команды тимлида
    отношения не имеют.

    `approved_not_closed` — отдельный счётчик, а не часть `closed`: статус
    APPROVED означает «отчёт утверждён, задача ещё не закрыта», и молча
    терять эти задачи в отчёте нельзя.

    `overdue` считается только среди задач этого же периода: дедлайн заполнен,
    уже прошёл, а задача не закрыта. Это «просрочена на момент формирования
    отчёта» — исторической просрочки по закрытым задачам здесь нет.
    """
    period_tasks = (
        Task.objects.filter(
            project__in=projects,
            created_at__gte=start,
            created_at__lt=end_exclusive,
        )
        .exclude(task_type=Task.TaskType.MANAGER_HANDOFF)
    )
    return period_tasks.aggregate(
        created=Count('id'),
        in_progress=Count('id', filter=Q(status=Task.Status.IN_PROGRESS)),
        ready_for_review=Count('id', filter=Q(status=Task.Status.READY_FOR_REVIEW)),
        approved_not_closed=Count('id', filter=Q(status=Task.Status.APPROVED)),
        closed=Count('id', filter=Q(status=Task.Status.CLOSED)),
        overdue=Count(
            'id',
            filter=Q(deadline__isnull=False)
            & Q(deadline__lt=now)
            & ~Q(status=Task.Status.CLOSED),
        ),
    )


def _report_counts(projects, start, end_exclusive) -> dict:
    """Отчёты, сданные в периоде, в разрезе текущего статуса проверки.

    Период считается по `Report.created_at` — моменту сдачи отчёта. Не по
    `reviewed_at`: отчёт относится к тому периоду, когда исполнитель его сдал,
    даже если тимлид проверил его позже. У `Report` нет прямой связи с
    проектом, поэтому фильтр идёт через `task__project`.
    """
    return Report.objects.filter(
        task__project__in=projects,
        created_at__gte=start,
        created_at__lt=end_exclusive,
    ).aggregate(
        submitted=Count('id', filter=Q(review_status=Report.ReviewStatus.PENDING)),
        approved=Count('id', filter=Q(review_status=Report.ReviewStatus.APPROVED)),
        rejected=Count('id', filter=Q(review_status=Report.ReviewStatus.REJECTED)),
    )


def _lead_counts(projects, start, end_exclusive) -> dict:
    """Лиды периода по текущей квалификации + передача менеджеру.

    Cold/Warm/Hot считаются по `Lead.created_at` и по квалификации **на момент
    отчёта**: это срез базы, а не история переходов.

    `handed_to_manager` строится по другому полю — `Lead.hot_handoff_at`, — и
    поэтому считается отдельным запросом. Лид, созданный задолго до начала
    периода, но переданный менеджеру внутри периода, обязан попасть в этот
    счётчик; фильтр по `created_at` его бы потерял. `hot_handoff_at` для этого
    подходит: он проставляется один раз при передаче и не является `auto_now`.
    """
    by_qualification = Lead.objects.filter(
        project__in=projects,
        created_at__gte=start,
        created_at__lt=end_exclusive,
    ).aggregate(
        cold=Count('id', filter=Q(qualification_status=Lead.Qualification.COLD)),
        warm=Count('id', filter=Q(qualification_status=Lead.Qualification.WARM)),
        hot=Count('id', filter=Q(qualification_status=Lead.Qualification.HOT)),
    )
    by_qualification['handed_to_manager'] = Lead.objects.filter(
        project__in=projects,
        hot_handoff_at__gte=start,
        hot_handoff_at__lt=end_exclusive,
    ).count()
    return by_qualification


def _team_counts(projects, start, end_exclusive) -> dict:
    """Состав команды и активность подбора.

    `ready` / `not_ready` — **текущий снимок**, а не состояние за период:
    у `RoomMember` нет ни времени смены готовности, ни `updated_at`, поэтому
    восстановить историческую готовность нечем. `not_ready` — все остальные
    фрилансеры комнаты (в проде это `pending`; `declined` в модели есть, но
    прод-кодом не выставляется). Директор и тимлид в счёт не идут: считаются
    только участники с `role_in_room == FREELANCER`.

    `selection_skips_declines` — «отказы / пропуски подбора за период»: строки
    `RoomSlotCandidate` с исходом SKIPPED или DECLINED, у которых `updated_at`
    попал в период. Ограничение контракта данных: на пару (слот, кандидат)
    хранится одна строка, `outcome` перезаписывается, а `updated_at` —
    `auto_now`, поэтому показатель отражает состояние строк, затронутых в
    периоде, а не полную историю решений подбора.
    """
    members = RoomMember.objects.filter(
        room__project__in=projects,
        role_in_room=RoomMember.RoleInRoom.FREELANCER,
    ).aggregate(
        total=Count('id'),
        ready=Count('id', filter=Q(ready_status=RoomMember.ReadyStatus.READY)),
    )
    skips = RoomSlotCandidate.objects.filter(
        slot__room__project__in=projects,
        outcome__in=[
            RoomSlotCandidate.Outcome.SKIPPED,
            RoomSlotCandidate.Outcome.DECLINED,
        ],
        updated_at__gte=start,
        updated_at__lt=end_exclusive,
    ).count()
    return {
        'ready': members['ready'],
        'not_ready': members['total'] - members['ready'],
        'selection_skips_declines': skips,
    }


def _sla_row(project, now) -> dict:
    """Строка SLA стартовой задачи одного проекта.

    Стартовая задача ищется только через `get_start_calls_task` — публичный
    read-only helper, владеющий ключом поиска. Своего условия поиска здесь
    нет и `ensure_start_calls_task` не вызывается: отчёт не должен создавать
    задачу побочным эффектом просмотра.

    Классификация закрытой задачи опирается на `Task.closed_at`. Если задача
    закрыта, дедлайн известен, а `closed_at` пуст — это задача, закрытая до
    появления поля. Такая строка получает отдельный `closed_time_unknown`
    вместо того, чтобы быть отнесённой к «в срок» или «просрочено» по догадке.
    """
    task = get_start_calls_task(project)
    row = {
        'project_id': project.id,
        'project_name': project.name,
        'status': SLA_NO_START_TASK,
        'deadline': None,
        'closed_at': None,
    }
    if task is None:
        return row

    row['deadline'] = task.deadline
    row['closed_at'] = task.closed_at

    if task.status == Task.Status.CLOSED:
        if task.deadline is None:
            row['status'] = SLA_ON_TIME
        elif task.closed_at is None:
            row['status'] = SLA_CLOSED_TIME_UNKNOWN
        elif task.closed_at <= task.deadline:
            row['status'] = SLA_ON_TIME
        else:
            row['status'] = SLA_OVERDUE
        return row

    if task.deadline is not None and task.deadline < now:
        row['status'] = SLA_OVERDUE
    else:
        row['status'] = SLA_IN_PROGRESS
    return row


def build_teamlead_period_report(*, user, date_from, date_to, project=None) -> dict:
    """Сводка тимлида за период. Только чтение, готовая структура для шаблона.

    :param user: пользователь; допускается только роль TEAMLEAD.
    :param date_from: `datetime.date`, включительно.
    :param date_to: `datetime.date`, включительно.
    :param project: конкретный `Project` тимлида либо `None` — тогда в отчёт
        идут **все** проекты, где `project.teamlead_id == user.id`, без
        фильтра по статусу проекта.

    :raises PermissionDenied: не аутентифицирован; роль не TEAMLEAD; передан
        чужой проект.
    :raises ValueError: `date_from > date_to`.

    Правила подсчёта:

    * **Задачи** — по `Task.created_at` в периоде, `manager_handoff` исключены.
      `approved_not_closed` — отдельный счётчик (статус APPROVED).
      `overdue` — среди задач периода: дедлайн есть, прошёл, статус не CLOSED.
    * **Отчёты** — по `Report.created_at` в периоде, разрез по `review_status`.
    * **Лиды** — Cold/Warm/Hot по `Lead.created_at` в периоде и текущей
      квалификации; `handed_to_manager` — отдельно, по `Lead.hot_handoff_at`
      в периоде (лид мог быть создан раньше периода).
    * **Команда** — текущий снимок `RoomMember` с ролью FREELANCER, без
      фильтра по датам. `selection_skips_declines` — `RoomSlotCandidate` с
      исходом SKIPPED/DECLINED и `updated_at` в периоде.
    * **SLA** — по одной строке на проект, стартовая задача через
      `get_start_calls_task`.

    Все временные границы — календарные дни в текущей таймзоне Django,
    диапазон полуоткрытый (см. `period_bounds`). Момент «сейчас» берётся один
    раз на вызов, чтобы просрочка задач и просрочка SLA считались от одной
    точки отсчёта.
    """
    projects = _projects_for_report(user, project)
    start, end_exclusive = period_bounds(date_from, date_to)
    now = timezone.now()

    return {
        'period': {
            'date_from': date_from,
            'date_to': date_to,
            'start': start,
            'end_exclusive': end_exclusive,
        },
        'scope': {
            'project': project,
            'projects': projects,
            'projects_count': len(projects),
            'is_all_projects': project is None,
        },
        'generated_at': now,
        'tasks': _task_counts(projects, start, end_exclusive, now),
        'reports': _report_counts(projects, start, end_exclusive),
        'leads': _lead_counts(projects, start, end_exclusive),
        'team': _team_counts(projects, start, end_exclusive),
        'sla': [_sla_row(item, now) for item in projects],
    }
