"""Комната как единое пространство из шести вкладок.

Проверяется навигационный фундамент: один источник разметки вкладок, их
порядок, активная вкладка на каждой странице и доступ по существующему RBAC.
Бизнес-логика задач, лидов и подбора здесь не тестируется — у неё свои файлы.
"""

import re

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.pipeline.models import Task
from apps.rooms.models import (
    Project,
    Room,
    RoomActivity,
    RoomDocument,
    RoomFunctionSlot,
    RoomMember,
)
from apps.rooms.services import (
    add_freelancer_to_room,
    assign_teamlead,
    launch_project,
)
from apps.test_helpers import make_director, make_freelancer, make_teamlead, make_user
from apps.users.models import User

#: Продуктовый порядок вкладок комнаты. Единственный источник разметки —
#: rooms/_room_header.html, поэтому список ожиданий здесь тоже один.
EXPECTED_TABS = ['Обзор', 'Команда', 'Задачи', 'Лиды', 'Материалы', 'Коммуникации']

NAV_RE = re.compile(r'<nav class="room-tabs".*?</nav>', re.DOTALL)
TAB_LINK_RE = re.compile(r'<a href="([^"]+)" class="([^"]*)">([^<]+)</a>')


def parse_room_tabs(html):
    """Ссылки навигации комнаты как список (url, css, label)."""
    nav = NAV_RE.search(html)
    assert nav is not None, 'На странице нет навигации комнаты'
    return [
        (url, css.strip(), label.strip())
        for url, css, label in TAB_LINK_RE.findall(nav.group(0))
    ]


class RoomTabsTestCase(TestCase):
    """Общая комната: директор, тимлид, фрилансер внутри; аутсайдер снаружи."""

    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir@tabs.test')
        self.teamlead = make_teamlead(email='tl@tabs.test')
        self.freelancer = make_freelancer(email='fr@tabs.test')
        self.outsider = make_freelancer(email='out@tabs.test')
        self.project = Project.objects.create(
            owner=self.director,
            name='Комната шести вкладок',
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

    def tab_urls(self):
        pid = self.project.id
        return {
            'overview': reverse('rooms:room_overview', kwargs={'project_id': pid}),
            'team': reverse('rooms:room_team', kwargs={'project_id': pid}),
            'tasks': reverse('pipeline:room_tasks', kwargs={'project_id': pid}),
            'leads': reverse('pipeline:room_leads', kwargs={'project_id': pid}),
            'documents': reverse('rooms:room_documents', kwargs={'project_id': pid}),
            'comms': reverse('rooms:room_comms', kwargs={'project_id': pid}),
        }


class RoomNavigationTests(RoomTabsTestCase):
    """Пункты 1–8: состав, порядок, активность и адреса вкладок."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.director)

    def test_overview_shows_six_tabs(self):
        response = self.client.get(self.tab_urls()['overview'])
        self.assertEqual(response.status_code, 200)
        tabs = parse_room_tabs(response.content.decode())
        self.assertEqual(len(tabs), 6)

    def test_tab_order_is_product_order(self):
        response = self.client.get(self.tab_urls()['overview'])
        labels = [
            label for _url, _css, label in parse_room_tabs(response.content.decode())
        ]
        self.assertEqual(labels, EXPECTED_TABS)

    def _assert_active(self, page_key, expected_label):
        """Открывает вкладку и проверяет, что активна ровно она одна."""
        response = self.client.get(self.tab_urls()[page_key])
        self.assertEqual(response.status_code, 200)
        tabs = parse_room_tabs(response.content.decode())
        active = [label for _url, css, label in tabs if 'active' in css.split()]
        self.assertEqual(active, [expected_label])

    def test_overview_tab_is_active(self):
        self._assert_active('overview', 'Обзор')

    def test_team_tab_is_active(self):
        self._assert_active('team', 'Команда')

    def test_tasks_tab_is_active(self):
        self._assert_active('tasks', 'Задачи')

    def test_leads_tab_is_active(self):
        self._assert_active('leads', 'Лиды')

    def test_materials_tab_is_active(self):
        self._assert_active('documents', 'Материалы')

    def test_comms_tab_is_active(self):
        self._assert_active('comms', 'Коммуникации')

    def test_tab_links_point_to_existing_room_urls(self):
        """Каждая ссылка ведёт на реальный адрес этой комнаты и открывается."""
        expected = self.tab_urls()
        response = self.client.get(expected['overview'])
        hrefs = [url for url, _css, _label in parse_room_tabs(response.content.decode())]
        self.assertEqual(
            hrefs,
            [
                expected['overview'],
                expected['team'],
                expected['tasks'],
                expected['leads'],
                expected['documents'],
                expected['comms'],
            ],
        )
        for href in hrefs:
            self.assertEqual(self.client.get(href).status_code, 200, href)

    def test_every_room_page_renders_the_same_tab_set(self):
        """Копий списка вкладок нет: все шесть страниц дают одну навигацию."""
        for key, url in self.tab_urls().items():
            with self.subTest(page=key):
                response = self.client.get(url)
                labels = [
                    label
                    for _url, _css, label in parse_room_tabs(response.content.decode())
                ]
                self.assertEqual(labels, EXPECTED_TABS)


class RoomMaterialsTabTests(RoomTabsTestCase):
    """Пункты 9–13: материалы работают как раньше, но называются иначе."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.director)
        self.url = reverse(
            'rooms:room_documents', kwargs={'project_id': self.project.id}
        )

    def _upload(self, title='Скрипт звонка', name='script.txt'):
        return self.client.post(
            reverse(
                'rooms:room_document_upload',
                kwargs={'project_id': self.project.id},
            ),
            {
                'title': title,
                'file': SimpleUploadedFile(name, b'hello', content_type='text/plain'),
            },
        )

    def test_existing_documents_are_listed_with_author_and_date(self):
        document = RoomDocument.objects.create(
            room=self.project.room,
            title='Презентация продукта',
            file=SimpleUploadedFile('deck.txt', b'deck', content_type='text/plain'),
            uploaded_by=self.teamlead,
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Презентация продукта')
        self.assertContains(response, self.teamlead.full_name)
        self.assertContains(response, document.created_at.strftime('%Y'))
        groups = response.context['material_groups']
        self.assertEqual(len(groups), 1)
        self.assertIn(document, groups[0]['documents'])

    def test_upload_flow_still_works(self):
        response = self._upload()
        self.assertRedirects(response, self.url)
        self.assertTrue(
            RoomDocument.objects.filter(
                room=self.project.room, title='Скрипт звонка'
            ).exists()
        )
        self.assertTrue(
            RoomActivity.objects.filter(
                room=self.project.room,
                event_type=RoomActivity.EventType.DOCUMENT_UPLOADED,
            ).exists()
        )

    def test_delete_flow_still_works(self):
        self._upload(title='Временный файл')
        document = RoomDocument.objects.get(
            room=self.project.room, title='Временный файл'
        )
        response = self.client.post(
            reverse(
                'rooms:room_document_delete',
                kwargs={'project_id': self.project.id, 'document_id': document.id},
            )
        )
        self.assertRedirects(response, self.url)
        self.assertFalse(RoomDocument.objects.filter(id=document.id).exists())

    def test_delete_permission_unchanged_for_foreign_document(self):
        """Права не переписаны: чужой файл фрилансер удалить не может."""
        document = RoomDocument.objects.create(
            room=self.project.room,
            title='Файл директора',
            file=SimpleUploadedFile('own.txt', b'x', content_type='text/plain'),
            uploaded_by=self.director,
        )
        self.client.force_login(self.freelancer)
        response = self.client.post(
            reverse(
                'rooms:room_document_delete',
                kwargs={'project_id': self.project.id, 'document_id': document.id},
            )
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(RoomDocument.objects.filter(id=document.id).exists())

    def test_ui_label_is_materials_not_documents(self):
        response = self.client.get(self.url)
        body = response.content.decode()
        self.assertIn('Материалы', body)
        self.assertNotIn('Документ', body)

    def test_backend_url_name_room_documents_still_resolves(self):
        """Переименована подпись, а не адрес: старый reverse обязан работать."""
        self.assertEqual(self.url, f'/projects/{self.project.id}/room/documents/')
        self.assertEqual(self.client.get(self.url).status_code, 200)


class RoomCommsTabTests(RoomTabsTestCase):
    """Пункты 14–18: доступ к коммуникациям и содержимое каркаса."""

    def setUp(self):
        super().setUp()
        self.url = reverse('rooms:room_comms', kwargs={'project_id': self.project.id})

    def test_member_with_room_access_opens_page(self):
        self.client.force_login(self.freelancer)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_outsider_gets_no_access(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_director_teamlead_freelancer_see_the_same_page(self):
        """Набор вкладок одинаковый для всех ролей с доступом к комнате."""
        for user in (self.director, self.teamlead, self.freelancer):
            with self.subTest(role=user.role):
                self.client.force_login(user)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 200)
                labels = [
                    label
                    for _url, _css, label in parse_room_tabs(response.content.decode())
                ]
                self.assertEqual(labels, EXPECTED_TABS)

    def test_page_contains_video_and_chat_sections(self):
        self.client.force_login(self.director)
        response = self.client.get(self.url)
        self.assertContains(response, 'Видеовстреча')
        self.assertContains(response, 'Чат комнаты')
        self.assertContains(response, 'id="comms-video"')
        self.assertContains(response, 'id="comms-chat"')

    def test_page_shows_chat_enabled_as_read_only_state(self):
        """chat_enabled только отображается: GET его не переключает."""
        self.client.force_login(self.director)
        self.assertFalse(self.project.room.chat_enabled)
        self.assertContains(self.client.get(self.url), 'выключен')

        Room.objects.filter(pk=self.project.room.pk).update(chat_enabled=True)
        self.assertContains(self.client.get(self.url), 'включён')
        self.project.room.refresh_from_db()
        self.assertTrue(self.project.room.chat_enabled)

    def test_get_does_not_create_or_change_data(self):
        """Каркас ничего не пишет: ни комнат, ни участников, ни событий."""
        self.client.force_login(self.director)
        counts_before = (
            Room.objects.count(),
            RoomMember.objects.count(),
            RoomActivity.objects.count(),
            RoomDocument.objects.count(),
        )
        room_before = list(
            Room.objects.filter(pk=self.project.room.pk).values(
                'chat_enabled', 'created_at'
            )
        )

        self.assertEqual(self.client.get(self.url).status_code, 200)

        self.assertEqual(
            (
                Room.objects.count(),
                RoomMember.objects.count(),
                RoomActivity.objects.count(),
                RoomDocument.objects.count(),
            ),
            counts_before,
        )
        self.assertEqual(
            list(
                Room.objects.filter(pk=self.project.room.pk).values(
                    'chat_enabled', 'created_at'
                )
            ),
            room_before,
        )

    def test_draft_project_without_room_is_redirected_not_created(self):
        """У черновика комнаты нет — и вкладка её не создаёт побочно."""
        draft = Project.objects.create(
            owner=self.director,
            name='Черновик без комнаты',
            status=Project.Status.DRAFT,
        )
        self.client.force_login(self.director)
        rooms_before = Room.objects.count()
        response = self.client.get(
            reverse('rooms:room_comms', kwargs={'project_id': draft.id})
        )
        self.assertRedirects(
            response,
            reverse('rooms:project_detail', kwargs={'project_id': draft.id}),
        )
        self.assertEqual(Room.objects.count(), rooms_before)


class RoomTabsRegressionTests(RoomTabsTestCase):
    """Пункты 19–23: соседние вкладки не сломаны новой навигацией."""

    def setUp(self):
        super().setUp()
        # Слот и задача нужны, чтобы вкладки показали рабочее содержимое,
        # а не пустые состояния: регрессия проверяется на живых блоках.
        self.slot = RoomFunctionSlot.objects.create(
            room=self.project.room,
            role_key='seller',
            slot_index=1,
            required_level=RoomFunctionSlot.Grade.MIDDLE,
        )
        self.task = Task.objects.create(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.director,
            title='Обзвонить базу',
            status=Task.Status.NEW,
        )

    def test_team_staffing_ui_still_renders(self):
        self.client.force_login(self.director)
        response = self.client.get(self.tab_urls()['team'])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Функциональные слоты')
        self.assertContains(response, 'Участники')
        self.assertIn('slot_cards', response.context)

    def test_team_slot_htmx_action_still_returns_slot_card(self):
        """HTMX-действия подбора продолжают отвечать partial-карточкой."""
        slot = self.slot
        self.client.force_login(self.director)
        response = self.client.post(
            reverse(
                'rooms:room_slot_auto_assign',
                kwargs={'project_id': self.project.id, 'slot_id': slot.id},
            ),
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'slot-card-{slot.id}')
        # HTMX отвечает только карточкой слота, без общей навигации страницы.
        self.assertNotContains(response, 'class="room-tabs"')

    def test_tasks_page_works(self):
        self.client.force_login(self.director)
        response = self.client.get(self.tab_urls()['tasks'])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'К работе')
        self.assertContains(response, self.task.title)

    def test_leads_page_works(self):
        self.client.force_login(self.director)
        response = self.client.get(self.tab_urls()['leads'])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Список лидов')

    def test_overview_keeps_activity_and_staffing_blocks(self):
        self.client.force_login(self.director)
        response = self.client.get(self.tab_urls()['overview'])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Лента событий')
        self.assertContains(response, 'Вводные проекта')
        self.assertIn('staffing_summary', response.context)
        self.assertIn('activities', response.context)

    def test_outsider_is_blocked_on_every_tab(self):
        """Границы доступа не расширены: аутсайдер не входит ни на одну вкладку."""
        self.client.force_login(self.outsider)
        for key, url in self.tab_urls().items():
            with self.subTest(tab=key):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_manager_without_membership_has_no_room_access(self):
        manager = make_user(email='mgr@tabs.test', role=User.Roles.MANAGER)
        self.client.force_login(manager)
        for key, url in self.tab_urls().items():
            with self.subTest(tab=key):
                self.assertEqual(self.client.get(url).status_code, 403)
