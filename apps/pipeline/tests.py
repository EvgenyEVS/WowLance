from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.pipeline.models import Lead, Report, Task
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
    def _build_project(self, suffix='1'):
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


class TaskReportFlowTests(PipelineProjectMixin, TestCase):
    def setUp(self):
        self._build_project('svc')

    def test_report_required_and_close_rules(self):
        task = create_task(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.teamlead,
            title='Сделать 10 звонков',
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

    def test_reject_report_returns_to_freelancer(self):
        task = create_task(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.teamlead,
            title='Аутрич LinkedIn',
        )
        start_task(task, self.freelancer)
        report = submit_report(
            task=task,
            author=self.freelancer,
            content_text='Отправил сообщение в LinkedIn, жду ответа',
            attachment=_png('a.png'),
        )
        with self.assertRaises(ValidationError):
            review_report(report=report, reviewer=self.teamlead, approve=False, comment='')

        review_report(
            report=report,
            reviewer=self.teamlead,
            approve=False,
            comment='Нет скриншота переписки',
        )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.REJECTED)

    def test_only_assignee_can_submit_report(self):
        task = create_task(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.teamlead,
            title='Только исполнитель',
        )
        start_task(task, self.freelancer)
        with self.assertRaises(PermissionDenied):
            submit_report(
                task=task,
                author=self.teamlead,
                content_text='Чужой отчёт не должен пройти валидацию здесь',
                attachment=_png(),
            )

    def test_only_project_teamlead_can_create_task(self):
        """Задачу ставит тимлид проекта — и больше никто.

        Прежде тест закрывал только фрилансера, потому что guard сервиса был
        шире (`user_can_manage_team`) и пропускал владельца проекта. Право
        сузилось до `user_can_create_task`, поэтому проверяются все роли
        сразу: иначе «директор снова может ставить задачи» вернулось бы
        незамеченным.

        Платформенный `ADMIN` в таблице не случайно: роль обслуживает
        систему и продуктового права ставить задачи в чужой комнате не даёт.
        """
        admin = make_user(email='adm-svc@pipe.test', role=User.Roles.ADMIN)

        task = create_task(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.teamlead,
            title='Задачу ставит тимлид',
        )
        self.assertEqual(task.created_by, self.teamlead)
        self.assertEqual(task.assignee, self.freelancer)
        self.assertEqual(task.status, Task.Status.NEW)

        for actor in (self.director, self.freelancer, admin, self.manager):
            with self.subTest(role=actor.role):
                with self.assertRaises(PermissionDenied):
                    create_task(
                        project=self.project,
                        assignee=self.freelancer,
                        created_by=actor,
                        title=f'Задача от {actor.role}',
                    )
                self.assertFalse(
                    Task.objects.filter(title=f'Задача от {actor.role}').exists()
                )


class LeadHotHandoffTests(PipelineProjectMixin, TestCase):
    def setUp(self):
        self._build_project('lead')

    def test_freelancer_cannot_set_hot_on_create(self):
        with self.assertRaises(ValidationError):
            create_lead(
                project=self.project,
                creator=self.freelancer,
                contact_info={'email': 'lead@ex.com'},
                source=Lead.Source.LINKEDIN,
                qualification_status=Lead.Qualification.HOT,
            )

    def test_hot_creates_manager_task(self):
        lead = create_lead(
            project=self.project,
            creator=self.freelancer,
            contact_info={'name': 'Игорь', 'email': 'igor@ex.com'},
            source=Lead.Source.BASE,
            qualification_status=Lead.Qualification.WARM,
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
        self.assertEqual(lead.status_history.count(), 2)

    def test_hot_without_manager_fails(self):
        User.objects.filter(role=User.Roles.MANAGER).delete()
        lead = create_lead(
            project=self.project,
            creator=self.freelancer,
            contact_info={'email': 'nomanager@ex.com'},
            source=Lead.Source.OTHER,
            qualification_status=Lead.Qualification.WARM,
        )
        with self.assertRaises(ValidationError):
            set_lead_qualification(
                lead=lead,
                new_status=Lead.Qualification.HOT,
                changed_by=self.teamlead,
                matched_hot_criteria=['Запросил демо'],
            )

    def test_freelancer_cannot_qualify_lead(self):
        lead = create_lead(
            project=self.project,
            creator=self.freelancer,
            contact_info={'email': 'q@ex.com'},
            source=Lead.Source.BASE,
            qualification_status=Lead.Qualification.COLD,
        )
        with self.assertRaises(PermissionDenied):
            set_lead_qualification(
                lead=lead,
                new_status=Lead.Qualification.WARM,
                changed_by=self.freelancer,
            )


class TaskViewFlowTests(PipelineProjectMixin, TestCase):
    def setUp(self):
        self._build_project('view')
        self.client = Client()

    def test_full_http_task_report_approve_close(self):
        self.client.login(username=self.teamlead.email, password=PASSWORD)
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
        self.assertRedirects(
            create,
            reverse('pipeline:room_tasks', kwargs={'project_id': self.project.id}),
        )
        task = Task.objects.get(title='Первые звонки по базе')
        self.assertEqual(len(task.checklist), 2)

        self.client.logout()
        self.client.login(username=self.freelancer.email, password=PASSWORD)
        self.client.post(
            reverse('pipeline:task_start', kwargs={
                'project_id': self.project.id,
                'task_id': task.id,
            }),
        )
        submit = self.client.post(
            reverse('pipeline:task_submit_report', kwargs={
                'project_id': self.project.id,
                'task_id': task.id,
            }),
            {
                'content_text': 'Сделал 10 звонков, два тёплых лида, скрин во вложении',
                'attachment': _png('report.png'),
            },
        )
        self.assertRedirects(
            submit,
            reverse('pipeline:task_detail', kwargs={
                'project_id': self.project.id,
                'task_id': task.id,
            }),
        )
        report = task.reports.get()

        self.client.logout()
        self.client.login(username=self.teamlead.email, password=PASSWORD)
        self.client.post(
            reverse('pipeline:task_review_report', kwargs={
                'project_id': self.project.id,
                'task_id': task.id,
                'report_id': report.id,
            }),
            {'action': 'approve', 'comment': 'Принято'},
        )
        close = self.client.post(
            reverse('pipeline:task_close', kwargs={
                'project_id': self.project.id,
                'task_id': task.id,
            }),
        )
        self.assertRedirects(
            close,
            reverse('pipeline:task_detail', kwargs={
                'project_id': self.project.id,
                'task_id': task.id,
            }),
        )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.CLOSED)

    def test_outsider_cannot_open_tasks(self):
        self.client.login(username=self.outsider.email, password=PASSWORD)
        response = self.client.get(
            reverse('pipeline:room_tasks', kwargs={'project_id': self.project.id}),
        )
        self.assertEqual(response.status_code, 403)

    def test_freelancer_sees_only_own_tasks(self):
        other = make_freelancer(email='other-view@pipe.test', password=PASSWORD)
        add_freelancer_to_room(self.project.room, other)
        mine = create_task(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.teamlead,
            title='Моя задача фрилансера',
        )
        create_task(
            project=self.project,
            assignee=other,
            created_by=self.teamlead,
            title='Чужая задача фрилансера',
        )
        self.client.login(username=self.freelancer.email, password=PASSWORD)
        response = self.client.get(
            reverse('pipeline:room_tasks', kwargs={'project_id': self.project.id}),
        )
        self.assertContains(response, mine.title)
        self.assertNotContains(response, 'Чужая задача фрилансера')

    # --- постановка задачи: право тимлида проекта -------------------------

    def task_create_url(self):
        return reverse('pipeline:task_create', kwargs={'project_id': self.project.id})

    def tasks_url(self):
        return reverse('pipeline:room_tasks', kwargs={'project_id': self.project.id})

    def post_new_task(self, title):
        return self.client.post(self.task_create_url(), {
            'title': title,
            'description': 'Проверка прав',
            'assignee': str(self.freelancer.id),
            'deadline': '',
            'checklist_text': '',
            'report_required': 'on',
        })

    def test_director_cannot_create_task_over_http(self):
        """Владелец проекта управляет командой, но задачи не ставит."""
        self.client.login(username=self.director.email, password=PASSWORD)
        response = self.post_new_task('Задача от директора')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Task.objects.filter(title='Задача от директора').exists())

    def test_freelancer_cannot_create_task_over_http(self):
        self.client.login(username=self.freelancer.email, password=PASSWORD)
        response = self.post_new_task('Задача от фрилансера')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Task.objects.filter(title='Задача от фрилансера').exists())

    def test_teamlead_creates_task_over_http(self):
        """Разрешённый путь остаётся прежним: редирект на доску и живая задача."""
        self.client.login(username=self.teamlead.email, password=PASSWORD)
        response = self.post_new_task('Задача от тимлида')
        self.assertRedirects(response, self.tasks_url())
        task = Task.objects.get(title='Задача от тимлида')
        self.assertEqual(task.created_by, self.teamlead)
        self.assertEqual(task.assignee, self.freelancer)

    def test_task_form_is_offered_to_the_teamlead_only(self):
        """Флаг, форма и разметка совпадают: страница не обещает лишнего.

        Проверяются все три уровня сразу — `can_create_task` в context,
        наличие `create_form` и видимость самой формы, — потому что
        рассогласование между ними и есть та ошибка, которую тест ловит.
        Директор на «Задачи» не заходит: редирект на «Обзор».
        """
        expectations = (
            (self.teamlead, True),
            (self.freelancer, False),
        )
        for user, expected in expectations:
            with self.subTest(role=user.role):
                self.client.force_login(user)
                response = self.client.get(self.tasks_url())
                self.assertEqual(response.status_code, 200)
                self.assertIs(response.context['can_create_task'], expected)
                if expected:
                    self.assertIsNotNone(response.context['create_form'])
                    self.assertContains(response, 'Новая задача')
                else:
                    self.assertIsNone(response.context['create_form'])
                    self.assertNotContains(response, 'Новая задача')

        self.client.force_login(self.director)
        response = self.client.get(self.tasks_url())
        self.assertRedirects(
            response,
            reverse('rooms:room_overview', kwargs={'project_id': self.project.id}),
        )

    def test_director_has_no_manage_team_and_no_tasks_tab(self):
        """Операционка у тимлида: директор не управляет командой и не видит доску."""
        self.client.force_login(self.director)
        response = self.client.get(self.tasks_url())
        self.assertRedirects(
            response,
            reverse('rooms:room_overview', kwargs={'project_id': self.project.id}),
        )
        overview = self.client.get(
            reverse('rooms:room_overview', kwargs={'project_id': self.project.id})
        )
        self.assertFalse(overview.context['can_manage_team'])
        self.assertFalse(overview.context['show_tasks_tab'])
        self.assertFalse(overview.context['show_team_tab'])

    def test_teamlead_board_keeps_four_columns_and_every_task_of_the_room(self):
        """Доска тимлида не урезана: четыре колонки и задачи всех исполнителей.

        Фильтр по исполнителю на вкладке «Задачи» действует только для
        фрилансера и менеджера. Тимлид под него не попадает — регресс на
        это, потому что остальные проверки колонок сделаны под директором,
        и случайное расширение фильтра осталось бы незамеченным.
        """
        other = make_freelancer(email='other-tl-board@pipe.test', password=PASSWORD)
        add_freelancer_to_room(self.project.room, other)
        mine = create_task(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.teamlead,
            title='Задача первого исполнителя',
        )
        theirs = create_task(
            project=self.project,
            assignee=other,
            created_by=self.teamlead,
            title='Задача второго исполнителя',
        )

        self.client.force_login(self.teamlead)
        response = self.client.get(self.tasks_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [column['title'] for column in response.context['kanban_columns']],
            ['К работе', 'В работе', 'На проверке', 'Готово'],
        )
        shown = {
            task.id
            for column in response.context['kanban_columns']
            for task in column['tasks']
        }
        self.assertIn(mine.id, shown)
        self.assertIn(theirs.id, shown)
        self.assertContains(response, mine.title)
        self.assertContains(response, theirs.title)


class LeadViewFlowTests(PipelineProjectMixin, TestCase):
    def setUp(self):
        self._build_project('lview')
        self.client = Client()

    def test_create_lead_and_qualify_hot_via_http(self):
        self.client.login(username=self.freelancer.email, password=PASSWORD)
        create = self.client.post(
            reverse('pipeline:lead_create', kwargs={'project_id': self.project.id}),
            {
                'name': 'Анна Клиент',
                'phone': '+79990001122',
                'email': 'anna@client.test',
                'linkedin': '',
                'source': Lead.Source.LINKEDIN,
                'notes': 'Интерес к КП',
                'qualification_status': Lead.Qualification.WARM,
            },
        )
        self.assertRedirects(
            create,
            reverse('pipeline:room_leads', kwargs={'project_id': self.project.id}),
        )
        lead = Lead.objects.get(project=self.project, creator=self.freelancer)
        self.assertEqual(lead.qualification_status, Lead.Qualification.WARM)

        self.client.logout()
        self.client.login(username=self.teamlead.email, password=PASSWORD)
        qualify = self.client.post(
            reverse('pipeline:lead_qualify', kwargs={
                'project_id': self.project.id,
                'lead_id': lead.id,
            }),
            {
                'qualification_status': Lead.Qualification.HOT,
                'matched_hot_criteria': 'Запросил демо\nСогласен на встречу',
                'comment': 'Горячий по чеклисту',
            },
        )
        self.assertRedirects(
            qualify,
            reverse('pipeline:lead_detail', kwargs={
                'project_id': self.project.id,
                'lead_id': lead.id,
            }),
        )
        lead.refresh_from_db()
        self.assertEqual(lead.qualification_status, Lead.Qualification.HOT)
        self.assertIsNotNone(lead.hot_handoff_at)
        self.assertEqual(lead.assigned_manager_id, self.manager.id)
        self.assertTrue(
            Task.objects.filter(
                lead=lead,
                assignee=self.manager,
                task_type=Task.TaskType.MANAGER_HANDOFF,
            ).exists()
        )

        self.client.logout()
        self.client.login(username=self.manager.email, password=PASSWORD)
        inbox = self.client.get(reverse('pipeline:manager_inbox'))
        self.assertEqual(inbox.status_code, 200)
        self.assertContains(inbox, 'Анна Клиент')

    def test_manager_inbox_forbidden_for_freelancer(self):
        self.client.login(username=self.freelancer.email, password=PASSWORD)
        response = self.client.get(reverse('pipeline:manager_inbox'))
        self.assertEqual(response.status_code, 403)

    def test_lead_history_recorded(self):
        lead = create_lead(
            project=self.project,
            creator=self.freelancer,
            contact_info={'name': 'История', 'email': 'hist@ex.com'},
            source=Lead.Source.BASE,
            qualification_status=Lead.Qualification.COLD,
        )
        set_lead_qualification(
            lead=lead,
            new_status=Lead.Qualification.WARM,
            changed_by=self.teamlead,
            comment='Прогрели',
        )
        statuses = list(lead.status_history.order_by('created_at').values_list(
            'new_status', flat=True,
        ))
        self.assertEqual(statuses, [
            Lead.Qualification.COLD,
            Lead.Qualification.WARM,
        ])
