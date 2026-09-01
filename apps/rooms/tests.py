from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.rooms.models import Project, Room, RoomActivity, RoomDocument, RoomMember
from apps.rooms.services import (
    add_freelancer_to_room,
    assign_teamlead,
    handle_project_paid,
    launch_project,
    user_can_access_project,
)
from apps.test_helpers import make_director, make_freelancer, make_teamlead, make_user
from apps.users.models import User


class RoomServiceTests(TestCase):
    def setUp(self):
        self.director = make_director()
        self.teamlead = make_teamlead()
        self.freelancer = make_freelancer()
        self.project = Project.objects.create(
            owner=self.director,
            name='Тестовый проект',
            project_type=Project.Type.BASE,
            seller_level=Project.SellerLevel.MIDDLE,
            input_data={
                'offer': 'Оффер',
                'utp': 'УТП',
                'audience': 'ЦА',
                'hot_criteria': 'Запросил демо',
            },
            budget=10000,
            status=Project.Status.DRAFT,
        )

    def test_launch_creates_room_and_director_member(self):
        launch_project(self.project)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertTrue(hasattr(self.project, 'room'))
        self.assertTrue(
            RoomMember.objects.filter(
                room=self.project.room,
                user=self.director,
                role_in_room=RoomMember.RoleInRoom.DIRECTOR,
            ).exists()
        )

    def test_assign_teamlead_and_add_freelancer_keeps_project_in_staffing(self):
        """Добавление фрилансера больше не активирует проект.

        Раньше первый фрилансер переводил проект `STAFFING → ACTIVE`.
        Теперь активация — результат подтверждённой готовности всей
        функциональной команды (см. `tests_staffing_workflow`).
        """
        launch_project(self.project)
        assign_teamlead(self.project, self.teamlead)
        self.project.refresh_from_db()
        self.assertEqual(self.project.teamlead_id, self.teamlead.id)
        add_freelancer_to_room(self.project.room, self.freelancer)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertTrue(
            RoomMember.objects.filter(
                room=self.project.room,
                user=self.freelancer,
                role_in_room=RoomMember.RoleInRoom.FREELANCER,
            ).exists()
        )

    def test_access_control(self):
        launch_project(self.project)
        stranger = make_user(email='stranger@example.com', role=User.Roles.FREELANCER)
        self.assertTrue(user_can_access_project(self.director, self.project))
        self.assertFalse(user_can_access_project(stranger, self.project))
        add_freelancer_to_room(self.project.room, stranger)
        self.assertTrue(user_can_access_project(stranger, self.project))


class RoomViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'TestPass123!'
        self.director = make_director(email='dir@rooms.test', password=self.password)
        self.teamlead = make_teamlead(email='tl@rooms.test', password=self.password)
        self.freelancer = make_freelancer(email='fr@rooms.test', password=self.password)
        self.outsider = make_freelancer(email='out@rooms.test', password=self.password)

    def _create_project_via_form(self):
        self.client.login(username='dir@rooms.test', password=self.password)
        response = self.client.post(
            reverse('rooms:project_create'),
            {
                'name': 'Запуск продаж',
                'project_type': Project.Type.LINKEDIN,
                'seller_level': Project.SellerLevel.SENIOR,
                'tariff_plan': 'launch',
                'budget': '50000',
                'kpi_target': '20',
                'start_date': '',
                'offer': 'Продаём SaaS',
                'utp': 'Быстрый ROI',
                'audience': 'CEO B2B',
                'hot_criteria': 'Согласен на демо',
            },
        )
        project = Project.objects.get(name='Запуск продаж')
        self.assertRedirects(
            response,
            reverse('rooms:project_detail', kwargs={'project_id': project.id}),
        )
        return project

    def test_director_creates_and_launches_project(self):
        project = self._create_project_via_form()
        self.assertEqual(project.status, Project.Status.DRAFT)
        self.assertEqual(project.offer, 'Продаём SaaS')

        launch = self.client.post(
            reverse('rooms:project_launch', kwargs={'project_id': project.id}),
        )
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.STAFFING)
        self.assertRedirects(
            launch,
            reverse('rooms:room_overview', kwargs={'project_id': project.id}),
        )

    def test_launch_requires_input_data(self):
        self.client.login(username='dir@rooms.test', password=self.password)
        project = Project.objects.create(
            owner=self.director,
            name='Пустой',
            input_data={},
            status=Project.Status.DRAFT,
        )
        response = self.client.post(
            reverse('rooms:project_launch', kwargs={'project_id': project.id}),
        )
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DRAFT)
        self.assertRedirects(
            response,
            reverse('rooms:project_detail', kwargs={'project_id': project.id}),
        )

    def test_freelancer_cannot_create_project(self):
        self.client.login(username='fr@rooms.test', password=self.password)
        response = self.client.get(reverse('rooms:project_create'))
        self.assertEqual(response.status_code, 403)

    def test_team_flow_assign_and_ready(self):
        project = self._create_project_via_form()
        self.client.post(reverse('rooms:project_launch', kwargs={'project_id': project.id}))
        project.refresh_from_db()

        assign = self.client.post(
            reverse('rooms:room_assign_teamlead', kwargs={'project_id': project.id}),
            {'teamlead': str(self.teamlead.id)},
        )
        self.assertRedirects(
            assign,
            reverse('rooms:room_overview', kwargs={'project_id': project.id}),
        )
        project.refresh_from_db()
        self.assertEqual(project.teamlead_id, self.teamlead.id)

        self.client.logout()
        self.client.login(username='tl@rooms.test', password=self.password)
        add = self.client.post(
            reverse('rooms:room_add_freelancer', kwargs={'project_id': project.id}),
            {'freelancer': str(self.freelancer.id)},
        )
        self.assertRedirects(
            add,
            reverse('rooms:room_team', kwargs={'project_id': project.id}),
        )
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.STAFFING)

        self.client.logout()
        self.client.login(username='fr@rooms.test', password=self.password)
        ready = self.client.post(
            reverse('rooms:room_confirm_ready', kwargs={'project_id': project.id}),
        )
        self.assertRedirects(
            ready,
            reverse('rooms:room_overview', kwargs={'project_id': project.id}),
        )
        member = RoomMember.objects.get(room=project.room, user=self.freelancer)
        self.assertEqual(member.ready_status, RoomMember.ReadyStatus.READY)
        # Функциональных слотов в этом ручном потоке нет, поэтому «команда
        # собрана на 100%» не выполняется и проект остаётся в подборе.
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.STAFFING)

    def test_outsider_cannot_access_room(self):
        project = self._create_project_via_form()
        self.client.post(reverse('rooms:project_launch', kwargs={'project_id': project.id}))
        self.client.logout()
        self.client.login(username='out@rooms.test', password=self.password)
        response = self.client.get(
            reverse('rooms:room_overview', kwargs={'project_id': project.id}),
        )
        self.assertEqual(response.status_code, 403)

    def test_document_upload(self):
        project = self._create_project_via_form()
        self.client.post(reverse('rooms:project_launch', kwargs={'project_id': project.id}))
        assign_teamlead(project, self.teamlead)
        self.client.logout()
        self.client.login(username='tl@rooms.test', password=self.password)
        response = self.client.post(
            reverse('rooms:room_document_upload', kwargs={'project_id': project.id}),
            {
                'title': 'Презентация',
                'file': SimpleUploadedFile(
                    'vision.pdf',
                    b'%PDF-1.4 vision',
                    content_type='application/pdf',
                ),
            },
        )
        self.assertRedirects(
            response,
            reverse('rooms:room_documents', kwargs={'project_id': project.id}),
        )
        self.assertTrue(
            RoomDocument.objects.filter(room=project.room, title='Презентация').exists()
        )

    def test_project_list_scoped_by_role(self):
        project = self._create_project_via_form()
        self.client.post(reverse('rooms:project_launch', kwargs={'project_id': project.id}))
        self.client.post(
            reverse('rooms:room_assign_teamlead', kwargs={'project_id': project.id}),
            {'teamlead': str(self.teamlead.id)},
        )
        self.client.logout()
        self.client.login(username='tl@rooms.test', password=self.password)
        self.client.post(
            reverse('rooms:room_add_freelancer', kwargs={'project_id': project.id}),
            {'freelancer': str(self.freelancer.id)},
        )

        self.client.logout()
        self.client.login(username='fr@rooms.test', password=self.password)
        response = self.client.get(reverse('rooms:project_list'))
        self.assertContains(response, project.name)

        self.client.logout()
        self.client.login(username='out@rooms.test', password=self.password)
        response = self.client.get(reverse('rooms:project_list'))
        self.assertNotContains(response, project.name)


class ProjectPaidServiceTests(TestCase):
    """Сервис-фасад оплаты: apps.rooms.services.handle_project_paid."""

    def setUp(self):
        self.director = make_director(email='paid-dir@rooms.test')
        self.project = Project.objects.create(
            owner=self.director,
            name='Оплаченный проект',
            input_data={
                'offer': 'Оффер',
                'utp': 'УТП',
                'audience': 'ЦА',
                'hot_criteria': 'Запросил демо',
            },
            status=Project.Status.DRAFT,
        )

    def test_handle_project_paid_starts_staffing_with_room_and_activity(self):
        room = handle_project_paid(self.project, actor=self.director)
        self.project.refresh_from_db()

        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertEqual(room.project_id, self.project.id)
        self.assertTrue(
            RoomActivity.objects.filter(
                room=room,
                event_type=RoomActivity.EventType.PROJECT_LAUNCHED,
            ).exists()
        )
        self.assertTrue(
            RoomMember.objects.filter(
                room=room,
                user=self.director,
                role_in_room=RoomMember.RoleInRoom.DIRECTOR,
            ).exists()
        )

    def test_repeated_payment_does_not_create_second_room(self):
        first = handle_project_paid(self.project, actor=self.director)
        second = handle_project_paid(self.project, actor=self.director)

        self.assertEqual(first.id, second.id)
        self.assertEqual(Room.objects.filter(project=self.project).count(), 1)
        self.assertEqual(
            RoomActivity.objects.filter(
                room=first,
                event_type=RoomActivity.EventType.PROJECT_LAUNCHED,
            ).count(),
            1,
        )

    def test_room_from_debug_launch_is_reused(self):
        """Комната старого DEBUG-запуска не дублируется тестовой оплатой."""
        launch_project(self.project, actor=self.director)
        self.project.refresh_from_db()
        room = handle_project_paid(self.project, actor=self.director)

        self.assertEqual(Room.objects.filter(project=self.project).count(), 1)
        self.assertEqual(room.id, self.project.room.id)
        self.assertEqual(self.project.status, Project.Status.STAFFING)


class TestPaymentFlowTests(TestCase):
    """Happy-path тестовой оплаты: из wizard и из карточки черновика."""

    def setUp(self):
        self.client = Client()
        self.password = 'TestPass123!'
        self.director = make_director(email='pay-dir@rooms.test', password=self.password)
        self.other_director = make_director(
            email='pay-other@rooms.test',
            password=self.password,
        )
        self.freelancer = make_freelancer(email='pay-fr@rooms.test', password=self.password)

    def _project_form_data(self, name):
        return {
            'name': name,
            'project_type': Project.Type.BASE,
            'seller_level': Project.SellerLevel.MIDDLE,
            'tariff_plan': 'launch',
            'budget': '1000',
            'kpi_target': '',
            'start_date': '',
            'offer': 'Продаём SaaS',
            'utp': 'Быстрый ROI',
            'audience': 'CEO B2B',
            'hot_criteria': 'Согласен на демо',
        }

    def _make_draft(self, name):
        return Project.objects.create(
            owner=self.director,
            name=name,
            input_data={
                'offer': 'Оффер',
                'utp': 'УТП',
                'audience': 'ЦА',
                'hot_criteria': 'Запросил демо',
            },
            status=Project.Status.DRAFT,
        )

    def _assert_paid_and_redirected(self, project, response):
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.STAFFING)
        self.assertEqual(Room.objects.filter(project=project).count(), 1)
        self.assertTrue(
            RoomActivity.objects.filter(
                room=project.room,
                event_type=RoomActivity.EventType.PROJECT_LAUNCHED,
            ).exists()
        )
        self.assertRedirects(
            response,
            reverse('rooms:room_overview', kwargs={'project_id': project.id}),
        )

    def test_wizard_test_payment_opens_room(self):
        self.client.login(username='pay-dir@rooms.test', password=self.password)

        step1 = self.client.post(
            reverse('rooms:setup_wizard'),
            {'step': '1', 'arch': 'cold_calling'},
        )
        self.assertEqual(step1.status_code, 302)

        data = self._project_form_data('Проект из wizard')
        data['step'] = '2'
        data['arch'] = 'cold_calling'
        self.client.post(reverse('rooms:setup_wizard'), data)

        project = Project.objects.get(name='Проект из wizard')
        self.assertEqual(project.status, Project.Status.DRAFT)

        step3 = self.client.get(
            reverse('rooms:setup_wizard'),
            {'step': '3', 'project': str(project.id)},
        )
        self.assertContains(step3, 'Тестовая оплата запуска')
        self.assertContains(step3, 'Оплатить и запустить')

        response = self.client.post(
            reverse('rooms:project_pay', kwargs={'project_id': project.id}),
        )
        self._assert_paid_and_redirected(project, response)

    def test_draft_card_test_payment_opens_room(self):
        self.client.login(username='pay-dir@rooms.test', password=self.password)
        self.client.post(
            reverse('rooms:project_create'),
            self._project_form_data('Проект из черновика'),
        )
        project = Project.objects.get(name='Проект из черновика')

        card = self.client.get(
            reverse('rooms:project_detail', kwargs={'project_id': project.id}),
        )
        self.assertContains(card, 'Тестовая оплата запуска')
        self.assertContains(card, 'Оплатить и запустить')

        response = self.client.post(
            reverse('rooms:project_pay', kwargs={'project_id': project.id}),
        )
        self._assert_paid_and_redirected(project, response)

    def test_repeated_payment_request_keeps_single_room(self):
        self.client.login(username='pay-dir@rooms.test', password=self.password)
        project = self._make_draft('Проект для повтора')

        first = self.client.post(
            reverse('rooms:project_pay', kwargs={'project_id': project.id}),
        )
        self._assert_paid_and_redirected(project, first)

        repeat = self.client.post(
            reverse('rooms:project_pay', kwargs={'project_id': project.id}),
        )
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.STAFFING)
        self.assertEqual(Room.objects.filter(project=project).count(), 1)
        self.assertEqual(
            RoomActivity.objects.filter(
                room=project.room,
                event_type=RoomActivity.EventType.PROJECT_LAUNCHED,
            ).count(),
            1,
        )
        self.assertEqual(repeat.status_code, 302)

    def test_other_users_cannot_pay_foreign_project(self):
        project = self._make_draft('Чужой проект')

        for email in ('pay-other@rooms.test', 'pay-fr@rooms.test'):
            self.client.logout()
            self.client.login(username=email, password=self.password)
            response = self.client.post(
                reverse('rooms:project_pay', kwargs={'project_id': project.id}),
            )
            self.assertEqual(response.status_code, 404)

        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DRAFT)
        self.assertFalse(Room.objects.filter(project=project).exists())

    def test_anonymous_cannot_pay(self):
        project = self._make_draft('Аноним не платит')
        response = self.client.post(
            reverse('rooms:project_pay', kwargs={'project_id': project.id}),
        )
        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DRAFT)
        self.assertFalse(Room.objects.filter(project=project).exists())

    def test_payment_requires_project_inputs(self):
        self.client.login(username='pay-dir@rooms.test', password=self.password)
        project = Project.objects.create(
            owner=self.director,
            name='Без вводных',
            input_data={},
            status=Project.Status.DRAFT,
        )
        response = self.client.post(
            reverse('rooms:project_pay', kwargs={'project_id': project.id}),
        )
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DRAFT)
        self.assertFalse(Room.objects.filter(project=project).exists())
        self.assertRedirects(
            response,
            reverse('rooms:project_detail', kwargs={'project_id': project.id}),
        )


class AddToRoomContextProcessorTests(TestCase):
    """`add_to_room` отдаёт форму BIZ-шаблонам без импорта rooms в profiles."""

    def setUp(self):
        self.client = Client()
        self.director = make_director(email='cp-dir@rooms.test')
        self.freelancer = make_freelancer(email='cp-fr@rooms.test')
        self.project = Project.objects.create(
            owner=self.director,
            name='Проект для подбора',
            project_type=Project.Type.BASE,
            input_data={'offer': 'o', 'utp': 'u', 'audience': 'a', 'hot_criteria': 'h'},
            status=Project.Status.DRAFT,
        )
        launch_project(self.project)

    def _catalog(self):
        return self.client.get(reverse('profiles:catalog'))

    def _card(self):
        return self.client.get(
            reverse('profiles:detail', kwargs={'user_id': self.freelancer.id}),
        )

    def test_director_sees_project_select_in_catalog_and_card(self):
        self.client.force_login(self.director)
        for response in (self._catalog(), self._card()):
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['can_add_to_room'])
            self.assertTrue(response.context['add_to_room_form'])
            self.assertContains(
                response,
                reverse(
                    'rooms:catalog_add_to_room',
                    kwargs={'user_id': self.freelancer.id},
                ),
            )
            self.assertContains(response, self.project.name)

    def test_teamlead_sees_only_own_projects(self):
        teamlead = make_teamlead(email='cp-tl@rooms.test')
        assign_teamlead(self.project, teamlead, actor=self.director)
        other_director = make_director(email='cp-dir2@rooms.test')
        other = Project.objects.create(
            owner=other_director,
            name='Чужой проект',
            project_type=Project.Type.BASE,
            input_data={'offer': 'o', 'utp': 'u', 'audience': 'a', 'hot_criteria': 'h'},
            status=Project.Status.DRAFT,
        )
        launch_project(other)

        self.client.force_login(teamlead)
        response = self._catalog()
        self.assertTrue(response.context['can_add_to_room'])
        self.assertContains(response, self.project.name)
        self.assertNotContains(response, other.name)

    def test_freelancer_has_no_add_to_room(self):
        self.client.force_login(self.freelancer)
        response = self._card()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['can_add_to_room'])
        self.assertFalse(response.context['add_to_room_form'])

    def test_director_without_staffing_projects_has_no_form(self):
        lonely = make_director(email='cp-dir3@rooms.test')
        self.client.force_login(lonely)
        response = self._catalog()
        self.assertFalse(response.context['can_add_to_room'])
        self.assertFalse(response.context['add_to_room_form'])

    def test_anonymous_page_renders_without_room_queries(self):
        self.client.logout()
        with self.assertNumQueries(0):
            response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['can_add_to_room'])
