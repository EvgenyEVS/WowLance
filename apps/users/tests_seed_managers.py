from django.core.management import call_command
from django.test import TestCase
from apps.users.models import User


class SeedManagersCommandTests(TestCase):
    def test_creates_manager_on_first_run(self):
        """Первый запуск создаёт менеджера."""
        self.assertEqual(User.objects.filter(role=User.Roles.MANAGER).count(), 0)

        call_command('seed_managers')

        manager = User.objects.get(email='manager@wowlance.demo')
        self.assertEqual(manager.role, User.Roles.MANAGER)
        self.assertEqual(manager.status, User.Status.ACTIVE)
        self.assertTrue(manager.is_email_verified)
        self.assertEqual(manager.first_name, 'Мария')
        self.assertEqual(manager.last_name, 'Менеджерова')
        self.assertTrue(manager.check_password('DemoPass123!'))

    def test_idempotent_on_second_run(self):
        """Второй запуск не создаёт дубль, а обновляет."""
        call_command('seed_managers')
        call_command('seed_managers')

        self.assertEqual(
            User.objects.filter(email='manager@wowlance.demo').count(),
            1,
        )

    def test_custom_password(self):
        """Флаг --password меняет пароль."""
        call_command('seed_managers', password='CustomPass123!')

        manager = User.objects.get(email='manager@wowlance.demo')
        self.assertTrue(manager.check_password('CustomPass123!'))

    def test_manager_has_no_freelancer_profile(self):
        """Менеджеру НЕ создаётся профиль фрилансера."""
        call_command('seed_managers')

        manager = User.objects.get(email='manager@wowlance.demo')
        self.assertFalse(hasattr(manager, 'freelancer_profile'))