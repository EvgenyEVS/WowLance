"""UI-тест метрики Team seats на Обзоре (Часть B задания 260904)."""
from django.test import Client, TestCase
from django.urls import reverse
from apps.rooms.models import Project, Room, RoomFunctionSlot, RoomMember
from apps.rooms.services import ensure_room_for_project, add_freelancer_to_room
from apps.test_helpers import make_director, make_teamlead, make_freelancer


class TeamSeatsMetricTests(TestCase):
    """Метрика Team seats на Обзоре директора/тимлида."""

    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir@seats.test')
        self.teamlead = make_teamlead(email='tl@seats.test')
        self.freelancer = make_freelancer(email='fr@seats.test')
        self.project = Project.objects.create(
            owner=self.director,
            name='Team Seats Test',
            status=Project.Status.STAFFING,
            teamlead=self.teamlead,
            input_data={'offer': 'Оффер', 'audience': 'ЦА', 'hot_criteria': 'Критерии'},
        )
        self.room = ensure_room_for_project(self.project)
        add_freelancer_to_room(self.room, self.freelancer)
        self.overview_url = reverse('rooms:room_overview', args=[self.project.id])

    def test_team_seats_metric_shows_filled_of_total(self):
        """Метрика показывает 'X of Y filled', а не 'Слоты X/Y'."""
        # Создаём 2 слота, один заполнен
        slot1 = RoomFunctionSlot.objects.create(
            room=self.room, role_key='seller_middle', slot_index=1
        )
        slot2 = RoomFunctionSlot.objects.create(
            room=self.room, role_key='seller_middle', slot_index=2
        )
        # Назначаем фрилансера на первый слот
        member = RoomMember.objects.get(room=self.room, user=self.freelancer)
        member.function_slot = slot1
        member.save()

        self.client.force_login(self.director)
        response = self.client.get(self.overview_url)

        # Проверяем новую формулировку
        self.assertContains(response, 'Team seats')
        self.assertContains(response, '1 of 2 filled')
        # Старая формулировка не должна присутствовать
        self.assertNotContains(response, 'Слоты')
        self.assertNotRegex(response.content.decode(), r'Слоты\s*</span>')

    def test_team_seats_shows_none_yet_when_zero_total(self):
        """Если слотов нет, показываем 'none yet', а не '0 of 0 filled'."""
        self.client.force_login(self.director)
        response = self.client.get(self.overview_url)

        self.assertContains(response, 'Team seats')
        self.assertContains(response, 'none yet')
        self.assertNotContains(response, '0 of 0 filled')

    def test_teamlead_sees_team_seats_metric(self):
        """Тимлид тоже видит метрику Team seats."""
        self.client.force_login(self.teamlead)
        response = self.client.get(self.overview_url)

        self.assertContains(response, 'Team seats')

    def test_freelancer_does_not_see_team_seats_metric(self):
        """Фрилансер не видит метрику Team seats (project_metrics у него None)."""
        self.client.force_login(self.freelancer)
        response = self.client.get(self.overview_url)

        self.assertNotContains(response, 'Team seats')