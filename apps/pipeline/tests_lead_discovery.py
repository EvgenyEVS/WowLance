"""Чеклист квалификации на карточке лида: факты ≠ статус колонки."""

from django.test import TestCase
from django.urls import reverse

from apps.pipeline.discovery import (
    HINT_COLD,
    HINT_HOT_READY,
    HINT_WARM,
    discovery_hint_key,
)
from apps.pipeline.models import Lead, Task
from apps.pipeline.services import create_lead, set_lead_qualification
from apps.pipeline.tests import PASSWORD, PipelineProjectMixin
from apps.test_helpers import make_user
from apps.users.models import User
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


class LeadDiscoveryManagerHandoffTests(PipelineProjectMixin, TestCase):
    """Менеджер после Hot handoff: карточка лида и чеклист — только чтение.

    Права даёт сам лид (`assigned_manager`), а не членство в комнате:
    менеджер платформы `RoomMember` не является, поэтому доска лидов и
    «Обзор» для него закрыты и после handoff.
    """

    def setUp(self):
        self._build_project('handoff')
        self.lead = create_lead(
            project=self.project,
            creator=self.freelancer,
            contact_info={'name': 'Пётр Горячий', 'phone': '+79005556677'},
            source=Lead.Source.LINKEDIN,
            qualification_status=Lead.Qualification.WARM,
        )
        # Handoff — существующей логикой, а не присваиванием assigned_manager:
        # тест обязан ломаться вместе с продуктовым сценарием.
        set_lead_qualification(
            lead=self.lead,
            new_status=Lead.Qualification.HOT,
            changed_by=self.teamlead,
            matched_hot_criteria=['Запросил демо'],
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.assigned_manager_id, self.manager.id)
        self.handoff_task = Task.objects.get(
            lead=self.lead,
            task_type=Task.TaskType.MANAGER_HANDOFF,
        )
        # Второй менеджер создаётся после handoff: `pick_manager_for_lead`
        # берёт первого по `date_joined`, и выбор остаётся однозначным.
        self.other_manager = make_user(
            email='m2handoff@pipe.test',
            role=User.Roles.MANAGER,
            password=PASSWORD,
        )
        self.detail_url = reverse(
            'pipeline:lead_detail',
            kwargs={'project_id': self.project.id, 'lead_id': self.lead.id},
        )
        self.discovery_url = reverse(
            'pipeline:lead_discovery',
            kwargs={'project_id': self.project.id, 'lead_id': self.lead.id},
        )

    def test_assigned_manager_reads_card_and_checklist_without_save(self):
        self.client.force_login(self.manager)

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Пётр Горячий')
        self.assertContains(response, 'Чеклист квалификации')
        self.assertContains(response, 'Следующий шаг')
        self.assertFalse(response.context['can_edit_discovery'])
        self.assertNotContains(response, 'Сохранить чеклист')

    def test_assigned_manager_cannot_post_discovery(self):
        self.lead.discovery_checks = {'need_task': True}
        self.lead.save(update_fields=['discovery_checks', 'updated_at'])
        self.client.force_login(self.manager)

        response = self.client.post(
            self.discovery_url,
            {'need_task': 'on', 'next_demo': 'on'},
        )

        self.assertEqual(response.status_code, 403)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.discovery_checks, {'need_task': True})

    def test_other_manager_cannot_open_the_lead(self):
        self.client.force_login(self.other_manager)

        self.assertEqual(self.client.get(self.detail_url).status_code, 403)
        self.assertEqual(
            self.client.post(self.discovery_url, {'need_task': 'on'}).status_code,
            403,
        )

    def test_assigned_manager_does_not_get_the_room(self):
        """Чтение одного лида не открывает доску лидов и «Обзор»."""
        self.client.force_login(self.manager)

        leads_board = self.client.get(
            reverse('pipeline:room_leads', kwargs={'project_id': self.project.id})
        )
        overview = self.client.get(
            reverse('rooms:room_overview', kwargs={'project_id': self.project.id})
        )

        self.assertEqual(leads_board.status_code, 403)
        self.assertEqual(overview.status_code, 403)

    def test_assigned_manager_field_alone_does_not_open_the_lead(self):
        """Исключение — только для роли MANAGER, а не для любого в поле.

        `Lead.assigned_manager` — обычный FK и ролью не ограничен, поэтому
        чужой тимлид, технически проставленный в это поле, обязан получить
        403: доступ мимо комнаты задуман для менеджера платформы с handoff.
        """
        outsider_teamlead = make_user(
            email='t2handoff@pipe.test',
            role=User.Roles.TEAMLEAD,
            password=PASSWORD,
        )
        self.lead.assigned_manager = outsider_teamlead
        self.lead.save(update_fields=['assigned_manager', 'updated_at'])
        self.client.force_login(outsider_teamlead)

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 403)

    def test_handoff_task_detail_links_to_the_lead(self):
        self.client.force_login(self.manager)

        response = self.client.get(
            reverse(
                'pipeline:task_detail',
                kwargs={'project_id': self.project.id, 'task_id': self.handoff_task.id},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['can_open_lead'])
        self.assertContains(response, f'href="{self.detail_url}"')
