"""Бизнес-логика задач, отчётов и лидов."""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.rooms.services import user_can_access_project, user_can_manage_team
from apps.users.models import User

from .models import Lead, LeadStatusHistory, Report, Task


class TaskCloseError(ValidationError):
    pass


def parse_checklist_text(raw: str) -> list:
    """Строки чеклиста → [{"text": ..., "done": false}]."""
    items = []
    for line in (raw or '').splitlines():
        text = line.strip().lstrip('-•* ').strip()
        if text:
            items.append({'text': text, 'done': False})
    return items


def checklist_to_text(checklist) -> str:
    if not checklist:
        return ''
    return '\n'.join(item.get('text', '') for item in checklist if item.get('text'))


@transaction.atomic
def create_task(*, project, assignee, created_by, title, description='', deadline=None,
                checklist=None, report_required=True, task_type=Task.TaskType.WORK,
                lead=None) -> Task:
    if not user_can_manage_team(created_by, project):
        raise PermissionDenied('Недостаточно прав для создания задачи.')

    task = Task(
        project=project,
        assignee=assignee,
        created_by=created_by,
        title=title.strip(),
        description=description,
        deadline=deadline,
        checklist=checklist or [],
        report_required=report_required,
        task_type=task_type,
        lead=lead,
        status=Task.Status.NEW,
    )
    task.full_clean()
    task.save()
    return task


@transaction.atomic
def start_task(task: Task, user: User) -> Task:
    if task.assignee_id != user.id:
        raise PermissionDenied('Только исполнитель может взять задачу.')
    if task.status not in {Task.Status.NEW, Task.Status.REJECTED}:
        raise ValidationError('Задачу нельзя взять в работу из текущего статуса.')
    task.status = Task.Status.IN_PROGRESS
    task.save(update_fields=['status', 'updated_at'])
    return task


@transaction.atomic
def submit_report(*, task: Task, author: User, content_text: str, attachment) -> Report:
    if task.assignee_id != author.id:
        raise PermissionDenied('Отчёт может сдать только исполнитель.')
    if task.status == Task.Status.CLOSED:
        raise ValidationError('Задача уже закрыта.')

    text = (content_text or '').strip()
    if len(text) < 10:
        raise ValidationError('Текст отчёта — минимум 10 символов.')
    if not attachment:
        raise ValidationError('Вложение (скриншот) обязательно.')

    report = Report(
        task=task,
        author=author,
        content_text=text,
        attachment=attachment,
        review_status=Report.ReviewStatus.PENDING,
    )
    report.full_clean()
    report.save()

    task.status = Task.Status.READY_FOR_REVIEW
    task.save(update_fields=['status', 'updated_at'])
    return report


@transaction.atomic
def review_report(*, report: Report, reviewer: User, approve: bool, comment: str = '') -> Report:
    task = report.task
    if not user_can_manage_team(reviewer, task.project):
        raise PermissionDenied('Проверять отчёты может тимлид или директор.')

    if report.review_status != Report.ReviewStatus.PENDING:
        raise ValidationError('Этот отчёт уже проверен.')

    report.reviewer_comment = (comment or '').strip()
    report.reviewed_by = reviewer
    report.reviewed_at = timezone.now()

    if approve:
        report.review_status = Report.ReviewStatus.APPROVED
        task.status = Task.Status.APPROVED
    else:
        if not report.reviewer_comment:
            raise ValidationError('При отклонении нужен комментарий.')
        report.review_status = Report.ReviewStatus.REJECTED
        task.status = Task.Status.REJECTED

    report.save()
    task.save(update_fields=['status', 'updated_at'])
    return report


@transaction.atomic
def close_task(task: Task, user: User) -> Task:
    if not (user_can_manage_team(user, task.project) or task.assignee_id == user.id):
        raise PermissionDenied('Нет прав закрыть задачу.')

    if task.report_required:
        report = task.latest_report
        if not report or report.review_status != Report.ReviewStatus.APPROVED:
            raise TaskCloseError('Нельзя закрыть задачу без утверждённого отчёта.')
        if task.status != Task.Status.APPROVED:
            raise TaskCloseError('Задача должна быть в статусе «Утверждена».')

    task.status = Task.Status.CLOSED
    task.save(update_fields=['status', 'updated_at'])
    return task


def pick_manager_for_lead(project) -> User | None:
    """Выбирает активного менеджера платформы (простая квота: первый по дате)."""
    return (
        User.objects.filter(role=User.Roles.MANAGER, status=User.Status.ACTIVE)
        .order_by('date_joined')
        .first()
    )


@transaction.atomic
def create_lead(*, project, creator: User, contact_info: dict, source: str,
                notes: str = '', qualification_status: str = Lead.Qualification.COLD) -> Lead:
    if not user_can_access_project(creator, project):
        raise PermissionDenied('Нет доступа к проекту.')
    if creator.role not in {User.Roles.FREELANCER, User.Roles.TEAMLEAD, User.Roles.ADMIN}:
        raise PermissionDenied('Лид создаёт фрилансер (или тимлид).')

    # Фрилансер не может сразу поставить Hot
    if (
        qualification_status == Lead.Qualification.HOT
        and creator.role == User.Roles.FREELANCER
    ):
        raise ValidationError('Статус «Горячий» выставляет только тимлид.')

    lead = Lead(
        project=project,
        creator=creator,
        contact_info=contact_info or {},
        source=source,
        notes=notes,
        qualification_status=qualification_status,
    )
    lead.save()
    LeadStatusHistory.objects.create(
        lead=lead,
        old_status='',
        new_status=qualification_status,
        changed_by=creator,
        comment='Создание лида',
    )
    return lead


@transaction.atomic
def set_lead_qualification(
    *,
    lead: Lead,
    new_status: str,
    changed_by: User,
    comment: str = '',
    matched_hot_criteria: list | None = None,
) -> Lead:
    if not user_can_manage_team(changed_by, lead.project):
        raise PermissionDenied('Квалифицировать лид может тимлид или директор.')

    old = lead.qualification_status
    if old == new_status and not matched_hot_criteria:
        return lead

    if new_status == Lead.Qualification.HOT:
        criteria = matched_hot_criteria or []
        project_criteria = (lead.project.input_data or {}).get('hot_criteria', '')
        if not criteria and not project_criteria:
            raise ValidationError(
                'Для статуса Hot укажите совпавшие критерии горячего лида.'
            )
        if criteria:
            lead.matched_hot_criteria = criteria

    lead.qualification_status = new_status
    LeadStatusHistory.objects.create(
        lead=lead,
        old_status=old,
        new_status=new_status,
        changed_by=changed_by,
        comment=comment,
    )

    if new_status == Lead.Qualification.HOT and old != Lead.Qualification.HOT:
        _handoff_hot_lead(lead, changed_by)

    lead.save()
    return lead


def _handoff_hot_lead(lead: Lead, changed_by: User) -> Task:
    manager = pick_manager_for_lead(lead.project)
    if manager is None:
        raise ValidationError(
            'Нет активного менеджера платформы. Создайте пользователя с ролью Manager.'
        )

    lead.assigned_manager = manager
    lead.hot_handoff_at = timezone.now()
    lead.save(update_fields=['assigned_manager', 'hot_handoff_at', 'updated_at'])

    contact = lead.contact_name or lead.contact_email or str(lead.id)
    task = Task(
        project=lead.project,
        assignee=manager,
        created_by=changed_by,
        lead=lead,
        title=f'Принять заявку: {contact}',
        description=(
            'Горячий лид. Связаться в течение 24 часов.\n'
            f'Критерии проекта: {(lead.project.input_data or {}).get("hot_criteria", "—")}\n'
            f'Совпавшие: {", ".join(lead.matched_hot_criteria) or "—"}\n'
            f'Заметки: {lead.notes or "—"}'
        ),
        deadline=timezone.now() + timedelta(hours=24),
        checklist=[
            {'text': 'Связаться с лидом', 'done': False},
            {'text': 'Уточнить потребность', 'done': False},
            {'text': 'Зафиксировать результат', 'done': False},
        ],
        status=Task.Status.NEW,
        task_type=Task.TaskType.MANAGER_HANDOFF,
        report_required=False,
    )
    task.save()
    return task
