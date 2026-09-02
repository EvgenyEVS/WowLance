"""Core: публичные страницы, кабинеты по ролям, демо-режим и финполоса.

Шесть инвариантов уровня продукта:

* лендинг открыт гостю, кабинет открывается каждой рабочей роли;
* демо-режим показывает ссылку активации без DEBUG, а seed-команда
  поднимает демо-проект в подборе;
* «Потрачено» берётся из бюджетов проектов, а «сэкономлено» —
  из именованных констант `apps.rooms.director_stats`.

Разметку, подписи кнопок и классы здесь не проверяем: это контракт
данных, а не вёрстки.
"""

from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.rooms.director_stats import (
    CLASSIC_HIRING_DAYS,
    PLATFORM_LAUNCH_DAYS,
    STAFF_FULL_COST_PER_HOUR_RUB,
    director_finance_metrics,
)
from apps.rooms.models import Project, RoomFunctionSlot, RoomMember
from apps.test_helpers import make_director, make_freelancer, make_teamlead
from apps.users.models import User


class CorePagesTests(TestCase):
    """Что отдаёт `core:home` гостю и каждой роли."""

    def test_landing_is_open_for_guest(self):
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'core/landing.html')

    def test_dashboards_render_for_director_teamlead_and_freelancer(self):
        cases = (
            (make_director(email='d@core.test'), 'core/director_dashboard.html'),
            (make_teamlead(email='t@core.test'), 'core/teamlead_dashboard.html'),
            (make_freelancer(email='f@core.test'), 'core/freelancer_dashboard.html'),
        )
        for user, template in cases:
            with self.subTest(role=user.role):
                self.client.force_login(user)
                response = self.client.get(reverse('core:home'))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, template)
                self.client.logout()


class DemoModeTests(TestCase):
    """Демо-контур: активация без DEBUG и посевной сценарий."""

    @override_settings(
        DEBUG=False,
        DEMO_MODE=True,
        EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend',
    )
    def test_demo_mode_shows_activation_link_without_debug(self):
        response = self.client.post(
            reverse('users:register') + '?role=freelancer',
            {
                'role': User.Roles.FREELANCER,
                'first_name': 'Демо',
                'last_name': 'Юзер',
                'email': 'demo.activate@example.com',
                'password1': 'DemoPass123!',
                'password2': 'DemoPass123!',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['demo_mode'])
        self.assertContains(response, 'activate')

    def test_seed_demo_scenario_creates_staffing_project_with_slots(self):
        call_command('seed_demo_scenario')
        director = User.objects.get(email='director@wowlance.demo')
        project = Project.objects.get(
            owner=director,
            name='Демо для стейкхолдеров',
        )
        self.assertEqual(project.status, Project.Status.STAFFING)
        self.assertTrue(hasattr(project, 'room'))
        self.assertTrue(
            RoomMember.objects.filter(
                room=project.room,
                role_in_room=RoomMember.RoleInRoom.TEAMLEAD,
            ).exists()
        )
        self.assertTrue(
            RoomFunctionSlot.objects.filter(room=project.room, is_active=True).exists()
        )


class DirectorFinanceMetricsTests(TestCase):
    """Финансовая полоса дашборда считается из снапшотов, а не из каталога."""

    def setUp(self):
        self.director = make_director(email='fin@core.test')

    def test_spent_sums_project_budgets_not_live_catalog(self):
        Project.objects.create(
            owner=self.director,
            name='A',
            budget=Decimal('97000.00'),
            status=Project.Status.STAFFING,
        )
        Project.objects.create(
            owner=self.director,
            name='B',
            budget=Decimal('3000.00'),
            status=Project.Status.DRAFT,
        )
        other = make_director(email='other-fin@core.test')
        Project.objects.create(
            owner=other,
            name='Чужой',
            budget=Decimal('999999.00'),
            status=Project.Status.ACTIVE,
        )
        finance = director_finance_metrics(self.director)
        self.assertEqual(finance['spent_total'], Decimal('100000.00'))

    def test_time_and_money_saved_come_from_named_constants(self):
        Project.objects.create(
            owner=self.director,
            name='Draft',
            status=Project.Status.DRAFT,
        )
        Project.objects.create(
            owner=self.director,
            name='Eco',
            budget=Decimal('97000.00'),
            status=Project.Status.STAFFING,
            input_data={
                'functional_roles': [
                    {'role_key': 'teamlead', 'count': 1, 'hours_per_unit': 160},
                    {'role_key': 'seller_middle', 'count': 1, 'hours_per_unit': 160},
                ],
            },
        )
        # Один запущенный проект: дни классического найма минус запуск платформы.
        expected_days = (CLASSIC_HIRING_DAYS - PLATFORM_LAUNCH_DAYS).quantize(
            Decimal('0.1'),
        )
        # Ставка штата × 320 ч состава − бюджет комнаты.
        expected_money = (
            STAFF_FULL_COST_PER_HOUR_RUB * Decimal(320) - Decimal('97000.00')
        )

        finance = director_finance_metrics(self.director)
        self.assertEqual(finance['time_saved_days'], expected_days)
        self.assertEqual(finance['money_saved_total'], expected_money)
        self.assertEqual(finance['time_saved_caption'], finance['money_saved_caption'])
