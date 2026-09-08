from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from apps.rooms.models import Project, Room, RoomChatMessage, ChatReadCursor
from apps.rooms.services import add_freelancer_to_room, assign_teamlead, ensure_room_for_project
from apps.test_helpers import make_director, make_freelancer, make_teamlead, make_user
from apps.users.models import User


class ChatAlertsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir@chat.test')
        self.teamlead = make_teamlead(email='tl@chat.test')
        self.freelancer = make_freelancer(email='fr@chat.test')
        self.manager = make_user(email='mgr@chat.test', role=User.Roles.MANAGER)

        self.project = Project.objects.create(
            owner=self.director,
            name='Chat Test Project',
            status=Project.Status.STAFFING,
            teamlead=self.teamlead,
        )
        self.room = ensure_room_for_project(self.project)
        # ВАЖНО: включаем чат для тестов
        self.room.chat_enabled = True
        self.room.save(update_fields=['chat_enabled'])

        add_freelancer_to_room(self.room, self.freelancer)

        self.alerts_url = reverse('rooms:chat_alerts')

    def test_freelancer_sees_team_but_not_dt(self):
        """Фрилансер видит непрочитанные в team, но игнорирует DT."""
        # Создаем курсор с last_read_at в прошлом, чтобы сообщение было непрочитанным
        ChatReadCursor.objects.create(
            user=self.freelancer,
            room=self.room,
            channel=RoomChatMessage.Channel.TEAM,
            last_read_at=timezone.now() - timedelta(minutes=1),
        )

        # Создаем сообщение в team
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.teamlead,
            channel=RoomChatMessage.Channel.TEAM,
            text='Привет'
        )
        # Создаем сообщение в DT (не должно учитываться для фрилансера)
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.director,
            channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD,
            text='Секрет'
        )

        self.client.force_login(self.freelancer)
        response = self.client.get(self.alerts_url)

        self.assertContains(response, '🔔')
        self.assertContains(response, 'class="bell-badge">1</span>')
        self.assertNotContains(response, 'Секрет')

    def test_teamlead_sees_both_channels(self):
        """Тимлид видит непрочитанные и в team, и в DT."""
        # Создаем курсоры с last_read_at в прошлом
        ChatReadCursor.objects.create(
            user=self.teamlead,
            room=self.room,
            channel=RoomChatMessage.Channel.TEAM,
            last_read_at=timezone.now() - timedelta(minutes=1),
        )
        ChatReadCursor.objects.create(
            user=self.teamlead,
            room=self.room,
            channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD,
            last_read_at=timezone.now() - timedelta(minutes=1),
        )

        RoomChatMessage.objects.create(
            room=self.room,
            author=self.freelancer,
            channel=RoomChatMessage.Channel.TEAM,
            text='Отчет'
        )
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.director,
            channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD,
            text='Договорились'
        )

        self.client.force_login(self.teamlead)
        response = self.client.get(self.alerts_url)

        self.assertContains(response, 'class="bell-badge">2</span>')
        self.assertContains(response, 'Отчет')
        self.assertContains(response, 'Договорились')

    def test_mark_read_on_comms_view(self):
        """Просмотр room_comms сбрасывает счетчик."""
        # Создаем курсор с last_read_at в прошлом
        ChatReadCursor.objects.create(
            user=self.teamlead,
            room=self.room,
            channel=RoomChatMessage.Channel.TEAM,
            last_read_at=timezone.now() - timedelta(minutes=1),
        )

        msg = RoomChatMessage.objects.create(
            room=self.room,
            author=self.freelancer,
            channel=RoomChatMessage.Channel.TEAM,
            text='Тест'
        )

        self.client.force_login(self.teamlead)
        # Сначала есть непрочитанные
        resp1 = self.client.get(self.alerts_url)
        self.assertContains(resp1, 'class="bell-badge">1</span>')

        # Заходим в чат
        self.client.get(reverse('rooms:room_comms', args=[self.project.id]))

        # Теперь непрочитанных нет
        resp2 = self.client.get(self.alerts_url)
        self.assertNotContains(resp2, 'bell-badge')

    def test_manager_and_guest_see_no_bell(self):
        """Менеджер и гость не видят колокольчик."""
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.freelancer,
            channel=RoomChatMessage.Channel.TEAM,
            text='Тест'
        )

        # Менеджер: получает 200, но без колокольчика
        self.client.force_login(self.manager)
        resp_mgr = self.client.get(self.alerts_url)
        self.assertEqual(resp_mgr.status_code, 200)
        self.assertNotContains(resp_mgr, '🔔')

        # Гость: получает редирект на логин (302)
        self.client.logout()
        resp_guest = self.client.get(self.alerts_url)
        self.assertEqual(resp_guest.status_code, 302)
        self.assertIn('/login/', resp_guest['Location'])

    def test_own_messages_do_not_trigger_badge(self):
        """Свои сообщения не считаются непрочитанными."""
        ChatReadCursor.objects.create(
            user=self.teamlead,
            room=self.room,
            channel=RoomChatMessage.Channel.TEAM,
            last_read_at=timezone.now() - timedelta(minutes=1),
        )

        RoomChatMessage.objects.create(
            room=self.room,
            author=self.teamlead,
            channel=RoomChatMessage.Channel.TEAM,
            text='Мое сообщение'
        )

        self.client.force_login(self.teamlead)
        response = self.client.get(self.alerts_url)
        self.assertNotContains(response, 'bell-badge')