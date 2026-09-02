"""Лёгкая модерация фрилансеров в Django admin.

Модерация — источник флага `is_verified`, по которому каталог решает, кого
показывать (см. `apps.profiles.tests`). Проверяется ровно два факта:

* staff-модератор может отобрать профили по верификации и массово
  верифицировать выбранные — флаг реально меняется;
* не-staff до действия не добирается, и профиль остаётся прежним.

Права даёт штатный Django admin — собственной проверки ролей у действий
нет, и весь admin здесь не тестируется.
"""

from django.test import TestCase
from django.urls import reverse

from apps.test_helpers import make_user
from apps.users.models import User

from .admin import FreelancerProfileAdmin
from .models import FreelancerProfile

CHANGELIST_URL = 'admin:profiles_freelancerprofile_changelist'


class ProfileModerationAdminTests(TestCase):
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

    def run_verify(self, profiles, user=None, *, follow=True):
        """Запускает массовое действие штатным путём — POST на changelist."""
        self.client.force_login(user or self.staff)
        return self.client.post(
            reverse(CHANGELIST_URL),
            {
                'action': 'verify_profiles',
                '_selected_action': [str(profile.pk) for profile in profiles],
            },
            follow=follow,
        )

    def test_staff_can_filter_by_verification_and_bulk_verify(self):
        """Отобрать неверифицированных и верифицировать их одним действием."""
        unverified = self.make_profile(is_verified=False)
        already_verified = self.make_profile(is_verified=True)

        self.assertIn('is_verified', FreelancerProfileAdmin.list_filter)

        self.client.force_login(self.staff)
        listing = self.client.get(
            reverse(CHANGELIST_URL), {'is_verified__exact': '0'}
        )
        shown = {obj.pk for obj in listing.context['cl'].queryset}
        self.assertIn(unverified.pk, shown)
        self.assertNotIn(already_verified.pk, shown)

        self.run_verify([unverified])

        unverified.refresh_from_db()
        self.assertTrue(unverified.is_verified)

    def test_non_staff_cannot_run_the_moderation_action(self):
        """Обычный фрилансер до массовой модерации не добирается."""
        profile = self.make_profile(is_verified=False)
        outsider = make_user(email='outsider@admin.test', role=User.Roles.FREELANCER)

        response = self.run_verify([profile], user=outsider, follow=False)

        # Django admin разворачивает не-staff на свой логин, действие не идёт.
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])
        profile.refresh_from_db()
        self.assertFalse(profile.is_verified)
