"""Жизненный цикл проекта и комнаты: запуск, оплата, доступ, состав.

Здесь остаётся только контракт «проект → комната»:

* `launch_project` создаёт комнату и заводит владельца участником;
* `handle_project_paid` — фасад тестовой оплаты, идемпотентный по комнате;
* кто имеет доступ к проекту и комнате, а кто получает 403/404;
* `assign_teamlead` / `add_freelancer_to_room` меняют состав, но не статус.

Подбор на функциональные слоты — `tests_staffing` и
`tests_staffing_matching`; состав и юнит-экономика — `tests_composition`;
ролевые матрицы комнаты — `tests_room_rbac`.
"""

from django.test import Client, TestCase
from django.urls import reverse

from apps.rooms.models import Project, Room, RoomActivity, RoomFunctionSlot, RoomMember
from apps.rooms.services import (
    add_freelancer_to_room,
    assign_teamlead,
    handle_project_paid,
    launch_project,
    user_can_access_project,
)
from apps.test_helpers import make_director, make_freelancer, make_teamlead, make_user
from apps.users.models import User

PROJECT_INPUTS = {
    'offer': 'Оффер',
    'utp': 'УТП',
    'audience': 'ЦА',
    'hot_criteria': 'Запросил демо',
}


class RoomServiceTests(TestCase):
    """Сервисный слой: запуск, доступ и состав комнаты."""

    def setUp(self):
        self.director = make_director()
        self.teamlead = make_teamlead()
        self.freelancer = make_freelancer()
        self.project = Project.objects.create(
            owner=self.director,
            name='Тестовый проект',
            project_type=Project.Type.BASE,
            seller_level=Project.SellerLevel.MIDDLE,
            input_data=dict(PROJECT_INPUTS),
            budget=10000,
            status=Project.Status.DRAFT,
        )

    def test_launch_creates_room_and_director_member(self):
        launch_project(self.project)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertTrue(hasattr(self.project, 'room'))
        self.assertTrue(
            RoomMember.objects.filter(
                room=self.project.room,
                user=self.director,
                role_in_room=RoomMember.RoleInRoom.DIRECTOR,
            ).exists()
        )

    def test_project_access_control(self):
        launch_project(self.project)
        stranger = make_user(email='stranger@example.com', role=User.Roles.FREELANCER)
        self.assertTrue(user_can_access_project(self.director, self.project))
        self.assertFalse(user_can_access_project(stranger, self.project))
        add_freelancer_to_room(self.project.room, stranger)
        self.assertTrue(user_can_access_project(stranger, self.project))

    def test_assign_teamlead_makes_him_the_room_teamlead(self):
        launch_project(self.project)
        assign_teamlead(self.project, self.teamlead)
        self.project.refresh_from_db()
        self.assertEqual(self.project.teamlead_id, self.teamlead.id)
        self.assertTrue(
            RoomMember.objects.filter(
                room=self.project.room,
                user=self.teamlead,
                role_in_room=RoomMember.RoleInRoom.TEAMLEAD,
            ).exists()
        )

    def test_add_freelancer_to_room_keeps_project_in_staffing(self):
        """Добавление фрилансера больше не активирует проект.

        Раньше первый фрилансер переводил проект `STAFFING → ACTIVE`.
        Теперь активация — результат подтверждённой готовности всей
        функциональной команды (см. `tests_staffing`).
        """
        launch_project(self.project)
        assign_teamlead(self.project, self.teamlead)
        add_freelancer_to_room(self.project.room, self.freelancer)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertTrue(
            RoomMember.objects.filter(
                room=self.project.room,
                user=self.freelancer,
                role_in_room=RoomMember.RoleInRoom.FREELANCER,
            ).exists()
        )


class RoomViewTests(TestCase):
    """HTTP-контур: запуск без вводных, чужая комната, роль создателя."""

    def setUp(self):
        self.client = Client()
        self.password = 'TestPass123!'
        self.director = make_director(email='dir@rooms.test', password=self.password)
        self.freelancer = make_freelancer(email='fr@rooms.test', password=self.password)
        self.outsider = make_freelancer(email='out@rooms.test', password=self.password)

    def test_launch_requires_input_data(self):
        self.client.login(username='dir@rooms.test', password=self.password)
        project = Project.objects.create(
            owner=self.director,
            name='Пустой',
            input_data={},
            status=Project.Status.DRAFT,
        )
        response = self.client.post(
            reverse('rooms:project_launch', kwargs={'project_id': project.id}),
        )
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.DRAFT)
        self.assertFalse(Room.objects.filter(project=project).exists())
        self.assertRedirects(
            response,
            reverse('rooms:project_detail', kwargs={'project_id': project.id}),
        )

    def test_outsider_cannot_access_room(self):
        project = Project.objects.create(
            owner=self.director,
            name='Закрытая комната',
            input_data=dict(PROJECT_INPUTS),
            status=Project.Status.DRAFT,
        )
        launch_project(project)

        self.client.login(username='out@rooms.test', password=self.password)
        response = self.client.get(
            reverse('rooms:room_overview', kwargs={'project_id': project.id}),
        )
        self.assertEqual(response.status_code, 403)

    def test_freelancer_cannot_create_project(self):
        self.client.login(username='fr@rooms.test', password=self.password)
        response = self.client.get(reverse('rooms:project_create'))
        self.assertEqual(response.status_code, 403)


class ProjectPaidTests(TestCase):
    """Тестовая оплата: фасад `handle_project_paid` и его владелец."""

    def setUp(self):
        self.client = Client()
        self.password = 'TestPass123!'
        self.director = make_director(email='paid-dir@rooms.test', password=self.password)
        self.project = Project.objects.create(
            owner=self.director,
            name='Оплаченный проект',
            input_data=dict(PROJECT_INPUTS),
            status=Project.Status.DRAFT,
        )

    def test_handle_project_paid_starts_staffing_with_room_and_activity(self):
        room = handle_project_paid(self.project, actor=self.director)
        self.project.refresh_from_db()

        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertEqual(room.project_id, self.project.id)
        self.assertTrue(
            RoomActivity.objects.filter(
                room=room,
                event_type=RoomActivity.EventType.PROJECT_LAUNCHED,
            ).exists()
        )
        self.assertTrue(
            RoomMember.objects.filter(
                room=room,
                user=self.director,
                role_in_room=RoomMember.RoleInRoom.DIRECTOR,
            ).exists()
        )

    def test_repeated_payment_does_not_create_second_room(self):
        first = handle_project_paid(self.project, actor=self.director)
        second = handle_project_paid(self.project, actor=self.director)

        self.assertEqual(first.id, second.id)
        self.assertEqual(Room.objects.filter(project=self.project).count(), 1)
        # Повторная оплата не переоткрывает комнату: запуск остаётся один.
        self.assertEqual(
            RoomActivity.objects.filter(
                room=first,
                event_type=RoomActivity.EventType.PROJECT_LAUNCHED,
            ).count(),
            1,
        )

    def test_handle_project_paid_applies_quick_start_slots_once(self):
        """Оплата черновика без состава → слот seller_middle; повтор не дублирует."""
        handle_project_paid(self.project, actor=self.director)
        slots = RoomFunctionSlot.objects.filter(
            room__project=self.project,
            role_key='seller_middle',
            is_active=True,
        )
        self.assertEqual(slots.count(), 1)

        handle_project_paid(self.project, actor=self.director)
        self.assertEqual(
            RoomFunctionSlot.objects.filter(
                room__project=self.project,
                role_key='seller_middle',
                is_active=True,
            ).count(),
            1,
        )

    def test_other_users_cannot_pay_a_foreign_project(self):
        make_director(email='pay-other@rooms.test', password=self.password)
        make_freelancer(email='pay-fr@rooms.test', password=self.password)

        for email in ('pay-other@rooms.test', 'pay-fr@rooms.test'):
            with self.subTest(actor=email):
                self.client.logout()
                self.client.login(username=email, password=self.password)
                response = self.client.post(
                    reverse('rooms:project_pay', kwargs={'project_id': self.project.id}),
                )
                self.assertEqual(response.status_code, 404)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.DRAFT)
        self.assertFalse(Room.objects.filter(project=self.project).exists())
