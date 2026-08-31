from django.test import Client, TestCase
from django.urls import reverse
from decimal import Decimal

from apps.rooms.director_stats import (
    CLASSIC_HIRING_DAYS,
    PLATFORM_LAUNCH_DAYS,
    STAFF_FULL_COST_PER_HOUR_RUB,
    director_finance_metrics,
    estimated_money_saved,
    estimated_time_saved_days,
)
from apps.rooms.models import Project
from apps.rooms.onboarding import director_metrics
from apps.test_helpers import make_director, make_freelancer, make_teamlead


class HomeViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'TestPass123!'

    def test_landing_for_anonymous(self):
        response = self.client.get(reverse('core:home'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'core/landing.html')

    def test_director_dashboard(self):
        make_director(email='d@core.test', password=self.password)
        self.client.login(username='d@core.test', password=self.password)
        response = self.client.get(reverse('core:home'))
        self.assertTemplateUsed(response, 'core/director_dashboard.html')
        self.assertContains(response, 'Создать проект')
        self.assertContains(response, 'Потратил')
        self.assertContains(response, 'Заработал')
        self.assertContains(response, 'Сэкономил времени')
        self.assertContains(response, 'Сэкономил денег')
        self.assertContains(response, 'контур сделок не в этом релизе')
        self.assertContains(response, 'по составу команды, тестовая оплата')
        self.assertContains(response, 'оценка платформы')
        self.assertContains(response, 'Запустить мастер')
        # Каталог вторичен: маленький ghost, не равный primary CTA.
        self.assertContains(response, 'catalog-secondary')
        self.assertContains(response, 'btn-sm">Каталог фрилансеров')

    def test_freelancer_dashboard(self):
        make_freelancer(email='f@core.test', password=self.password)
        self.client.login(username='f@core.test', password=self.password)
        response = self.client.get(reverse('core:home'))
        self.assertTemplateUsed(response, 'core/freelancer_dashboard.html')

    def test_teamlead_dashboard(self):
        make_teamlead(email='t@core.test', password=self.password)
        self.client.login(username='t@core.test', password=self.password)
        response = self.client.get(reverse('core:home'))
        self.assertTemplateUsed(response, 'core/teamlead_dashboard.html')


class DirectorFinanceMetricsTests(TestCase):
    def setUp(self):
        self.director = make_director(email='fin@core.test')

    def test_empty_director_has_zero_finance(self):
        metrics = director_metrics(self.director)
        self.assertEqual(metrics['spent_total'], Decimal('0.00'))
        self.assertEqual(metrics['earned_total'], Decimal('0.00'))
        self.assertEqual(metrics['time_saved_days'], Decimal('0'))
        self.assertEqual(metrics['money_saved_total'], Decimal('0.00'))
        self.assertEqual(metrics['earned_caption'], 'контур сделок не в этом релизе')

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

    def test_time_saved_uses_named_constants_per_launched_project(self):
        Project.objects.create(
            owner=self.director,
            name='Draft',
            status=Project.Status.DRAFT,
        )
        Project.objects.create(
            owner=self.director,
            name='Live',
            status=Project.Status.STAFFING,
        )
        expected = (CLASSIC_HIRING_DAYS - PLATFORM_LAUNCH_DAYS).quantize(Decimal('0.1'))
        self.assertEqual(estimated_time_saved_days(1), expected)
        finance = director_finance_metrics(self.director)
        self.assertEqual(finance['time_saved_days'], expected)

    def test_money_saved_from_snapshot_hours_minus_budget(self):
        Project.objects.create(
            owner=self.director,
            name='Eco',
            budget=Decimal('97000.00'),
            status=Project.Status.STAFFING,
            input_data={
                'functional_roles': [
                    {
                        'role_key': 'teamlead',
                        'count': 1,
                        'hours_per_unit': 160,
                    },
                    {
                        'role_key': 'seller_middle',
                        'count': 1,
                        'hours_per_unit': 160,
                    },
                ],
            },
        )
        # 800 ₽/ч × 320 ч − 97 000 = 159 000
        expected = STAFF_FULL_COST_PER_HOUR_RUB * Decimal(320) - Decimal('97000.00')
        self.assertEqual(estimated_money_saved(Project.objects.filter(owner=self.director)), expected)
        finance = director_finance_metrics(self.director)
        self.assertEqual(finance['money_saved_total'], expected)
        self.assertEqual(finance['money_saved_caption'], 'оценка платформы')

    def test_earned_never_computed_from_leads(self):
        finance = director_finance_metrics(self.director)
        self.assertEqual(finance['earned_total'], Decimal('0.00'))
        self.assertIn('сделок', finance['earned_caption'])
