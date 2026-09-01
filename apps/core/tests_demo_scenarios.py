"""Сценарии демо-харденинга: регистрация, seed, staffing, кабинеты."""

from pathlib import Path

from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.profiles.models import FreelancerProfile
from apps.rooms.models import Project, RoomFunctionSlot, RoomMember
from apps.rooms.presets import FUNCTIONAL_ROLE_PACKAGES
from apps.rooms.services import save_functional_roles_and_sync_slots
from apps.rooms.unit_economics import get_project_composition
from apps.users.models import User


class DemoHardeningScenarioTests(TestCase):
    def test_01_demo_mode_shows_activation_without_debug(self):
        with override_settings(
            DEBUG=False,
            DEMO_MODE=True,
            EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend',
        ):
            response = Client().post(
                reverse('users:register') + '?role=freelancer',
                {
                    'role': User.Roles.FREELANCER,
                    'first_name': 'Сцен',
                    'last_name': 'Арий',
                    'email': 'scenario.activate@example.com',
                    'password1': 'DemoPass123!',
                    'password2': 'DemoPass123!',
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'activate')
        self.assertTrue(response.context['demo_mode'])

    def test_02_without_demo_registration_redirects_to_login(self):
        with override_settings(
            DEBUG=False,
            DEMO_MODE=False,
            EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        ):
            response = Client().post(
                reverse('users:register') + '?role=freelancer',
                {
                    'role': User.Roles.FREELANCER,
                    'first_name': 'Без',
                    'last_name': 'Демо',
                    'email': 'scenario.noshown@example.com',
                    'password1': 'DemoPass123!',
                    'password2': 'DemoPass123!',
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_03_seed_and_staff_cabinets(self):
        call_command('seed_demo_scenario')
        director = User.objects.get(email='director@wowlance.demo')
        project = Project.objects.get(
            owner=director, name='Демо для стейкхолдеров',
        )
        self.assertEqual(project.status, Project.Status.STAFFING)
        self.assertTrue(
            RoomMember.objects.filter(
                room=project.room,
                role_in_room=RoomMember.RoleInRoom.TEAMLEAD,
            ).exists()
        )
        slots = RoomFunctionSlot.objects.filter(room=project.room, is_active=True)
        self.assertTrue(slots.exists())
        self.assertGreaterEqual(slots.filter(member__isnull=False).count(), 1)

        client = Client()
        self.assertTrue(
            client.login(username='director@wowlance.demo', password='DemoPass123!')
        )
        self.assertEqual(
            client.get(
                reverse('rooms:room_overview', kwargs={'project_id': project.id})
            ).status_code,
            200,
        )
        # Директор на «Команду» не заходит — редирект на обзор.
        team_resp = client.get(
            reverse('rooms:room_team', kwargs={'project_id': project.id})
        )
        self.assertEqual(team_resp.status_code, 302)
        self.assertIn(
            reverse('rooms:room_overview', kwargs={'project_id': project.id}),
            team_resp['Location'],
        )

        client = Client()
        self.assertTrue(
            client.login(username='teamlead@wowlance.demo', password='DemoPass123!')
        )
        self.assertEqual(
            client.get(
                reverse('rooms:room_team', kwargs={'project_id': project.id})
            ).status_code,
            200,
        )

        client = Client()
        self.assertTrue(
            client.login(username='manager@wowlance.demo', password='DemoPass123!')
        )
        self.assertEqual(
            client.get(reverse('pipeline:manager_inbox')).status_code,
            200,
        )

    def test_04_empty_pool_reports_unfilled_opened_slots(self):
        call_command('seed_demo_scenario')
        director = User.objects.get(email='director@wowlance.demo')
        project = Project.objects.get(
            owner=director, name='Демо для стейкхолдеров',
        )
        FreelancerProfile.objects.update(
            is_available=False, is_verified=False, video_url='',
        )
        composition = {
            e['role_key']: e['count'] for e in get_project_composition(project)
        }
        # Берём seller-роль из пакета, не teamlead (у TL другая логика назначения).
        package = FUNCTIONAL_ROLE_PACKAGES['quick_start']
        seller_key = next(
            (k for k in package.composition if k != 'teamlead'),
            None,
        )
        self.assertIsNotNone(seller_key)
        composition[seller_key] = composition.get(seller_key, 0) + 1
        result = save_functional_roles_and_sync_slots(project, composition, director)
        self.assertGreaterEqual(result.unfilled_opened_slots, 1)

    def test_05_landing_login_and_error_templates(self):
        self.assertEqual(Client().get('/').status_code, 200)
        self.assertEqual(Client().get(reverse('users:login')).status_code, 200)
        for name in ('400.html', '403.html', '404.html', '500.html'):
            self.assertTrue(Path('templates', name).is_file(), name)

    def test_06_sqlite_timeout_and_demo_setting(self):
        from django.conf import settings

        timeout = (settings.DATABASES['default'].get('OPTIONS') or {}).get('timeout')
        self.assertEqual(timeout, 30)
        self.assertTrue(hasattr(settings, 'DEMO_MODE'))
