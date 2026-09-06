"""Чеклист квалификации на карточке лида: факты ≠ статус колонки."""

from django.test import TestCase
from django.urls import reverse

from apps.pipeline.discovery import (
    HINT_COLD,
    HINT_HOT_READY,
    HINT_WARM,
    discovery_hint_key,
)
from apps.pipeline.models import Lead
from apps.pipeline.services import create_lead, set_lead_qualification
from apps.pipeline.tests import PipelineProjectMixin
from django.core.exceptions import PermissionDenied


class LeadDiscoveryHintUnitTests(TestCase):
    def test_hint_matrix(self):
        cases = (
            ({}, HINT_COLD),
            ({'need_task': True, 'talk_replied': True}, HINT_WARM),
            (
                {
                    'need_task': True,
                    'time_has': True,
                    'next_demo': True,
                },
                HINT_HOT_READY,
            ),
            ({'talk_replied': True}, 'thin'),
            ({'need_task': True}, 'thin'),
        )
        for checks, expected in cases:
            with self.subTest(checks=checks, expected=expected):
                self.assertEqual(discovery_hint_key(checks), expected)


class LeadDiscoveryHttpTests(PipelineProjectMixin, TestCase):
    def setUp(self):
        self._build_project('disc')
        self.lead = create_lead(
            project=self.project,
            creator=self.freelancer,
            contact_info={'name': 'Игорь Клиент', 'phone': '+79001112233'},
            source=Lead.Source.BASE,
            notes='После звонка',
            qualification_status=Lead.Qualification.COLD,
        )
        self.detail_url = reverse(
            'pipeline:lead_detail',
            kwargs={'project_id': self.project.id, 'lead_id': self.lead.id},
        )
        self.discovery_url = reverse(
            'pipeline:lead_discovery',
            kwargs={'project_id': self.project.id, 'lead_id': self.lead.id},
        )

    def test_owner_freelancer_sees_grid_and_checklist_groups(self):
        self.client.force_login(self.freelancer)
        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'lead-detail-grid')
        self.assertContains(response, 'Игорь Клиент')
        self.assertContains(response, '+79001112233')
        self.assertContains(response, 'Need')
        self.assertContains(response, 'Следующий шаг')
        self.assertContains(response, 'Сохранить чеклист')
        self.assertEqual(response.context['discovery_hint'], HINT_COLD)

    def test_warm_checks_do_not_change_qualification_status(self):
        self.client.force_login(self.freelancer)
        response = self.client.post(
            self.discovery_url,
            {'need_task': 'on', 'talk_replied': 'on'},
        )

        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.qualification_status, Lead.Qualification.COLD)
        self.assertTrue(self.lead.discovery_checks.get('need_task'))
        self.assertTrue(self.lead.discovery_checks.get('talk_replied'))
        self.assertFalse(self.lead.discovery_checks.get('next_demo'))

        page = self.client.get(self.detail_url)
        self.assertEqual(page.context['discovery_hint'], HINT_WARM)
        self.assertEqual(page.context['discovery_hint'], discovery_hint_key(self.lead.discovery_checks))

    def test_hot_ready_hint_without_hot_status(self):
        self.client.force_login(self.freelancer)
        self.client.post(
            self.discovery_url,
            {
                'need_task': 'on',
                'time_has': 'on',
                'next_demo': 'on',
            },
        )
        self.lead.refresh_from_db()

        self.assertEqual(self.lead.qualification_status, Lead.Qualification.COLD)
        self.assertIsNone(self.lead.hot_handoff_at)
        self.assertEqual(discovery_hint_key(self.lead.discovery_checks), HINT_HOT_READY)

        page = self.client.get(self.detail_url)
        self.assertEqual(page.context['discovery_hint'], HINT_HOT_READY)
        self.assertIn('тимлиду', page.context['discovery_hint_text'])

    def test_outsider_freelancer_discovery_forbidden(self):
        self.client.force_login(self.outsider)
        response = self.client.post(
            self.discovery_url,
            {'need_task': 'on'},
        )
        self.assertEqual(response.status_code, 403)

    def test_director_reads_but_cannot_post_discovery(self):
        self.client.force_login(self.director)
        get_response = self.client.get(self.detail_url)
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, 'Чеклист квалификации')
        self.assertFalse(get_response.context['can_edit_discovery'])
        self.assertNotContains(get_response, 'Сохранить чеклист')

        post_response = self.client.post(
            self.discovery_url,
            {'need_task': 'on'},
        )
        self.assertEqual(post_response.status_code, 403)
        self.lead.refresh_from_db()
        self.assertFalse(bool(self.lead.discovery_checks.get('need_task')))

    def test_teamlead_can_save_discovery_and_still_has_hot_form(self):
        self.client.force_login(self.teamlead)
        response = self.client.post(
            self.discovery_url,
            {'need_task': 'on', 'talk_questions': 'on', 'budget_named': 'on'},
        )
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertTrue(self.lead.discovery_checks.get('budget_named'))

        page = self.client.get(self.detail_url)
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context['can_edit_discovery'])
        self.assertIsNotNone(page.context['qualify_form'])
        self.assertContains(page, 'Квалификация (тимлид)')


class LeadDiscoveryDoesNotBypassHotGuardTests(PipelineProjectMixin, TestCase):
    """Фрилансер с полным next_* всё ещё не ставит Hot через set_lead_qualification."""

    def setUp(self):
        self._build_project('hotguard')
        self.lead = create_lead(
            project=self.project,
            creator=self.freelancer,
            contact_info={'email': 'guard@ex.com'},
            source=Lead.Source.OTHER,
            qualification_status=Lead.Qualification.WARM,
        )

    def test_full_next_checks_still_cannot_qualify_hot(self):
        self.lead.discovery_checks = {
            'need_task': True,
            'time_has': True,
            'next_demo': True,
            'next_proposal': True,
            'next_this_week': True,
            'next_leaning': True,
        }
        self.lead.save(update_fields=['discovery_checks', 'updated_at'])

        with self.assertRaises(PermissionDenied):
            set_lead_qualification(
                lead=self.lead,
                new_status=Lead.Qualification.HOT,
                changed_by=self.freelancer,
                matched_hot_criteria=['Запросил демо'],
            )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.qualification_status, Lead.Qualification.WARM)
