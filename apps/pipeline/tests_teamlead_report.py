"""Отчёт тимлида за период: сервис и HTTP-контур.

Первая половина файла — unit-тесты `apps.pipeline.teamlead_report`: права
доступа, границы периода, правила подсчёта по каждой группе, статусы SLA и
отсутствие записей в БД.

Вторая часть — HTTP: форма на дашборде тимлида, thin view и URL
`/teamlead/report/`. Правила подсчёта там намеренно не дублируются —
проверяется контур «запрос -> форма -> сервис -> шаблон».

Третья часть — UI страницы отчёта: пять блоков статистики, русские подписи
статусов SLA, empty-state и кнопка печати.

Поля `auto_now_add` / `auto_now` в тестах сдвигаются через `queryset.update()`:
обычный `create()`/`save()` их перезаписал бы, и объект нельзя было бы
поставить в нужную дату.
"""

from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.rooms.models import (
    Project,
    RoomActivity,
    RoomFunctionSlot,
    RoomMember,
    RoomSlotCandidate,
)
from apps.rooms.services import ensure_room_for_project
from apps.test_helpers import make_director, make_freelancer, make_teamlead, make_user
from apps.users.models import User

from .models import Lead, Report, Task
from .services import START_CALLS_TITLE
from .teamlead_report import (
    DEFAULT_PERIOD_DAYS,
    SLA_CLOSED_TIME_UNKNOWN,
    SLA_IN_PROGRESS,
    SLA_NO_START_TASK,
    SLA_ON_TIME,
    SLA_OVERDUE,
    build_teamlead_period_report,
    default_report_period,
    period_bounds,
)

#: Насколько далеко за пределы периода уносятся «старые» объекты.
FAR_OUTSIDE = timedelta(days=30)

#: Пароль всех тестовых пользователей (совпадает с `apps.test_helpers`).
PASSWORD = 'TestPass123!'


def _png(name='shot.png'):
    return SimpleUploadedFile(name, b'\x89PNG\r\n\x1a\nfake', content_type='image/png')


class TeamleadReportTestCase(TestCase):
    """Общая фикстура: два проекта своего тимлида и один чужой.

    Проекты создаются напрямую через ORM, а не через staffing-сервисы: тесты
    отчёта проверяют агрегацию, и им не нужна ни автоматика активации, ни
    правила подбора.
    """

    def setUp(self):
        self.teamlead = make_teamlead(email='tl@report.test')
        self.other_teamlead = make_teamlead(email='tl2@report.test')
        self.director = make_director(email='dir@report.test')
        self.freelancer = make_freelancer(email='fr@report.test')
        self.freelancer2 = make_freelancer(email='fr2@report.test')
        self.manager = make_user(email='mgr@report.test', role=User.Roles.MANAGER)

        self.project = self._make_project('Первый проект', self.teamlead)
        self.project2 = self._make_project('Второй проект', self.teamlead)
        self.foreign_project = self._make_project('Чужой проект', self.other_teamlead)

        # Период: сегодня и шесть предыдущих дней.
        self.date_from, self.date_to = default_report_period()
        self.now = timezone.now()

    # -- фикстуры -----------------------------------------------------------

    def _make_project(self, name, teamlead, status=Project.Status.ACTIVE):
        project = Project.objects.create(
            owner=self.director,
            name=name,
            teamlead=teamlead,
            status=status,
            input_data={'hot_criteria': 'Запросил демо'},
        )
        ensure_room_for_project(project)
        return project

    def _report(self, project=None, **kwargs):
        return build_teamlead_period_report(
            user=kwargs.pop('user', self.teamlead),
            date_from=kwargs.pop('date_from', self.date_from),
            date_to=kwargs.pop('date_to', self.date_to),
            project=project,
        )

    def _task(self, project=None, *, title='Задача периода', **kwargs):
        return Task.objects.create(
            project=project or self.project,
            assignee=self.freelancer,
            created_by=self.teamlead,
            title=title,
            **kwargs,
        )

    def _lead(self, project=None, *, name='Контакт', **kwargs):
        return Lead.objects.create(
            project=project or self.project,
            creator=self.freelancer,
            contact_info={'name': name},
            source=Lead.Source.BASE,
            **kwargs,
        )

    def _member(self, project, user, ready_status=RoomMember.ReadyStatus.PENDING):
        return RoomMember.objects.create(
            room=project.room,
            user=user,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            ready_status=ready_status,
        )

    def _candidate(self, project, user, outcome, *, role_key='seller', slot_index=1):
        slot, _created = RoomFunctionSlot.objects.get_or_create(
            room=project.room,
            role_key=role_key,
            slot_index=slot_index,
        )
        return RoomSlotCandidate.objects.create(
            slot=slot,
            candidate=user,
            outcome=outcome,
        )

    def _start_calls_task(self, project, **kwargs):
        """Стартовая задача SLA тем же ключом, что и `get_start_calls_task`."""
        return Task.objects.create(
            project=project,
            assignee=self.teamlead,
            title=START_CALLS_TITLE,
            task_type=Task.TaskType.ONBOARDING,
            report_required=False,
            **kwargs,
        )

    @staticmethod
    def _move_out_of_period(model, pk, field='created_at'):
        """Уносит объект далеко за пределы периода, минуя auto_now-поля."""
        model.objects.filter(pk=pk).update(
            **{field: timezone.now() - FAR_OUTSIDE}
        )


# ---------------------------------------------------------------------------
# 1-4. Доступ и область выборки
# ---------------------------------------------------------------------------


class TeamleadReportAccessTests(TeamleadReportTestCase):
    def test_teamlead_gets_only_own_projects(self):
        report = self._report()
        names = {p.name for p in report['scope']['projects']}
        self.assertEqual(names, {'Первый проект', 'Второй проект'})
        self.assertNotIn('Чужой проект', names)

    def test_project_none_aggregates_every_project_of_this_teamlead(self):
        self._task(self.project, title='Задача первого проекта')
        self._task(self.project2, title='Задача второго проекта')
        self._task(self.foreign_project, title='Задача чужого проекта')

        report = self._report()

        self.assertEqual(report['scope']['projects_count'], 2)
        self.assertTrue(report['scope']['is_all_projects'])
        self.assertEqual(report['tasks']['created'], 2)

    def test_project_none_keeps_projects_of_any_status(self):
        archived = self._make_project(
            'Архивный проект', self.teamlead, status=Project.Status.ARCHIVED
        )
        self._task(archived, title='Задача архивного проекта')

        report = self._report()

        self.assertEqual(report['scope']['projects_count'], 3)
        self.assertEqual(report['tasks']['created'], 1)

    def test_foreign_project_is_forbidden(self):
        with self.assertRaises(PermissionDenied):
            self._report(project=self.foreign_project)

    def test_other_roles_are_forbidden(self):
        for actor in (self.director, self.freelancer, self.manager):
            with self.subTest(role=actor.role):
                with self.assertRaises(PermissionDenied):
                    self._report(user=actor)

    def test_admin_is_forbidden_too(self):
        admin = make_user(email='adm@report.test', role=User.Roles.ADMIN)
        with self.assertRaises(PermissionDenied):
            self._report(user=admin)

    def test_reversed_period_raises_instead_of_swapping_dates(self):
        with self.assertRaises(ValueError):
            self._report(date_from=self.date_to, date_to=self.date_from - timedelta(days=1))


# ---------------------------------------------------------------------------
# 5-8. Задачи
# ---------------------------------------------------------------------------


class TeamleadReportTaskTests(TeamleadReportTestCase):
    def test_task_outside_period_is_not_counted(self):
        inside = self._task(title='Задача внутри периода')
        outside = self._task(title='Задача вне периода')
        self._move_out_of_period(Task, outside.pk)

        report = self._report()

        self.assertEqual(report['tasks']['created'], 1)
        self.assertEqual(
            list(
                Task.objects.filter(pk=inside.pk).values_list('title', flat=True)
            ),
            ['Задача внутри периода'],
        )

    def test_period_boundaries_are_inclusive_on_both_days(self):
        tz = timezone.get_current_timezone()
        start, end_exclusive = period_bounds(self.date_from, self.date_to)

        first_moment = self._task(title='Первая секунда периода')
        Task.objects.filter(pk=first_moment.pk).update(created_at=start)
        last_moment = self._task(title='Последняя секунда периода')
        Task.objects.filter(pk=last_moment.pk).update(
            created_at=end_exclusive - timedelta(microseconds=1)
        )
        just_before = self._task(title='За миг до периода')
        Task.objects.filter(pk=just_before.pk).update(
            created_at=start - timedelta(microseconds=1)
        )
        just_after = self._task(title='Ровно конец периода')
        Task.objects.filter(pk=just_after.pk).update(created_at=end_exclusive)

        report = self._report()

        self.assertEqual(report['tasks']['created'], 2)
        self.assertEqual(start.tzinfo, tz)

    def test_manager_handoff_tasks_are_excluded(self):
        self._task(title='Обычная рабочая задача')
        self._task(
            title='Принять заявку: Игорь',
            task_type=Task.TaskType.MANAGER_HANDOFF,
        )

        report = self._report()

        self.assertEqual(report['tasks']['created'], 1)

    def test_approved_goes_to_approved_not_closed(self):
        self._task(title='Утверждена, не закрыта', status=Task.Status.APPROVED)
        self._task(title='Закрытая задача', status=Task.Status.CLOSED)

        report = self._report()

        self.assertEqual(report['tasks']['approved_not_closed'], 1)
        self.assertEqual(report['tasks']['closed'], 1)
        self.assertEqual(report['tasks']['created'], 2)

    def test_every_status_bucket(self):
        self._task(title='Новая задача', status=Task.Status.NEW)
        self._task(title='Задача в работе', status=Task.Status.IN_PROGRESS)
        self._task(title='Задача на проверке', status=Task.Status.READY_FOR_REVIEW)
        self._task(title='Утверждённая задача', status=Task.Status.APPROVED)
        self._task(title='Закрытая задача', status=Task.Status.CLOSED)

        tasks = self._report()['tasks']

        self.assertEqual(tasks['created'], 5)
        self.assertEqual(tasks['in_progress'], 1)
        self.assertEqual(tasks['ready_for_review'], 1)
        self.assertEqual(tasks['approved_not_closed'], 1)
        self.assertEqual(tasks['closed'], 1)

    def test_overdue_counts_only_unclosed_tasks_of_the_period(self):
        past = timezone.now() - timedelta(hours=1)
        future = timezone.now() + timedelta(days=1)

        self._task(title='Просрочена и открыта', status=Task.Status.IN_PROGRESS,
                   deadline=past)
        self._task(title='Просрочена но закрыта', status=Task.Status.CLOSED,
                   deadline=past)
        self._task(title='Дедлайн в будущем', status=Task.Status.IN_PROGRESS,
                   deadline=future)
        self._task(title='Задача без дедлайна', status=Task.Status.IN_PROGRESS)
        stale = self._task(title='Просрочена но вне периода',
                           status=Task.Status.IN_PROGRESS, deadline=past)
        self._move_out_of_period(Task, stale.pk)

        report = self._report()

        self.assertEqual(report['tasks']['overdue'], 1)

    def test_single_project_scope_ignores_the_other_project(self):
        self._task(self.project, title='Задача первого проекта')
        self._task(self.project2, title='Задача второго проекта')

        report = self._report(project=self.project)

        self.assertEqual(report['scope']['projects_count'], 1)
        self.assertEqual(report['tasks']['created'], 1)


# ---------------------------------------------------------------------------
# 9. Отчёты
# ---------------------------------------------------------------------------


class TeamleadReportReportTests(TeamleadReportTestCase):
    def _make_report(self, task, review_status, **kwargs):
        return Report.objects.create(
            task=task,
            author=self.freelancer,
            content_text='Текст отчёта достаточной длины.',
            attachment=_png(),
            review_status=review_status,
            **kwargs,
        )

    def test_reports_are_counted_by_review_status(self):
        task = self._task(title='Задача с отчётами')
        self._make_report(task, Report.ReviewStatus.PENDING)
        self._make_report(task, Report.ReviewStatus.APPROVED)
        self._make_report(task, Report.ReviewStatus.APPROVED)
        self._make_report(task, Report.ReviewStatus.REJECTED)

        reports = self._report()['reports']

        self.assertEqual(reports['submitted'], 1)
        self.assertEqual(reports['approved'], 2)
        self.assertEqual(reports['rejected'], 1)

    def test_report_outside_period_is_not_counted(self):
        task = self._task(title='Задача со старым отчётом')
        old = self._make_report(task, Report.ReviewStatus.APPROVED)
        self._move_out_of_period(Report, old.pk)

        self.assertEqual(self._report()['reports']['approved'], 0)

    def test_period_uses_created_at_not_reviewed_at(self):
        """Отчёт относится к периоду сдачи, даже если проверен много позже."""
        task = self._task(title='Отчёт сдан в периоде')
        self._make_report(
            task,
            Report.ReviewStatus.APPROVED,
            reviewed_by=self.teamlead,
            reviewed_at=timezone.now() + FAR_OUTSIDE,
        )

        self.assertEqual(self._report()['reports']['approved'], 1)

    def test_foreign_project_reports_are_not_counted(self):
        foreign_task = self._task(self.foreign_project, title='Чужая задача')
        self._make_report(foreign_task, Report.ReviewStatus.APPROVED)

        self.assertEqual(self._report()['reports']['approved'], 0)


# ---------------------------------------------------------------------------
# 10-11. Лиды
# ---------------------------------------------------------------------------


class TeamleadReportLeadTests(TeamleadReportTestCase):
    def test_leads_are_grouped_by_current_qualification(self):
        self._lead(name='Холодный', qualification_status=Lead.Qualification.COLD)
        self._lead(name='Тёплый', qualification_status=Lead.Qualification.WARM)
        self._lead(name='Тёплый второй', qualification_status=Lead.Qualification.WARM)
        self._lead(name='Горячий', qualification_status=Lead.Qualification.HOT)

        leads = self._report()['leads']

        self.assertEqual(leads['cold'], 1)
        self.assertEqual(leads['warm'], 2)
        self.assertEqual(leads['hot'], 1)

    def test_lead_outside_period_is_not_counted(self):
        old = self._lead(name='Старый', qualification_status=Lead.Qualification.WARM)
        self._move_out_of_period(Lead, old.pk)

        self.assertEqual(self._report()['leads']['warm'], 0)

    def test_handoff_counts_old_lead_handed_over_inside_the_period(self):
        """Ключевой случай: лид создан до периода, передан менеджеру внутри."""
        lead = self._lead(name='Давний лид', qualification_status=Lead.Qualification.HOT)
        Lead.objects.filter(pk=lead.pk).update(
            created_at=timezone.now() - FAR_OUTSIDE,
            hot_handoff_at=timezone.now(),
            assigned_manager=self.manager,
        )

        leads = self._report()['leads']

        # По дате создания он в период не попадает…
        self.assertEqual(leads['hot'], 0)
        # …но передача менеджеру произошла внутри периода.
        self.assertEqual(leads['handed_to_manager'], 1)

    def test_handoff_outside_period_is_not_counted(self):
        lead = self._lead(name='Передан давно', qualification_status=Lead.Qualification.HOT)
        Lead.objects.filter(pk=lead.pk).update(
            hot_handoff_at=timezone.now() - FAR_OUTSIDE,
        )

        self.assertEqual(self._report()['leads']['handed_to_manager'], 0)

    def test_lead_without_handoff_is_not_counted(self):
        self._lead(name='Не передан', qualification_status=Lead.Qualification.HOT)

        report = self._report()

        self.assertEqual(report['leads']['hot'], 1)
        self.assertEqual(report['leads']['handed_to_manager'], 0)


# ---------------------------------------------------------------------------
# 12-13. Команда и подбор
# ---------------------------------------------------------------------------


class TeamleadReportTeamTests(TeamleadReportTestCase):
    def test_ready_and_not_ready_is_a_current_snapshot(self):
        self._member(self.project, self.freelancer, RoomMember.ReadyStatus.READY)
        self._member(self.project, self.freelancer2, RoomMember.ReadyStatus.PENDING)

        team = self._report()['team']

        self.assertEqual(team['ready'], 1)
        self.assertEqual(team['not_ready'], 1)

    def test_team_ignores_the_period(self):
        """Состав — снимок: участник, добавленный давно, всё равно считается."""
        member = self._member(self.project, self.freelancer, RoomMember.ReadyStatus.READY)
        RoomMember.objects.filter(pk=member.pk).update(
            joined_at=timezone.now() - FAR_OUTSIDE
        )

        self.assertEqual(self._report()['team']['ready'], 1)

    def test_director_and_teamlead_are_not_counted_as_team(self):
        # `ensure_room_for_project` уже добавил директора участником комнаты.
        self.assertTrue(
            RoomMember.objects.filter(
                room__project=self.project,
                role_in_room=RoomMember.RoleInRoom.DIRECTOR,
            ).exists()
        )

        team = self._report()['team']

        self.assertEqual(team['ready'], 0)
        self.assertEqual(team['not_ready'], 0)

    def test_skips_and_declines_are_counted_by_updated_at(self):
        self._candidate(
            self.project, self.freelancer,
            RoomSlotCandidate.Outcome.SKIPPED, slot_index=1,
        )
        self._candidate(
            self.project, self.freelancer2,
            RoomSlotCandidate.Outcome.DECLINED, slot_index=2,
        )

        self.assertEqual(self._report()['team']['selection_skips_declines'], 2)

    def test_other_outcomes_are_not_counted(self):
        self._candidate(
            self.project, self.freelancer,
            RoomSlotCandidate.Outcome.SHOWN, slot_index=1,
        )
        self._candidate(
            self.project, self.freelancer2,
            RoomSlotCandidate.Outcome.ASSIGNED, slot_index=2,
        )

        self.assertEqual(self._report()['team']['selection_skips_declines'], 0)

    def test_skip_outside_period_is_not_counted(self):
        candidate = self._candidate(
            self.project, self.freelancer, RoomSlotCandidate.Outcome.SKIPPED,
        )
        self._move_out_of_period(RoomSlotCandidate, candidate.pk, field='updated_at')

        self.assertEqual(self._report()['team']['selection_skips_declines'], 0)


# ---------------------------------------------------------------------------
# 14. SLA
# ---------------------------------------------------------------------------


class TeamleadReportSlaTests(TeamleadReportTestCase):
    def _sla_for(self, project):
        rows = self._report(project=project)['sla']
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_one_row_per_project(self):
        rows = self._report()['sla']
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row['project_name'] for row in rows},
            {'Первый проект', 'Второй проект'},
        )

    def test_no_start_task(self):
        row = self._sla_for(self.project)

        self.assertEqual(row['status'], SLA_NO_START_TASK)
        self.assertEqual(row['project_id'], self.project.id)
        self.assertIsNone(row['deadline'])
        self.assertIsNone(row['closed_at'])

    def test_on_time_when_closed_before_deadline(self):
        deadline = timezone.now() + timedelta(hours=5)
        self._start_calls_task(
            self.project,
            status=Task.Status.CLOSED,
            deadline=deadline,
            closed_at=deadline - timedelta(hours=1),
        )

        row = self._sla_for(self.project)

        self.assertEqual(row['status'], SLA_ON_TIME)
        self.assertEqual(row['deadline'], deadline)
        self.assertIsNotNone(row['closed_at'])

    def test_overdue_when_closed_after_deadline(self):
        deadline = timezone.now() - timedelta(hours=5)
        self._start_calls_task(
            self.project,
            status=Task.Status.CLOSED,
            deadline=deadline,
            closed_at=deadline + timedelta(hours=1),
        )

        self.assertEqual(self._sla_for(self.project)['status'], SLA_OVERDUE)

    def test_on_time_when_closed_exactly_at_the_deadline(self):
        deadline = timezone.now() - timedelta(hours=5)
        self._start_calls_task(
            self.project,
            status=Task.Status.CLOSED,
            deadline=deadline,
            closed_at=deadline,
        )

        self.assertEqual(self._sla_for(self.project)['status'], SLA_ON_TIME)

    def test_on_time_when_closed_without_a_deadline(self):
        self._start_calls_task(
            self.project,
            status=Task.Status.CLOSED,
            deadline=None,
            closed_at=timezone.now(),
        )

        self.assertEqual(self._sla_for(self.project)['status'], SLA_ON_TIME)

    def test_closed_time_unknown_for_historical_task(self):
        """Закрыта до появления `closed_at` — гадать «в срок» нельзя."""
        self._start_calls_task(
            self.project,
            status=Task.Status.CLOSED,
            deadline=timezone.now() - timedelta(hours=5),
            closed_at=None,
        )

        row = self._sla_for(self.project)

        self.assertEqual(row['status'], SLA_CLOSED_TIME_UNKNOWN)
        self.assertIsNone(row['closed_at'])

    def test_overdue_for_open_task_past_its_deadline(self):
        self._start_calls_task(
            self.project,
            status=Task.Status.NEW,
            deadline=timezone.now() - timedelta(hours=1),
        )

        self.assertEqual(self._sla_for(self.project)['status'], SLA_OVERDUE)

    def test_in_progress_for_open_task_before_its_deadline(self):
        self._start_calls_task(
            self.project,
            status=Task.Status.NEW,
            deadline=timezone.now() + timedelta(hours=10),
        )

        self.assertEqual(self._sla_for(self.project)['status'], SLA_IN_PROGRESS)

    def test_sla_ignores_the_reporting_period(self):
        """SLA — состояние стартовой задачи, а не событие внутри периода."""
        task = self._start_calls_task(
            self.project,
            status=Task.Status.CLOSED,
            deadline=timezone.now(),
            closed_at=timezone.now() - timedelta(hours=1),
        )
        self._move_out_of_period(Task, task.pk)

        self.assertEqual(self._sla_for(self.project)['status'], SLA_ON_TIME)


# ---------------------------------------------------------------------------
# 15-16. Побочные эффекты и период по умолчанию
# ---------------------------------------------------------------------------


class TeamleadReportPurityTests(TeamleadReportTestCase):
    def test_building_the_report_writes_nothing(self):
        self._task(title='Задача для наполнения')
        self._lead(name='Лид для наполнения')
        self._member(self.project, self.freelancer, RoomMember.ReadyStatus.READY)
        self._candidate(self.project, self.freelancer2,
                        RoomSlotCandidate.Outcome.SKIPPED)

        watched = (Task, Report, Lead, RoomMember, RoomActivity,
                   RoomSlotCandidate, RoomFunctionSlot, Project)
        before = {model: model.objects.count() for model in watched}

        self._report()
        self._report(project=self.project)

        for model in watched:
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), before[model])

    def test_report_does_not_create_the_start_calls_task(self):
        """Отчёт читает стартовую задачу, но не создаёт её побочным эффектом."""
        self.assertEqual(Task.objects.filter(title=START_CALLS_TITLE).count(), 0)

        self._report()

        self.assertEqual(Task.objects.filter(title=START_CALLS_TITLE).count(), 0)

    def test_default_period_is_today_and_six_previous_days(self):
        date_from, date_to = default_report_period()

        self.assertEqual(date_to, timezone.localdate())
        self.assertEqual(date_to - date_from, timedelta(days=6))
        self.assertEqual((date_to - date_from).days + 1, DEFAULT_PERIOD_DAYS)

    def test_default_period_bounds_cover_seven_whole_days(self):
        start, end_exclusive = period_bounds(*default_report_period())

        self.assertEqual((end_exclusive - start), timedelta(days=DEFAULT_PERIOD_DAYS))
        self.assertEqual(timezone.localtime(start).hour, 0)
        self.assertEqual(timezone.localtime(start).minute, 0)
        self.assertEqual(timezone.localtime(end_exclusive).hour, 0)

    def test_single_day_period_is_allowed(self):
        today = timezone.localdate()
        start, end_exclusive = period_bounds(today, today)

        self.assertEqual(end_exclusive - start, timedelta(days=1))


# ---------------------------------------------------------------------------
# HTTP: форма на дашборде, thin view, URL /teamlead/report/
#
# Здесь проверяется только контур «запрос -> форма -> сервис -> шаблон».
# Правила подсчёта уже покрыты unit-тестами сервиса выше и через HTTP
# намеренно не дублируются.
# ---------------------------------------------------------------------------


class TeamleadReportDashboardTests(TeamleadReportTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('core:home')

    def test_dashboard_keeps_three_metrics_and_both_links(self):
        self.client.login(username=self.teamlead.email, password=PASSWORD)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'core/teamlead_dashboard.html')
        for label in ('Проекты', 'На проверке', 'Warm / Hot'):
            self.assertContains(response, label)
        self.assertContains(response, reverse('rooms:project_list'))
        self.assertContains(response, reverse('profiles:catalog'))
        self.assertContains(response, 'Мои проекты')
        self.assertContains(response, 'Каталог')

    def test_dashboard_shows_the_period_report_form(self):
        self.client.login(username=self.teamlead.email, password=PASSWORD)

        response = self.client.get(self.url)

        self.assertContains(response, 'Отчёт за период')
        self.assertContains(response, 'Сформировать')
        self.assertContains(
            response, 'action="%s"' % reverse('pipeline:teamlead_report')
        )
        self.assertIn('report_form', response.context)

    def test_dashboard_form_offers_only_own_projects(self):
        self.client.login(username=self.teamlead.email, password=PASSWORD)

        response = self.client.get(self.url)

        choices = list(response.context['report_form'].fields['project'].queryset)
        self.assertEqual(
            {p.name for p in choices}, {'Первый проект', 'Второй проект'}
        )
        self.assertNotContains(response, 'Чужой проект')
        self.assertContains(response, 'Все проекты')

    def test_dashboard_form_is_prefilled_with_the_default_period(self):
        self.client.login(username=self.teamlead.email, password=PASSWORD)

        form = self.client.get(self.url).context['report_form']

        date_from, date_to = default_report_period()
        self.assertEqual(form.fields['date_from'].initial, date_from)
        self.assertEqual(form.fields['date_to'].initial, date_to)


class TeamleadReportViewAccessTests(TeamleadReportTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('pipeline:teamlead_report')

    def test_url_is_teamlead_report(self):
        self.assertEqual(self.url, '/teamlead/report/')

    def test_teamlead_gets_the_page(self):
        self.client.login(username=self.teamlead.email, password=PASSWORD)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'pipeline/teamlead_report.html')
        self.assertContains(response, 'Отчёт тимлида за период')

    def test_other_roles_are_forbidden(self):
        admin = make_user(email='adm-http@report.test', role=User.Roles.ADMIN)
        for actor in (self.director, self.freelancer, self.manager, admin):
            with self.subTest(role=actor.role):
                self.client.login(username=actor.email, password=PASSWORD)
                self.assertEqual(self.client.get(self.url).status_code, 403)
                self.client.logout()

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])


class TeamleadReportViewProjectTests(TeamleadReportTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('pipeline:teamlead_report')
        self.client.login(username=self.teamlead.email, password=PASSWORD)

    def test_own_project_is_accepted(self):
        response = self.client.get(self.url, {'project': str(self.project.id)})

        self.assertEqual(response.status_code, 200)
        report = response.context['report']
        self.assertFalse(report['scope']['is_all_projects'])
        self.assertEqual(report['scope']['projects_count'], 1)
        self.assertEqual(report['scope']['project'], self.project)

    def test_empty_project_means_all_own_projects(self):
        response = self.client.get(self.url, {'project': ''})

        self.assertEqual(response.status_code, 200)
        report = response.context['report']
        self.assertTrue(report['scope']['is_all_projects'])
        self.assertEqual(report['scope']['projects_count'], 2)

    def test_foreign_project_is_forbidden(self):
        response = self.client.get(
            self.url, {'project': str(self.foreign_project.id)}
        )

        self.assertEqual(response.status_code, 403)

    def test_unknown_project_is_a_form_error_not_a_crash(self):
        for value in ('не-uuid', '11111111-1111-1111-1111-111111111111'):
            with self.subTest(project=value):
                response = self.client.get(self.url, {'project': value})

                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context['report'])
                self.assertTrue(response.context['form'].errors)


class TeamleadReportViewPeriodTests(TeamleadReportTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('pipeline:teamlead_report')
        self.client.login(username=self.teamlead.email, password=PASSWORD)

    def _assert_default_period(self, response):
        date_from, date_to = default_report_period()
        period = response.context['report']['period']
        self.assertEqual(period['date_from'], date_from)
        self.assertEqual(period['date_to'], date_to)
        self.assertEqual((period['date_to'] - period['date_from']).days, 6)

    def test_no_parameters_means_the_default_period(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self._assert_default_period(response)

    def test_blank_parameters_mean_the_default_period(self):
        response = self.client.get(
            self.url, {'date_from': '', 'date_to': '', 'project': ''}
        )

        self.assertEqual(response.status_code, 200)
        self._assert_default_period(response)

    def test_explicit_period_is_passed_through(self):
        response = self.client.get(self.url, {
            'date_from': '2026-03-01',
            'date_to': '2026-03-31',
        })

        period = response.context['report']['period']
        self.assertEqual(period['date_from'].isoformat(), '2026-03-01')
        self.assertEqual(period['date_to'].isoformat(), '2026-03-31')

    def test_reversed_period_is_a_form_error_not_a_server_error(self):
        response = self.client.get(self.url, {
            'date_from': '2026-03-31',
            'date_to': '2026-03-01',
        })

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['report'])
        self.assertTrue(response.context['form'].errors)
        self.assertContains(response, 'не может быть позже')

    def test_malformed_date_is_a_form_error_not_a_server_error(self):
        response = self.client.get(self.url, {'date_from': 'вчера'})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['report'])
        self.assertTrue(response.context['form'].errors)


class TeamleadReportViewPayloadTests(TeamleadReportTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('pipeline:teamlead_report')
        self.client.login(username=self.teamlead.email, password=PASSWORD)

    def test_view_hands_the_service_result_to_the_template(self):
        self._task(title='Задача для витрины')

        response = self.client.get(self.url)
        report = response.context['report']

        # View сам ничего не агрегирует — структура целиком из сервиса.
        self.assertEqual(
            set(report),
            {'period', 'scope', 'generated_at', 'tasks', 'reports',
             'leads', 'team', 'sla'},
        )
        self.assertEqual(report['tasks']['created'], 1)
        self.assertEqual(len(report['sla']), 2)

    def test_get_writes_nothing(self):
        watched = (Task, Report, Lead, RoomMember, RoomActivity,
                   RoomSlotCandidate, RoomFunctionSlot, Project)
        before = {model: model.objects.count() for model in watched}

        self.client.get(self.url)
        self.client.get(self.url, {'project': str(self.project.id)})

        for model in watched:
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), before[model])


# ---------------------------------------------------------------------------
# UI: полная страница отчёта и печать
#
# Проверяется только отображение: что шаблон показывает цифры сервиса,
# русские подписи SLA и кнопку печати. Правила подсчёта покрыты
# unit-тестами выше и здесь не дублируются.
# ---------------------------------------------------------------------------


class TeamleadReportPageTests(TeamleadReportTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('pipeline:teamlead_report')
        self.client.login(username=self.teamlead.email, password=PASSWORD)

    def test_page_shows_all_five_blocks(self):
        response = self.client.get(self.url)

        for heading in ('Задачи', 'Отчёты', 'Лиды', 'Команда', 'SLA'):
            with self.subTest(block=heading):
                self.assertContains(response, heading)

    def test_page_shows_the_period_and_all_projects_scope(self):
        response = self.client.get(self.url)

        date_from, date_to = default_report_period()
        self.assertContains(response, date_from.strftime('%d.%m.%Y'))
        self.assertContains(response, date_to.strftime('%d.%m.%Y'))
        self.assertContains(response, 'Все проекты')

    def test_page_shows_the_selected_project_name(self):
        response = self.client.get(self.url, {'project': str(self.project.id)})

        self.assertContains(response, 'Первый проект')

    def test_task_numbers_from_the_service_reach_the_page(self):
        self._task(title='Первая задача в работе', status=Task.Status.IN_PROGRESS)
        self._task(title='Вторая задача в работе', status=Task.Status.IN_PROGRESS)
        self._task(title='Утверждена, не закрыта', status=Task.Status.APPROVED)

        response = self.client.get(self.url, {'project': str(self.project.id)})

        tasks = response.context['report']['tasks']
        self.assertEqual(tasks['created'], 3)
        self.assertEqual(tasks['in_progress'], 2)
        self.assertEqual(tasks['approved_not_closed'], 1)
        # Подписи блока «Задачи» — ровно те, что просил руководитель.
        self.assertContains(response, 'Создано')
        self.assertContains(response, 'Утверждено, не закрыто')
        self.assertContains(response, 'Просрочено')

    def test_report_and_lead_labels_are_rendered(self):
        self._lead(name='Тёплый контакт', qualification_status=Lead.Qualification.WARM)

        response = self.client.get(self.url, {'project': str(self.project.id)})

        self.assertEqual(response.context['report']['leads']['warm'], 1)
        # «Сдано» — подпись PENDING по договорённости.
        self.assertContains(response, 'Сдано')
        self.assertContains(response, 'Отклонено')
        self.assertContains(response, 'Передано менеджеру')
        for label in ('Cold', 'Warm', 'Hot'):
            self.assertContains(response, label)

    def test_team_block_labels(self):
        self._member(self.project, self.freelancer, RoomMember.ReadyStatus.READY)

        response = self.client.get(self.url, {'project': str(self.project.id)})

        self.assertEqual(response.context['report']['team']['ready'], 1)
        self.assertContains(response, 'Ready')
        self.assertContains(response, 'Не ready')
        self.assertContains(response, 'Отказы / пропуски подбора за период')
        # Показатель подбора не должен называться «замены».
        self.assertNotContains(response, 'Замены')


class TeamleadReportSlaLabelTests(TeamleadReportTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('pipeline:teamlead_report')
        self.client.login(username=self.teamlead.email, password=PASSWORD)

    def _page(self):
        return self.client.get(self.url, {'project': str(self.project.id)})

    def test_no_start_task_label(self):
        self.assertContains(self._page(), 'Нет стартовой задачи')

    def test_on_time_label(self):
        deadline = timezone.now() + timedelta(hours=5)
        self._start_calls_task(
            self.project,
            status=Task.Status.CLOSED,
            deadline=deadline,
            closed_at=deadline - timedelta(hours=1),
        )

        response = self._page()

        self.assertContains(response, 'В срок')
        self.assertNotContains(response, 'Нет стартовой задачи')

    def test_overdue_label(self):
        deadline = timezone.now() - timedelta(hours=5)
        self._start_calls_task(
            self.project,
            status=Task.Status.CLOSED,
            deadline=deadline,
            closed_at=deadline + timedelta(hours=1),
        )

        self.assertContains(self._page(), 'Просрочена')

    def test_closed_time_unknown_label_never_claims_on_time_or_overdue(self):
        self._start_calls_task(
            self.project,
            status=Task.Status.CLOSED,
            deadline=timezone.now() - timedelta(hours=5),
            closed_at=None,
        )

        response = self._page()

        self.assertContains(response, 'Закрыта, время закрытия неизвестно')
        self.assertNotContains(response, 'Просрочена')

    def test_deadline_and_closed_at_are_rendered(self):
        deadline = timezone.now() - timedelta(hours=5)
        closed_at = deadline - timedelta(hours=1)
        self._start_calls_task(
            self.project,
            status=Task.Status.CLOSED,
            deadline=deadline,
            closed_at=closed_at,
        )

        response = self._page()

        self.assertContains(
            response, timezone.localtime(deadline).strftime('%d.%m.%Y %H:%M')
        )
        self.assertContains(
            response, timezone.localtime(closed_at).strftime('%d.%m.%Y %H:%M')
        )

    def test_empty_state_when_the_teamlead_has_no_projects(self):
        lonely = make_teamlead(email='lonely@report.test')
        self.client.logout()
        self.client.login(username=lonely.email, password=PASSWORD)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['report']['sla'], [])
        self.assertContains(response, 'Нет проектов для отчёта.')


class TeamleadReportPrintTests(TeamleadReportTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('pipeline:teamlead_report')
        self.client.login(username=self.teamlead.email, password=PASSWORD)

    def test_print_button_uses_window_print(self):
        response = self.client.get(self.url)

        self.assertContains(response, 'Печать')
        self.assertContains(response, 'window.print()')

    def test_no_pdf_and_no_download_endpoint(self):
        response = self.client.get(self.url)
        body = response.content.decode()

        for forbidden in ('pdf', 'PDF', 'download'):
            with self.subTest(marker=forbidden):
                self.assertNotIn(forbidden, body)

    def test_rendering_the_page_writes_nothing(self):
        self._task(title='Задача для печати')
        watched = (Task, Report, Lead, RoomMember, RoomActivity,
                   RoomSlotCandidate, RoomFunctionSlot, Project)
        before = {model: model.objects.count() for model in watched}

        self.client.get(self.url)
        self.client.get(self.url, {'project': str(self.project.id)})

        for model in watched:
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), before[model])


class TeamleadReportInvalidFormPageTests(TeamleadReportTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('pipeline:teamlead_report')
        self.client.login(username=self.teamlead.email, password=PASSWORD)

    def test_invalid_range_shows_errors_and_hides_the_statistics(self):
        response = self.client.get(self.url, {
            'date_from': '2026-03-31',
            'date_to': '2026-03-01',
        })

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['report'])
        self.assertContains(response, 'не может быть позже')
        # Ни одного статистического блока и ни кнопки печати.
        for label in ('Создано', 'Утверждено, не закрыто', 'Передано менеджеру',
                      'Отказы / пропуски подбора за период', 'window.print()'):
            with self.subTest(hidden=label):
                self.assertNotContains(response, label)
