from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.profiles.models import FreelancerProfile
from apps.test_helpers import make_director, make_freelancer, make_user
from apps.users.models import User
from apps.users.tokens import account_activation_token


class RegistrationTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_register_freelancer_creates_pending_user_and_sends_mail(self):
        # Django test runner всегда ставит DEBUG=False → редирект на login
        response = self.client.post(
            reverse('users:register') + '?role=freelancer',
            {
                'first_name': 'Иван',
                'last_name': 'Петров',
                'email': 'ivan@example.com',
                'password1': 'StrongPass123!',
                'password2': 'StrongPass123!',
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

    @override_settings(DEBUG=True)
    def test_register_shows_activation_link_in_debug(self):
        response = self.client.post(
            reverse('users:register') + '?role=freelancer',
            {
                'first_name': 'Иван',
                'last_name': 'Петров',
                'email': 'ivan-debug@example.com',
                'password1': 'StrongPass123!',
                'password2': 'StrongPass123!',
                'role': User.Roles.FREELANCER,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'users/registration_success.html')
        self.assertIn('activate', response.context['activation_url'])

    def test_register_rejects_admin_role(self):
        response = self.client.post(
            reverse('users:register'),
            {
                'first_name': 'Hack',
                'last_name': 'Admin',
                'email': 'hack@example.com',
                'password1': 'StrongPass123!',
                'password2': 'StrongPass123!',
                'role': User.Roles.ADMIN,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email='hack@example.com').exists())
        self.assertTrue(response.context['form'].errors)

    def test_register_without_role_fails(self):
        response = self.client.post(
            reverse('users:register'),
            {
                'first_name': 'No',
                'last_name': 'Role',
                'email': 'norole@example.com',
                'password1': 'StrongPass123!',
                'password2': 'StrongPass123!',
            },
        )
        self.assertFalse(User.objects.filter(email='norole@example.com').exists())


class ActivationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = make_user(
            email='pending@example.com',
            role=User.Roles.FREELANCER,
            status=User.Status.PENDING,
            password='StrongPass123!',
        )

    def test_activate_freelancer_creates_profile_and_logs_in(self):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = account_activation_token.make_token(self.user)
        response = self.client.get(
            reverse('users:activate', kwargs={'uidb64': uid, 'token': token}),
        )
        self.assertRedirects(response, reverse('core:home'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.ACTIVE)
        self.assertTrue(self.user.is_email_verified)
        self.assertTrue(self.user.is_active)
        self.assertTrue(
            FreelancerProfile.objects.filter(user=self.user).exists()
        )
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_activate_invalid_token(self):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        response = self.client.get(
            reverse('users:activate', kwargs={'uidb64': uid, 'token': 'bad-token'}),
        )
        self.assertRedirects(response, reverse('users:login'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.PENDING)


class LoginTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'StrongPass123!'
        self.user = make_director(
            email='boss@example.com',
            password=self.password,
        )

    def test_login_success(self):
        response = self.client.post(
            reverse('users:login'),
            {'username': 'boss@example.com', 'password': self.password},
        )
        self.assertRedirects(response, reverse('core:home'))

    def test_login_pending_blocked(self):
        pending = make_user(
            email='wait@example.com',
            role=User.Roles.DIRECTOR,
            status=User.Status.PENDING,
            password=self.password,
        )
        response = self.client.post(
            reverse('users:login'),
            {'username': pending.email, 'password': self.password},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_login_open_redirect_blocked(self):
        response = self.client.post(
            reverse('users:login'),
            {
                'username': 'boss@example.com',
                'password': self.password,
                'next': 'https://evil.example/phish',
            },
        )
        self.assertRedirects(response, reverse('core:home'))
