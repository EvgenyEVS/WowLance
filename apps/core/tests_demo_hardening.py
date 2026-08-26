"""Тесты демо-харденинга: активация без DEBUG и seed сценария."""

from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.rooms.models import Project, RoomFunctionSlot, RoomMember
from apps.users.models import User


class DemoModeActivationTests(TestCase):
    @override_settings(
        DEBUG=False,
        DEMO_MODE=True,
        EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend',
    )
    def test_registration_shows_activation_link_in_demo_mode_without_debug(self):
        client = Client()
        response = client.post(
            reverse('users:register') + '?role=freelancer',
            {
                'role': User.Roles.FREELANCER,
                'first_name': 'Демо',
                'last_name': 'Юзер',
                'email': 'demo.activate@example.com',
                'password1': 'DemoPass123!',
                'password2': 'DemoPass123!',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'users/registration_success.html')
        self.assertContains(response, 'activate')
        self.assertTrue(response.context['demo_mode'])


class SeedDemoScenarioTests(TestCase):
    def test_seed_demo_scenario_creates_staffing_project_with_slots(self):
        call_command('seed_demo_scenario')
        director = User.objects.get(email='director@wowlance.demo')
        project = Project.objects.get(owner=director, name='Демо для стейкхолдеров')
        self.assertEqual(project.status, Project.Status.STAFFING)
        self.assertTrue(hasattr(project, 'room'))
        self.assertTrue(
            RoomMember.objects.filter(
                room=project.room,
                role_in_room=RoomMember.RoleInRoom.TEAMLEAD,
            ).exists()
        )
        self.assertTrue(
            RoomFunctionSlot.objects.filter(room=project.room, is_active=True).exists()
        )
