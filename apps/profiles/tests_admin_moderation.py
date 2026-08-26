"""Лёгкая модерация фрилансеров в Django admin.

Проверяется ровно то, что добавил этот этап:

* `is_verified` можно отфильтровать в списке профилей;
* два массовых действия — «Верифицировать» и «Снять верификацию» —
  переводят выбранные профили и не трогают невыбранные;
* доступ к действиям даёт обычный Django admin, а не собственная проверка
  ролей: анонимный и не-staff пользователь до них не доходит.

Каталог и его бейдж «На модерации» здесь не проверяются — это
`apps.profiles.tests`, и дублировать их этот файл не должен.
"""

from django.contrib import admin
from django.test import TestCase
from django.urls import reverse

from apps.test_helpers import make_user
from apps.users.models import User

from .admin import FreelancerProfileAdmin
from .models import FreelancerProfile

CHANGELIST_URL = 'admin:profiles_freelancerprofile_changelist'


class ProfileModerationAdminTestCase(TestCase):
    """Staff-модератор и пул профилей в обоих состояниях верификации."""

    def setUp(self):
        self.staff = make_user(
            email='moderator@admin.test',
            role=User.Roles.ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self._profile_seq = 0

    def make_profile(self, *, is_verified):
        self._profile_seq += 1
        user = make_user(
            email=f'fr{self._profile_seq}@admin.test',
            role=User.Roles.FREELANCER,
            first_name=f'Фрилансер{self._profile_seq}',
        )
        return FreelancerProfile.objects.create(user=user, is_verified=is_verified)

    def run_action(self, action, profiles, user=None):
        """Запускает массовое действие штатным путём — POST на changelist."""
        self.client.force_login(user or self.staff)
        return self.client.post(
            reverse(CHANGELIST_URL),
            {
                'action': action,
                '_selected_action': [str(profile.pk) for profile in profiles],
            },
            follow=True,
        )

    def verified_map(self):
        return dict(
            FreelancerProfile.objects.values_list('pk', 'is_verified')
        )


# ---------------------------------------------------------------------------
# 1. Фильтр списка
# ---------------------------------------------------------------------------


class VerificationListFilterTests(ProfileModerationAdminTestCase):
    def test_is_verified_is_available_as_a_list_filter(self):
        """Модератор может отобрать профили по статусу верификации."""
        self.assertIn('is_verified', FreelancerProfileAdmin.list_filter)

    def test_changelist_accepts_the_is_verified_filter(self):
        """Фильтр не просто объявлен — он работает в самом списке."""
        verified = self.make_profile(is_verified=True)
        unverified = self.make_profile(is_verified=False)

        self.client.force_login(self.staff)
        response = self.client.get(
            reverse(CHANGELIST_URL), {'is_verified__exact': '0'}
        )

        shown = {obj.pk for obj in response.context['cl'].queryset}
        self.assertIn(unverified.pk, shown)
        self.assertNotIn(verified.pk, shown)


# ---------------------------------------------------------------------------
# 2. Массовая верификация
# ---------------------------------------------------------------------------


class BulkVerifyTests(ProfileModerationAdminTestCase):
    def test_bulk_verify_switches_several_profiles_to_true(self):
        """Несколько неверифицированных профилей верифицируются одним действием."""
        first = self.make_profile(is_verified=False)
        second = self.make_profile(is_verified=False)

        self.run_action('verify_profiles', [first, second])

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.is_verified)
        self.assertTrue(second.is_verified)

    def test_bulk_verify_leaves_unselected_profiles_alone(self):
        """Действие касается только выбранных строк."""
        selected = self.make_profile(is_verified=False)
        untouched = self.make_profile(is_verified=False)

        self.run_action('verify_profiles', [selected])

        untouched.refresh_from_db()
        self.assertFalse(untouched.is_verified)

    def test_bulk_verify_is_idempotent(self):
        """Повторная верификация уже верифицированного ничего не ломает."""
        profile = self.make_profile(is_verified=True)

        self.run_action('verify_profiles', [profile])

        profile.refresh_from_db()
        self.assertTrue(profile.is_verified)

    def test_bulk_verify_refreshes_updated_at(self):
        """`queryset.update()` не запускает `auto_now` — дата ставится явно."""
        profile = self.make_profile(is_verified=False)
        before = profile.updated_at

        self.run_action('verify_profiles', [profile])

        profile.refresh_from_db()
        self.assertGreater(profile.updated_at, before)


# ---------------------------------------------------------------------------
# 3. Массовое снятие верификации
# ---------------------------------------------------------------------------


class BulkUnverifyTests(ProfileModerationAdminTestCase):
    def test_bulk_unverify_switches_several_profiles_to_false(self):
        """Несколько верифицированных профилей возвращаются на модерацию."""
        first = self.make_profile(is_verified=True)
        second = self.make_profile(is_verified=True)

        self.run_action('unverify_profiles', [first, second])

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_verified)
        self.assertFalse(second.is_verified)

    def test_bulk_unverify_leaves_unselected_profiles_alone(self):
        """Снятие верификации тоже точечное."""
        selected = self.make_profile(is_verified=True)
        untouched = self.make_profile(is_verified=True)

        self.run_action('unverify_profiles', [selected])

        untouched.refresh_from_db()
        self.assertTrue(untouched.is_verified)

    def test_verify_and_unverify_are_mirror_operations(self):
        """Два действия возвращают выборку ровно в исходное состояние."""
        profiles = [self.make_profile(is_verified=False) for _ in range(3)]
        before = self.verified_map()

        self.run_action('verify_profiles', profiles)
        self.run_action('unverify_profiles', profiles)

        self.assertEqual(self.verified_map(), before)


# ---------------------------------------------------------------------------
# 4. Доступ
# ---------------------------------------------------------------------------


class ModerationAccessTests(ProfileModerationAdminTestCase):
    def test_both_actions_are_registered_in_admin(self):
        """Действия видны в списке ровно под своими подписями."""
        model_admin = admin.site._registry[FreelancerProfile]
        request = self.client.request().wsgi_request
        request.user = self.staff

        actions = model_admin.get_actions(request)

        self.assertIn('verify_profiles', actions)
        self.assertIn('unverify_profiles', actions)
        self.assertEqual(actions['verify_profiles'][2], 'Верифицировать')
        self.assertEqual(actions['unverify_profiles'][2], 'Снять верификацию')

    def test_anonymous_user_cannot_run_the_action(self):
        """Без входа в admin действие не выполняется, а уводит на логин."""
        profile = self.make_profile(is_verified=False)

        response = self.client.post(
            reverse(CHANGELIST_URL),
            {'action': 'verify_profiles', '_selected_action': [str(profile.pk)]},
            follow=True,
        )

        profile.refresh_from_db()
        self.assertFalse(profile.is_verified)
        self.assertContains(response, 'assword')

    def test_non_staff_user_cannot_run_the_action(self):
        """Обычный фрилансер до массовой модерации не добирается."""
        profile = self.make_profile(is_verified=False)
        outsider = make_user(email='outsider@admin.test', role=User.Roles.FREELANCER)

        self.run_action('verify_profiles', [profile], user=outsider)

        profile.refresh_from_db()
        self.assertFalse(profile.is_verified)

    def test_moderation_does_not_introduce_its_own_role_check(self):
        """Права даёт штатный Django admin, своей роли модератора нет.

        Пользователь со `is_staff` и правом на изменение профилей проходит
        независимо от продуктовой роли (`User.Roles`), потому что действия
        её не смотрят.
        """
        profile = self.make_profile(is_verified=False)
        staff_director = make_user(
            email='staff-director@admin.test',
            role=User.Roles.DIRECTOR,
            is_staff=True,
            is_superuser=True,
        )

        self.run_action('verify_profiles', [profile], user=staff_director)

        profile.refresh_from_db()
        self.assertTrue(profile.is_verified)


# ---------------------------------------------------------------------------
# 5. Границы этапа
# ---------------------------------------------------------------------------


class ModerationBoundaryTests(ProfileModerationAdminTestCase):
    def test_admin_module_does_not_import_room_modules(self):
        """ADR-001: BIZ не зависит от ROOM — модерация этого не меняет."""
        from . import admin as profiles_admin

        source = __import__('inspect').getsource(profiles_admin)

        self.assertNotIn('apps.rooms', source)
        self.assertNotIn('apps.pipeline', source)

    def test_moderation_uses_the_existing_flag_only(self):
        """Ни новой модели, ни нового статуса: меняется тот же `is_verified`."""
        field = FreelancerProfile._meta.get_field('is_verified')

        self.assertEqual(field.get_internal_type(), 'BooleanField')
