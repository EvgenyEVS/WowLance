"""Тесты Epic A/B: архитектура, wizard, каталог→комната, invite, метрики."""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.pipeline.models import Task
from apps.profiles.models import FreelancerProfile
from apps.rooms.models import Project, RoomActivity, RoomMember, TeamleadInvite
from apps.rooms.onboarding import director_metrics, director_onboarding, onboarding_progress
from apps.rooms.presets import get_architecture_preset
from apps.rooms.services import (
    accept_teamlead_invite,
    assign_teamlead,
    create_teamlead_invite,
    launch_project,
)
from apps.test_helpers import make_director, make_freelancer, make_teamlead, make_user
from apps.users.models import User


class ArchitecturePresetTests(TestCase):
    def test_presets_exist(self):
        for key in ('cold_calling', 'linkedin', 'scaleup'):
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
        session = client.session
        self.assertEqual(session.get('architecture_preset'), 'cold_calling')

    def test_apply_architecture_director_goes_to_wizard(self):
        director = make_director(email='arch@test.com')
        client = Client()
        client.force_login(director)
        response = client.get(
            reverse('rooms:apply_architecture') + '?arch=linkedin',
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/setup/', response['Location'])
        self.assertIn('step=2', response['Location'])


class SetupWizardTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.director = make_director(email='wiz@test.com')
        self.client.force_login(self.director)

    def test_wizard_creates_project_from_preset_and_launches(self):
        # step 1 → 2
        r1 = self.client.post(
            reverse('rooms:setup_wizard') + '?step=1',
            {'step': '1', 'arch': 'cold_calling'},
        )
        self.assertEqual(r1.status_code, 302)

        preset = get_architecture_preset('cold_calling')
        r2 = self.client.post(
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
        self.assertEqual(r2.status_code, 302)
        project = Project.objects.get(owner=self.director)
        self.assertEqual(project.project_type, Project.Type.BASE)
        self.assertEqual(project.input_data.get('architecture'), 'cold_calling')

        r3 = self.client.post(
            reverse('rooms:setup_wizard') + f'?step=3&project={project.id}',
            {'step': '3', 'action': 'launch'},
        )
        self.assertEqual(r3.status_code, 302)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.STAFFING)
        self.assertTrue(hasattr(project, 'room'))
        self.assertTrue(
            RoomActivity.objects.filter(
                room=project.room,
                event_type=RoomActivity.EventType.PROJECT_LAUNCHED,
            ).exists()
        )


class CatalogAddToRoomTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.director = make_director(email='cat@test.com')
        self.teamlead = make_teamlead(email='cattl@test.com')
        self.freelancer = make_freelancer(email='sale@test.com')
        profile = self.freelancer.freelancer_profile
        profile.skills = ['SPIN', 'Cold calls']
        profile.rating = 4.5
        profile.is_available = True
        profile.save()
        self.project = Project.objects.create(
            owner=self.director,
            name='Staffing project',
            project_type=Project.Type.BASE,
            input_data={
                'offer': 'o', 'utp': 'u', 'audience': 'a', 'hot_criteria': 'h',
            },
            status=Project.Status.DRAFT,
        )
        launch_project(self.project)
        assign_teamlead(self.project, self.teamlead)
        self.client.force_login(self.teamlead)

    def test_add_from_catalog_to_room(self):
        url = reverse('rooms:catalog_add_to_room', kwargs={'user_id': self.freelancer.id})
        response = self.client.post(url, {'project': str(self.project.id)})
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

    def test_baseball_card_page_contains_skills_and_cta(self):
        response = self.client.get(
            reverse('profiles:detail', kwargs={'user_id': self.freelancer.id}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'SPIN')
        self.assertContains(response, 'В комнату')
        self.assertContains(response, '★')


class TeamleadInviteTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.director = make_director(email='inv@test.com')
        self.project = Project.objects.create(
            owner=self.director,
            name='Invite project',
            input_data={
                'offer': 'o', 'utp': 'u', 'audience': 'a', 'hot_criteria': 'h',
            },
            status=Project.Status.DRAFT,
        )
        launch_project(self.project)

    def test_create_invite_and_register_teamlead(self):
        self.client.force_login(self.director)
        response = self.client.post(
            reverse('rooms:room_create_teamlead_invite', kwargs={'project_id': self.project.id}),
        )
        self.assertEqual(response.status_code, 302)
        invite = TeamleadInvite.objects.get(project=self.project, is_active=True)

        self.client.logout()
        accept_url = reverse(
            'rooms:teamlead_invite_accept',
            kwargs={'token': invite.token},
        )
        response = self.client.post(accept_url, {
            'first_name': 'Тим',
            'last_name': 'Лид',
            'email': 'newtl@test.com',
            'password1': 'StrongPass123!',
            'password2': 'StrongPass123!',
        })
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email='newtl@test.com')
        self.assertEqual(user.role, User.Roles.TEAMLEAD)
        self.project.refresh_from_db()
        self.assertEqual(self.project.teamlead_id, user.id)

    def test_existing_teamlead_accepts_invite(self):
        tl = make_teamlead(email='existtl@test.com')
        invite = create_teamlead_invite(self.project, self.director)
        accept_teamlead_invite(invite, tl)
        self.project.refresh_from_db()
        self.assertEqual(self.project.teamlead_id, tl.id)


class DashboardMetricsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.director = make_director(email='met@test.com')
        self.freelancer = make_freelancer(email='fmet@test.com')
        profile = self.freelancer.freelancer_profile
        profile.country = 'RU'
        profile.skills = ['BANT']
        profile.save()

    def test_director_dashboard_shows_metrics_and_checklist(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Чеклист запуска')
        self.assertContains(response, 'Горячие лиды')
        self.assertContains(response, 'Потратил')
        self.assertContains(response, '0 ₽')
        self.assertContains(response, 'контур сделок не в этом релизе')
        metrics = director_metrics(self.director)
        self.assertEqual(metrics['projects_total'], 0)
        self.assertEqual(metrics['earned_total'], Decimal('0.00'))

    def test_freelancer_dashboard_checklist(self):
        self.client.force_login(self.freelancer)
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Чеклист старта')
        progress = onboarding_progress(director_onboarding(self.director))
        self.assertFalse(progress['complete'])

    def test_legal_pages(self):
        for name in ('core:about', 'core:privacy', 'core:terms'):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200)


class RoomHubPolishTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.director = make_director(email='hub@test.com')
        self.teamlead = make_teamlead(email='hubtl@test.com')
        self.freelancer = make_freelancer(email='hubf@test.com')
        self.project = Project.objects.create(
            owner=self.director,
            name='Hub',
            input_data={
                'offer': 'o', 'utp': 'u', 'audience': 'a', 'hot_criteria': 'h',
            },
            status=Project.Status.DRAFT,
        )
        launch_project(self.project)
        assign_teamlead(self.project, self.teamlead)
        from apps.rooms.services import add_freelancer_to_room
        add_freelancer_to_room(self.project.room, self.freelancer, actor=self.teamlead)
        Task.objects.create(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.director,
            title='Позвонить 10 лидам',
            status=Task.Status.NEW,
        )
        self.client.force_login(self.director)

    def test_overview_has_activity_and_kanban(self):
        response = self.client.get(
            reverse('rooms:room_overview', kwargs={'project_id': self.project.id}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Лента событий')
        self.assertContains(response, 'К работе')
        self.assertContains(response, 'Позвонить 10 лидам')

    def test_tasks_kanban_columns(self):
        self.client.force_login(self.teamlead)
        response = self.client.get(
            reverse('pipeline:room_tasks', kwargs={'project_id': self.project.id}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'На проверке')
        self.assertContains(response, 'Готово')

    def test_documents_dropbox_empty_state(self):
        response = self.client.get(
            reverse('rooms:room_documents', kwargs={'project_id': self.project.id}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Папка пуста')

    def test_document_upload_logs_activity(self):
        self.client.force_login(self.teamlead)
        upload = SimpleUploadedFile('brief.txt', b'hello', content_type='text/plain')
        response = self.client.post(
            reverse('rooms:room_document_upload', kwargs={'project_id': self.project.id}),
            {'title': 'Бриф', 'file': upload},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            RoomActivity.objects.filter(
                room=self.project.room,
                event_type=RoomActivity.EventType.DOCUMENT_UPLOADED,
            ).exists()
        )
