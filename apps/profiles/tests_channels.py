"""Структурированные каналы работы фрилансера (фундамент для будущего подбора).

`does_cold_calling` и `does_linkedin_outreach` — независимые признаки,
пригодные для жёсткого ORM-фильтра. Свободный текст `skills` для подбора
не используется и здесь не проверяется.
"""

from django.test import TestCase

from apps.profiles.models import FreelancerProfile
from apps.test_helpers import make_freelancer


class FreelancerChannelFieldsTests(TestCase):
    def setUp(self):
        self.freelancer = make_freelancer(email='channels@example.com')
        self.profile = self.freelancer.freelancer_profile

    def test_channels_default_to_false(self):
        self.assertFalse(self.profile.does_cold_calling)
        self.assertFalse(self.profile.does_linkedin_outreach)

    def test_channels_are_independent(self):
        self.profile.does_cold_calling = True
        self.profile.save(update_fields=['does_cold_calling'])
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.does_cold_calling)
        self.assertFalse(self.profile.does_linkedin_outreach)

        self.profile.does_linkedin_outreach = True
        self.profile.save(update_fields=['does_linkedin_outreach'])
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.does_cold_calling)
        self.assertTrue(self.profile.does_linkedin_outreach)

    def test_channels_are_filterable_by_orm(self):
        cold = self.profile
        cold.does_cold_calling = True
        cold.save(update_fields=['does_cold_calling'])

        linkedin_user = make_freelancer(email='linkedin@example.com')
        linkedin = linkedin_user.freelancer_profile
        linkedin.does_linkedin_outreach = True
        linkedin.save(update_fields=['does_linkedin_outreach'])

        both_user = make_freelancer(email='both@example.com')
        both = both_user.freelancer_profile
        both.does_cold_calling = True
        both.does_linkedin_outreach = True
        both.save(update_fields=['does_cold_calling', 'does_linkedin_outreach'])

        cold_ids = set(
            FreelancerProfile.objects.filter(does_cold_calling=True).values_list('id', flat=True)
        )
        linkedin_ids = set(
            FreelancerProfile.objects.filter(does_linkedin_outreach=True).values_list(
                'id', flat=True
            )
        )
        self.assertEqual(cold_ids, {cold.id, both.id})
        self.assertEqual(linkedin_ids, {linkedin.id, both.id})

    def test_skills_json_field_untouched(self):
        self.profile.skills = ['Холодные звонки', 'SPIN']
        self.profile.save(update_fields=['skills'])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.skills, ['Холодные звонки', 'SPIN'])
