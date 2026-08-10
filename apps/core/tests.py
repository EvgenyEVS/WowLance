from django.test import Client, TestCase
from django.urls import reverse

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
