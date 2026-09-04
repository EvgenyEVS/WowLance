"""Операционка комнаты: задачи, отчёты, лиды, передача Hot менеджеру.

Продуктовый контур pipeline:

* жизненный цикл задачи create → start → report → review → close;
* Quality Gate: без утверждённого отчёта задача не закрывается;
* постановка задач — право тимлида проекта, не владельца;
* Hot-лид уходит назначенному менеджеру отдельной задачей MANAGER_HANDOFF;
* фрилансер видит и открывает только свои задачи и лиды;
* начисление за утверждённый отчёт создаётся ровно один раз.

Раскладка досок проверяется по данным (`apps.pipeline.kanban`), а не по
разметке. Отчёт тимлида живёт в `tests_teamlead_report`.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.pipeline.accruals import DEMO_ACCRUAL_USD_PER_APPROVED_REPORT
from apps.pipeline.kanban import lead_columns, task_columns
from apps.pipeline.models import FreelancerAccrual, Lead, Report, Task
from apps.pipeline.services import (
    TaskCloseError,
    close_task,
    create_lead,
    create_task,
    review_report,
    set_lead_qualification,
    start_task,
    submit_report,
)
from apps.rooms.models import Project
from apps.rooms.services import add_freelancer_to_room, assign_teamlead, launch_project
from apps.test_helpers import make_director, make_freelancer, make_teamlead, make_user
from apps.users.models import User

PASSWORD = 'TestPass123!'


def _png(name='shot.png'):
    return SimpleUploadedFile(name, b'\x89PNG\r\n\x1a\nfake', content_type='image/png')


class PipelineProjectMixin:
    """Запущенный проект: директор-владелец, тимлид, фрилансер, менеджер."""

    def _build_project(self, suffix='1'):
        self.client = Client()
        self.director = make_director(email=f'd{suffix}@pipe.test', password=PASSWORD)
        self.teamlead = make_teamlead(email=f't{suffix}@pipe.test', password=PASSWORD)
        self.freelancer = make_freelancer(email=f'f{suffix}@pipe.test', password=PASSWORD)
        self.outsider = make_freelancer(email=f'o{suffix}@pipe.test', password=PASSWORD)
        self.manager = make_user(
            email=f'm{suffix}@pipe.test',
            role=User.Roles.MANAGER,
            password=PASSWORD,
        )
        self.project = Project.objects.create(
            owner=self.director,
            name=f'Pipeline Project {suffix}',
            input_data={
                'offer': 'Оффер',
                'audience': 'ЦА',
                'hot_criteria': 'Запросил демо',
            },
            status=Project.Status.DRAFT,
        )
        launch_project(self.project)
        assign_teamlead(self.project, self.teamlead)
        add_freelancer_to_room(self.project.room, self.freelancer)

    def tasks_url(self):
        return reverse('pipeline:room_tasks', kwargs={'project_id': self.project.id})

    def make_task(self, title, assignee=None, **extra):
        return create_task(
            project=self.project,
            assignee=assignee or self.freelancer,
            created_by=self.teamlead,
            title=title,
            **extra,
        )


# ---------------------------------------------------------------------------
# 1-4, 14-15. Жизненный цикл задачи и отчёт
# ---------------------------------------------------------------------------


class TaskLifecycleTests(PipelineProjectMixin, TestCase):
    def setUp(self):
        self._build_project('life')

    def approved_task(self, title='Задача с отчётом'):
        """Задача с `report_required=True`, доведённая до APPROVED."""
        task = self.make_task(title)
        report = submit_report(
            task=task,
            author=self.freelancer,
            content_text='Отчёт по задаче: обзвонил десять контактов.',
            attachment=_png(),
        )
        review_report(report=report, reviewer=self.teamlead, approve=True, comment='Ок')
        task.refresh_from_db()
        return task

    def test_task_lifecycle_create_start_report_review_close(self):
        """Полный продуктовый путь по HTTP: тимлид ставит, фрилансер сдаёт."""
        self.client.force_login(self.teamlead)
        create = self.client.post(
            reverse('pipeline:task_create', kwargs={'project_id': self.project.id}),
            {
                'title': 'Первые звонки по базе',
                'description': '10 касаний',
                'assignee': str(self.freelancer.id),
                'deadline': '',
                'checklist_text': 'Скрипт\nФиксация результата',
                'report_required': 'on',
            },
        )
        self.assertRedirects(create, self.tasks_url())
        task = Task.objects.get(title='Первые звонки по базе')
        self.assertEqual(len(task.checklist), 2)

        detail_url = reverse(
            'pipeline:task_detail',
            kwargs={'project_id': self.project.id, 'task_id': task.id},
        )

        self.client.force_login(self.freelancer)
        self.client.post(
            reverse(
                'pipeline:task_start',
                kwargs={'project_id': self.project.id, 'task_id': task.id},
            )
        )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.IN_PROGRESS)

        submit = self.client.post(
            reverse(
                'pipeline:task_submit_report',
                kwargs={'project_id': self.project.id, 'task_id': task.id},
            ),
            {
                'content_text': 'Сделал 10 звонков, два тёплых лида, скрин во вложении',
                'attachment': _png('report.png'),
            },
        )
        self.assertRedirects(submit, detail_url)
        report = task.reports.get()

        self.client.force_login(self.teamlead)
        self.client.post(
            reverse(
                'pipeline:task_review_report',
                kwargs={
                    'project_id': self.project.id,
                    'task_id': task.id,
                    'report_id': report.id,
                },
            ),
            {'action': 'approve', 'comment': 'Принято'},
        )
        close = self.client.post(
            reverse(
                'pipeline:task_close',
                kwargs={'project_id': self.project.id, 'task_id': task.id},
            )
        )

        self.assertRedirects(close, detail_url)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.CLOSED)

    def test_quality_gate_blocks_close_without_approved_report(self):
        """Закрыть задачу можно только после утверждённого отчёта."""
        task = self.make_task(
            'Сделать 10 звонков',
            checklist=[{'text': 'Скрипт', 'done': False}],
        )
        start_task(task, self.freelancer)

        with self.assertRaises(ValidationError):
            submit_report(
                task=task,
                author=self.freelancer,
                content_text='коротко',
                attachment=None,
            )

        report = submit_report(
            task=task,
            author=self.freelancer,
            content_text='Позвонил клиенту, интерес есть, скрин во вложении',
            attachment=_png(),
        )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.READY_FOR_REVIEW)
        self.assertEqual(report.review_status, Report.ReviewStatus.PENDING)

        with self.assertRaises(TaskCloseError):
            close_task(task, self.teamlead)

        review_report(report=report, reviewer=self.teamlead, approve=True, comment='Ок')
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.APPROVED)

        close_task(task, self.teamlead)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.CLOSED)

    def test_reject_report_returns_task_to_freelancer(self):
        task = self.make_task('Аутрич LinkedIn')
        start_task(task, self.freelancer)
        report = submit_report(
            task=task,
            author=self.freelancer,
            content_text='Отправил сообщение в LinkedIn, жду ответа',
            attachment=_png('a.png'),
        )

        with self.assertRaises(ValidationError):
            review_report(
                report=report, reviewer=self.teamlead, approve=False, comment=''
            )

        review_report(
            report=report,
            reviewer=self.teamlead,
            approve=False,
            comment='Нет скриншота переписки',
        )

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.REJECTED)

    def test_only_assignee_can_submit_report(self):
        task = self.make_task('Только исполнитель')
        start_task(task, self.freelancer)

        with self.assertRaises(PermissionDenied):
            submit_report(
                task=task,
                author=self.teamlead,
                content_text='Чужой отчёт не должен пройти валидацию здесь',
                attachment=_png(),
            )

    def test_close_sets_closed_at_for_report_required_task(self):
        task = self.approved_task()
        self.assertIsNone(task.closed_at)

        close_task(task, self.teamlead)

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.CLOSED)
        self.assertIsNotNone(task.closed_at)

    def test_second_close_keeps_the_original_closed_at(self):
        """`closed_at` — отметка первого закрытия, а не последнего сохранения."""
        task = self.make_task('Закрывается дважды', report_required=False)
        first = timezone.make_aware(
            datetime(2026, 3, 17, 9, 0, 0), timezone.get_current_timezone()
        )
        second = first + timedelta(days=2)

        with patch('apps.pipeline.services.timezone.now', return_value=first):
            close_task(task, self.teamlead)
        task.refresh_from_db()
        self.assertEqual(task.closed_at, first)

        with patch('apps.pipeline.services.timezone.now', return_value=second):
            close_task(task, self.teamlead)

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.CLOSED)
        self.assertEqual(task.closed_at, first)
        # Почему не `updated_at`: он сдвинулся, а `closed_at` остался на месте.
        self.assertEqual(task.updated_at, second)


# ---------------------------------------------------------------------------
# 5-6, 10. Кто ставит задачи и кто их видит
# ---------------------------------------------------------------------------


class TaskPermissionTests(PipelineProjectMixin, TestCase):
    def setUp(self):
        self._build_project('perm')

    def create_url(self):
        return reverse('pipeline:task_create', kwargs={'project_id': self.project.id})

    def post_new_task(self, title):
        return self.client.post(
            self.create_url(),
            {
                'title': title,
                'description': 'Проверка прав',
                'assignee': str(self.freelancer.id),
                'deadline': '',
                'checklist_text': '',
                'report_required': 'on',
            },
        )

    def test_task_creation_is_teamlead_only_in_service_and_over_http(self):
        """Право сузилось до тимлида проекта — владелец задачи не ставит.

        Платформенный ADMIN в матрице не случайно: роль обслуживает систему
        и продуктового права ставить задачи в чужой комнате не даёт.
        """
        admin = make_user(email='adm-perm@pipe.test', role=User.Roles.ADMIN)

        task = self.make_task('Задачу ставит тимлид')
        self.assertEqual(task.created_by, self.teamlead)
        self.assertEqual(task.status, Task.Status.NEW)

        for actor in (self.director, self.freelancer, admin, self.manager):
            with self.subTest(role=actor.role, layer='service'):
                with self.assertRaises(PermissionDenied):
                    create_task(
                        project=self.project,
                        assignee=self.freelancer,
                        created_by=actor,
                        title=f'Задача от {actor.role}',
                    )

        self.client.force_login(self.director)
        response = self.post_new_task('Задача от директора')

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Task.objects.filter(title='Задача от директора').exists())

    def test_teamlead_creates_task_over_http(self):
        self.client.force_login(self.teamlead)

        response = self.post_new_task('Задача от тимлида')

        self.assertRedirects(response, self.tasks_url())
        task = Task.objects.get(title='Задача от тимлида')
        self.assertEqual(task.created_by, self.teamlead)
        self.assertEqual(task.assignee, self.freelancer)

    def test_freelancer_gets_403_on_foreign_task_and_lead(self):
        """Чужая задача и чужой лид закрыты адресом, а не только выдачей списка."""
        other = make_freelancer(email='other-perm@pipe.test', password=PASSWORD)
        add_freelancer_to_room(self.project.room, other)
        foreign_task = self.make_task('Чужая задача', assignee=other)
        foreign_lead = create_lead(
            project=self.project,
            creator=other,
            contact_info={'email': 'foreign@ex.com'},
            source=Lead.Source.BASE,
            qualification_status=Lead.Qualification.COLD,
        )

        self.client.force_login(self.freelancer)
        urls = {
            'task': reverse(
                'pipeline:task_detail',
                kwargs={'project_id': self.project.id, 'task_id': foreign_task.id},
            ),
            'lead': reverse(
                'pipeline:lead_detail',
                kwargs={'project_id': self.project.id, 'lead_id': foreign_lead.id},
            ),
        }
        for kind, url in urls.items():
            with self.subTest(object=kind):
                self.assertEqual(self.client.get(url).status_code, 403)

        # Своё — открывается: урезан доступ, а не страница.
        own = self.make_task('Моя задача')
        own_url = reverse(
            'pipeline:task_detail',
            kwargs={'project_id': self.project.id, 'task_id': own.id},
        )
        self.assertEqual(self.client.get(own_url).status_code, 200)


# ---------------------------------------------------------------------------
# 7-9, 18. Лиды и передача Hot менеджеру
# ---------------------------------------------------------------------------


class LeadHandoffTests(PipelineProjectMixin, TestCase):
    def setUp(self):
        self._build_project('lead')

    def make_lead(self, qualification=Lead.Qualification.WARM, **extra):
        return create_lead(
            project=self.project,
            creator=self.freelancer,
            contact_info=extra.pop('contact_info', {'email': 'lead@ex.com'}),
            source=extra.pop('source', Lead.Source.BASE),
            qualification_status=qualification,
            **extra,
        )

    def test_freelancer_cannot_set_or_qualify_a_lead_as_hot(self):
        """Ни при создании, ни правкой существующего лида — Hot не фрилансеру."""
        with self.assertRaises(ValidationError):
            self.make_lead(Lead.Qualification.HOT)

        lead = self.make_lead(Lead.Qualification.COLD)
        with self.assertRaises(PermissionDenied):
            set_lead_qualification(
                lead=lead,
                new_status=Lead.Qualification.WARM,
                changed_by=self.freelancer,
            )

        lead.refresh_from_db()
        self.assertEqual(lead.qualification_status, Lead.Qualification.COLD)

    def test_hot_lead_creates_manager_handoff_task(self):
        lead = self.make_lead(
            contact_info={'name': 'Игорь', 'email': 'igor@ex.com'},
            notes='Интерес к продукту',
        )

        set_lead_qualification(
            lead=lead,
            new_status=Lead.Qualification.HOT,
            changed_by=self.teamlead,
            comment='Запросил демо',
            matched_hot_criteria=['Запросил демо'],
        )

        lead.refresh_from_db()
        self.assertEqual(lead.qualification_status, Lead.Qualification.HOT)
        self.assertIsNotNone(lead.hot_handoff_at)
        self.assertEqual(lead.assigned_manager_id, self.manager.id)
        task = Task.objects.get(lead=lead, task_type=Task.TaskType.MANAGER_HANDOFF)
        self.assertEqual(task.assignee_id, self.manager.id)
        self.assertFalse(task.report_required)
        self.assertTrue(task.deadline <= timezone.now() + timedelta(hours=24, minutes=1))

    def test_hot_without_manager_fails(self):
        User.objects.filter(role=User.Roles.MANAGER).delete()
        lead = self.make_lead(contact_info={'email': 'nomanager@ex.com'})

        with self.assertRaises(ValidationError):
            set_lead_qualification(
                lead=lead,
                new_status=Lead.Qualification.HOT,
                changed_by=self.teamlead,
                matched_hot_criteria=['Запросил демо'],
            )

        lead.refresh_from_db()
        self.assertNotEqual(lead.qualification_status, Lead.Qualification.HOT)

    def test_lead_status_history_is_recorded(self):
        lead = self.make_lead(
            Lead.Qualification.COLD,
            contact_info={'name': 'История', 'email': 'hist@ex.com'},
        )

        set_lead_qualification(
            lead=lead,
            new_status=Lead.Qualification.WARM,
            changed_by=self.teamlead,
            comment='Прогрели',
        )

        statuses = list(
            lead.status_history.order_by('created_at').values_list(
                'new_status', flat=True
            )
        )
        self.assertEqual(
            statuses, [Lead.Qualification.COLD, Lead.Qualification.WARM]
        )

    def test_freelancer_post_qualification_returns_403(self):
        """Фрилансер POST квалификации → 403, статус лида не меняется.
        Тимлид по-прежнему редирект и смена статуса (золотой путь не ломается).
        """
        lead = self.make_lead(Lead.Qualification.COLD)
        qualify_url = reverse(
            'pipeline:lead_qualify',
            kwargs={'project_id': self.project.id, 'lead_id': lead.id},
        )
        payload = {
            'qualification_status': Lead.Qualification.HOT,
            'comment': 'Попытка фрилансера',
        }

        with self.subTest(actor='freelancer'):
            self.client.force_login(self.freelancer)
            response = self.client.post(qualify_url, payload)
            self.assertEqual(response.status_code, 403)
            lead.refresh_from_db()
            self.assertEqual(lead.qualification_status, Lead.Qualification.COLD)

        with self.subTest(actor='teamlead'):
            self.client.force_login(self.teamlead)
            response = self.client.post(qualify_url, payload)
            # Тимлид — редирект (200/302), статус меняется
            self.assertIn(response.status_code, (200, 302))
            lead.refresh_from_db()
            self.assertEqual(lead.qualification_status, Lead.Qualification.HOT)


# ---------------------------------------------------------------------------
# 11-12. Инбокс менеджера
# ---------------------------------------------------------------------------


class ManagerInboxTests(PipelineProjectMixin, TestCase):
    def setUp(self):
        self._build_project('inbox')
        self.inbox_url = reverse('pipeline:manager_inbox')

    def test_manager_inbox_opens_for_manager(self):
        lead = create_lead(
            project=self.project,
            creator=self.freelancer,
            contact_info={'name': 'Анна Клиент', 'email': 'anna@client.test'},
            source=Lead.Source.LINKEDIN,
            qualification_status=Lead.Qualification.WARM,
        )
        set_lead_qualification(
            lead=lead,
            new_status=Lead.Qualification.HOT,
            changed_by=self.teamlead,
            matched_hot_criteria=['Запросил демо'],
        )
        handoff = Task.objects.get(lead=lead, task_type=Task.TaskType.MANAGER_HANDOFF)

        self.client.force_login(self.manager)
        response = self.client.get(self.inbox_url)

        self.assertEqual(response.status_code, 200)
        self.assertIn(handoff, list(response.context['tasks']))

    def test_manager_inbox_forbidden_for_freelancer(self):
        self.client.force_login(self.freelancer)

        response = self.client.get(self.inbox_url)

        self.assertEqual(response.status_code, 403)

    def test_manager_opens_own_handoff_but_not_room_overview(self):
        """Assignee открывает handoff; Обзор комнаты и чужой фрилансер — 403.
        Менеджер не видит навигацию комнаты и «← К задачам», но видит
        переход в inbox («Горячие лиды»). Тимлид/директор навигацию видят.
        """
        lead = create_lead(
            project=self.project,
            creator=self.freelancer,
            contact_info={'name': 'Hot Клиент', 'email': 'hot@client.test'},
            source=Lead.Source.BASE,
            qualification_status=Lead.Qualification.WARM,
        )
        set_lead_qualification(
            lead=lead,
            new_status=Lead.Qualification.HOT,
            changed_by=self.teamlead,
            matched_hot_criteria=['Запросил демо'],
        )
        handoff = Task.objects.get(lead=lead, task_type=Task.TaskType.MANAGER_HANDOFF)
        task_url = reverse(
            'pipeline:task_detail',
            kwargs={'project_id': self.project.id, 'task_id': handoff.id},
        )
        overview_url = reverse(
            'rooms:room_overview', kwargs={'project_id': self.project.id}
        )
        inbox_url = reverse('pipeline:manager_inbox')
        tasks_url = reverse('pipeline:room_tasks', kwargs={'project_id': self.project.id})

        with self.subTest(actor='manager'):
            self.client.force_login(self.manager)
            response = self.client.get(task_url)
            self.assertEqual(response.status_code, 200)
            # Навигации комнаты нет
            self.assertNotContains(response, 'room-tabs')
            # Нет ссылки «← К задачам»
            self.assertNotContains(response, '← К задачам')
            # Но есть переход в inbox
            self.assertContains(response, '← Горячие лиды')
            self.assertContains(response, inbox_url)
            # Обзор комнаты — 403
            self.assertEqual(self.client.get(overview_url).status_code, 403)

        with self.subTest(actor='room_freelancer'):
            self.client.force_login(self.freelancer)
            self.assertEqual(self.client.get(task_url).status_code, 403)

        with self.subTest(actor='teamlead'):
            self.client.force_login(self.teamlead)
            response = self.client.get(task_url)
            self.assertEqual(response.status_code, 200)
            # Тимлид видит навигацию комнаты и «← К задачам»
            self.assertContains(response, 'room-tabs')
            self.assertContains(response, '← К задачам')

        with self.subTest(actor='director'):
            self.client.force_login(self.director)
            response = self.client.get(task_url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'room-tabs')
            self.assertContains(response, '← К задачам')


# ---------------------------------------------------------------------------
# 13. Контракт досок
# ---------------------------------------------------------------------------


class KanbanContractTests(PipelineProjectMixin, TestCase):
    def setUp(self):
        self._build_project('kanban')

    def test_task_and_lead_columns_contract(self):
        """Четыре колонки задач и три колонки лидов из одного определения.

        Проверяется контракт данных: набор и порядок ключей плюс свойство
        разбиения — каждый объект попадает ровно в одну колонку. Подписи и
        разметка контрактом не являются.
        """
        # Раскладка — чистая функция над списком: объекты создаются напрямую,
        # чтобы покрыть каждый статус, включая недостижимые сервисом переходы.
        tasks = [
            Task.objects.create(
                project=self.project,
                assignee=self.freelancer,
                created_by=self.teamlead,
                title=f'Задача {index}',
                status=status,
            )
            for index, status in enumerate(Task.Status.values)
        ]
        leads = [
            Lead.objects.create(
                project=self.project,
                creator=self.freelancer,
                contact_info={'email': f'lead{index}@ex.com'},
                source=Lead.Source.BASE,
                qualification_status=status,
            )
            for index, status in enumerate(Lead.Qualification.values)
        ]

        task_board = task_columns(tasks)
        self.assertEqual(
            [column['key'] for column in task_board],
            ['todo', 'in_progress', 'review', 'done'],
        )
        placed_tasks = [task for column in task_board for task in column['tasks']]
        self.assertEqual(
            {task.id for task in placed_tasks}, {task.id for task in tasks}
        )
        self.assertEqual(len(placed_tasks), len(tasks))

        lead_board = lead_columns(leads)
        self.assertEqual(
            [column['key'] for column in lead_board], ['cold', 'warm', 'hot']
        )
        placed_leads = [lead for column in lead_board for lead in column['leads']]
        self.assertEqual(
            {lead.id for lead in placed_leads}, {lead.id for lead in leads}
        )
        self.assertEqual(len(placed_leads), len(leads))

        # Страница берёт колонки из того же определения, а не из своей копии.
        self.client.force_login(self.teamlead)
        page_keys = [
            column['key']
            for column in self.client.get(self.tasks_url()).context['kanban_columns']
        ]
        self.assertEqual(page_keys, [column['key'] for column in task_board])


# ---------------------------------------------------------------------------
# 16-17. Начисления фрилансера
# ---------------------------------------------------------------------------


class FreelancerAccrualTests(PipelineProjectMixin, TestCase):
    def setUp(self):
        self._build_project('accrual')
        self.earnings_url = reverse('pipeline:freelancer_accruals')
        self.project_earnings_url = reverse(
            'pipeline:freelancer_project_accruals',
            kwargs={'project_id': self.project.id},
        )

    def submit(self, title, name='r.png'):
        task = self.make_task(title)
        return submit_report(
            task=task,
            author=self.freelancer,
            content_text='Достаточно длинный текст отчёта для валидации.',
            attachment=_png(name),
        )

    def test_approved_report_creates_one_accrual_and_repeat_does_not_duplicate(self):
        """Деньги начисляются ровно один раз за утверждённый отчёт."""
        rejected = self.submit('Отклонят', 'r1.png')
        review_report(
            report=rejected,
            reviewer=self.teamlead,
            approve=False,
            comment='Переделать',
        )
        self.assertEqual(FreelancerAccrual.objects.count(), 0)

        approved = self.submit('Примут', 'r2.png')
        review_report(
            report=approved, reviewer=self.teamlead, approve=True, comment='Ок'
        )

        accrual = FreelancerAccrual.objects.get()
        self.assertEqual(accrual.amount, DEMO_ACCRUAL_USD_PER_APPROVED_REPORT)
        self.assertEqual(accrual.freelancer_id, self.freelancer.id)
        self.assertEqual(accrual.project_id, self.project.id)

        with self.assertRaises(ValidationError):
            review_report(
                report=approved,
                reviewer=self.teamlead,
                approve=True,
                comment='Ещё раз',
            )

        self.assertEqual(FreelancerAccrual.objects.count(), 1)

    def test_earnings_pages_are_freelancer_only(self):
        self.client.force_login(self.freelancer)
        for label, url in (
            ('total', self.earnings_url),
            ('project', self.project_earnings_url),
        ):
            with self.subTest(page=label, role='freelancer'):
                self.assertEqual(self.client.get(url).status_code, 200)

        for user, role in (
            (self.director, 'director'),
            (self.teamlead, 'teamlead'),
            (self.outsider, 'outsider'),
        ):
            self.client.force_login(user)
            with self.subTest(page='project', role=role):
                self.assertEqual(
                    self.client.get(self.project_earnings_url).status_code, 403
                )

        for user, role in ((self.director, 'director'), (self.teamlead, 'teamlead')):
            self.client.force_login(user)
            with self.subTest(page='total', role=role):
                self.assertEqual(self.client.get(self.earnings_url).status_code, 403)
