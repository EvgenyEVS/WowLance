"""Комната глазами роли: какие вкладки видны, что открывается, что закрыто.

Один проект, четыре пользователя — владелец-директор, тимлид, фрилансер-участник
и посторонний. Проверяется поверхность доступа комнаты:

* набор вкладок по роли и совпадение «ссылка видна» ↔ «адрес открывается»;
* операционка (команда, постановка задач) — у тимлида, не у владельца;
* «Обзор» фрилансера показывает только его задачи;
* материалы: тимлид пишет, фрилансер читает, чужое не удаляет;
* посторонний не входит ни на одну вкладку.

Видимость проверяется по реальным `reverse()`-адресам и флагам контекста,
а не по разметке: подписи и классы — не контракт. Бизнес-логика задач и
лидов живёт в `apps.pipeline.tests`, подбор — в `tests_staffing`.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.pipeline.models import Task
from apps.rooms.models import RoomActivity, RoomDocument, RoomMember
from apps.test_helpers import make_freelancer, make_staffed_project


class RoomRbacTestCase(TestCase):
    """Комната в подборе: директор-владелец, тимлид, фрилансер, посторонний.

    Фикстура готовит только данные. Участие фрилансера создаётся здесь прямой
    записью `RoomMember`: способ попадания в комнату — предмет `rooms/tests.py`,
    а не этого файла, и прятать его в хелпер было бы подменой RBAC-проверки.
    """

    def setUp(self):
        fixture = make_staffed_project(slots=1)
        self.project = fixture.project
        self.room = fixture.room
        self.director = fixture.director
        self.teamlead = fixture.teamlead
        self.project.input_data = {
            'offer': 'Оффер',
            'utp': 'УТП',
            'audience': 'ЦА',
            'hot_criteria': 'Запросил демо',
        }
        self.project.save(update_fields=['input_data'])

        self.freelancer = make_freelancer(email='member@rbac.test')
        RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )
        self.outsider = make_freelancer(email='outsider@rbac.test')

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

    def overview_for(self, user):
        self.client.force_login(user)
        response = self.client.get(self.tab_urls()['overview'])
        self.assertEqual(response.status_code, 200)
        return response


# ---------------------------------------------------------------------------
# 1-5. Вкладки: что видно и что открывается
# ---------------------------------------------------------------------------


class RoomTabVisibilityTests(RoomRbacTestCase):
    def test_three_roles_get_three_tab_sets(self):
        """Один проект, три логина — три разных набора вкладок.

        Флаги контекста и реальные адреса проверяются вместе: скрытая ссылка
        без закрытого адреса защитой не является (см. #3).
        """
        urls = self.tab_urls()
        expectations = (
            # роль,           team,  tasks
            (self.director, False, False),
            (self.teamlead, True, True),
            (self.freelancer, False, True),
        )
        for user, show_team, show_tasks in expectations:
            with self.subTest(role=user.role):
                response = self.overview_for(user)
                self.assertIs(response.context['show_team_tab'], show_team)
                self.assertIs(response.context['show_tasks_tab'], show_tasks)

                for key, expected in (('team', show_team), ('tasks', show_tasks)):
                    with self.subTest(tab=key):
                        if expected:
                            self.assertContains(response, urls[key])
                        else:
                            self.assertNotContains(response, urls[key])

                # Общая часть комнаты одинакова для всех трёх ролей.
                for key in ('leads', 'documents', 'comms'):
                    with self.subTest(tab=key):
                        self.assertContains(response, urls[key])

    def test_freelancer_nav_has_no_team_link(self):
        response = self.overview_for(self.freelancer)

        self.assertNotContains(response, self.tab_urls()['team'])
        self.assertIs(response.context['show_team_tab'], False)

    def test_freelancer_direct_get_room_team_is_forbidden(self):
        """Скрытая вкладка — не защита: адрес закрыт самим view."""
        self.client.force_login(self.freelancer)

        response = self.client.get(self.tab_urls()['team'])

        self.assertEqual(response.status_code, 403)

    def test_director_room_team_redirects_to_overview(self):
        self.client.force_login(self.director)

        response = self.client.get(self.tab_urls()['team'])

        self.assertRedirects(response, self.tab_urls()['overview'])

    def test_teamlead_opens_room_team(self):
        self.client.force_login(self.teamlead)

        response = self.client.get(self.tab_urls()['team'])

        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# 6-9. «Обзор» фрилансера и постановка задач
# ---------------------------------------------------------------------------


class RoomOverviewScopeTests(RoomRbacTestCase):
    def make_task(self, assignee, title):
        return Task.objects.create(
            project=self.project,
            assignee=assignee,
            created_by=self.teamlead,
            title=title,
        )

    def test_freelancer_overview_has_no_team_management(self):
        """Управление командой ведёт на адрес «Команды» — его у фрилансера нет.

        Проверяется адрес, а не подпись кнопки: у тимлида он на «Обзоре» есть,
        у фрилансера отсутствует, значит проверка не вырождена.
        """
        team_url = self.tab_urls()['team']

        self.assertContains(self.overview_for(self.teamlead), team_url)
        self.assertNotContains(self.overview_for(self.freelancer), team_url)

    def test_freelancer_sees_only_his_own_tasks_on_overview(self):
        mine = self.make_task(self.freelancer, 'Моя задача')
        self.make_task(self.teamlead, 'Задача тимлида')

        response = self.overview_for(self.freelancer)

        self.assertTrue(response.context['is_freelancer_task_preview'])
        preview = response.context['my_tasks_preview']
        self.assertEqual([task.id for task in preview], [mine.id])
        self.assertEqual(
            {task.assignee_id for task in preview}, {self.freelancer.id}
        )
        # Общая доска фрилансеру не отдаётся вовсе.
        self.assertEqual(response.context['kanban_preview'], [])

    def test_freelancer_does_not_see_foreign_tasks(self):
        other_freelancer = make_freelancer(email='other-member@rbac.test')
        RoomMember.objects.create(
            room=self.room,
            user=other_freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )
        self.make_task(self.freelancer, 'Моя задача')
        foreign = self.make_task(other_freelancer, 'Чужая задача')

        response = self.overview_for(self.freelancer)

        self.assertNotIn(
            foreign.id, [task.id for task in response.context['my_tasks_preview']]
        )
        self.assertNotContains(response, foreign.title)
        # Задача не исчезла из проекта — она просто не принадлежит этому исполнителю.
        self.assertTrue(Task.objects.filter(pk=foreign.pk).exists())

    def test_task_creation_belongs_to_the_teamlead_only(self):
        """Право ставить задачи — у тимлида; владелец на доску даже не заходит."""
        tasks_url = self.tab_urls()['tasks']
        for user, expected in ((self.teamlead, True), (self.freelancer, False)):
            with self.subTest(role=user.role):
                self.client.force_login(user)
                response = self.client.get(tasks_url)
                self.assertEqual(response.status_code, 200)
                self.assertIs(response.context['can_create_task'], expected)

        self.client.force_login(self.director)
        self.assertRedirects(
            self.client.get(tasks_url), self.tab_urls()['overview']
        )


# ---------------------------------------------------------------------------
# 10-13. Материалы комнаты
# ---------------------------------------------------------------------------


class RoomMaterialsRbacTests(RoomRbacTestCase):
    def setUp(self):
        super().setUp()
        self.documents_url = self.tab_urls()['documents']
        self.upload_url = reverse(
            'rooms:room_document_upload', kwargs={'project_id': self.project.id}
        )

    def upload(self, title='Скрипт звонка', name='script.txt'):
        return self.client.post(
            self.upload_url,
            {
                'title': title,
                'file': SimpleUploadedFile(name, b'hello', content_type='text/plain'),
            },
        )

    def make_document(self, uploaded_by, title):
        return RoomDocument.objects.create(
            room=self.room,
            title=title,
            file=SimpleUploadedFile('deck.txt', b'deck', content_type='text/plain'),
            uploaded_by=uploaded_by,
        )

    def delete_url(self, document):
        return reverse(
            'rooms:room_document_delete',
            kwargs={'project_id': self.project.id, 'document_id': document.id},
        )

    def test_director_cannot_upload_materials(self):
        self.client.force_login(self.director)

        response = self.upload()

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            RoomDocument.objects.filter(room=self.room, title='Скрипт звонка').exists()
        )

    def test_teamlead_upload_flow_works(self):
        self.client.force_login(self.teamlead)

        response = self.upload()

        self.assertRedirects(response, self.documents_url)
        self.assertTrue(
            RoomDocument.objects.filter(room=self.room, title='Скрипт звонка').exists()
        )
        self.assertTrue(
            RoomActivity.objects.filter(
                room=self.room,
                event_type=RoomActivity.EventType.DOCUMENT_UPLOADED,
            ).exists()
        )

    def test_freelancer_reads_existing_document(self):
        document = self.make_document(self.teamlead, 'Презентация продукта')
        self.client.force_login(self.freelancer)

        response = self.client.get(self.documents_url)

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            document,
            [doc for group in response.context['material_groups']
             for doc in group['documents']],
        )

    def test_freelancer_cannot_delete_a_foreign_document(self):
        document = self.make_document(self.director, 'Файл директора')
        self.client.force_login(self.freelancer)

        response = self.client.post(self.delete_url(document))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(RoomDocument.objects.filter(id=document.id).exists())


# ---------------------------------------------------------------------------
# 14-15. Шапка сайта и внешние границы комнаты
# ---------------------------------------------------------------------------


class RoomBoundaryTests(RoomRbacTestCase):
    def test_freelancer_header_has_no_catalog(self):
        """Каталог — инструмент найма: в шапке фрилансера его адреса нет."""
        catalog_url = reverse('profiles:catalog')
        home_url = reverse('core:home')

        self.client.force_login(self.director)
        self.assertContains(self.client.get(home_url), catalog_url)

        self.client.force_login(self.freelancer)
        self.assertNotContains(self.client.get(home_url), catalog_url)

    def test_outsider_is_blocked_on_every_tab(self):
        self.client.force_login(self.outsider)

        for key, url in self.tab_urls().items():
            with self.subTest(tab=key):
                self.assertEqual(self.client.get(url).status_code, 403)
