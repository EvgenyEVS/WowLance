from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.pipeline.services import START_CALLS_TITLE, ensure_start_calls_task
from apps.rooms.models import Project, RoomMember
from apps.rooms.services import assign_teamlead, launch_project
from apps.test_helpers import make_director, make_freelancer, make_teamlead


class FreelancerOverviewUITests(TestCase):
    def setUp(self):
        self.client = Client()

        self.director = make_director(email='director@overview-ui.test')
        self.teamlead = make_teamlead(email='teamlead@overview-ui.test')
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
        self.overview_url = reverse(
            'rooms:room_overview', kwargs={'project_id': self.project.id}
        )

    def test_freelancer_overview_has_no_team_management(self):
        """Фрилансер на Обзоре не видит блок 'Команда' и кнопку 'Управление командой'."""
        self.client.force_login(self.freelancer)
        response = self.client.get(self.overview_url)

        self.assertEqual(response.status_code, 200)
        # Проверяем отсутствие кнопки управления командой
        self.assertNotContains(response, 'Управление командой')

    def test_freelancer_overview_has_no_catalog_link(self):
        """Фрилансер на Обзоре не видит ссылку на каталог."""
        self.client.force_login(self.freelancer)
        response = self.client.get(self.overview_url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Открыть каталог')

    def test_freelancer_base_has_no_catalog(self):
        """В шапке сайта у фрилансера нет пункта 'Каталог'."""
        self.client.force_login(self.freelancer)
        response = self.client.get(reverse('core:home'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Каталог')

    def test_director_overview_is_read_only_ops_stay_with_teamlead(self):
        """Директор на Обзоре без операционных кнопок: состав и назначение тимлида."""
        self.client.force_login(self.director)
        response = self.client.get(self.overview_url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Управление командой')
        self.assertNotContains(response, '>Команда</h3>')
        self.assertNotContains(response, '>Команда</a>')
        self.assertContains(response, 'Назначить тимлида')

    def test_freelancer_overview_is_personal_workspace(self):
        """Обзор фрилансера: вводные, свои задачи, своя статистика и состав без экономики."""
        self.client.force_login(self.freelancer)
        response = self.client.get(self.overview_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Вводные проекта')
        self.assertContains(response, 'Мои задачи')
        self.assertContains(response, 'id="freelancer-project-stats"')
        self.assertContains(response, 'Отчёты приняты')
        self.assertIsNotNone(response.context['my_project_stats'])
        self.assertContains(response, 'Состав команды')
        self.assertContains(response, 'id="team-composition-overview"')
        self.assertContains(response, 'Закрытые лиды (всего)')
        self.assertContains(response, 'Закрытые лиды (проект)')

        self.assertNotContains(response, 'functional-roles-configurator')
        self.assertNotContains(response, 'Юнит-экономика')
        self.assertNotContains(response, 'Производительность')
        self.assertNotContains(response, 'Hot leads / мес')
        self.assertNotContains(response, '<h3>Подбор команды</h3>')
        self.assertNotContains(response, 'Лента событий')
        self.assertNotContains(
            response, 'SLA запустится после готовности команды'
        )
        self.assertNotContains(response, 'Назначить тимлида')

    def test_freelancer_ready_button_is_only_at_the_top(self):
        self.client.force_login(self.freelancer)
        response = self.client.get(self.overview_url)
        body = response.content.decode()
        self.assertEqual(body.count('Готов к работе'), 1)
        self.assertLess(
            body.index('Готов к работе'),
            body.index('freelancer-project-stats'),
        )

    def test_freelancer_sla_only_when_start_task_assigned_to_him(self):
        assign_teamlead(self.project, self.teamlead)
        task, _created = ensure_start_calls_task(self.project)
        # По умолчанию исполнитель — тимлид; баннера у фрилансера быть не должно.
        self.assertEqual(task.assignee_id, self.teamlead.id)
        self.client.force_login(self.freelancer)
        response = self.client.get(self.overview_url)
        self.assertIsNone(response.context['start_calls_task'])
        self.assertNotContains(response, 'SLA стартовой задачи')
        self.assertNotContains(
            response, 'SLA запустится после готовности команды'
        )

        task.assignee = self.freelancer
        task.deadline = timezone.now() + timedelta(hours=12)
        task.save(update_fields=['assignee', 'deadline'])
        response = self.client.get(self.overview_url)
        self.assertEqual(response.context['start_calls_task'].id, task.id)
        self.assertContains(response, 'SLA стартовой задачи')
        self.assertContains(response, START_CALLS_TITLE)

    def test_freelancer_team_composition_shows_names_and_lead_counts(self):
        assign_teamlead(self.project, self.teamlead)
        from apps.pipeline.models import Lead
        from apps.rooms.models import RoomFunctionSlot, RoomMember
        from apps.rooms.services import save_functional_roles_and_sync_slots

        save_functional_roles_and_sync_slots(
            self.project,
            [
                {'role_key': 'teamlead', 'count': 1},
                {'role_key': 'seller_middle', 'count': 1},
            ],
            self.director,
        )
        slot = RoomFunctionSlot.objects.filter(
            room=self.project.room, role_key='seller_middle', is_active=True
        ).first()
        self.assertIsNotNone(slot)
        member = RoomMember.objects.get(
            room=self.project.room, user=self.freelancer
        )
        member.function_slot = slot
        member.role_key = 'seller_middle'
        member.save(update_fields=['function_slot', 'role_key'])

        Lead.objects.create(
            project=self.project,
            creator=self.freelancer,
            contact_info={'name': 'На проекте'},
        )
        other = Project.objects.create(
            owner=self.director,
            name='Другой проект',
            status=Project.Status.DRAFT,
            input_data={'offer': 'o', 'utp': 'u', 'audience': 'a', 'hot_criteria': 'h'},
        )
        Lead.objects.create(
            project=other,
            creator=self.freelancer,
            contact_info={'name': 'На другом'},
        )

        self.client.force_login(self.freelancer)
        response = self.client.get(self.overview_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.freelancer.full_name)
        self.assertContains(response, self.teamlead.full_name)
        rows = response.context['team_composition_rows']
        seller = next(r for r in rows if r['user_id'] == self.freelancer.id)
        self.assertEqual(seller['leads_on_project'], 1)
        self.assertEqual(seller['leads_total'], 2)

    def test_director_and_teamlead_keep_ops_overview_blocks(self):
        assign_teamlead(self.project, self.teamlead)
        for user in (self.director, self.teamlead):
            with self.subTest(role=user.role):
                self.client.force_login(user)
                response = self.client.get(self.overview_url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Лента событий')
                self.assertContains(response, 'functional-roles-configurator')
                self.assertContains(response, 'Юнит-экономика')
                self.assertIsNone(response.context['my_project_stats'])
                # Idle SLA, пока стартовой задачи нет.
                self.assertContains(
                    response, 'SLA запустится после готовности команды'
                )
