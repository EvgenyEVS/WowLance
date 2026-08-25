"""Automation & SLA: активация проекта, стартовая задача и SLA на «Обзоре».

Слой, который проверяется здесь:

* `apps.pipeline.services.ensure_start_calls_task` / `get_start_calls_task` —
  идемпотентное создание и единый поиск стартовой задачи «Начать звонки»;
* `apps.rooms.staffing.services.sync_project_activation` — переход
  STAFFING → ACTIVE вместе с этой задачей в одной транзакции;
* `rooms:room_overview` — server-driven SLA-блок и его состояния.

Правила подбора (matching) и проекция состава в слоты здесь не проверяются —
у них свои наборы тестов. В этом файле они появляются только как граничные
регрессии: этап Automation & SLA не должен был их расширить.
"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.pipeline.models import Task
from apps.pipeline.services import (
    START_CALLS_SLA,
    START_CALLS_TITLE,
    ensure_start_calls_task,
    get_start_calls_task,
)
from apps.profiles.models import FreelancerProfile
from apps.rooms.models import (
    Project,
    Room,
    RoomActivity,
    RoomFunctionSlot,
    RoomMember,
)
from apps.rooms.services import save_functional_roles_and_sync_slots
from apps.rooms.staffing.matching import CHANNEL_REQUIREMENTS
from apps.rooms.staffing.projection import (
    MANUAL_FLOW_ROLE_KEYS,
    PROJECTED_ROLE_KEYS,
)
from apps.rooms.staffing.services import (
    assign_candidate_to_slot,
    confirm_freelancer_readiness,
    is_functional_team_ready,
    sync_project_activation,
)
from apps.test_helpers import make_director, make_teamlead, make_user
from apps.users.models import User

PASSWORD = 'TestPass123!'
VIDEO_URL = 'https://youtu.be/demo-presentation'

#: Нейтральное состояние SLA-блока: команда ещё не готова, задачи нет.
IDLE_TEXT = 'SLA запустится после готовности команды'
OVERDUE_TEXT = 'SLA просрочен'


class AutomationSlaTestCase(TestCase):
    """Комната в подборе с двумя активными слотами и пулом кандидатов."""

    @classmethod
    def setUpTestData(cls):
        cls.director = make_director(email='dir-sla@example.com', password=PASSWORD)
        cls.teamlead = make_teamlead(email='tl-sla@example.com', password=PASSWORD)
        cls.outsider = make_user(
            email='out-sla@example.com',
            role=User.Roles.FREELANCER,
            password=PASSWORD,
        )
        cls.candidates = [
            make_user(
                email=f'sla-cand{index}@example.com',
                role=User.Roles.FREELANCER,
                password=PASSWORD,
                first_name=f'Кандидат{index}',
            )
            for index in range(1, 4)
        ]

    def setUp(self):
        self.client = Client()
        self.project = Project.objects.create(
            owner=self.director,
            name='Проект автоматизации',
            status=Project.Status.STAFFING,
            teamlead=self.teamlead,
        )
        self.room = Room.objects.create(project=self.project)
        RoomMember.objects.create(
            room=self.room,
            user=self.director,
            role_in_room=RoomMember.RoleInRoom.DIRECTOR,
        )
        RoomMember.objects.create(
            room=self.room,
            user=self.teamlead,
            role_in_room=RoomMember.RoleInRoom.TEAMLEAD,
        )
        self.slots = [
            RoomFunctionSlot.objects.create(
                room=self.room,
                role_key='seller',
                slot_index=index,
                required_level=RoomFunctionSlot.Grade.MIDDLE,
            )
            for index in (1, 2)
        ]

    # --- подготовка данных ------------------------------------------------

    def make_profile(self, user, **overrides):
        """Профиль, проходящий все hard filters слота."""
        fields = {
            'level': FreelancerProfile.Level.MIDDLE,
            'is_available': True,
            'is_verified': True,
            'video_url': VIDEO_URL,
            'rating': Decimal('4.50'),
            'acceptance_rate': Decimal('90.00'),
            'experience_projects': 10,
        }
        # Признаки каналов берутся из таблицы Matching Engine, а не пишутся
        # строками: канал слота и поле профиля связаны в одном месте.
        for field in CHANNEL_REQUIREMENTS.values():
            fields[field] = True
        fields.update(overrides)
        return FreelancerProfile.objects.create(user=user, **fields)

    def staff(self, count=2):
        """Сажает `count` кандидатов на слоты. Возвращает участников."""
        members = []
        for slot, user in zip(self.slots[:count], self.candidates):
            self.make_profile(user)
            members.append(assign_candidate_to_slot(slot, user, self.director))
        return members

    def make_ready(self, member):
        return confirm_freelancer_readiness(member, member.user)

    def activate(self):
        """Полный штатный путь до ACTIVE. Возвращает список участников."""
        members = self.staff()
        for member in members:
            self.make_ready(member)
        self.project.refresh_from_db()
        return members

    def start_task(self):
        return get_start_calls_task(self.project)

    def start_tasks_qs(self):
        return Task.objects.filter(project=self.project, title=START_CALLS_TITLE)

    def overview(self, user=None):
        self.client.force_login(user or self.director)
        return self.client.get(
            reverse('rooms:room_overview', kwargs={'project_id': self.project.id})
        )

    def overview_html(self, user=None):
        return self.overview(user).content.decode()


# ---------------------------------------------------------------------------
# 1. Активация и создание стартовой задачи
# ---------------------------------------------------------------------------


class StartCallsActivationTests(AutomationSlaTestCase):
    def test_partial_readiness_creates_no_task(self):
        """1. Готов не весь состав — задачи нет и проект остаётся в подборе."""
        members = self.staff()
        self.make_ready(members[0])

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertIsNone(self.start_task())
        self.assertEqual(self.start_tasks_qs().count(), 0)

    def test_all_slots_ready_activate_project(self):
        """2. Все активные слоты READY — проект становится ACTIVE."""
        self.activate()

        self.assertEqual(self.project.status, Project.Status.ACTIVE)

    def test_activation_creates_exactly_one_start_task(self):
        """3. Активация создаёт ровно одну задачу «Начать звонки»."""
        self.activate()

        self.assertEqual(self.start_tasks_qs().count(), 1)
        self.assertEqual(self.start_task().title, START_CALLS_TITLE)

    def test_start_task_type_is_onboarding(self):
        """4. Структурный ключ задачи — тип ONBOARDING, а не только название."""
        self.activate()

        self.assertEqual(self.start_task().task_type, Task.TaskType.ONBOARDING)

    def test_start_task_does_not_require_report(self):
        """5. Отчёт со скрином для стартовой задачи не обязателен."""
        self.activate()

        self.assertFalse(self.start_task().report_required)

    def test_start_task_has_no_creator(self):
        """6. Задача системная: `created_by` пуст, автора-человека у неё нет."""
        self.activate()

        self.assertIsNone(self.start_task().created_by)

    def test_assignee_is_teamlead_when_assigned(self):
        """7. Тимлид назначен — задача его."""
        self.activate()

        self.assertEqual(self.start_task().assignee_id, self.teamlead.id)

    def test_assignee_falls_back_to_owner_without_teamlead(self):
        """8. Тимлида нет — задача уходит директору (owner NOT NULL)."""
        RoomMember.objects.filter(room=self.room, user=self.teamlead).delete()
        self.project.teamlead = None
        self.project.save(update_fields=['teamlead'])

        self.activate()

        self.assertEqual(self.start_task().assignee_id, self.director.id)

    def test_deadline_is_timezone_aware(self):
        """9. Дедлайн timezone-aware: сравнения с `timezone.now()` корректны."""
        self.activate()

        deadline = self.start_task().deadline
        self.assertIsNotNone(deadline)
        self.assertIsNotNone(deadline.tzinfo)
        self.assertIsNotNone(deadline.utcoffset())

    def test_deadline_is_created_at_plus_sla(self):
        """10. Дедлайн = момент создания задачи + 24 часа."""
        self.activate()

        task = self.start_task()
        self.assertEqual(START_CALLS_SLA, timedelta(hours=24))
        self.assertAlmostEqual(
            task.deadline,
            task.created_at + START_CALLS_SLA,
            delta=timedelta(seconds=5),
        )

    def test_task_created_activity_is_written_once(self):
        """11. В ленту комнаты попадает одно событие TASK_CREATED."""
        self.activate()

        events = RoomActivity.objects.filter(
            room=self.room,
            event_type=RoomActivity.EventType.TASK_CREATED,
        )
        self.assertEqual(events.count(), 1)
        self.assertIn(START_CALLS_TITLE, events.first().message)


# ---------------------------------------------------------------------------
# 2. Идемпотентность
# ---------------------------------------------------------------------------


class StartCallsIdempotencyTests(AutomationSlaTestCase):
    def test_repeated_readiness_keeps_single_task(self):
        """12. Повторное подтверждение готовности не создаёт вторую задачу."""
        members = self.activate()

        for member in members:
            self.make_ready(member)
            self.make_ready(member)

        self.assertEqual(self.start_tasks_qs().count(), 1)

    def test_repeated_readiness_does_not_move_deadline(self):
        """13. Повторная готовность не сдвигает уже сохранённый дедлайн."""
        members = self.activate()
        deadline = self.start_task().deadline

        for member in members:
            self.make_ready(member)

        self.assertEqual(self.start_task().deadline, deadline)

    def test_repeated_sync_keeps_single_task(self):
        """14. Повторный `sync_project_activation` — без второго перехода."""
        self.activate()

        self.assertFalse(sync_project_activation(self.project, actor=self.director))
        self.assertFalse(sync_project_activation(self.project, actor=self.director))

        self.assertEqual(self.start_tasks_qs().count(), 1)
        self.assertEqual(
            RoomActivity.objects.filter(
                room=self.room,
                event_type=RoomActivity.EventType.TASK_CREATED,
            ).count(),
            1,
        )

    def test_manual_rollback_to_staffing_reuses_existing_task(self):
        """15. Ручной откат ACTIVE → STAFFING не плодит вторую задачу.

        Статус проекта редактируется в админке, поэтому «переход случился
        один раз» не является достаточной защитой: ключ идемпотентности
        живёт на самой задаче.
        """
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

    def test_ensure_service_is_idempotent_on_its_own(self):
        """15b. Сам сервис при прямом повторном вызове ничего не дублирует."""
        self.activate()
        first = self.start_task()

        task, created = ensure_start_calls_task(self.project, actor=self.director)

        self.assertFalse(created)
        self.assertEqual(task.id, first.id)
        self.assertEqual(task.deadline, first.deadline)
        self.assertEqual(self.start_tasks_qs().count(), 1)

    def test_get_start_calls_task_finds_exactly_this_task(self):
        """16. Единый helper находит именно стартовую задачу проекта."""
        self.activate()
        created_task = Task.objects.get(
            project=self.project,
            task_type=Task.TaskType.ONBOARDING,
            title=START_CALLS_TITLE,
        )
        # Посторонние задачи проекта helper не подхватывает.
        Task.objects.create(
            project=self.project,
            assignee=self.teamlead,
            title='Первые звонки по базе',
            task_type=Task.TaskType.WORK,
        )

        self.assertEqual(get_start_calls_task(self.project).id, created_task.id)

    def test_get_start_calls_task_returns_none_before_activation(self):
        """16b. Пока задачи нет, helper честно возвращает None и не создаёт её."""
        self.assertIsNone(get_start_calls_task(self.project))
        self.assertEqual(Task.objects.filter(project=self.project).count(), 0)


# ---------------------------------------------------------------------------
# 3. Транзакционная граница
# ---------------------------------------------------------------------------


class StartCallsAtomicityTests(AutomationSlaTestCase):
    """Активация, стартовая задача и готовность коммитятся вместе или никак.

    Падение создания задачи имитируется подменой сервиса: он импортируется
    внутри `sync_project_activation`, поэтому patch модульного атрибута
    перехватывает именно тот вызов, который делает оркестрация.
    """

    FAILURE = 'создание стартовой задачи упало'

    def _broken_task_creation(self):
        return mock.patch(
            'apps.pipeline.services.ensure_start_calls_task',
            side_effect=RuntimeError(self.FAILURE),
        )

    def test_task_failure_rolls_back_activation(self):
        """17. Задача не создалась — проект не остался ACTIVE."""
        members = self.staff()
        self.make_ready(members[0])

        with self._broken_task_creation():
            with self.assertRaises(RuntimeError):
                self.make_ready(members[1])

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertEqual(self.start_tasks_qs().count(), 0)

    def test_task_failure_rolls_back_member_readiness(self):
        """18. Откатывается и готовность участника, вызвавшего активацию."""
        members = self.staff()
        self.make_ready(members[0])

        with self._broken_task_creation():
            with self.assertRaises(RuntimeError):
                self.make_ready(members[1])

        members[1].refresh_from_db()
        self.assertEqual(members[1].ready_status, RoomMember.ReadyStatus.PENDING)

    def test_task_failure_leaves_no_partial_activity(self):
        """19. Ни события активации, ни события задачи в ленте не остаётся."""
        members = self.staff()
        self.make_ready(members[0])
        ready_before = RoomActivity.objects.filter(
            room=self.room, event_type=RoomActivity.EventType.READY,
        ).count()

        with self._broken_task_creation():
            with self.assertRaises(RuntimeError):
                self.make_ready(members[1])

        self.assertEqual(
            RoomActivity.objects.filter(
                room=self.room, event_type=RoomActivity.EventType.READY,
            ).count(),
            ready_before,
        )
        self.assertEqual(
            RoomActivity.objects.filter(
                room=self.room, event_type=RoomActivity.EventType.TASK_CREATED,
            ).count(),
            0,
        )

    def test_recovery_after_failure_still_activates_once(self):
        """19b. После починки штатный путь доводит проект до ACTIVE.

        Откат не должен оставлять систему в состоянии, из которого активация
        больше невозможна: переход STAFFING → ACTIVE случается позже, но
        ровно один раз.

        Повтор моделируется как новый запрос: участник перечитывается из БД.
        Это не формальность — после отката объект в памяти остаётся с
        `READY`, которого в базе уже нет, и повторное подтверждение по
        такому объекту ничего бы не записало. В продукте каждый POST берёт
        участника заново (`views.room_confirm_ready`), поэтому состояния
        «залипшего» объекта там не существует.
        """
        members = self.staff()
        self.make_ready(members[0])
        with self._broken_task_creation():
            with self.assertRaises(RuntimeError):
                self.make_ready(members[1])

        members[1].refresh_from_db()
        self.make_ready(members[1])

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)
        self.assertEqual(self.start_tasks_qs().count(), 1)


# ---------------------------------------------------------------------------
# 4. Регрессия семантики готовности
# ---------------------------------------------------------------------------


class ReadinessRegressionTests(AutomationSlaTestCase):
    def test_empty_active_slot_blocks_activation(self):
        """20. Пустой активный слот блокирует активацию и задачу."""
        members = self.staff(count=1)
        self.make_ready(members[0])

        self.project.refresh_from_db()
        self.assertFalse(is_functional_team_ready(self.room))
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertEqual(self.start_tasks_qs().count(), 0)

    def test_pending_member_blocks_activation(self):
        """21. Занятый, но не подтверждённый слот блокирует."""
        members = self.staff()
        self.make_ready(members[0])

        self.assertEqual(
            RoomMember.objects.get(pk=members[1].pk).ready_status,
            RoomMember.ReadyStatus.PENDING,
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertEqual(self.start_tasks_qs().count(), 0)

    def test_all_ready_passes(self):
        """22. Полная готовность — активация и одна задача."""
        self.activate()

        self.assertTrue(is_functional_team_ready(self.room))
        self.assertEqual(self.project.status, Project.Status.ACTIVE)
        self.assertEqual(self.start_tasks_qs().count(), 1)

    def test_room_without_active_slots_never_activates(self):
        """23. Нет активных слотов — нет активации и нет SLA-задачи."""
        for slot in self.slots:
            slot.is_active = False
            slot.save(update_fields=['is_active'])

        self.assertFalse(is_functional_team_ready(self.room))
        self.assertFalse(sync_project_activation(self.project, actor=self.director))
        self.assertEqual(self.start_tasks_qs().count(), 0)

    def test_teamlead_does_not_block_activation(self):
        """24. Тимлид по-прежнему вне слотов и не мешает активации.

        Его `ready_status` остаётся PENDING — семантика готовности тимлида в
        этом этапе не менялась.
        """
        self.activate()

        teamlead_member = RoomMember.objects.get(room=self.room, user=self.teamlead)
        self.assertEqual(teamlead_member.ready_status, RoomMember.ReadyStatus.PENDING)
        self.assertIsNone(teamlead_member.function_slot)
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

    def test_teamlead_still_cannot_confirm_freelancer_readiness(self):
        """24b. Кнопка готовности осталась фрилансерской."""
        from apps.rooms.staffing.services import StaffingError

        teamlead_member = RoomMember.objects.get(room=self.room, user=self.teamlead)

        with self.assertRaises(StaffingError):
            confirm_freelancer_readiness(teamlead_member, self.teamlead)

    def test_readiness_is_confirmed_only_by_the_member(self):
        """24c. Директор не может подтвердить готовность за исполнителя."""
        members = self.staff(count=1)

        with self.assertRaises(PermissionDenied):
            confirm_freelancer_readiness(members[0], self.director)

    def test_database_assistant_only_composition_creates_no_sla_task(self):
        """25. Состав «тимлид + ассистент базы» не даёт исполнительских слотов.

        Ни одна из этих функций не проецируется в `RoomFunctionSlot`, поэтому
        активных слотов у комнаты нет, проект остаётся в подборе, а SLA-задача
        не создаётся. Base-matching этот этап не чинит — тест фиксирует
        честное поведение, а не желаемое.
        """
        project = Project.objects.create(
            owner=self.director,
            name='Только база',
            status=Project.Status.STAFFING,
        )
        Room.objects.create(project=project)

        save_functional_roles_and_sync_slots(
            project,
            [
                {'role_key': 'teamlead', 'count': 1},
                {'role_key': 'database_assistant', 'count': 1},
            ],
            self.director,
        )
        project.refresh_from_db()

        self.assertEqual(
            project.room.function_slots.filter(is_active=True).count(), 0
        )
        self.assertFalse(is_functional_team_ready(project.room))
        self.assertFalse(sync_project_activation(project, actor=self.director))
        self.assertEqual(project.status, Project.Status.STAFFING)
        self.assertIsNone(get_start_calls_task(project))


# ---------------------------------------------------------------------------
# 5. SLA-блок на «Обзоре»
# ---------------------------------------------------------------------------


class OverviewSlaTests(AutomationSlaTestCase):
    def sla_block(self, html):
        """Разметка самого SLA-блока, без остальной страницы.

        Проверять состояние по всему HTML нельзя: скрипт обратного отсчёта
        содержит и селектор `data-sla-deadline`, и класс `sla-overdue`, и
        текст просрочки — это его работа. Состояние определяется блоком,
        который отдал сервер, поэтому тесты смотрят именно на него.
        """
        start = html.index('class="sla-banner')
        return html[start:html.index('</div>', start)]

    def fr_staffing_cells(self, html):
        """Все ячейки подбора таблицы юнит-экономики."""
        cells = []
        marker = 'class="fr-staffing '
        start = html.find(marker)
        while start != -1:
            end = html.index('</td>', start)
            cells.append(html[start:end])
            start = html.find(marker, end)
        return cells

    def test_staffing_without_task_shows_neutral_state(self):
        """26. Проект в подборе: нейтральное состояние, без таймера."""
        html = self.overview_html()
        block = self.sla_block(html)

        self.assertIn(IDLE_TEXT, block)
        self.assertNotIn('data-sla-deadline=', html)
        self.assertNotIn(OVERDUE_TEXT, block)
        # Мёртвого скрипта на странице без таймера тоже нет.
        self.assertNotIn('data-sla-countdown', html)

    def test_active_project_shows_deadline(self):
        """27. После активации виден дедлайн стартовой задачи."""
        self.activate()
        block = self.sla_block(self.overview_html())

        self.assertIn(START_CALLS_TITLE, block)
        self.assertIn('data-sla-deadline=', block)
        self.assertNotIn(IDLE_TEXT, block)

    def test_absolute_deadline_comes_from_the_database(self):
        """28. Показанная дата — сохранённый `Task.deadline`, а не «сейчас»."""
        self.activate()
        deadline = self.start_task().deadline
        expected = timezone.localtime(deadline).strftime('%d.%m.%Y %H:%M')

        self.assertIn(expected, self.overview_html())

    def test_two_gets_do_not_change_the_deadline(self):
        """29. GET ничего не пересчитывает и не создаёт."""
        self.activate()
        before = self.start_task().deadline

        first = self.overview_html()
        second = self.overview_html()

        self.assertEqual(self.start_task().deadline, before)
        self.assertEqual(self.start_tasks_qs().count(), 1)
        # Оба ответа несут один и тот же машинный дедлайн.
        machine = timezone.localtime(before).isoformat()
        self.assertIn(machine, first)
        self.assertIn(machine, second)

    def test_future_deadline_shows_active_sla_state(self):
        """30. Дедлайн впереди — активное состояние, без отметки просрочки."""
        self.activate()
        block = self.sla_block(self.overview_html())

        self.assertIn('sla-active', block)
        self.assertNotIn('sla-overdue', block)
        self.assertNotIn(OVERDUE_TEXT, block)

    def test_expired_deadline_shows_overdue_state(self):
        """31. Дедлайн в прошлом — просрочка считается на сервере."""
        self.activate()
        past = timezone.now() - timedelta(hours=1)
        self.start_tasks_qs().update(deadline=past)

        block = self.sla_block(self.overview_html())

        self.assertIn('sla-overdue', block)
        self.assertNotIn('sla-active', block)
        self.assertIn(OVERDUE_TEXT, block)
        # Дедлайн остаётся видимым и в просроченном состоянии.
        self.assertIn(timezone.localtime(past).strftime('%d.%m.%Y %H:%M'), block)

    def test_closed_task_shows_completed_state(self):
        """32. Закрытая задача — завершённое состояние без таймера."""
        self.activate()
        self.start_tasks_qs().update(status=Task.Status.CLOSED)

        html = self.overview_html()
        block = self.sla_block(html)

        self.assertIn('sla-done', block)
        self.assertIn('закрыта', block)
        self.assertNotIn(OVERDUE_TEXT, block)
        # Отсчитывать нечего: ни атрибута дедлайна, ни скрипта на странице.
        self.assertNotIn('data-sla-deadline=', html)
        self.assertNotIn('data-sla-countdown', html)

    def test_wording_never_claims_a_call_happened(self):
        """33. Формулировки не выдают закрытие задачи за состоявшийся звонок."""
        self.activate()
        self.start_tasks_qs().update(status=Task.Status.CLOSED)

        html = self.overview_html()

        self.assertNotIn('звонок состоялся', html)
        self.assertNotIn('Первый звонок', html)
        self.assertNotIn('первые звонки в течение', html)
        self.assertIn('не факт состоявшегося звонка', self.sla_block(html))

    def test_countdown_is_not_inside_the_unit_economics_cell(self):
        """34. Таймер живёт в SLA-блоке, а не в ячейке подбора таблицы."""
        self.activate()
        html = self.overview_html()

        self.assertIn('data-sla-countdown', html)
        for cell in self.fr_staffing_cells(html):
            self.assertNotIn('data-sla-countdown', cell)
            self.assertNotIn('SLA', cell)

    def test_data_deadline_matches_the_stored_task_deadline(self):
        """35. Машинный дедлайн в разметке совпадает с сохранённым в БД."""
        self.activate()
        deadline = self.start_task().deadline

        html = self.overview_html()

        self.assertIn(
            f'data-sla-deadline="{timezone.localtime(deadline).isoformat()}"',
            html,
        )

    def test_freelancer_member_sees_the_same_server_state(self):
        """35b. SLA-блок доступен участникам обычным доступом к «Обзору».

        Отдельного endpoint под SLA нет: это часть страницы комнаты.
        """
        members = self.activate()
        deadline = self.start_task().deadline

        block = self.sla_block(self.overview_html(user=members[0].user))

        self.assertIn(
            f'data-sla-deadline="{timezone.localtime(deadline).isoformat()}"',
            block,
        )

    def test_outsider_still_has_no_access(self):
        """35c. Появление SLA-блока не открыло комнату посторонним."""
        self.activate()
        self.client.force_login(self.outsider)

        response = self.client.get(
            reverse('rooms:room_overview', kwargs={'project_id': self.project.id})
        )

        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# 6. Границы этапа
# ---------------------------------------------------------------------------


class AutomationSlaBoundaryTests(AutomationSlaTestCase):
    def test_readiness_post_ignores_client_supplied_task_fields(self):
        """36. Клиент не может задать дедлайн, название или исполнителя."""
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

    def test_matching_contract_is_unchanged(self):
        """37. Automation & SLA не расширил правила подбора.

        В частности, канал `base` ассистента базы в требованиях подбора
        по-прежнему отсутствует: base-matching — отдельный этап.

        Имена полей профиля здесь намеренно не выписаны строками: они живут
        только в `matching.py`, и это отдельно зафиксировано тестом
        `MatchingBoundaryTests.test_channel_filters_live_only_in_matching_module`.
        """
        self.assertEqual(
            set(CHANNEL_REQUIREMENTS),
            {
                RoomFunctionSlot.Channel.COLD_CALLING,
                RoomFunctionSlot.Channel.LINKEDIN,
            },
        )
        self.assertNotIn('base', RoomFunctionSlot.Channel.values)

    def test_projection_contract_is_unchanged(self):
        """38. Набор проецируемых функций и ручной поток тимлида не тронуты."""
        self.assertEqual(
            PROJECTED_ROLE_KEYS,
            frozenset({'seller_middle', 'seller_senior', 'linkedin_leadgen'}),
        )
        self.assertEqual(MANUAL_FLOW_ROLE_KEYS, frozenset({'teamlead'}))

    def test_no_new_migrations_are_required_for_rooms(self):
        """39. Схема `rooms` не менялась."""
        self._assert_no_migrations('rooms')

    def test_no_new_migrations_are_required_for_pipeline(self):
        """40. Стартовая задача обходится существующей схемой `pipeline`."""
        self._assert_no_migrations('pipeline')

    def _assert_no_migrations(self, app_label):
        out = StringIO()
        try:
            call_command(
                'makemigrations', app_label, '--check', '--dry-run', stdout=out
            )
        except SystemExit as exc:  # pragma: no cover - падает только при регрессии
            self.fail(
                f'Появились несозданные миграции {app_label}:\n{out.getvalue()}\n{exc}'
            )
