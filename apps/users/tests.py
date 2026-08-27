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
        self.assertContains(response, 'роль', status_code=200)

    def test_register_duplicate_email_shows_error(self):
        make_user(
            email='taken@example.com',
            role=User.Roles.FREELANCER,
            status=User.Status.ACTIVE,
        )
        response = self.client.post(
            reverse('users:register') + '?role=freelancer',
            {
                'first_name': 'Другой',
                'last_name': 'Юзер',
                'email': 'Taken@example.com',
                'password1': 'StrongPass123!',
                'password2': 'StrongPass123!',
                'role': User.Roles.FREELANCER,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'уже зарегистрирован')
        self.assertEqual(User.objects.filter(email__iexact='taken@example.com').count(), 1)

    def test_register_page_without_role_is_usable(self):
        response = self.client.get(reverse('users:register'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Директор')
        self.assertContains(response, 'Фрилансер')
        self.assertContains(response, 'Зарегистрироваться')
        self.assertNotContains(response, 'disabled')

    @override_settings(DEBUG=True)
    def test_register_reuses_pending_account_and_resends_link(self):
        pending = make_user(
            email='ghost@example.com',
            role=User.Roles.DIRECTOR,
            status=User.Status.PENDING,
            password='OldPass123!',
        )
        response = self.client.post(
            reverse('users:register') + '?role=director',
            {
                'first_name': 'Новый',
                'last_name': 'Директор',
                'email': 'ghost@example.com',
                'password1': 'NewPass123!',
                'password2': 'NewPass123!',
                'role': User.Roles.DIRECTOR,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'users/registration_success.html')
        self.assertEqual(User.objects.filter(email='ghost@example.com').count(), 1)
        pending.refresh_from_db()
        self.assertEqual(pending.first_name, 'Новый')
        self.assertTrue(pending.check_password('NewPass123!'))
        self.assertEqual(pending.status, User.Status.PENDING)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(DEBUG=True)
    def test_resend_activation_for_pending(self):
        make_user(
            email='wait2@example.com',
            role=User.Roles.FREELANCER,
            status=User.Status.PENDING,
        )
        response = self.client.post(
            reverse('users:resend_activation'),
            {'email': 'wait2@example.com'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'users/registration_success.html')
        self.assertEqual(len(mail.outbox), 1)

    def test_login_pending_with_correct_password_points_to_resend(self):
        user = make_user(
            email='pendlogin@example.com',
            role=User.Roles.DIRECTOR,
            status=User.Status.PENDING,
            password='StrongPass123!',
        )
        response = self.client.post(
            reverse('users:login'),
            {'username': user.email, 'password': 'StrongPass123!'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/resend-activation/', response['Location'])


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
        self.assertRedirects(response, reverse('users:resend_activation'))
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

    def test_logout_requires_post(self):
        self.client.login(username='boss@example.com', password=self.password)
        response = self.client.get(reverse('users:logout'))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_logout_post_clears_session(self):
        self.client.login(username='boss@example.com', password=self.password)
        response = self.client.post(reverse('users:logout'))
        self.assertRedirects(response, reverse('core:home'))
        # После logout следующий запрос — аноним
        home = self.client.get(reverse('core:home'))
        self.assertNotContains(home, 'Выйти')
        self.assertContains(home, 'Войти')

    def test_base_uses_local_assets_not_cdn(self):
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn('fonts.googleapis.com', content)
        self.assertNotIn('fonts.gstatic.com', content)
        self.assertNotIn('unpkg.com', content)
        self.assertIn('/static/css/fonts.css', content)
        self.assertIn('/static/js/htmx.min.js', content)

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
        self.assertEqual(response.status_code, 302)
        self.assertIn('/resend-activation/', response['Location'])
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


class WowTalentRefRegistrationTest(TestCase):
    def test_wowtalent_ref_prefills_form_and_shows_banner(self):
        """Тест: известный реферальный код заполняет форму и включает флаг баннера."""
        url = reverse('users:register') + '?role=freelancer&ref=wowtalent_demo'
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)

        # 1. Проверяем, что флаг для баннера передан в контекст
        self.assertTrue(response.context['is_wowtalent_ref'])

        # 2. Проверяем, что форма получила initial-данные из stub-клиента
        form = response.context['form']
        self.assertEqual(form.initial.get('first_name'), 'Иван')
        self.assertEqual(form.initial.get('last_name'), 'Иванов')
        self.assertEqual(form.initial.get('email'), 'ivan.ivanov@wowtalent.demo')
        self.assertEqual(form.initial.get('role'), 'freelancer')

    def test_unknown_ref_does_not_break_form(self):
        """Тест: неизвестный реферальный код не ломает форму и не показывает баннер."""
        url = reverse('users:register') + '?role=freelancer&ref=unknown_code_xyz'
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)

        # 1. Флага баннера быть не должно
        self.assertFalse(response.context.get('is_wowtalent_ref', False))

        # 2. Форма должна содержать только role, остальные поля пустые
        form = response.context['form']
        self.assertEqual(form.initial.get('role'), 'freelancer')
        self.assertIsNone(form.initial.get('first_name'))
        self.assertIsNone(form.initial.get('last_name'))
        self.assertIsNone(form.initial.get('email'))