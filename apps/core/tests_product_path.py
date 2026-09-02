"""Продуктовый путь Epic A/B: архитектура → wizard → оплата → комната.

Один сквозной маршрут директора и его границы:

* пресеты архитектуры объявлены и применяются гостю и директору по-разному;
* мастер из трёх шагов создаёт проект из пресета и запускает его;
* stub-оплата открывает комнату (RBAC оплаты живёт в `apps.rooms.tests`);
* добавление из каталога идёт через URL `rooms`, а не через `profiles`
  (граница ADR: бизнес-логика комнаты не протекает в профили);
* приглашение тимлида — регистрацией нового и принятием существующим;
* метрики директора не выдумывают выручку;
* юридические страницы живы.
"""

from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse

from apps.pipeline.models import Lead
from apps.rooms.models import (
    Project,
    Room,
    RoomActivity,
    RoomMember,
    TeamleadInvite,
)
from apps.rooms.presets import ARCHITECTURE_PRESETS, get_architecture_preset
from apps.rooms.services import (
    accept_teamlead_invite,
    assign_teamlead,
    create_teamlead_invite,
    launch_project,
)
from apps.test_helpers import make_director, make_freelancer, make_teamlead
from apps.users.models import User

PROJECT_INPUTS = {
    'offer': 'Оффер',
    'utp': 'УТП',
    'audience': 'ЦА',
    'hot_criteria': 'Запросил демо',
}


class ArchitecturePresetTests(TestCase):
    """Точка входа «Применить архитектуру» для гостя и для директора."""

    def test_architecture_presets_are_declared(self):
        self.assertTrue(ARCHITECTURE_PRESETS)
        for key in ARCHITECTURE_PRESETS:
            with self.subTest(preset=key):
                preset = get_architecture_preset(key)
                self.assertIsNotNone(preset)
                self.assertIn('offer', preset['input_data'])

    def test_apply_architecture_anonymous_redirects_to_register(self):
        client = Client()
        response = client.get(
            reverse('rooms:apply_architecture') + '?arch=cold_calling',
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/register/', response['Location'])
        self.assertIn('arch=cold_calling', response['Location'])
        self.assertEqual(client.session.get('architecture_preset'), 'cold_calling')

    def test_apply_architecture_director_goes_to_wizard(self):
        self.client.force_login(make_director(email='arch@test.com'))
        response = self.client.get(
            reverse('rooms:apply_architecture') + '?arch=linkedin',
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/setup/', response['Location'])
        self.assertIn('step=2', response['Location'])


class SetupWizardTests(TestCase):
    """Golden path мастера: один сценарий на три шага, без дробления."""

    def setUp(self):
        self.director = make_director(email='wiz@test.com')
        self.client.force_login(self.director)

    def test_wizard_three_steps_create_project_from_preset_and_launch(self):
        step1 = self.client.post(
            reverse('rooms:setup_wizard') + '?step=1',
            {'step': '1', 'arch': 'cold_calling'},
        )
        self.assertEqual(step1.status_code, 302)

        preset = get_architecture_preset('cold_calling')
        step2 = self.client.post(
            reverse('rooms:setup_wizard') + '?step=2',
            {
                'step': '2',
                'arch': 'cold_calling',
                'name': preset['project_name'],
                'project_type': preset['project_type'],
                'seller_level': preset['seller_level'],
                'tariff_plan': preset['tariff_plan'],
                'budget': str(preset['budget']),
                'kpi_target': str(preset['kpi_target']),
                'offer': preset['input_data']['offer'],
                'utp': preset['input_data']['utp'],
                'audience': preset['input_data']['audience'],
                'hot_criteria': preset['input_data']['hot_criteria'],
            },
        )
        self.assertEqual(step2.status_code, 302)
        project = Project.objects.get(owner=self.director)
        self.assertEqual(project.project_type, Project.Type.BASE)
        self.assertEqual(project.input_data.get('architecture'), 'cold_calling')

        step3 = self.client.post(
            reverse('rooms:setup_wizard') + f'?step=3&project={project.id}',
            {'step': '3', 'action': 'launch'},
        )
        self.assertEqual(step3.status_code, 302)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.STAFFING)
        self.assertTrue(hasattr(project, 'room'))
        self.assertTrue(
            RoomActivity.objects.filter(
                room=project.room,
                event_type=RoomActivity.EventType.PROJECT_LAUNCHED,
            ).exists()
        )


class TestPaymentTests(TestCase):
    """Тестовая оплата как продуктовый результат: черновик → комната."""

    def setUp(self):
        self.director = make_director(email='pay@test.com')
        self.client.force_login(self.director)

    def test_payment_stub_opens_the_room(self):
        project = Project.objects.create(
            owner=self.director,
            name='Оплаченный проект',
            input_data=dict(PROJECT_INPUTS),
            status=Project.Status.DRAFT,
        )
        response = self.client.post(
            reverse('rooms:project_pay', kwargs={'project_id': project.id}),
        )
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


class CatalogAddToRoomTests(TestCase):
    """Каталог наполняет команду только через URL комнаты."""

    def setUp(self):
        self.director = make_director(email='cat@test.com')
        self.teamlead = make_teamlead(email='cattl@test.com')
        self.freelancer = make_freelancer(email='sale@test.com')
        self.project = Project.objects.create(
            owner=self.director,
            name='Staffing project',
            project_type=Project.Type.BASE,
            input_data=dict(PROJECT_INPUTS),
            status=Project.Status.DRAFT,
        )
        launch_project(self.project)
        assign_teamlead(self.project, self.teamlead)
        self.client.force_login(self.teamlead)

    def test_add_from_catalog_to_room_goes_through_the_rooms_url(self):
        response = self.client.post(
            reverse(
                'rooms:catalog_add_to_room',
                kwargs={'user_id': self.freelancer.id},
            ),
            {'project': str(self.project.id)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            RoomMember.objects.filter(
                room=self.project.room,
                user=self.freelancer,
                role_in_room=RoomMember.RoleInRoom.FREELANCER,
            ).exists()
        )
        # Добавление из каталога наполняет команду, но не активирует проект:
        # ACTIVE наступает только когда функциональная команда подтвердила
        # готовность (apps.rooms.staffing.services.sync_project_activation).
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)


class TeamleadInviteTests(TestCase):
    """Приглашение тимлида: новый регистрируется, существующий принимает."""

    def setUp(self):
        self.director = make_director(email='inv@test.com')
        self.project = Project.objects.create(
            owner=self.director,
            name='Invite project',
            input_data=dict(PROJECT_INPUTS),
            status=Project.Status.DRAFT,
        )
        launch_project(self.project)

    def test_create_invite_and_register_teamlead(self):
        self.client.force_login(self.director)
        created = self.client.post(
            reverse(
                'rooms:room_create_teamlead_invite',
                kwargs={'project_id': self.project.id},
            ),
        )
        self.assertEqual(created.status_code, 302)
        invite = TeamleadInvite.objects.get(project=self.project, is_active=True)

        self.client.logout()
        accepted = self.client.post(
            reverse('rooms:teamlead_invite_accept', kwargs={'token': invite.token}),
            {
                'first_name': 'Тим',
                'last_name': 'Лид',
                'email': 'newtl@test.com',
                'password1': 'StrongPass123!',
                'password2': 'StrongPass123!',
            },
        )
        self.assertEqual(accepted.status_code, 302)
        user = User.objects.get(email='newtl@test.com')
        self.assertEqual(user.role, User.Roles.TEAMLEAD)
        self.project.refresh_from_db()
        self.assertEqual(self.project.teamlead_id, user.id)

    def test_existing_teamlead_accepts_invite(self):
        teamlead = make_teamlead(email='existtl@test.com')
        invite = create_teamlead_invite(self.project, self.director)
        accept_teamlead_invite(invite, teamlead)
        self.project.refresh_from_db()
        self.assertEqual(self.project.teamlead_id, teamlead.id)


class DirectorDashboardMetricsTests(TestCase):
    """Дашборд директора отдаёт числа, а не обещания выручки."""

    def setUp(self):
        self.director = make_director(email='met@test.com')
        self.client.force_login(self.director)

    def test_director_metrics_do_not_invent_revenue(self):
        project = Project.objects.create(
            owner=self.director,
            name='Метрики',
            budget=Decimal('50000.00'),
            input_data=dict(PROJECT_INPUTS),
            status=Project.Status.STAFFING,
        )
        Lead.objects.create(
            project=project,
            creator=self.director,
            qualification_status=Lead.Qualification.HOT,
        )
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 200)

        metrics = response.context['metrics']
        self.assertEqual(metrics['projects_total'], 1)
        self.assertEqual(metrics['hot_leads'], 1)
        self.assertEqual(metrics['spent_total'], Decimal('50000.00'))
        # Горячий лид не превращается в выручку: контур сделок вне релиза.
        self.assertEqual(metrics['earned_total'], Decimal('0.00'))
        self.assertIn('сделок', metrics['earned_caption'])

    def test_legal_pages_are_reachable(self):
        for name in ('core:about', 'core:privacy', 'core:terms'):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
