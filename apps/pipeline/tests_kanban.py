"""Канбаны ROOM: четыре колонки задач и три колонки лидов (Issue #11).

Проверяется только раскладка и её отображение. Правила переходов статусов
(взять в работу, сдать отчёт, проверка, quality gate, закрытие, передача
Hot-лида менеджеру) живут в `apps.pipeline.services`, покрыты
`apps/pipeline/tests.py` и этим этапом не менялись — здесь они появляются
только регрессиями «не сломалось».

Никаких новых статусов и миграций за этими тестами не стоит:
`Task.Status` и `Lead.Qualification` используются как есть.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.rooms.models import Project
from apps.rooms.services import add_freelancer_to_room, ensure_room_for_project
from apps.test_helpers import make_director, make_freelancer, make_teamlead, make_user
from apps.users.models import User

from .kanban import lead_columns, task_columns
from .models import Lead, Report, Task
from .services import close_task, review_report, set_lead_qualification, submit_report

#: Продуктовый порядок колонок доски задач (Issue #11).
EXPECTED_TASK_TITLES = ['К работе', 'В работе', 'На проверке', 'Готово']

#: Продуктовый порядок колонок доски лидов.
EXPECTED_LEAD_TITLES = ['Cold', 'Warm', 'Hot']


class KanbanTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir@kanban.test')
        self.teamlead = make_teamlead(email='tl@kanban.test')
        self.freelancer = make_freelancer(email='fr@kanban.test')
        self.manager = make_user(email='mgr@kanban.test', role=User.Roles.MANAGER)

        self.project = Project.objects.create(
            owner=self.director,
            name='Проект досок',
            status=Project.Status.ACTIVE,
            teamlead=self.teamlead,
            input_data={'hot_criteria': 'Запросил демо'},
        )
        self.room = ensure_room_for_project(self.project)
        add_freelancer_to_room(self.room, self.freelancer)

        self.tasks_url = reverse('pipeline:room_tasks', args=[self.project.id])
        self.leads_url = reverse('pipeline:room_leads', args=[self.project.id])

    def make_task(self, title, status=Task.Status.NEW):
        return Task.objects.create(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.director,
            title=title,
            status=status,
        )

    def make_lead(self, name, qualification=Lead.Qualification.COLD):
        return Lead.objects.create(
            project=self.project,
            creator=self.freelancer,
            contact_info={'name': name},
            source=Lead.Source.BASE,
            qualification_status=qualification,
        )


# ---------------------------------------------------------------------------
# 8. Доска задач: четыре колонки
# ---------------------------------------------------------------------------


class TaskKanbanColumnsTests(KanbanTestCase):
    def columns_by_key(self, tasks):
        return {column['key']: column for column in task_columns(tasks)}

    def test_four_columns_in_product_order(self):
        self.assertEqual(
            [column['title'] for column in task_columns([])], EXPECTED_TASK_TITLES
        )

    def test_new_goes_to_todo(self):
        task = self.make_task('Новая задача', Task.Status.NEW)
        self.assertEqual(self.columns_by_key([task])['todo']['tasks'], [task])

    def test_in_progress_has_its_own_column(self):
        task = self.make_task('Задача в работе', Task.Status.IN_PROGRESS)
        columns = self.columns_by_key([task])
        self.assertEqual(columns['in_progress']['tasks'], [task])
        self.assertEqual(columns['todo']['tasks'], [])

    def test_ready_for_review_goes_to_review(self):
        task = self.make_task('На проверку', Task.Status.READY_FOR_REVIEW)
        self.assertEqual(self.columns_by_key([task])['review']['tasks'], [task])

    def test_completed_states_go_to_done(self):
        approved = self.make_task('Утверждённая', Task.Status.APPROVED)
        closed = self.make_task('Закрытая', Task.Status.CLOSED)
        columns = self.columns_by_key([approved, closed])
        self.assertEqual(columns['done']['tasks'], [approved, closed])

    def test_rejected_returns_to_todo(self):
        """Отклонённую задачу исполнитель берёт заново — это снова работа."""
        task = self.make_task('Отклонённая', Task.Status.REJECTED)
        self.assertEqual(self.columns_by_key([task])['todo']['tasks'], [task])

    def test_every_status_lands_in_exactly_one_column(self):
        """Раскладка — разбиение: задача не может показаться дважды."""
        tasks = [
            self.make_task(f'Задача {index}', status)
            for index, status in enumerate(Task.Status.values)
        ]
        placed = [task for column in task_columns(tasks) for task in column['tasks']]
        self.assertEqual(len(placed), len(tasks))
        self.assertEqual({task.id for task in placed}, {task.id for task in tasks})

    def test_page_renders_four_column_headers(self):
        self.make_task('Видимая задача')
        self.client.force_login(self.teamlead)
        response = self.client.get(self.tasks_url)
        for title in EXPECTED_TASK_TITLES:
            with self.subTest(title=title):
                self.assertContains(response, title)

    def test_page_columns_come_from_the_shared_definition(self):
        self.make_task('Видимая задача')
        self.client.force_login(self.teamlead)
        titles = [
            column['title']
            for column in self.client.get(self.tasks_url).context['kanban_columns']
        ]
        self.assertEqual(titles, EXPECTED_TASK_TITLES)

    def test_task_and_overview_boards_use_the_same_columns(self):
        """Обе страницы берут колонки из одного места и не расходятся."""
        self.make_task('Видимая задача')
        self.client.force_login(self.teamlead)
        tasks_page = self.client.get(self.tasks_url).context['kanban_columns']
        overview = self.client.get(
            reverse('rooms:room_overview', args=[self.project.id])
        ).context['kanban_preview']
        self.assertEqual(
            [column['title'] for column in tasks_page],
            [column['title'] for column in overview],
        )


class TaskWorkflowRegressionTests(KanbanTestCase):
    """Существующий workflow задач досками не задет."""

    def make_report(self, task):
        return submit_report(
            task=task,
            author=self.freelancer,
            content_text='Отчёт по задаче, десять символов и больше.',
            attachment=SimpleUploadedFile(
                'screen.png', b'screenshot', content_type='image/png'
            ),
        )

    def test_report_submit_moves_task_to_review_column(self):
        task = self.make_task('Задача с отчётом', Task.Status.IN_PROGRESS)
        self.make_report(task)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.READY_FOR_REVIEW)
        columns = {column['key']: column['tasks'] for column in task_columns([task])}
        self.assertEqual(columns['review'], [task])

    def test_approved_report_moves_task_to_done_column(self):
        task = self.make_task('Задача на утверждение', Task.Status.IN_PROGRESS)
        report = self.make_report(task)
        review_report(report=report, reviewer=self.teamlead, approve=True)
        task.refresh_from_db()
        columns = {column['key']: column['tasks'] for column in task_columns([task])}
        self.assertEqual(columns['done'], [task])

    def test_quality_gate_still_blocks_closing_without_an_approved_report(self):
        task = self.make_task('Без отчёта', Task.Status.IN_PROGRESS)
        with self.assertRaises(Exception):
            close_task(task, self.teamlead)
        task.refresh_from_db()
        self.assertNotEqual(task.status, Task.Status.CLOSED)

    def test_close_task_after_approval_still_works(self):
        task = self.make_task('Полный цикл', Task.Status.IN_PROGRESS)
        report = self.make_report(task)
        review_report(report=report, reviewer=self.teamlead, approve=True)
        task.refresh_from_db()
        close_task(task, self.teamlead)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.CLOSED)
        self.assertEqual(report.task.reports.first().review_status,
                         Report.ReviewStatus.APPROVED)

    def test_rejected_report_returns_the_task_to_the_first_column(self):
        task = self.make_task('Отклонить отчёт', Task.Status.IN_PROGRESS)
        report = self.make_report(task)
        review_report(
            report=report,
            reviewer=self.teamlead,
            approve=False,
            comment='Нужны скриншоты по каждому звонку.',
        )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.REJECTED)
        columns = {column['key']: column['tasks'] for column in task_columns([task])}
        self.assertEqual(columns['todo'], [task])


# ---------------------------------------------------------------------------
# 9. Доска лидов: Cold / Warm / Hot
# ---------------------------------------------------------------------------


class LeadKanbanColumnsTests(KanbanTestCase):
    def columns_by_key(self, leads):
        return {column['key']: column for column in lead_columns(leads)}

    def test_three_columns_in_product_order(self):
        self.assertEqual(
            [column['title'] for column in lead_columns([])], EXPECTED_LEAD_TITLES
        )

    def test_cold_lead_goes_to_cold(self):
        lead = self.make_lead('Холодный', Lead.Qualification.COLD)
        self.assertEqual(self.columns_by_key([lead])['cold']['leads'], [lead])

    def test_warm_lead_goes_to_warm(self):
        lead = self.make_lead('Тёплый', Lead.Qualification.WARM)
        self.assertEqual(self.columns_by_key([lead])['warm']['leads'], [lead])

    def test_hot_lead_goes_to_hot(self):
        lead = self.make_lead('Горячий', Lead.Qualification.HOT)
        self.assertEqual(self.columns_by_key([lead])['hot']['leads'], [lead])

    def test_every_qualification_lands_in_exactly_one_column(self):
        leads = [
            self.make_lead(f'Лид {index}', status)
            for index, status in enumerate(Lead.Qualification.values)
        ]
        placed = [lead for column in lead_columns(leads) for lead in column['leads']]
        self.assertEqual({lead.id for lead in placed}, {lead.id for lead in leads})

    def test_page_renders_three_column_headers(self):
        self.make_lead('Видимый лид')
        self.client.force_login(self.director)
        response = self.client.get(self.leads_url)
        for title in EXPECTED_LEAD_TITLES:
            with self.subTest(title=title):
                self.assertContains(response, title)

    def test_page_keeps_the_useful_card_data_and_action(self):
        """Карточка не потеряла данные, которые были в строке таблицы."""
        lead = self.make_lead('Пётр Клиентов', Lead.Qualification.WARM)
        self.client.force_login(self.director)
        response = self.client.get(self.leads_url)
        self.assertContains(response, 'Пётр Клиентов')
        self.assertContains(response, lead.get_source_display())
        self.assertContains(response, self.freelancer.full_name)
        self.assertContains(
            response,
            reverse('pipeline:lead_detail', args=[self.project.id, lead.id]),
        )

    def test_page_columns_come_from_the_shared_definition(self):
        self.make_lead('Видимый лид')
        self.client.force_login(self.director)
        titles = [
            column['title']
            for column in self.client.get(self.leads_url).context['lead_columns']
        ]
        self.assertEqual(titles, EXPECTED_LEAD_TITLES)

    def test_empty_board_keeps_the_existing_empty_state(self):
        self.client.force_login(self.director)
        self.assertContains(self.client.get(self.leads_url), 'Лидов пока нет')


class HotHandoffRegressionTests(KanbanTestCase):
    """Передача Hot-лида менеджеру со сроком 24 часа не тронута."""

    def test_hot_qualification_still_creates_a_manager_task(self):
        lead = self.make_lead('Горячий контакт', Lead.Qualification.WARM)
        set_lead_qualification(
            lead=lead,
            new_status=Lead.Qualification.HOT,
            changed_by=self.teamlead,
            matched_hot_criteria=['Запросил демо'],
        )
        lead.refresh_from_db()

        self.assertEqual(lead.qualification_status, Lead.Qualification.HOT)
        self.assertEqual(lead.assigned_manager_id, self.manager.id)
        self.assertIsNotNone(lead.hot_handoff_at)

        task = Task.objects.get(
            project=self.project, task_type=Task.TaskType.MANAGER_HANDOFF
        )
        self.assertEqual(task.assignee_id, self.manager.id)
        self.assertLessEqual(
            (task.deadline - lead.hot_handoff_at).total_seconds(), 24 * 3600 + 5
        )

    def test_hot_lead_appears_in_the_hot_column(self):
        lead = self.make_lead('Горячий контакт', Lead.Qualification.WARM)
        set_lead_qualification(
            lead=lead,
            new_status=Lead.Qualification.HOT,
            changed_by=self.teamlead,
            matched_hot_criteria=['Запросил демо'],
        )
        lead.refresh_from_db()
        columns = {column['key']: column['leads'] for column in lead_columns([lead])}
        self.assertEqual(columns['hot'], [lead])
        self.assertEqual(columns['warm'], [])
