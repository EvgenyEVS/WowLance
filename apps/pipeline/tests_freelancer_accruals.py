"""Кнопки заработка фрилансера и журнал начислений."""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.pipeline.accruals import DEMO_ACCRUAL_USD_PER_APPROVED_REPORT
from apps.pipeline.models import FreelancerAccrual, Task
from apps.pipeline.services import create_task, review_report, submit_report
from apps.rooms.models import Project
from apps.rooms.services import add_freelancer_to_room, assign_teamlead, launch_project
from apps.test_helpers import make_director, make_freelancer, make_teamlead


PASSWORD = 'TestPass123!'


def _png(name='shot.png'):
    return SimpleUploadedFile(name, b'\x89PNG\r\n\x1a\nfake', content_type='image/png')


class FreelancerAccrualsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir@accrual.test', password=PASSWORD)
        self.teamlead = make_teamlead(email='tl@accrual.test', password=PASSWORD)
        self.freelancer = make_freelancer(email='fr@accrual.test', password=PASSWORD)
        self.outsider = make_freelancer(email='out@accrual.test', password=PASSWORD)
        self.project = Project.objects.create(
            owner=self.director,
            name='Проект начислений',
            input_data={
                'offer': 'Оффер',
                'utp': 'УТП',
                'audience': 'ЦА',
                'hot_criteria': 'Hot',
            },
            status=Project.Status.DRAFT,
        )
        launch_project(self.project)
        assign_teamlead(self.project, self.teamlead)
        add_freelancer_to_room(self.project.room, self.freelancer)
        self.overview_url = reverse(
            'rooms:room_overview', kwargs={'project_id': self.project.id}
        )
        self.earnings_url = reverse('pipeline:freelancer_accruals')
        self.project_earnings_url = reverse(
            'pipeline:freelancer_project_accruals',
            kwargs={'project_id': self.project.id},
        )

    def _approve_report(self, *, project=None, title='Задача на отчёт'):
        project = project or self.project
        task = create_task(
            project=project,
            assignee=self.freelancer,
            created_by=self.teamlead,
            title=title,
        )
        report = submit_report(
            task=task,
            author=self.freelancer,
            content_text='Достаточно длинный текст отчёта для валидации.',
            attachment=_png(),
        )
        review_report(
            report=report,
            reviewer=self.teamlead,
            approve=True,
            comment='Ок',
        )
        return report

    def test_freelancer_sees_earnings_buttons_with_zero(self):
        self.client.force_login(self.freelancer)
        home = self.client.get(reverse('core:home'))
        self.assertEqual(home.status_code, 200)
        self.assertContains(home, 'Я заработал всего 0 долларов на WowLance')
        self.assertContains(home, f'href="{self.earnings_url}"')

        overview = self.client.get(self.overview_url)
        self.assertEqual(overview.status_code, 200)
        self.assertContains(overview, 'Я заработал всего 0 долларов на WowLance')
        self.assertContains(
            overview, 'Я заработал 0 долларов на этом проекте'
        )
        self.assertContains(overview, f'href="{self.project_earnings_url}"')
        body = overview.content.decode()
        self.assertIn('<a ', body[body.index('Я заработал 0 долларов на этом проекте') - 120:])

    def test_director_and_teamlead_do_not_see_earnings_buttons(self):
        for user in (self.director, self.teamlead):
            with self.subTest(role=user.role):
                self.client.force_login(user)
                overview = self.client.get(self.overview_url)
                self.assertEqual(overview.status_code, 200)
                self.assertNotContains(overview, 'Я заработал всего')
                self.assertNotContains(overview, 'на этом проекте')

    def test_earnings_pages_rbac(self):
        self.client.force_login(self.freelancer)
        self.assertEqual(self.client.get(self.earnings_url).status_code, 200)
        response = self.client.get(self.project_earnings_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'room-tabs')
        self.assertContains(response, 'Начислений пока нет.')

        for user in (self.director, self.teamlead):
            with self.subTest(role=user.role):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.earnings_url).status_code, 403)
                self.assertEqual(
                    self.client.get(self.project_earnings_url).status_code, 403
                )

        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.project_earnings_url).status_code, 403)

    def test_approve_creates_accrual_and_updates_buttons(self):
        self._approve_report()
        self.assertEqual(FreelancerAccrual.objects.count(), 1)
        accrual = FreelancerAccrual.objects.get()
        self.assertEqual(accrual.amount, DEMO_ACCRUAL_USD_PER_APPROVED_REPORT)
        self.assertEqual(accrual.freelancer_id, self.freelancer.id)
        self.assertEqual(accrual.project_id, self.project.id)

        self.client.force_login(self.freelancer)
        overview = self.client.get(self.overview_url)
        self.assertContains(overview, 'Я заработал всего 10 долларов на WowLance')
        self.assertContains(overview, 'Я заработал 10 долларов на этом проекте')

        other = Project.objects.create(
            owner=self.director,
            name='Второй проект',
            input_data={
                'offer': 'o', 'utp': 'u', 'audience': 'a', 'hot_criteria': 'h',
            },
            status=Project.Status.DRAFT,
        )
        launch_project(other)
        assign_teamlead(other, self.teamlead)
        add_freelancer_to_room(other.room, self.freelancer)
        self._approve_report(project=other, title='Отчёт на втором')

        overview = self.client.get(self.overview_url)
        self.assertContains(overview, 'Я заработал всего 20 долларов на WowLance')
        self.assertContains(overview, 'Я заработал 10 долларов на этом проекте')

        home = self.client.get(reverse('core:home'))
        self.assertContains(home, 'Я заработал всего 20 долларов на WowLance')

    def test_reject_and_repeat_approve_do_not_duplicate(self):
        task = create_task(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.teamlead,
            title='Отклонят',
        )
        report = submit_report(
            task=task,
            author=self.freelancer,
            content_text='Достаточно длинный текст отчёта для валидации.',
            attachment=_png('r1.png'),
        )
        review_report(
            report=report,
            reviewer=self.teamlead,
            approve=False,
            comment='Переделать',
        )
        self.assertEqual(FreelancerAccrual.objects.count(), 0)

        report2 = submit_report(
            task=task,
            author=self.freelancer,
            content_text='Вторая попытка — достаточно длинный текст отчёта.',
            attachment=_png('r2.png'),
        )
        review_report(
            report=report2,
            reviewer=self.teamlead,
            approve=True,
            comment='Ок',
        )
        self.assertEqual(FreelancerAccrual.objects.count(), 1)

        # Повторный approve уже проверенного отчёта запрещён сервисом.
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            review_report(
                report=report2,
                reviewer=self.teamlead,
                approve=True,
                comment='Ещё раз',
            )
        self.assertEqual(FreelancerAccrual.objects.count(), 1)
        self.assertEqual(
            FreelancerAccrual.objects.get().amount,
            Decimal('10'),
        )
