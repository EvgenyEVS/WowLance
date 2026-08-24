"""Чат конкретной комнаты: модель, feature toggle, RBAC, безопасность, опрос.

Проверяется только чат. Навигация вкладок живёт в `tests_room_tabs.py`,
подбор команды — в `tests_staffing_*.py`; ожидания оттуда здесь не копируются,
а импортируются, чтобы у списка вкладок остался один источник истины.

Чат по ADR-001 сделан без сокетов: обычный POST + HTMX-опрос. Поэтому тесты
говорят на языке HTTP-ответов (partial / redirect / 403), а не событий.
"""

import importlib

from django.apps import apps as django_apps
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.rooms.chat import (
    CHAT_HISTORY_LIMIT,
    CHAT_MESSAGE_MAX_LENGTH,
    ChatDisabledError,
    post_chat_message,
    recent_chat_messages,
)
from apps.rooms.forms import RoomChatMessageForm
from apps.rooms.models import (
    Project,
    Room,
    RoomActivity,
    RoomChatMessage,
    RoomMember,
)
from apps.rooms.services import (
    add_freelancer_to_room,
    assign_teamlead,
    ensure_room_for_project,
    launch_project,
)
from apps.rooms.tests_room_tabs import EXPECTED_TABS, parse_room_tabs
from apps.test_helpers import make_director, make_freelancer, make_teamlead, make_user
from apps.users.models import User


class RoomChatTestCase(TestCase):
    """Живая комната: директор-владелец, тимлид, фрилансер-участник, аутсайдер."""

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
        self.project.refresh_from_db()
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

    def disable_chat(self):
        """Выключает чат так же, как это делает админ: правкой поля комнаты."""
        Room.objects.filter(pk=self.room.pk).update(chat_enabled=False)
        self.room.refresh_from_db()


class RoomChatMessageModelTests(RoomChatTestCase):
    """Пункты 1–4: сама модель сообщения."""

    def test_message_is_created_with_room_author_and_text(self):
        message = RoomChatMessage.objects.create(
            room=self.room,
            author=self.freelancer,
            text='Первый контакт с базой сделан.',
        )
        self.assertEqual(message.room, self.room)
        self.assertEqual(message.author, self.freelancer)
        self.assertEqual(message.text, 'Первый контакт с базой сделан.')
        self.assertIsNotNone(message.created_at)
        self.assertEqual(self.room.chat_messages.count(), 1)

    def test_default_ordering_is_oldest_first(self):
        first = RoomChatMessage.objects.create(
            room=self.room, author=self.director, text='Раз'
        )
        second = RoomChatMessage.objects.create(
            room=self.room, author=self.teamlead, text='Два'
        )
        third = RoomChatMessage.objects.create(
            room=self.room, author=self.freelancer, text='Три'
        )
        self.assertEqual(
            list(RoomChatMessage.objects.all()),
            [first, second, third],
        )

    def test_deleting_author_keeps_message_with_null_author(self):
        """Сообщение — часть истории комнаты и переживает уход автора."""
        message = RoomChatMessage.objects.create(
            room=self.room, author=self.freelancer, text='Ушедший участник писал сюда'
        )
        self.freelancer.delete()
        message.refresh_from_db()
        self.assertIsNone(message.author)
        self.assertEqual(message.text, 'Ушедший участник писал сюда')

    def test_deleting_room_cascades_chat_messages(self):
        RoomChatMessage.objects.create(room=self.room, author=self.director, text='Раз')
        RoomChatMessage.objects.create(room=self.room, author=self.director, text='Два')
        self.assertEqual(RoomChatMessage.objects.count(), 2)
        self.room.delete()
        self.assertEqual(RoomChatMessage.objects.count(), 0)


class RoomChatFeatureToggleTests(RoomChatTestCase):
    """Пункты 5–8: `Room.chat_enabled` как настоящий переключатель."""

    def test_data_migration_enables_chat_for_existing_rooms(self):
        """Комнаты, созданные до чата, миграция включает, а не оставляет молча."""
        legacy_project = Project.objects.create(
            owner=self.director, name='Комната до чата', status=Project.Status.STAFFING
        )
        legacy_room = Room.objects.create(project=legacy_project, chat_enabled=False)

        migration = importlib.import_module(
            'apps.rooms.migrations.0006_enable_chat_for_existing_rooms'
        )
        migration.enable_chat_for_existing_rooms(django_apps, connection.schema_editor())

        legacy_room.refresh_from_db()
        self.assertTrue(legacy_room.chat_enabled)

    def test_ensure_room_for_project_creates_room_with_chat_enabled(self):
        project = Project.objects.create(
            owner=self.director, name='Новый проект', status=Project.Status.STAFFING
        )
        room = ensure_room_for_project(project)
        self.assertTrue(room.chat_enabled)

    def test_ensure_room_does_not_re_enable_manually_disabled_chat(self):
        """Выключение чата в админке остаётся в силе: defaults не перезаписывают."""
        self.disable_chat()
        room = ensure_room_for_project(self.project)
        self.assertFalse(room.chat_enabled)

    def test_disabled_chat_blocks_messages_and_send(self):
        """Отключённый чат — это отказ сервера, а не просто скрытый блок."""
        self.disable_chat()
        self.client.force_login(self.director)
        self.assertEqual(self.client.get(self.messages_url).status_code, 403)
        response = self.client.post(self.send_url, {'text': 'Проскочит?'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(RoomChatMessage.objects.count(), 0)

    def test_service_refuses_to_post_into_disabled_chat(self):
        """Защита не только во view: сервис тоже не пишет в выключенный чат."""
        self.disable_chat()
        with self.assertRaises(ChatDisabledError):
            post_chat_message(self.room, self.director, 'Мимо view')
        self.assertEqual(RoomChatMessage.objects.count(), 0)

    def test_comms_page_shows_disabled_state_and_stays_available(self):
        self.disable_chat()
        self.client.force_login(self.director)
        response = self.client.get(self.comms_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Чат отключён')
        self.assertNotContains(response, 'id="chat-messages"')
        self.assertNotContains(response, self.send_url)

    def test_comms_page_shows_working_chat_when_enabled(self):
        self.client.force_login(self.director)
        response = self.client.get(self.comms_url)
        self.assertContains(response, 'id="chat-messages"')
        self.assertContains(response, self.messages_url)
        self.assertContains(response, self.send_url)


class RoomChatRbacTests(RoomChatTestCase):
    """Пункты 9–14: доступ к чату — тот же RBAC комнаты, без своих ролей."""

    def assert_can_read_and_write(self, user, text):
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.messages_url).status_code, 200)
        response = self.client.post(self.send_url, {'text': text})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            RoomChatMessage.objects.filter(
                room=self.room, author=user, text=text
            ).exists()
        )

    def assert_denied(self, user=None):
        if user is not None:
            self.client.force_login(user)
        self.assertEqual(self.client.get(self.messages_url).status_code, 403)
        self.assertEqual(
            self.client.post(self.send_url, {'text': 'Чужая комната'}).status_code, 403
        )
        self.assertFalse(
            RoomChatMessage.objects.filter(text='Чужая комната').exists()
        )

    def test_director_reads_and_writes(self):
        self.assert_can_read_and_write(self.director, 'Сообщение директора')

    def test_teamlead_reads_and_writes(self):
        self.assert_can_read_and_write(self.teamlead, 'Сообщение тимлида')

    def test_freelancer_member_reads_and_writes(self):
        self.assert_can_read_and_write(self.freelancer, 'Сообщение фрилансера')

    def test_admin_keeps_global_access(self):
        admin = make_user(email='admin@chat.test', role=User.Roles.ADMIN)
        self.assert_can_read_and_write(admin, 'Сообщение админа')

    def test_outsider_is_denied_on_both_endpoints(self):
        self.assert_denied(self.outsider)

    def test_manager_without_membership_is_denied(self):
        self.assert_denied(make_user(email='mgr@chat.test', role=User.Roles.MANAGER))

    def test_anonymous_is_redirected_to_login(self):
        for url in (self.messages_url, self.send_url):
            with self.subTest(url=url):
                response = (
                    self.client.get(url)
                    if url == self.messages_url
                    else self.client.post(url, {'text': 'Аноним'})
                )
                self.assertEqual(response.status_code, 302)
                self.assertIn('/login/', response['Location'])
        self.assertEqual(RoomChatMessage.objects.count(), 0)

    def test_removed_freelancer_loses_chat_access(self):
        """Удаление RoomMember закрывает и чтение, и запись — без отдельных правил."""
        self.assert_can_read_and_write(self.freelancer, 'Пока я в команде')

        RoomMember.objects.filter(room=self.room, user=self.freelancer).delete()

        self.client.force_login(self.freelancer)
        self.assertEqual(self.client.get(self.messages_url).status_code, 403)
        response = self.client.post(self.send_url, {'text': 'Уже не в команде'})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            RoomChatMessage.objects.filter(text='Уже не в команде').exists()
        )

    def test_direct_post_into_foreign_room_is_denied(self):
        """Прямой POST мимо UI в чужую комнату не проходит."""
        foreign_director = make_director(email='foreign@chat.test')
        foreign_project = Project.objects.create(
            owner=foreign_director, name='Чужой проект', status=Project.Status.STAFFING
        )
        ensure_room_for_project(foreign_project)

        self.client.force_login(self.freelancer)
        response = self.client.post(
            reverse(
                'rooms:room_chat_send', kwargs={'project_id': foreign_project.id}
            ),
            {'text': 'Вторжение'},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(RoomChatMessage.objects.filter(text='Вторжение').exists())


class RoomChatFormAndSecurityTests(RoomChatTestCase):
    """Пункты 15–19: форма, лимиты, экранирование, CSRF."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.director)

    def test_normal_text_is_accepted(self):
        response = self.client.post(self.send_url, {'text': '  Созвон в 15:00  '})
        self.assertRedirects(response, self.comms_url)
        message = RoomChatMessage.objects.get()
        self.assertEqual(message.text, 'Созвон в 15:00')
        self.assertEqual(message.author, self.director)

    def test_whitespace_only_message_is_rejected(self):
        for raw in ('', '   ', ' \n\t  \n '):
            with self.subTest(raw=repr(raw)):
                self.client.post(self.send_url, {'text': raw})
                self.assertEqual(RoomChatMessage.objects.count(), 0)
        self.assertFalse(RoomChatMessageForm({'text': '   '}).is_valid())

    def test_message_longer_than_limit_is_rejected(self):
        self.client.post(self.send_url, {'text': 'я' * (CHAT_MESSAGE_MAX_LENGTH + 1)})
        self.assertEqual(RoomChatMessage.objects.count(), 0)

        self.client.post(self.send_url, {'text': 'я' * CHAT_MESSAGE_MAX_LENGTH})
        self.assertEqual(RoomChatMessage.objects.count(), 1)

    def test_form_limits_are_declared_explicitly(self):
        field = RoomChatMessageForm().fields['text']
        self.assertTrue(field.required)
        self.assertTrue(field.strip)
        self.assertEqual(field.max_length, CHAT_MESSAGE_MAX_LENGTH)
        self.assertEqual(CHAT_MESSAGE_MAX_LENGTH, 2000)

    def test_script_tags_are_escaped_in_partial(self):
        payload = '<script>alert("xss")</script>'
        self.client.post(self.send_url, {'text': payload})
        response = self.client.get(self.messages_url)
        body = response.content.decode()
        self.assertNotIn('<script>', body)
        self.assertIn('&lt;script&gt;', body)
        self.assertIn('&quot;xss&quot;', body)

    def test_script_tags_are_escaped_on_comms_page(self):
        RoomChatMessage.objects.create(
            room=self.room, author=self.director, text='<b>жирный</b>'
        )
        body = self.client.get(self.comms_url).content.decode()
        self.assertNotIn('<b>жирный</b>', body)
        self.assertIn('&lt;b&gt;', body)

    def test_send_is_post_only(self):
        self.assertEqual(self.client.get(self.send_url).status_code, 405)

    def test_messages_endpoint_rejects_unsafe_methods(self):
        """Опрос обязан быть read-only, поэтому POST на него не принимается."""
        self.assertEqual(
            self.client.post(self.messages_url, {'text': 'Через опрос'}).status_code,
            405,
        )
        self.assertEqual(RoomChatMessage.objects.count(), 0)

    def test_csrf_protection_is_not_disabled(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.director)
        response = csrf_client.post(self.send_url, {'text': 'Без токена'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(RoomChatMessage.objects.count(), 0)

    def test_form_on_page_carries_csrf_token(self):
        self.assertContains(self.client.get(self.comms_url), 'csrfmiddlewaretoken')


class RoomChatHtmxTests(RoomChatTestCase):
    """Пункты 20–22: HTMX-опрос, HTMX-отправка и fallback без JavaScript."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.director)

    def test_messages_endpoint_returns_partial_without_room_navigation(self):
        RoomChatMessage.objects.create(
            room=self.room, author=self.director, text='Только лента'
        )
        response = self.client.get(self.messages_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Только лента')
        self.assertNotContains(response, 'class="room-tabs"')
        self.assertNotContains(response, '<html')
        self.assertNotContains(response, 'id="chat-messages"')

    def test_htmx_send_returns_updated_partial(self):
        response = self.client.post(
            self.send_url, {'text': 'HTMX-сообщение'}, HTTP_HX_REQUEST='true'
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'HTMX-сообщение')
        self.assertNotContains(response, 'class="room-tabs"')
        self.assertTrue(RoomChatMessage.objects.filter(text='HTMX-сообщение').exists())

    def test_htmx_send_with_invalid_text_returns_partial_with_error(self):
        response = self.client.post(
            self.send_url, {'text': '   '}, HTTP_HX_REQUEST='true'
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Сообщение не может быть пустым.')
        self.assertEqual(RoomChatMessage.objects.count(), 0)

    def test_plain_post_falls_back_to_redirect(self):
        response = self.client.post(self.send_url, {'text': 'Без JavaScript'})
        self.assertRedirects(response, self.comms_url)
        self.assertTrue(RoomChatMessage.objects.filter(text='Без JavaScript').exists())

    def test_plain_post_with_invalid_text_redirects_with_flash(self):
        response = self.client.post(self.send_url, {'text': ''}, follow=True)
        self.assertEqual(response.status_code, 200)
        flash = [str(m) for m in response.context['messages']]
        self.assertIn('Сообщение не может быть пустым.', flash)
        self.assertEqual(RoomChatMessage.objects.count(), 0)

    def test_page_polls_without_websockets(self):
        body = self.client.get(self.comms_url).content.decode()
        self.assertIn('hx-trigger="every 7s', body)
        self.assertIn('document.visibilityState', body)
        self.assertIn('hx-swap="innerHTML"', body)
        self.assertNotIn('WebSocket', body)
        self.assertNotIn('EventSource', body)


class RoomChatHistoryAndPerformanceTests(RoomChatTestCase):
    """Пункты 23–27: лимит истории, порядок, отсутствие N+1 и записей на GET."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.director)

    def make_messages(self, count, author=None):
        return [
            RoomChatMessage.objects.create(
                room=self.room,
                author=author or self.director,
                text=f'Сообщение {index:03d}',
            )
            for index in range(count)
        ]

    def test_only_last_messages_are_shown(self):
        self.make_messages(CHAT_HISTORY_LIMIT + 10)
        body = self.client.get(self.messages_url).content.decode()
        self.assertEqual(body.count('class="chat-message"'), CHAT_HISTORY_LIMIT)
        self.assertNotIn('Сообщение 000', body)
        self.assertNotIn('Сообщение 009', body)
        self.assertIn('Сообщение 010', body)
        self.assertIn('Сообщение 059', body)

    def test_selector_returns_last_messages_oldest_first(self):
        self.make_messages(CHAT_HISTORY_LIMIT + 5)
        texts = [message.text for message in recent_chat_messages(self.room)]
        self.assertEqual(len(texts), CHAT_HISTORY_LIMIT)
        self.assertEqual(texts, sorted(texts))
        self.assertEqual(texts[0], 'Сообщение 005')
        self.assertEqual(texts[-1], 'Сообщение 054')

    def test_partial_renders_oldest_first(self):
        self.make_messages(3)
        body = self.client.get(self.messages_url).content.decode()
        self.assertLess(body.index('Сообщение 000'), body.index('Сообщение 001'))
        self.assertLess(body.index('Сообщение 001'), body.index('Сообщение 002'))

    def test_selector_fetches_messages_and_authors_in_one_query(self):
        """select_related убирает N+1: автор каждого сообщения не стоит запроса."""
        for index in range(10):
            RoomChatMessage.objects.create(
                room=self.room,
                author=make_freelancer(email=f'author{index}@chat.test'),
                text=f'Сообщение автора {index}',
            )
        with self.assertNumQueries(1):
            for message in recent_chat_messages(self.room):
                self.assertTrue(message.author.full_name)

    def test_messages_endpoint_query_count_does_not_grow_with_history(self):
        """Стоимость одного опроса не зависит от размера переписки."""
        self.make_messages(3)
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(self.client.get(self.messages_url).status_code, 200)

        for index in range(30):
            RoomChatMessage.objects.create(
                room=self.room,
                author=make_freelancer(email=f'many{index}@chat.test'),
                text=f'Ещё сообщение {index}',
            )
        with CaptureQueriesContext(connection) as large:
            self.assertEqual(self.client.get(self.messages_url).status_code, 200)

        self.assertEqual(len(large.captured_queries), len(small.captured_queries))

    def test_polling_get_writes_nothing(self):
        self.make_messages(3)
        counts_before = (
            RoomChatMessage.objects.count(),
            RoomActivity.objects.count(),
            Room.objects.count(),
            RoomMember.objects.count(),
        )
        for _ in range(3):
            self.assertEqual(self.client.get(self.messages_url).status_code, 200)
        self.assertEqual(
            (
                RoomChatMessage.objects.count(),
                RoomActivity.objects.count(),
                Room.objects.count(),
                RoomMember.objects.count(),
            ),
            counts_before,
        )

    def test_sending_message_does_not_create_room_activity(self):
        """Лента комнаты — события, а не копия переписки."""
        activities_before = RoomActivity.objects.count()
        for index in range(5):
            self.client.post(self.send_url, {'text': f'Сообщение {index}'})
        self.assertEqual(RoomChatMessage.objects.count(), 5)
        self.assertEqual(RoomActivity.objects.count(), activities_before)

    def test_chat_of_other_room_is_not_mixed_in(self):
        other_project = Project.objects.create(
            owner=self.director, name='Соседняя комната', status=Project.Status.STAFFING
        )
        other_room = ensure_room_for_project(other_project)
        RoomChatMessage.objects.create(
            room=other_room, author=self.director, text='Соседняя переписка'
        )
        RoomChatMessage.objects.create(
            room=self.room, author=self.director, text='Наша переписка'
        )
        body = self.client.get(self.messages_url).content.decode()
        self.assertIn('Наша переписка', body)
        self.assertNotIn('Соседняя переписка', body)


class RoomChatPartialRenderingTests(RoomChatTestCase):
    """Как partial показывает автора, время и пустое состояние."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.director)

    def test_empty_state_is_shown_when_there_are_no_messages(self):
        self.assertContains(self.client.get(self.messages_url), 'Сообщений пока нет')

    def test_message_shows_author_name_and_time(self):
        message = RoomChatMessage.objects.create(
            room=self.room, author=self.teamlead, text='Отчёты приняты'
        )
        response = self.client.get(self.messages_url)
        self.assertContains(response, self.teamlead.full_name)
        self.assertContains(response, 'Отчёты приняты')
        self.assertContains(response, message.created_at.strftime('%Y'))

    def test_deleted_author_is_shown_as_removed_participant(self):
        RoomChatMessage.objects.create(
            room=self.room, author=self.freelancer, text='Сообщение ушедшего'
        )
        self.freelancer.delete()
        response = self.client.get(self.messages_url)
        self.assertContains(response, 'Удалённый участник')
        self.assertContains(response, 'Сообщение ушедшего')


class RoomChatRegressionTests(RoomChatTestCase):
    """Пункты 28–29: чат не сломал видео-заглушку и навигацию комнаты."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.director)

    def test_video_placeholder_is_untouched(self):
        response = self.client.get(self.comms_url)
        self.assertContains(response, 'id="comms-video"')
        self.assertContains(response, 'Видеовстреча')
        self.assertContains(response, 'Следующий этап')

    def test_jitsi_is_not_implemented_in_this_step(self):
        body = self.client.get(self.comms_url).content.decode()
        self.assertNotIn('jit.si', body)
        self.assertNotIn('<iframe', body)

    def test_six_tab_navigation_still_works(self):
        response = self.client.get(self.comms_url)
        labels = [
            label for _url, _css, label in parse_room_tabs(response.content.decode())
        ]
        self.assertEqual(labels, EXPECTED_TABS)

    def test_comms_tab_is_still_reachable_for_every_member(self):
        for user in (self.director, self.teamlead, self.freelancer):
            with self.subTest(role=user.role):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.comms_url).status_code, 200)

    def test_draft_project_without_room_has_no_chat_endpoints(self):
        """У черновика комнаты нет — чат её не создаёт побочно."""
        draft = Project.objects.create(
            owner=self.director, name='Черновик', status=Project.Status.DRAFT
        )
        rooms_before = Room.objects.count()
        self.assertEqual(
            self.client.get(
                reverse(
                    'rooms:room_chat_messages', kwargs={'project_id': draft.id}
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                reverse('rooms:room_chat_send', kwargs={'project_id': draft.id}),
                {'text': 'В несуществующую комнату'},
            ).status_code,
            404,
        )
        self.assertEqual(Room.objects.count(), rooms_before)
