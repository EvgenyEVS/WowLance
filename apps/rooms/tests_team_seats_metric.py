"""Метрика Team seats на Обзоре (не «Слоты»).

Приёмка docs/260904 — задание колокольчик чата и team seats.md (часть B).
"""

from django.test import Client, TestCase
from django.urls import reverse

from apps.rooms.models import RoomMember
from apps.rooms.services import add_freelancer_to_room
from apps.test_helpers import make_freelancer, make_staffed_project


class TeamSeatsOverviewMetricTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staffed = make_staffed_project(slots=4, prefix='seats-')
        self.overview_url = reverse(
            'rooms:room_overview',
            kwargs={'project_id': self.staffed.project.id},
        )

    def test_team_seats_filled_copy(self):
        fr1 = make_freelancer(email='seat1@test.com')
        fr2 = make_freelancer(email='seat2@test.com')
        m1 = add_freelancer_to_room(self.staffed.room, fr1)
        m2 = add_freelancer_to_room(self.staffed.room, fr2)
        m1.function_slot = self.staffed.slots[0]
        m1.save(update_fields=['function_slot'])
        m2.function_slot = self.staffed.slots[1]
        m2.save(update_fields=['function_slot'])
        # Не ready — всё равно filled.
        self.assertEqual(m1.ready_status, RoomMember.ReadyStatus.PENDING)

        self.client.force_login(self.staffed.director)
        response = self.client.get(self.overview_url)

        self.assertContains(response, 'Team seats')
        self.assertContains(response, '2 of 4 filled')
        self.assertNotContains(
            response,
            '<span class="metric-label">Слоты</span>',
            html=False,
        )

    def test_team_seats_none_yet(self):
        empty = make_staffed_project(slots=0, prefix='empty-')
        url = reverse(
            'rooms:room_overview',
            kwargs={'project_id': empty.project.id},
        )
        self.client.force_login(empty.director)
        response = self.client.get(url)
        self.assertContains(response, 'Team seats')
        self.assertContains(response, 'none yet')
        self.assertNotContains(response, '0 of 0 filled')
