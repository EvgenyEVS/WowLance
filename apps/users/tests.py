"""Вход в платформу: регистрация, активация, логин, referral-stub, seed.

Продуктовый контур BIZ-идентичности:

* регистрация создаёт PENDING-пользователя и шлёт письмо со ссылкой;
* ссылка активации переводит в ACTIVE, заводит профиль и логинит;
* логин работает для ACTIVE и закрыт для PENDING;
* `?ref=wowtalent_...` — stub-клиент WOW Talent (ADR-001);
* `seed_managers` — демо-менеджер с нужной ролью, идемпотентно.
"""

from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.profiles.models import FreelancerProfile
from apps.test_helpers import make_director, make_user
from apps.users.models import User
from apps.users.tokens import account_activation_token

PASSWORD = 'StrongPass123!'


class RegistrationTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_register_creates_pending_user_and_sends_activation_mail(self):
        response = self.client.post(
            reverse('users:register') + '?role=freelancer',
            {
                'first_name': 'Иван',
                'last_name': 'Петров',
                'email': 'ivan@example.com',
                'password1': PASSWORD,
                'password2': PASSWORD,
                'role': User.Roles.FREELANCER,
            },
        )

        self.assertRedirects(response, reverse('users:login'))
        user = User.objects.get(email='ivan@example.com')
        self.assertEqual(user.role, User.Roles.FREELANCER)
        self.assertEqual(user.status, User.Status.PENDING)
        self.assertFalse(user.is_active)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('/activate/', mail.outbox[0].body)

    def test_register_rejects_admin_role(self):
        """Роль приходит из браузера — ADMIN себе назначить нельзя."""
        response = self.client.post(
            reverse('users:register'),
            {
                'first_name': 'Hack',
                'last_name': 'Admin',
                'email': 'hack@example.com',
                'password1': PASSWORD,
                'password2': PASSWORD,
                'role': User.Roles.ADMIN,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email='hack@example.com').exists())
        self.assertTrue(response.context['form'].errors)


class ActivationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(
            email='pending@example.com',
            role=User.Roles.FREELANCER,
            status=User.Status.PENDING,
            password=PASSWORD,
        )

    def activation_url(self, token):
        return reverse(
            'users:activate',
            kwargs={
                'uidb64': urlsafe_base64_encode(force_bytes(self.user.pk)),
                'token': token,
            },
        )

    def test_activation_link_creates_profile_and_logs_in(self):
        token = account_activation_token.make_token(self.user)

        response = self.client.get(self.activation_url(token))

        self.assertRedirects(response, reverse('core:home'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.ACTIVE)
        self.assertTrue(self.user.is_email_verified)
        self.assertTrue(self.user.is_active)
        self.assertTrue(FreelancerProfile.objects.filter(user=self.user).exists())
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_activation_rejects_invalid_token(self):
        response = self.client.get(self.activation_url('bad-token'))

        self.assertRedirects(response, reverse('users:resend_activation'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.PENDING)


class LoginTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.active = make_director(email='boss@example.com', password=PASSWORD)

    def test_login_succeeds_for_active_and_is_blocked_for_pending(self):
        """Одна дверь, два состояния: ACTIVE входит, PENDING идёт за письмом."""
        response = self.client.post(
            reverse('users:login'),
            {'username': self.active.email, 'password': PASSWORD},
        )
        self.assertRedirects(response, reverse('core:home'))

        self.client.logout()
        pending = make_user(
            email='wait@example.com',
            role=User.Roles.DIRECTOR,
            status=User.Status.PENDING,
            password=PASSWORD,
        )

        blocked = self.client.post(
            reverse('users:login'),
            {'username': pending.email, 'password': PASSWORD},
        )

        self.assertEqual(blocked.status_code, 302)
        self.assertIn('/resend-activation/', blocked['Location'])
        self.assertFalse(blocked.wsgi_request.user.is_authenticated)

    def test_login_open_redirect_is_blocked(self):
        response = self.client.post(
            reverse('users:login'),
            {
                'username': self.active.email,
                'password': PASSWORD,
                'next': 'https://evil.example/phish',
            },
        )

        self.assertRedirects(response, reverse('core:home'))


class WowTalentRefRegistrationTests(TestCase):
    def test_wowtalent_ref_prefills_form_and_shows_banner(self):
        """ADR-001: клиент WOW Talent — stub в BIZ, контракт ответа зафиксирован."""
        response = self.client.get(
            reverse('users:register') + '?role=freelancer&ref=wowtalent_demo'
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_wowtalent_ref'])
        form = response.context['form']
        self.assertEqual(form.initial.get('first_name'), 'Иван')
        self.assertEqual(form.initial.get('last_name'), 'Иванов')
        self.assertEqual(form.initial.get('email'), 'ivan.ivanov@wowtalent.demo')
        self.assertEqual(form.initial.get('role'), 'freelancer')


class SeedManagersCommandTests(TestCase):
    def test_seed_managers_creates_manager_role_and_is_idempotent(self):
        """Создаётся менеджер, роль именно MANAGER, повтор не плодит дубля."""
        self.assertEqual(User.objects.filter(role=User.Roles.MANAGER).count(), 0)

        call_command('seed_managers', stdout=StringIO())

        manager = User.objects.get(email='manager@wowlance.demo')
        self.assertEqual(manager.role, User.Roles.MANAGER)
        self.assertEqual(manager.status, User.Status.ACTIVE)
        self.assertTrue(manager.is_email_verified)
        # Менеджер — не фрилансер: профиль исполнителя ему не заводится.
        self.assertFalse(hasattr(manager, 'freelancer_profile'))

        call_command('seed_managers', stdout=StringIO())

        self.assertEqual(
            User.objects.filter(email='manager@wowlance.demo').count(), 1
        )
        self.assertEqual(User.objects.filter(role=User.Roles.MANAGER).count(), 1)
