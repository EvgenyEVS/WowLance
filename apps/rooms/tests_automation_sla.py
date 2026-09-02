"""Automation & SLA: активация проекта, стартовая задача и её дедлайн.

Проверяется один сквозной автомат:

* `apps.rooms.staffing.services.sync_project_activation` — переход
  STAFFING → ACTIVE вместе со стартовой задачей в одной транзакции;
* `apps.pipeline.services.ensure_start_calls_task` / `get_start_calls_task` —
  идемпотентное создание задачи «Начать звонки» и SLA 24 часа;
* `rooms:room_overview` — серверное состояние SLA в контексте страницы.

Актор в фикстуре не произвольный: ручной подбор на слот делает **тимлид**
проекта (`user_can_manage_team`), автоподбор при покупке состава —
**владелец-директор** (`user_can_edit_functional_roles`). Здесь всегда
первый предикат. Готовность подтверждает сам участник.

Семантика готовности и правила подбора живут в `tests_staffing`,
состав — в `tests_composition`; здесь они не дублируются.
"""

from datetime import timedelta
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.pipeline.models import Task
from apps.pipeline.services import (
    START_CALLS_SLA,
    START_CALLS_TITLE,
    get_start_calls_task,
)
from apps.rooms.models import Project, RoomMember
from apps.rooms.staffing.services import (
    assign_candidate_to_slot,
    confirm_freelancer_readiness,
    sync_project_activation,
)
from apps.test_helpers import make_staffed_project


class AutomationSlaTestCase(TestCase):
    """Комната в подборе: два активных слота и два годных кандидата."""

    def setUp(self):
        fixture = make_staffed_project(slots=2, candidates=2)
        self.project = fixture.project
        self.room = fixture.room
        self.director = fixture.director
        self.teamlead = fixture.teamlead
        self.slots = fixture.slots
        self.candidates = fixture.candidates
        self.client = Client()

    def staff(self, count=2, fixture=None):
        """Сажает кандидатов на слоты. Ручной подбор — действие тимлида."""
        slots = fixture.slots if fixture else self.slots
        candidates = fixture.candidates if fixture else self.candidates
        actor = fixture.teamlead if fixture else self.teamlead
        return [
            assign_candidate_to_slot(slot, user, actor)
            for slot, user in zip(slots[:count], candidates[:count])
        ]

    def make_ready(self, member):
        """Готовность подтверждает только сам участник слота."""
        return confirm_freelancer_readiness(member, member.user)

    def activate(self):
        """Штатный путь до ACTIVE. Возвращает участников слотов."""
        members = self.staff()
        for member in members:
            self.make_ready(member)
        self.project.refresh_from_db()
        return members

    def start_task(self, project=None):
        return get_start_calls_task(project or self.project)

    def start_tasks_qs(self, project=None):
        return Task.objects.filter(
            project=project or self.project,
            title=START_CALLS_TITLE,
        )

    def overview(self, user):
        self.client.force_login(user)
        return self.client.get(
            reverse('rooms:room_overview', kwargs={'project_id': self.project.id})
        )


class StartCallsActivationTests(AutomationSlaTestCase):
    """Готовность команды поднимает проект и заводит стартовую задачу."""

    def test_readiness_activates_project_and_creates_start_task_with_24h_sla(self):
        self.activate()

        self.assertEqual(self.project.status, Project.Status.ACTIVE)
        self.assertEqual(self.start_tasks_qs().count(), 1)

        task = self.start_task()
        self.assertEqual(task.title, START_CALLS_TITLE)
        self.assertEqual(task.task_type, Task.TaskType.ONBOARDING)
        self.assertEqual(START_CALLS_SLA, timedelta(hours=24))
        self.assertAlmostEqual(
            task.deadline,
            task.created_at + START_CALLS_SLA,
            delta=timedelta(seconds=5),
        )

    def test_partial_readiness_creates_no_task(self):
        members = self.staff()
        self.make_ready(members[0])

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertIsNone(self.start_task())
        self.assertEqual(self.start_tasks_qs().count(), 0)

    def test_start_task_assignee_is_teamlead_or_falls_back_to_owner(self):
        with self.subTest(case='teamlead assigned'):
            self.activate()
            self.assertEqual(self.start_task().assignee_id, self.teamlead.id)

        with self.subTest(case='no teamlead'):
            # Отдельная комната: подбор делает её тимлид, а к моменту
            # готовности проект остаётся без тимлида (поле nullable).
            fixture = make_staffed_project(slots=2, candidates=2, prefix='no-tl-')
            members = self.staff(fixture=fixture)
            RoomMember.objects.filter(
                room=fixture.room,
                user=fixture.teamlead,
            ).delete()
            Project.objects.filter(pk=fixture.project.pk).update(teamlead=None)
            fixture.project.refresh_from_db()

            for member in members:
                self.make_ready(member)

            fixture.project.refresh_from_db()
            self.assertEqual(fixture.project.status, Project.Status.ACTIVE)
            self.assertEqual(
                self.start_task(fixture.project).assignee_id,
                fixture.director.id,
            )


class StartCallsIdempotencyTests(AutomationSlaTestCase):
    """Повтор готовности и ручной откат статуса не плодят вторую задачу."""

    def test_repeated_readiness_keeps_single_task_and_deadline(self):
        members = self.activate()
        deadline = self.start_task().deadline

        for member in members:
            self.make_ready(member)
            self.make_ready(member)

        self.assertEqual(self.start_tasks_qs().count(), 1)
        self.assertEqual(self.start_task().deadline, deadline)

    def test_manual_rollback_to_staffing_reuses_the_existing_task(self):
        """Статус проекта правят в админке, поэтому «переход случился один
        раз» не защищает: ключ идемпотентности живёт на самой задаче."""
        self.activate()
        task_id = self.start_task().id
        deadline = self.start_task().deadline

        Project.objects.filter(pk=self.project.pk).update(
            status=Project.Status.STAFFING,
        )
        self.project.refresh_from_db()
        self.assertTrue(sync_project_activation(self.project, actor=self.director))

        self.assertEqual(self.start_tasks_qs().count(), 1)
        self.assertEqual(self.start_task().id, task_id)
        self.assertEqual(self.start_task().deadline, deadline)


class StartCallsServerStateTests(AutomationSlaTestCase):
    """Поля задачи и состояние SLA считает сервер, а не клиент."""

    def test_readiness_post_ignores_client_supplied_task_fields(self):
        members = self.staff()
        self.make_ready(members[0])

        self.client.force_login(members[1].user)
        forged_deadline = (timezone.now() + timedelta(days=365)).isoformat()
        response = self.client.post(
            reverse('rooms:room_confirm_ready', kwargs={'project_id': self.project.id}),
            {
                'deadline': forged_deadline,
                'title': 'Взломанная задача',
                'assignee': str(members[1].user.id),
                'task_type': Task.TaskType.WORK,
                'report_required': 'on',
            },
        )
        self.assertEqual(response.status_code, 302)

        task = self.start_task()
        self.assertIsNotNone(task)
        self.assertEqual(task.title, START_CALLS_TITLE)
        self.assertEqual(task.task_type, Task.TaskType.ONBOARDING)
        self.assertEqual(task.assignee_id, self.teamlead.id)
        self.assertFalse(task.report_required)
        self.assertLess(task.deadline, timezone.now() + timedelta(hours=25))

    def test_expired_deadline_shows_server_computed_overdue(self):
        self.activate()

        # Дедлайн ещё впереди: просрочки нет.
        fresh = self.overview(self.teamlead)
        self.assertEqual(fresh.status_code, 200)
        self.assertFalse(fresh.context['start_calls_is_overdue'])
        self.assertFalse(fresh.context['start_calls_is_done'])

        self.start_tasks_qs().update(deadline=timezone.now() - timedelta(hours=1))

        expired = self.overview(self.teamlead)
        self.assertTrue(expired.context['start_calls_is_overdue'])
        self.assertFalse(expired.context['start_calls_is_done'])
        self.assertEqual(
            expired.context['start_calls_deadline'],
            self.start_task().deadline,
        )


class StartCallsAtomicityTests(AutomationSlaTestCase):
    """Активация, задача и готовность коммитятся вместе или никак.

    Падение создания задачи имитируется подменой сервиса: он импортируется
    внутри `sync_project_activation`, поэтому patch модульного атрибута
    перехватывает именно тот вызов, который делает оркестрация.
    """

    def test_task_failure_rolls_back_activation(self):
        members = self.staff()
        self.make_ready(members[0])

        with mock.patch(
            'apps.pipeline.services.ensure_start_calls_task',
            side_effect=RuntimeError('создание стартовой задачи упало'),
        ):
            with self.assertRaises(RuntimeError):
                self.make_ready(members[1])

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertEqual(self.start_tasks_qs().count(), 0)
        # Полусохранённого состояния не остаётся: готовность тоже откатилась.
        members[1].refresh_from_db()
        self.assertEqual(members[1].ready_status, RoomMember.ReadyStatus.PENDING)
