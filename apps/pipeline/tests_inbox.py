from django.test import TestCase, Client
from django.urls import reverse
from apps.users.models import User


class ManagerInboxEmptyStateTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.manager = User.objects.create_user(
            username='manager@test.com',
            email='manager@test.com',
            password='TestPass123!',
            role=User.Roles.MANAGER,
            status=User.Status.ACTIVE,
        )

    def test_inbox_shows_seed_hint_when_no_managers(self):
        """Если менеджеров в системе нет — показывается подсказка про seed."""
        # 1. Удаляем менеджера, созданного в setUp, чтобы проверить подсказку
        self.manager.delete()

        # 2. Создаём админа, который имеет доступ к inbox, но не является менеджером
        admin_user = User.objects.create_user(
            username='admin@test.com',
            email='admin@test.com',
            password='TestPass123!',
            role=User.Roles.ADMIN,
            status=User.Status.ACTIVE,
        )
        self.client.force_login(admin_user)

        # 3. Убеждаемся, что активных менеджеров в системе действительно нет
        self.assertEqual(
            User.objects.filter(role=User.Roles.MANAGER, status=User.Status.ACTIVE).count(),
            0
        )

        # 4. Проверяем ответ
        response = self.client.get(reverse('pipeline:manager_inbox'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Открытых задач по Hot-лидам нет')
        self.assertContains(response, 'seed_managers')

    def test_inbox_without_tasks_has_clear_message(self):
        """Если задачи есть, но их нет — показывается понятное сообщение."""
        self.client.force_login(self.manager)
        response = self.client.get(reverse('pipeline:manager_inbox'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Открытых задач по Hot-лидам нет')