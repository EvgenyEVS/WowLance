"""Отчёт тимлида за период: сервис и HTTP-контур.

Проверяется:

* доступ — отчёт строит только тимлид и только по своим проектам;
* период отсекает задачи, отчёты и лиды, а обе даты входят в него;
* цифры отчёта покрывают задачи, лиды и готовность команды;
* передача лида менеджеру считается по дате передачи, а не создания;
* чтение остаётся чтением — ни сервис, ни GET ничего не пишут;
* контур «дашборд с формой → страница отчёта» работает целиком.

Правила SLA стартовой задачи живут в `apps.rooms.tests_automation_sla`.
Подписи, вёрстка и печать контрактом не являются и здесь не проверяются.

Поля `auto_now_add` / `auto_now` сдвигаются через `queryset.update()`:
обычный `create()`/`save()` их перезаписал бы.
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
from .teamlead_report import (
    build_teamlead_period_report,
    default_report_period,
    period_bounds,
)

#: Насколько далеко за пределы периода уносятся «старые» объекты.
FAR_OUTSIDE = timedelta(days=30)

#: Структура, которую сервис отдаёт шаблону.
REPORT_SECTIONS = {
    'period', 'scope', 'generated_at', 'tasks', 'reports', 'leads', 'team', 'sla',
}

#: Модели, которых чтение отчёта не должно касаться.
WATCHED_MODELS = (
    Task, Report, Lead, RoomMember, RoomActivity,
    RoomSlotCandidate, RoomFunctionSlot, Project,
)


def _png(name='shot.png'):
    return SimpleUploadedFile(name, b'\x89PNG\r\n\x1a\nfake', content_type='image/png')


class TeamleadReportTestCase(TestCase):
    """Два проекта своего тимлида и один чужой.

    Проекты создаются напрямую через ORM: тесты отчёта проверяют агрегацию,
    и им не нужна ни автоматика активации, ни правила подбора.
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

        self.date_from, self.date_to = default_report_period()
        self.report_url = reverse('pipeline:teamlead_report')

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

    def _make_report(self, task, review_status, **kwargs):
        return Report.objects.create(
            task=task,
            author=self.freelancer,
            content_text='Текст отчёта достаточной длины.',
            attachment=_png(),
            review_status=review_status,
            **kwargs,
        )

    def _member(self, project, user, ready_status=RoomMember.ReadyStatus.PENDING):
        return RoomMember.objects.create(
            room=project.room,
            user=user,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            ready_status=ready_status,
        )

    @staticmethod
    def _move_out_of_period(model, pk, field='created_at'):
        """Уносит объект далеко за пределы периода, минуя auto_now-поля."""
        model.objects.filter(pk=pk).update(**{field: timezone.now() - FAR_OUTSIDE})

    def _snapshot(self):
        return {model: model.objects.count() for model in WATCHED_MODELS}

    def _assert_nothing_written(self, before):
        for model in WATCHED_MODELS:
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), before[model])


# ---------------------------------------------------------------------------
# 1. Доступ
# ---------------------------------------------------------------------------


class TeamleadReportAccessTests(TeamleadReportTestCase):
    def test_report_is_forbidden_for_non_teamlead_and_foreign_project(self):
        """Отчёт — инструмент тимлида, и только по своим проектам."""
        admin = make_user(email='adm@report.test', role=User.Roles.ADMIN)

        for actor in (self.director, self.freelancer, self.manager, admin):
            with self.subTest(role=actor.role):
                with self.assertRaises(PermissionDenied):
                    self._report(user=actor)

        with self.assertRaises(PermissionDenied):
            self._report(project=self.foreign_project)

        # Свой проект тимлиду по-прежнему доступен: урезан доступ, а не отчёт.
        self.assertIn('tasks', self._report(project=self.project))


# ---------------------------------------------------------------------------
# 2-5. Период и цифры
# ---------------------------------------------------------------------------


class TeamleadReportPeriodTests(TeamleadReportTestCase):
    def test_period_cuts_tasks_reports_and_leads(self):
        """Одна граница периода режет все три группы одинаково."""
        inside_task = self._task(title='Задача внутри периода')
        outside_task = self._task(title='Задача вне периода')
        self._move_out_of_period(Task, outside_task.pk)

        self._make_report(inside_task, Report.ReviewStatus.APPROVED)
        old_report = self._make_report(inside_task, Report.ReviewStatus.APPROVED)
        self._move_out_of_period(Report, old_report.pk)

        self._lead(name='Тёплый', qualification_status=Lead.Qualification.WARM)
        old_lead = self._lead(name='Старый', qualification_status=Lead.Qualification.WARM)
        self._move_out_of_period(Lead, old_lead.pk)

        report = self._report()

        self.assertEqual(report['tasks']['created'], 1)
        self.assertEqual(report['reports']['approved'], 1)
        self.assertEqual(report['leads']['warm'], 1)

    def test_period_boundaries_are_inclusive_on_both_days(self):
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

        self.assertEqual(self._report()['tasks']['created'], 2)

    def test_report_numbers_cover_tasks_leads_and_team_readiness(self):
        """Один отчёт — три группы цифр сразу, а не три отдельных прогона."""
        self._task(title='Новая задача', status=Task.Status.NEW)
        self._task(title='Задача в работе', status=Task.Status.IN_PROGRESS)
        self._task(title='Задача на проверке', status=Task.Status.READY_FOR_REVIEW)
        self._task(title='Утверждённая задача', status=Task.Status.APPROVED)
        self._task(title='Закрытая задача', status=Task.Status.CLOSED)

        self._lead(name='Холодный', qualification_status=Lead.Qualification.COLD)
        self._lead(name='Тёплый', qualification_status=Lead.Qualification.WARM)
        self._lead(name='Тёплый второй', qualification_status=Lead.Qualification.WARM)
        self._lead(name='Горячий', qualification_status=Lead.Qualification.HOT)

        self._member(self.project, self.freelancer, RoomMember.ReadyStatus.READY)
        self._member(self.project, self.freelancer2, RoomMember.ReadyStatus.PENDING)

        report = self._report()

        self.assertEqual(report['tasks']['created'], 5)
        self.assertEqual(report['tasks']['in_progress'], 1)
        self.assertEqual(report['tasks']['ready_for_review'], 1)
        self.assertEqual(report['tasks']['approved_not_closed'], 1)
        self.assertEqual(report['tasks']['closed'], 1)

        self.assertEqual(report['leads']['cold'], 1)
        self.assertEqual(report['leads']['warm'], 2)
        self.assertEqual(report['leads']['hot'], 1)

        self.assertEqual(report['team']['ready'], 1)
        self.assertEqual(report['team']['not_ready'], 1)

    def test_handoff_counts_old_lead_handed_over_inside_the_period(self):
        """Лид создан до периода, но передан менеджеру внутри — считается."""
        lead = self._lead(name='Давний лид', qualification_status=Lead.Qualification.HOT)
        Lead.objects.filter(pk=lead.pk).update(
            created_at=timezone.now() - FAR_OUTSIDE,
            hot_handoff_at=timezone.now(),
            assigned_manager=self.manager,
        )

        leads = self._report()['leads']

        # По дате создания в период он не попадает…
        self.assertEqual(leads['hot'], 0)
        # …но передача менеджеру произошла внутри периода.
        self.assertEqual(leads['handed_to_manager'], 1)


# ---------------------------------------------------------------------------
# 6-8. Чтение остаётся чтением; контур дашборд → страница
# ---------------------------------------------------------------------------


class TeamleadReportHttpTests(TeamleadReportTestCase):
    def setUp(self):
        super().setUp()
        self._task(title='Задача для наполнения')
        self._lead(name='Лид для наполнения')
        self._member(self.project, self.freelancer, RoomMember.ReadyStatus.READY)

    def test_building_the_report_writes_nothing(self):
        before = self._snapshot()
        project_before = Project.objects.get(pk=self.project.pk)

        self._report()
        self._report(project=self.project)

        self._assert_nothing_written(before)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, project_before.status)
        self.assertEqual(self.project.input_data, project_before.input_data)

    def test_report_view_get_writes_nothing(self):
        self.client.force_login(self.teamlead)
        before = self._snapshot()

        self.client.get(self.report_url)
        self.client.get(self.report_url, {'project': str(self.project.id)})

        self._assert_nothing_written(before)

    def test_dashboard_offers_the_form_and_report_page_renders_its_blocks(self):
        """Контур целиком: форма на дашборде ведёт на живую страницу отчёта."""
        self.client.force_login(self.teamlead)

        dashboard = self.client.get(reverse('core:home'))
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn('report_form', dashboard.context)
        self.assertContains(dashboard, self.report_url)
        # Форма предлагает только свои проекты тимлида.
        choices = dashboard.context['report_form'].fields['project'].queryset
        self.assertEqual(
            {project.name for project in choices},
            {'Первый проект', 'Второй проект'},
        )

        page = self.client.get(self.report_url)
        self.assertEqual(page.status_code, 200)
        report = page.context['report']
        # View сам ничего не агрегирует — структура целиком из сервиса.
        self.assertEqual(set(report), REPORT_SECTIONS)
        self.assertEqual(report['tasks']['created'], 1)
        self.assertEqual(report['team']['ready'], 1)
