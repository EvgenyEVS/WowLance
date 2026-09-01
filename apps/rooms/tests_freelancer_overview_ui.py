from django.test import Client, TestCase
from django.urls import reverse

from apps.rooms.models import Project, RoomMember
from apps.rooms.services import launch_project
from apps.test_helpers import make_director, make_freelancer


class FreelancerOverviewUITests(TestCase):
    def setUp(self):
        self.client = Client()

        self.director = make_director(email='director@overview-ui.test')
        self.project = Project.objects.create(
            owner=self.director,
            name='Тестовый проект',
            input_data={'offer': 'Оффер', 'utp': 'УТП', 'audience': 'ЦА', 'hot_criteria': 'Hot'},
            status=Project.Status.DRAFT,
        )
        launch_project(self.project)

        self.freelancer = make_freelancer(email='freelancer@overview-ui.test')
        RoomMember.objects.create(
            room=self.project.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )

    def test_freelancer_overview_has_no_team_management(self):
        """Фрилансер на Обзоре не видит блок 'Команда' и кнопку 'Управление командой'."""
        self.client.force_login(self.freelancer)
        response = self.client.get(reverse('rooms:room_overview', kwargs={'project_id': self.project.id}))

        self.assertEqual(response.status_code, 200)
        # Проверяем отсутствие кнопки управления командой
        self.assertNotContains(response, 'Управление командой')

    def test_freelancer_overview_has_no_catalog_link(self):
        """Фрилансер на Обзоре не видит ссылку на каталог."""
        self.client.force_login(self.freelancer)
        response = self.client.get(reverse('rooms:room_overview', kwargs={'project_id': self.project.id}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Открыть каталог')

    def test_freelancer_base_has_no_catalog(self):
        """В шапке сайта у фрилансера нет пункта 'Каталог'."""
        self.client.force_login(self.freelancer)
        response = self.client.get(reverse('core:home'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Каталог')

    def test_director_overview_has_team_management(self):
        """Директор на Обзоре видит 'Управление командой' и список команды."""
        self.client.force_login(self.director)
        response = self.client.get(reverse('rooms:room_overview', kwargs={'project_id': self.project.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Управление командой')
        self.assertContains(response, 'Команда')
