"""Коммуникации комнаты: общий чат, приватный контур директор↔тимлид, видео.

Проверяется только контур коммуникаций:

* общий канал команды — чтение и запись участником, отказ постороннему;
* приватный канал директор↔тимлид — фрилансеру не виден и недоступен;
* каналы изолированы, а видеоссылки команды и DT-контура различаются;
* опрос (HTMX-поллинг) читает и ничего не пишет;
* пустое сообщение не создаёт записи, а разметка пользователя экранируется.

Чат по ADR-001 сделан без сокетов: обычный POST + HTMX-опрос, поэтому тесты
говорят на языке HTTP-ответов и контекста, а не событий. Навигация вкладок,
разметка и лейблы сюда не входят.
"""

from django.test import Client, TestCase
from django.urls import reverse

from apps.rooms.chat import post_chat_message, recent_chat_messages
from apps.rooms.models import (
    Project,
    RoomActivity,
    RoomChatMessage,
    RoomMember,
)
from apps.rooms.services import (
    add_freelancer_to_room,
    assign_teamlead,
    director_teamlead_video_call_url,
    ensure_room_for_project,
    launch_project,
    room_video_call_url,
)
from apps.test_helpers import make_director, make_freelancer, make_teamlead


class RoomChatTestCase(TestCase):
    """Живая комната: директор-владелец, тимлид, фрилансер и посторонний."""

    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir@chat.test')
        self.teamlead = make_teamlead(email='tl@chat.test')
        self.freelancer = make_freelancer(email='fr@chat.test')
        self.outsider = make_freelancer(email='out@chat.test')
        self.project = Project.objects.create(
            owner=self.director,
            name='Комната с чатом',
            project_type=Project.Type.BASE,
            seller_level=Project.SellerLevel.MIDDLE,
            input_data={
                'offer': 'Оффер',
                'utp': 'УТП',
                'audience': 'ЦА',
                'hot_criteria': 'Запросил демо',
            },
            budget=10000,
            status=Project.Status.DRAFT,
        )
        launch_project(self.project)
        assign_teamlead(self.project, self.teamlead)
        add_freelancer_to_room(self.project.room, self.freelancer)
        self.project.refresh_from_db()
        self.room = self.project.room

        self.comms_url = reverse(
            'rooms:room_comms', kwargs={'project_id': self.project.id}
        )
        self.messages_url = reverse(
            'rooms:room_chat_messages', kwargs={'project_id': self.project.id}
        )
        self.send_url = reverse(
            'rooms:room_chat_send', kwargs={'project_id': self.project.id}
        )
        self.dt_messages_url = reverse(
            'rooms:room_dt_chat_messages', kwargs={'project_id': self.project.id}
        )
        self.dt_send_url = reverse(
            'rooms:room_dt_chat_send', kwargs={'project_id': self.project.id}
        )
        self.dt_hub_url = reverse(
            'rooms:room_comms_teamlead', kwargs={'project_id': self.project.id}
        )


class RoomChatAccessTests(RoomChatTestCase):
    """Доступ к чату — тот же RBAC комнаты, без собственных ролей."""

    def test_member_reads_and_writes_the_common_channel(self):
        for user, text in (
            (self.director, 'Сообщение директора'),
            (self.teamlead, 'Сообщение тимлида'),
            (self.freelancer, 'Сообщение фрилансера'),
        ):
            with self.subTest(role=user.role):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.messages_url).status_code, 200)
                response = self.client.post(self.send_url, {'text': text})
                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    RoomChatMessage.objects.filter(
                        room=self.room, author=user, text=text
                    ).exists()
                )

    def test_outsider_is_denied_on_both_endpoints(self):
        self.client.force_login(self.outsider)

        self.assertEqual(self.client.get(self.messages_url).status_code, 403)
        self.assertEqual(
            self.client.post(self.send_url, {'text': 'Чужая комната'}).status_code,
            403,
        )
        self.assertFalse(RoomChatMessage.objects.filter(text='Чужая комната').exists())

    def test_removed_freelancer_loses_chat_access(self):
        """Удаление RoomMember закрывает и чтение, и запись — без своих правил."""
        self.client.force_login(self.freelancer)
        self.assertEqual(self.client.get(self.messages_url).status_code, 200)
        self.assertEqual(
            self.client.post(self.send_url, {'text': 'Пока я в команде'}).status_code,
            302,
        )

        RoomMember.objects.filter(room=self.room, user=self.freelancer).delete()

        self.assertEqual(self.client.get(self.messages_url).status_code, 403)
        self.assertEqual(
            self.client.post(self.send_url, {'text': 'Уже не в команде'}).status_code,
            403,
        )
        self.assertFalse(
            RoomChatMessage.objects.filter(text='Уже не в команде').exists()
        )


class DirectorTeamleadCommsTests(RoomChatTestCase):
    """Приватный контур директор↔тимлид: видимость, доступ, изоляция."""

    def test_dt_section_is_hidden_from_freelancer(self):
        cases = (
            (self.director, True),
            (self.teamlead, True),
            (self.freelancer, False),
        )
        for user, expected in cases:
            with self.subTest(role=user.role):
                self.client.force_login(user)
                response = self.client.get(self.comms_url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.context['show_director_teamlead_comms'],
                    expected,
                )
                self.assertEqual(
                    'dt_video_call_url' in response.context,
                    expected,
                )

    def test_freelancer_gets_403_on_dt_endpoints(self):
        self.client.force_login(self.freelancer)

        self.assertEqual(self.client.get(self.dt_messages_url).status_code, 403)
        self.assertEqual(
            self.client.post(self.dt_send_url, {'text': 'секрет'}).status_code,
            403,
        )
        self.assertEqual(self.client.get(self.dt_hub_url).status_code, 403)
        self.assertFalse(RoomChatMessage.objects.filter(text='секрет').exists())

    def test_channels_are_isolated(self):
        post_chat_message(
            self.room,
            self.director,
            'команда',
            channel=RoomChatMessage.Channel.TEAM,
        )
        post_chat_message(
            self.room,
            self.director,
            'приватно',
            channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD,
        )
        team = recent_chat_messages(
            self.room, channel=RoomChatMessage.Channel.TEAM
        )
        private = recent_chat_messages(
            self.room, channel=RoomChatMessage.Channel.DIRECTOR_TEAMLEAD
        )
        self.assertEqual([m.text for m in team], ['команда'])
        self.assertEqual([m.text for m in private], ['приватно'])

        self.client.force_login(self.director)
        team_body = self.client.get(self.messages_url).content.decode()
        dt_body = self.client.get(self.dt_messages_url).content.decode()
        self.assertIn('команда', team_body)
        self.assertNotIn('приватно', team_body)
        self.assertIn('приватно', dt_body)
        self.assertNotIn('команда', dt_body)

    def test_video_link_and_dt_channel_are_two_different_links(self):
        team_url = room_video_call_url(self.room)
        dt_url = director_teamlead_video_call_url(self.project)
        self.assertNotEqual(team_url, dt_url)

        self.client.force_login(self.director)
        context = self.client.get(self.comms_url).context
        self.assertEqual(context['team_video_call_url'], team_url)
        self.assertEqual(context['dt_video_call_url'], dt_url)

        # Ссылки строятся из идентификаторов, поэтому у соседней комнаты
        # они свои и с нашими не совпадают.
        other_project = Project.objects.create(
            owner=self.director,
            name='Соседняя комната',
            status=Project.Status.STAFFING,
        )
        other_room = ensure_room_for_project(other_project)
        self.assertNotEqual(room_video_call_url(other_room), team_url)
        self.assertNotEqual(
            director_teamlead_video_call_url(other_project),
            dt_url,
        )


class RoomChatPollingAndSafetyTests(RoomChatTestCase):
    """Опрос ничего не пишет, мусор не сохраняется, разметка экранируется."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.director)

    def test_poll_endpoint_returns_messages_and_writes_nothing(self):
        for index in range(3):
            post_chat_message(self.room, self.director, f'Сообщение {index}')
        counts_before = (
            RoomChatMessage.objects.count(),
            RoomActivity.objects.count(),
            RoomMember.objects.count(),
        )

        for _ in range(3):
            response = self.client.get(self.messages_url)
            self.assertEqual(response.status_code, 200)
            body = response.content.decode()
            for index in range(3):
                self.assertIn(f'Сообщение {index}', body)

        self.assertEqual(
            (
                RoomChatMessage.objects.count(),
                RoomActivity.objects.count(),
                RoomMember.objects.count(),
            ),
            counts_before,
        )

    def test_whitespace_only_message_creates_no_record(self):
        for raw in ('', '   ', ' \n\t  \n '):
            with self.subTest(raw=repr(raw)):
                self.client.post(self.send_url, {'text': raw})
                self.assertEqual(RoomChatMessage.objects.count(), 0)

    def test_script_tags_are_escaped_in_partial(self):
        self.client.post(self.send_url, {'text': '<script>alert("xss")</script>'})

        body = self.client.get(self.messages_url).content.decode()
        self.assertNotIn('<script>', body)
        self.assertIn('&lt;script&gt;', body)
        self.assertIn('&quot;xss&quot;', body)
