"""Колокольчик чата: бейдж разговоров, RBAC каналов, mark-read, XSS тоста.

Приёмка docs/260904 — задание колокольчик чата и team seats.md (часть A).
"""

from datetime import timedelta
from html import escape

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.rooms.models import (
    ChatReadCursor,
    Project,
    RoomChatMessage,
)
from apps.rooms.services import (
    add_freelancer_to_room,
    assign_teamlead,
    ensure_room_for_project,
    launch_project,
)
from apps.test_helpers import (
    make_director,
    make_freelancer,
    make_teamlead,
    make_user,
)
from apps.users.models import User


class ChatAlertsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir@bell.test')
        self.teamlead = make_teamlead(email='tl@bell.test')
        self.freelancer = make_freelancer(email='fr@bell.test')
        self.manager = make_user(email='mgr@bell.test', role=User.Roles.MANAGER)

        self.project = Project.objects.create(
            owner=self.director,
            name='Bell Project',
            project_type=Project.Type.BASE,
            seller_level=Project.SellerLevel.MIDDLE,
            input_data={
                'offer': 'Оффер',
                'utp': 'УТП',
                'audience': 'ЦА',
                'hot_criteria': 'Демо',
            },
            budget=10000,
            status=Project.Status.DRAFT,
        )
        launch_project(self.project)
        assign_teamlead(self.project, self.teamlead)
        self.room = ensure_room_for_project(self.project)
        self.room.chat_enabled = True
        self.room.save(update_fields=['chat_enabled'])
        add_freelancer_to_room(self.room, self.freelancer)

        self.alerts_url = reverse('rooms:chat_alerts')
        self.comms_url = reverse(
            'rooms:room_comms', kwargs={'project_id': self.project.id}
        )

    def _past_cursor(self, user, channel=RoomChatMessage.Channel.TEAM):
        ChatReadCursor.objects.update_or_create(
            user=user,
            room=self.room,
            channel=channel,
            defaults={'last_read_at': timezone.now() - timedelta(minutes=5)},
        )

    def test_freelancer_sees_team_but_not_dt(self):
        self._past_cursor(self.freelancer, RoomChatMessage.Channel.TEAM)
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.teamlead,
            channel=RoomChatMessage.Channel.TEAM,
            text='Привет команде',
        )
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.director,
            channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD,
            text='Секрет DT',
        )

        self.client.force_login(self.freelancer)
        response = self.client.get(self.alerts_url)

        self.assertContains(response, '🔔')
        self.assertContains(response, 'class="bell-badge">1</span>')
        self.assertNotContains(response, 'Секрет DT')
        self.assertNotContains(response, '#comms-dt-chat')

    def test_own_messages_do_not_count(self):
        self._past_cursor(self.freelancer, RoomChatMessage.Channel.TEAM)
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.freelancer,
            channel=RoomChatMessage.Channel.TEAM,
            text='Моё',
        )

        self.client.force_login(self.freelancer)
        response = self.client.get(self.alerts_url)
        self.assertNotContains(response, 'bell-badge')

    def test_teamlead_badge_counts_two_channels(self):
        self._past_cursor(self.teamlead, RoomChatMessage.Channel.TEAM)
        self._past_cursor(
            self.teamlead, RoomChatMessage.Channel.DIRECTOR_TEAMLEAD
        )
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.freelancer,
            channel=RoomChatMessage.Channel.TEAM,
            text='Отчёт',
        )
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.director,
            channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD,
            text='Договорились',
        )

        self.client.force_login(self.teamlead)
        response = self.client.get(self.alerts_url)

        self.assertContains(response, 'class="bell-badge">2</span>')
        self.assertContains(response, 'Команда')
        self.assertContains(response, 'С директором')
        self.assertContains(response, '#comms-dt-chat')
        self.assertContains(response, '#comms-team')

        session = self.client.session
        session['chat_alerts_last_poll'] = (
            timezone.now() - timedelta(minutes=1)
        ).isoformat()
        session.save()
        toasted = self.client.get(self.alerts_url)
        self.assertContains(toasted, 'Отчёт')
        self.assertContains(toasted, 'Договорились')

    def test_room_comms_marks_team_and_dt_read(self):
        self._past_cursor(self.teamlead, RoomChatMessage.Channel.TEAM)
        self._past_cursor(
            self.teamlead, RoomChatMessage.Channel.DIRECTOR_TEAMLEAD
        )
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.freelancer,
            channel=RoomChatMessage.Channel.TEAM,
            text='Team unread',
        )
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.director,
            channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD,
            text='DT unread',
        )

        self.client.force_login(self.teamlead)
        before = self.client.get(self.alerts_url)
        self.assertContains(before, 'class="bell-badge">2</span>')

        self.client.get(self.comms_url)

        after = self.client.get(self.alerts_url)
        self.assertNotContains(after, 'bell-badge')

    def test_two_rooms_two_conversations(self):
        other = Project.objects.create(
            owner=self.director,
            name='Second Bell',
            project_type=Project.Type.BASE,
            seller_level=Project.SellerLevel.MIDDLE,
            input_data={
                'offer': 'Оффер',
                'utp': 'УТП',
                'audience': 'ЦА',
                'hot_criteria': 'Демо',
            },
            budget=5000,
            status=Project.Status.DRAFT,
        )
        launch_project(other)
        assign_teamlead(other, self.teamlead)
        other_room = ensure_room_for_project(other)
        other_room.chat_enabled = True
        other_room.save(update_fields=['chat_enabled'])

        for room in (self.room, other_room):
            ChatReadCursor.objects.update_or_create(
                user=self.teamlead,
                room=room,
                channel=RoomChatMessage.Channel.TEAM,
                defaults={'last_read_at': timezone.now() - timedelta(minutes=5)},
            )
            RoomChatMessage.objects.create(
                room=room,
                author=self.director,
                channel=RoomChatMessage.Channel.TEAM,
                text=f'Hi {room.project.name}',
            )

        self.client.force_login(self.teamlead)
        response = self.client.get(self.alerts_url)

        self.assertContains(response, 'class="bell-badge">2</span>')
        self.assertContains(response, 'Bell Project')
        self.assertContains(response, 'Second Bell')
        self.assertContains(
            response,
            reverse('rooms:room_comms', kwargs={'project_id': self.project.id}),
        )
        self.assertContains(
            response,
            reverse('rooms:room_comms', kwargs={'project_id': other.id}),
        )

    def test_manager_and_guest_have_no_bell(self):
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.freelancer,
            channel=RoomChatMessage.Channel.TEAM,
            text='Тест',
        )

        self.client.force_login(self.manager)
        mgr = self.client.get(self.alerts_url)
        self.assertEqual(mgr.status_code, 200)
        self.assertEqual(mgr.content, b'')

        home = self.client.get(reverse('core:home'))
        self.assertEqual(home.status_code, 200)
        self.assertNotContains(home, 'id="header-chat-bell"')

        self.client.logout()
        guest = self.client.get(self.alerts_url)
        self.assertEqual(guest.status_code, 302)
        self.assertIn('/login/', guest['Location'])

    def test_toast_text_is_escaped(self):
        self._past_cursor(self.teamlead, RoomChatMessage.Channel.TEAM)
        payload = '<script>alert(1)</script>'
        RoomChatMessage.objects.create(
            room=self.room,
            author=self.freelancer,
            channel=RoomChatMessage.Channel.TEAM,
            text=payload,
        )

        self.client.force_login(self.teamlead)
        session = self.client.session
        session['chat_alerts_last_poll'] = (
            timezone.now() - timedelta(minutes=1)
        ).isoformat()
        session.save()

        response = self.client.get(self.alerts_url)
        self.assertContains(response, escape(payload))
        self.assertNotContains(response, payload)

    def test_bell_tag_tolerates_missing_request(self):
        """handler500 рендерит base без RequestContext — не падаем вторым KeyError."""
        from django.template import Context, Template

        html = Template('{% load chat_bell %}{% header_chat_bell %}').render(
            Context({})
        )
        self.assertEqual(html.strip(), '')
