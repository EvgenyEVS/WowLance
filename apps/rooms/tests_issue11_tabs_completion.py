"""Issue #11, финальный этап: оставшиеся однозначные Acceptance Criteria.

Что здесь проверяется (и только это):

1. бейдж грейда в конфигураторе приходит из серверного каталога;
2. SLA подбора 1 час у существующего пустого активного слота;
3. UX удаления функции с назначенным исполнителем не обещает невозможного;
4. лента «Обзора» — ровно последние 10 событий;
5. «Требуется по плану проекта» на вкладке «Команда»;
6. публичный фасад `apps.rooms.services.confirm_freelancer_readiness`;
7. человеческие названия функций вместо `role_key` в UI подбора;
8. канбан задач из четырёх колонок (см. также `apps.pipeline.tests_kanban`);
9. доска лидов Cold / Warm / Hot (там же);
10. правка вводных проекта директором-владельцем;
11. кнопка видеокомнаты Jitsi на вкладке «Коммуникации».

Чего здесь нет намеренно: правил подбора, проекции состава в слоты,
экономики и валидации состава, автоматики активации и SLA стартовой задачи —
всё это уже покрыто своими файлами и этим этапом не менялось. Границы
(matching / projection / 24-часовой SLA) проверяются только регрессиями «не
расширилось».
"""

import subprocess
import sys
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.pipeline.models import Task
from apps.rooms import configurator
from apps.rooms import functional_roles as catalog
from apps.rooms.models import (
    Project,
    Room,
    RoomActivity,
    RoomFunctionSlot,
    RoomMember,
)
from apps.rooms.services import (
    JITSI_BASE_URL,
    VISION_INPUT_KEYS,
    add_freelancer_to_room,
    ensure_room_for_project,
    log_room_activity,
    room_video_call_url,
    save_functional_roles_and_sync_slots,
    update_project_vision,
    user_can_edit_project_vision,
)
from apps.rooms.staffing import selectors
from apps.rooms.staffing.selectors import (
    SEARCH_SLA,
    SEARCH_SLA_OVERDUE_LABEL,
    SEARCH_SLA_PREFIX,
    format_countdown,
)
from apps.rooms.unit_economics import get_project_composition
from apps.test_helpers import make_director, make_freelancer, make_teamlead, make_user
from apps.users.models import User

#: Ожидаемые подписи грейдов по функциям каталога — из формулировки Issue.
EXPECTED_GRADE_LABELS = {
    'teamlead': 'N/A',
    'seller_middle': 'Middle',
    'seller_senior': 'Senior',
    'linkedin_leadgen': 'Middle',
    'database_assistant': 'Junior',
}


class RoomCompletionTestCase(TestCase):
    """Комната директора в подборе: тимлид, фрилансер, посторонние."""

    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir@issue11.test')
        self.other_director = make_director(email='other-dir@issue11.test')
        self.teamlead = make_teamlead(email='tl@issue11.test')
        self.freelancer = make_freelancer(email='fr@issue11.test')
        self.manager = make_user(email='mng@issue11.test', role=User.Roles.MANAGER)

        self.project = Project.objects.create(
            owner=self.director,
            name='Комната завершения Issue 11',
            status=Project.Status.STAFFING,
            teamlead=self.teamlead,
            input_data={
                'offer': 'Исходный оффер',
                'utp': 'Исходное УТП',
                'audience': 'Исходная ЦА',
                'hot_criteria': 'Исходные критерии',
                'architecture': 'cold_calling',
            },
        )
        self.room = ensure_room_for_project(self.project)
        add_freelancer_to_room(self.room, self.freelancer)
        RoomMember.objects.create(
            room=self.room,
            user=self.manager,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )

        self.overview_url = reverse('rooms:room_overview', args=[self.project.id])
        self.team_url = reverse('rooms:room_team', args=[self.project.id])
        self.comms_url = reverse('rooms:room_comms', args=[self.project.id])
        self.vision_url = reverse('rooms:room_vision_update', args=[self.project.id])

    # --- хелперы ---------------------------------------------------------

    def save_composition(self, **counts):
        """Штатное сохранение состава вместе с проекцией в слоты."""
        return save_functional_roles_and_sync_slots(
            self.project,
            [{'role_key': key, 'count': value} for key, value in counts.items()],
            self.director,
        )

    def make_slot(self, role_key='seller_middle', slot_index=1, **fields):
        return RoomFunctionSlot.objects.create(
            room=self.room, role_key=role_key, slot_index=slot_index, **fields
        )

    def assign(self, slot, user=None):
        member = RoomMember.objects.get(room=self.room, user=user or self.freelancer)
        member.function_slot = slot
        member.save()
        return member

    def get(self, url, user=None):
        if user is not None:
            self.client.force_login(user)
        return self.client.get(url)

    def overview_html(self, user=None):
        return self.get(self.overview_url, user).content.decode()

    def fr_row(self, html, role_key):
        start = html.index(f'id="fr-row-{role_key}"')
        return html[start:html.index('</tr>', start)]

    def staffing_cell(self, html, role_key='seller_middle'):
        row = self.fr_row(html, role_key)
        start = row.index('class="fr-staffing ')
        return row[start:row.index('</td>', start)]


# ---------------------------------------------------------------------------
# 1. Бейдж грейда
# ---------------------------------------------------------------------------


class GradeBadgeTests(RoomCompletionTestCase):
    def test_catalog_maps_every_role_to_its_public_grade_label(self):
        """Подписи грейдов задаёт каталог, а не шаблон."""
        for role_key, expected in EXPECTED_GRADE_LABELS.items():
            with self.subTest(role_key=role_key):
                self.assertEqual(catalog.role_grade_display(role_key), expected)

    def test_teamlead_has_no_grade_in_the_catalog(self):
        """N/A — не «неизвестно»: грейда у тимлида в структуре нет."""
        self.assertIsNone(catalog.FUNCTIONAL_ROLES['teamlead'].grade)
        self.assertEqual(catalog.grade_display(None), catalog.GRADE_NOT_APPLICABLE)

    def test_unknown_grade_value_does_not_crash(self):
        """Историческое значение показывается как есть, страница не падает."""
        self.assertEqual(catalog.grade_display('lead'), 'lead')

    def test_configurator_row_takes_grade_from_the_structural_catalog(self):
        """Строка конфигуратора отдаёт готовую подпись, а не сырой грейд."""
        self.save_composition(teamlead=1, seller_senior=1)
        rows = {
            row.role_key: row
            for row in self.get(self.overview_url, self.director).context['fr_rows']
        }
        self.assertEqual(rows['seller_senior'].grade_display, 'Senior')
        self.assertEqual(rows['teamlead'].grade_display, 'N/A')

    def test_overview_renders_a_grade_badge_next_to_every_role(self):
        self.save_composition(
            teamlead=1, seller_middle=1, seller_senior=1,
            linkedin_leadgen=1, database_assistant=1,
        )
        html = self.overview_html(self.director)
        for role_key, expected in EXPECTED_GRADE_LABELS.items():
            with self.subTest(role_key=role_key):
                row = self.fr_row(html, role_key)
                self.assertIn('fr-badge-grade', row)
                self.assertIn(expected, row)

    def test_public_role_titles_are_unchanged(self):
        """Бейдж добавлен рядом с названием, сами названия не тронуты."""
        self.save_composition(teamlead=1, seller_middle=1)
        html = self.overview_html(self.director)
        for role_key in ('teamlead', 'seller_middle'):
            with self.subTest(role_key=role_key):
                self.assertIn(
                    catalog.FUNCTIONAL_ROLES[role_key].label,
                    self.fr_row(html, role_key),
                )


# ---------------------------------------------------------------------------
# 1b. Fixed Teamlead UX не изменён
# ---------------------------------------------------------------------------


class FixedTeamleadUxRegressionTests(RoomCompletionTestCase):
    """Текущее решение по тимлиду сохраняется дословно.

    По последнему решению руководителя: «+» и «−» у обязательной функции
    отключены, кнопки «✕» нет вообще, а числовой ввод количества ≥ 1
    остаётся допустимым. Этот этап это поведение не трогает.
    """

    def setUp(self):
        super().setUp()
        self.save_composition(teamlead=1, seller_middle=1)

    def teamlead_row(self):
        return self.fr_row(self.overview_html(self.director), 'teamlead')

    def test_plus_and_minus_are_disabled_for_teamlead(self):
        row = self.teamlead_row()
        self.assertIn('disabled', row)
        self.assertEqual(row.count('aria-disabled="true"'), 2)

    def test_teamlead_has_no_remove_button(self):
        self.assertNotIn('fr-remove', self.teamlead_row())

    def test_numeric_count_above_one_is_rejected(self):
        """Прямой POST count=2 не обходит правило «тимлид только один»."""
        self.client.force_login(self.director)
        response = self.client.post(
            reverse('rooms:room_functional_roles_update', args=[self.project.id]),
            {'role_key': 'teamlead', 'action': 'set', 'count': '2'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'только один')
        self.project.refresh_from_db()
        counts = {
            entry['role_key']: entry['count']
            for entry in get_project_composition(self.project)
        }
        self.assertEqual(counts['teamlead'], 1)


# ---------------------------------------------------------------------------
# 2. SLA подбора: 1 час от появления слота
# ---------------------------------------------------------------------------


class StaffingSearchSlaTests(RoomCompletionTestCase):
    """SLA подбора считается по существующим слотам, созданным проекцией.

    Слоты здесь не выдумываются вручную там, где их создаёт продуктовый
    путь: состав сохраняется штатным сервисом, и проекция сама заводит
    нужное количество `RoomFunctionSlot`. Иначе тест проверял бы данные,
    которых в реальной комнате не бывает.
    """

    def setUp(self):
        super().setUp()
        # `database_assistant` слотов не получает (канал `base`) — на нём
        # проверяется честный прочерк «подбор не запущен».
        self.save_composition(teamlead=1, seller_middle=2, database_assistant=1)
        self.slots = list(
            RoomFunctionSlot.objects.filter(
                room=self.room, role_key='seller_middle'
            ).order_by('slot_index')
        )
        self.slot = self.slots[0]

    def card_for(self, slot):
        return selectors.slot_card_for(slot)

    def test_sla_is_one_hour(self):
        self.assertEqual(SEARCH_SLA, timedelta(hours=1))

    def test_deadline_is_slot_created_at_plus_one_hour(self):
        """MVP-якорь — `RoomFunctionSlot.created_at`, отдельного поля нет."""
        card = self.card_for(self.slot)
        self.assertEqual(card.search_deadline, self.slot.created_at + SEARCH_SLA)

    def test_empty_active_slot_is_searching(self):
        card = self.card_for(self.slot)
        self.assertTrue(card.is_searching)
        self.assertFalse(card.search_is_overdue)
        self.assertTrue(card.search_sla_display.startswith(SEARCH_SLA_PREFIX))

    def test_assigned_slot_gets_no_search_timer(self):
        """У назначенного исполнителя таймера поиска нет вообще."""
        self.assign(self.slot)
        card = self.card_for(self.slot)
        self.assertFalse(card.is_searching)
        self.assertIsNone(card.search_deadline)
        self.assertEqual(card.search_sla_display, '')

    def test_inactive_slot_gets_no_search_timer(self):
        RoomFunctionSlot.objects.filter(pk=self.slot.pk).update(is_active=False)
        card = self.card_for(self.slot)
        self.assertFalse(card.is_searching)
        self.assertIsNone(card.search_deadline)

    def test_overdue_is_computed_on_the_server(self):
        RoomFunctionSlot.objects.filter(pk=self.slot.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )
        card = self.card_for(self.slot)
        self.assertTrue(card.search_is_overdue)
        self.assertEqual(card.search_sla_display, SEARCH_SLA_OVERDUE_LABEL)
        self.assertEqual(card.search_seconds_left, 0)

    def test_countdown_format_is_hh_mm_ss(self):
        self.assertEqual(format_countdown(42 * 60 + 17), '00:42:17')
        self.assertEqual(format_countdown(3600), '01:00:00')
        self.assertEqual(format_countdown(-5), '00:00:00')

    def test_cell_shows_searching_label_and_sla_for_empty_slot(self):
        cell = self.staffing_cell(self.overview_html(self.director))
        self.assertIn(configurator.SEARCHING_LABEL, cell)
        self.assertIn('data-staffing-sla=', cell)
        self.assertIn(SEARCH_SLA_PREFIX, cell)

    def test_cell_shows_no_sla_for_assigned_member(self):
        """Занятый слот показывает человека и не показывает таймер поиска."""
        for slot in self.slots:
            RoomFunctionSlot.objects.filter(pk=slot.pk).update(is_active=False)
        RoomFunctionSlot.objects.filter(pk=self.slot.pk).update(is_active=True)
        self.assign(self.slot)
        cell = self.staffing_cell(self.overview_html(self.director))
        self.assertIn(self.freelancer.full_name, cell)
        self.assertNotIn('data-staffing-sla=', cell)

    def test_role_without_slots_gets_no_fake_sla(self):
        """Слота нет — нейтральный прочерк, никакого SLA."""
        cell = self.staffing_cell(
            self.overview_html(self.director), 'database_assistant'
        )
        self.assertIn(configurator.EMPTY_VALUE, cell)
        self.assertNotIn('data-staffing-sla=', cell)
        self.assertNotIn(configurator.SEARCHING_LABEL, cell)

    def test_each_empty_slot_gets_its_own_sla_entry(self):
        """Несколько слотов одной функции — по записи и таймеру на каждый."""
        self.assertEqual(len(self.slots), 2)
        cell = self.staffing_cell(self.overview_html(self.director))
        self.assertEqual(cell.count('data-staffing-sla='), 2)

    def test_mixed_slots_show_person_without_timer_and_empty_with_timer(self):
        self.assign(self.slots[0])
        cell = self.staffing_cell(self.overview_html(self.director))
        self.assertIn(self.freelancer.full_name, cell)
        self.assertEqual(cell.count('data-staffing-sla='), 1)

    def test_overdue_slot_is_marked_by_the_server_in_markup(self):
        RoomFunctionSlot.objects.filter(room=self.room).update(
            created_at=timezone.now() - timedelta(hours=3)
        )
        cell = self.staffing_cell(self.overview_html(self.director))
        self.assertIn('staffing-sla-overdue', cell)
        self.assertIn(SEARCH_SLA_OVERDUE_LABEL, cell)

    def test_project_sla_banner_attributes_are_not_reused(self):
        """Два SLA не путаются: у таймера подбора собственные атрибуты."""
        html = self.overview_html(self.director)
        self.assertIn('data-staffing-sla=', html)
        self.assertNotIn('data-sla-deadline=', html)

    def test_get_does_not_create_or_change_anything(self):
        """Дедлайн нигде не сохраняется, слот не меняется, слоты не создаются.

        «Команду» открывает тимлид (200); директор уходит на обзор (302);
        фрилансер получает 403. Отказные пути тоже обязаны быть безобидными.
        """
        before = (self.slot.is_active, self.slot.updated_at, self.slot.created_at)
        slots_before = RoomFunctionSlot.objects.count()

        for user in (self.director, self.teamlead):
            self.assertEqual(self.get(self.overview_url, user).status_code, 200)
        self.assertEqual(self.get(self.team_url, self.teamlead).status_code, 200)
        self.assertEqual(self.get(self.team_url, self.director).status_code, 302)

        # «Обзор» фрилансеру по-прежнему доступен, «Команда» — нет.
        self.assertEqual(self.get(self.overview_url, self.freelancer).status_code, 200)
        self.assertEqual(self.get(self.team_url, self.freelancer).status_code, 403)

        self.slot.refresh_from_db()
        self.assertEqual(
            (self.slot.is_active, self.slot.updated_at, self.slot.created_at), before
        )
        self.assertEqual(RoomFunctionSlot.objects.count(), slots_before)

    def test_slot_cards_still_take_one_query(self):
        """SLA считается в памяти: числа запросов он не увеличивает."""
        with self.assertNumQueries(1):
            cards = selectors.slot_cards(self.room)
            [(card.is_searching, card.search_sla_display) for card in cards]


# ---------------------------------------------------------------------------
# 3. UX удаления функции с назначенным исполнителем
# ---------------------------------------------------------------------------


class RemoveWithAssignedMemberUxTests(RoomCompletionTestCase):
    def setUp(self):
        super().setUp()
        self.save_composition(teamlead=1, seller_middle=1)
        self.slot = RoomFunctionSlot.objects.get(
            room=self.room, role_key='seller_middle', slot_index=1
        )

    def actions_cell(self, html, role_key='seller_middle'):
        row = self.fr_row(html, role_key)
        start = row.index('class="fr-cell-actions"')
        return row[start:row.index('</td>', start)]

    def test_free_role_keeps_a_working_remove_button(self):
        cell = self.actions_cell(self.overview_html(self.director))
        self.assertIn('<form', cell)
        self.assertNotIn('disabled', cell)

    def test_assigned_role_gets_a_disabled_button_with_the_reason(self):
        self.assign(self.slot)
        cell = self.actions_cell(self.overview_html(self.director))
        self.assertIn('disabled', cell)
        self.assertIn(configurator.REMOVE_BLOCKED_HINT, cell)
        self.assertNotIn('<form', cell)

    def test_no_misleading_confirm_is_offered(self):
        """Браузерный confirm, после которого backend всё равно отказывал, убран."""
        self.assign(self.slot)
        self.assertNotIn('hx-confirm', self.overview_html(self.director))

    def test_backend_still_refuses_a_direct_post(self):
        """UI не обходит и не ослабляет проекцию: прямой POST по-прежнему падает."""
        self.assign(self.slot)
        self.client.force_login(self.director)
        response = self.client.post(
            reverse('rooms:room_functional_roles_update', args=[self.project.id]),
            {'role_key': 'seller_middle', 'action': 'set', 'count': '0'},
            headers={'HX-Request': 'true'},
        )
        self.assertContains(response, 'Сначала снимите исполнителя')
        self.slot.refresh_from_db()
        self.assertTrue(self.slot.is_active)
        self.assertIn(
            'seller_middle',
            [entry['role_key'] for entry in get_project_composition(self.project)],
        )

    def test_member_and_slot_survive_the_blocked_ui(self):
        """UI ничего не удаляет и не отвязывает сам."""
        member = self.assign(self.slot)
        self.overview_html(self.director)
        member.refresh_from_db()
        self.slot.refresh_from_db()
        self.assertEqual(member.function_slot_id, self.slot.id)
        self.assertTrue(self.slot.is_active)

    def test_decrement_button_stays_available(self):
        """«−» остаётся server-driven: его этот этап не блокирует."""
        self.assign(self.slot)
        row = self.fr_row(self.overview_html(self.director), 'seller_middle')
        self.assertIn('value="dec"', row)


# ---------------------------------------------------------------------------
# 4. Лента событий: последние 10
# ---------------------------------------------------------------------------


class ActivityFeedLimitTests(RoomCompletionTestCase):
    def make_activities(self, count):
        for index in range(count):
            log_room_activity(
                self.room,
                f'Событие номер {index}',
                RoomActivity.EventType.OTHER,
                actor=self.director,
            )

    def test_overview_shows_exactly_ten_activities(self):
        self.make_activities(15)
        response = self.get(self.overview_url, self.director)
        self.assertEqual(len(response.context['activities']), 10)

    def test_only_the_latest_events_are_shown(self):
        self.make_activities(15)
        response = self.get(self.overview_url, self.director)
        messages = [item.message for item in response.context['activities']]
        self.assertIn('Событие номер 14', messages)
        self.assertNotIn('Событие номер 0', messages)

    def test_shorter_feed_is_not_padded(self):
        self.make_activities(3)
        response = self.get(self.overview_url, self.director)
        # В комнате уже есть события от её создания, но их точно меньше десяти.
        self.assertLessEqual(len(response.context['activities']), 10)
        self.assertIn(
            'Событие номер 2',
            [item.message for item in response.context['activities']],
        )

    def test_logging_itself_is_unchanged(self):
        """Ограничен только вывод: в БД лента остаётся полной."""
        self.make_activities(15)
        self.get(self.overview_url, self.director)
        self.assertGreaterEqual(
            RoomActivity.objects.filter(room=self.room).count(), 15
        )


# ---------------------------------------------------------------------------
# 5. «Требуется по плану проекта» на вкладке «Команда»
# ---------------------------------------------------------------------------


class PlannedTeamBlockTests(RoomCompletionTestCase):
    def setUp(self):
        super().setUp()
        self.save_composition(
            teamlead=1, seller_middle=2, database_assistant=1,
        )

    def planned_rows(self, user=None):
        response = self.get(self.team_url, user or self.teamlead)
        return {row.role_key: row for row in response.context['planned_roles']}

    def planned_row_html(self, html, role_key):
        start = html.index(f'id="planned-role-{role_key}"')
        return html[start:html.index('</tr>', start)]

    def test_block_is_rendered_on_the_team_tab(self):
        html = self.get(self.team_url, self.teamlead).content.decode()
        self.assertIn(configurator.PLANNED_TEAM_TITLE, html)

    def test_every_ordered_function_is_listed_with_count_and_grade(self):
        rows = self.planned_rows()
        self.assertEqual(rows['seller_middle'].count, 2)
        self.assertEqual(rows['seller_middle'].grade_display, 'Middle')
        self.assertEqual(rows['teamlead'].count, 1)
        self.assertEqual(rows['teamlead'].grade_display, 'N/A')

    def test_database_assistant_is_visible_without_execution_slots(self):
        """Функция без слотов не исчезает: подбор по ней просто не создан."""
        row = self.planned_rows()['database_assistant']
        self.assertFalse(row.has_slots)
        self.assertEqual(row.execution_note, configurator.EXECUTION_PENDING_LABEL)
        self.assertEqual(row.grade_display, 'Junior')

    def test_teamlead_is_visible_as_a_fixed_ordered_function(self):
        row = self.planned_rows()['teamlead']
        self.assertTrue(row.is_fixed)
        self.assertFalse(row.has_slots)
        self.assertEqual(row.execution_note, configurator.EXECUTION_PENDING_LABEL)

    def test_pending_note_is_rendered_for_roles_without_slots(self):
        html = self.get(self.team_url, self.teamlead).content.decode()
        for role_key in ('teamlead', 'database_assistant'):
            with self.subTest(role_key=role_key):
                self.assertIn(
                    configurator.EXECUTION_PENDING_LABEL,
                    self.planned_row_html(html, role_key),
                )

    def test_role_with_slots_shows_execution_status_instead_of_the_note(self):
        row = self.planned_rows()['seller_middle']
        self.assertTrue(row.has_slots)
        self.assertEqual(row.execution_note, '')
        self.assertEqual(row.staffing.slots_total, 2)

    def test_assigned_member_is_shown_in_the_execution_column(self):
        slot = RoomFunctionSlot.objects.get(
            room=self.room, role_key='seller_middle', slot_index=1
        )
        self.assign(slot)
        html = self.get(self.team_url, self.teamlead).content.decode()
        self.assertIn(
            self.freelancer.full_name, self.planned_row_html(html, 'seller_middle')
        )

    def test_block_is_display_only_and_creates_nothing(self):
        """Блок «Требуется по плану» только показывает — ни слотов, ни участников.

        Смысл теста прежний: открытие вкладки ничего не создаёт. Блок смотрит
        тимлид; директору вкладка недоступна (редирект на обзор), фрилансеру —
        403. Отказные пути тоже обязаны быть без побочных эффектов.
        """
        slots_before = set(RoomFunctionSlot.objects.values_list('id', flat=True))
        members_before = set(RoomMember.objects.values_list('id', flat=True))

        self.assertEqual(self.get(self.team_url, self.teamlead).status_code, 200)
        self.assertEqual(self.get(self.team_url, self.director).status_code, 302)
        self.assertEqual(self.get(self.team_url, self.freelancer).status_code, 403)

        self.assertEqual(
            set(RoomFunctionSlot.objects.values_list('id', flat=True)), slots_before
        )
        self.assertEqual(
            set(RoomMember.objects.values_list('id', flat=True)), members_before
        )

    def test_empty_composition_shows_an_honest_empty_state(self):
        empty = Project.objects.create(
            owner=self.director, name='Без состава', status=Project.Status.STAFFING,
        )
        ensure_room_for_project(empty)
        empty.teamlead = self.teamlead
        empty.save(update_fields=['teamlead'])
        response = self.get(
            reverse('rooms:room_team', args=[empty.id]), self.teamlead
        )
        self.assertEqual(response.context['planned_roles'], [])
        self.assertContains(response, 'Состав команды ещё не сохранён')


# ---------------------------------------------------------------------------
# 6. Публичный фасад confirm_freelancer_readiness
# ---------------------------------------------------------------------------


class ReadinessFacadeTests(RoomCompletionTestCase):
    def test_facade_is_exposed_by_rooms_services(self):
        from apps.rooms import services

        self.assertTrue(callable(services.confirm_freelancer_readiness))

    def test_facade_is_a_wrapper_with_a_function_level_import(self):
        """Модульного реэкспорта нет — иначе граф импортов замкнулся бы."""
        import inspect

        from apps.rooms import services
        from apps.rooms.staffing import services as staffing_services

        self.assertIsNot(
            services.confirm_freelancer_readiness,
            staffing_services.confirm_freelancer_readiness,
        )
        source = inspect.getsource(services.confirm_freelancer_readiness)
        self.assertIn('from .staffing.services import', source)

    def test_module_source_has_no_module_level_staffing_import(self):
        import inspect

        from apps.rooms import services

        module_source = inspect.getsource(services)
        for line in module_source.splitlines():
            if line.startswith('from .staffing') or line.startswith('import .staffing'):
                self.fail(f'Модульный импорт staffing в services.py: {line!r}')

    def test_signature_keeps_actor_required(self):
        import inspect

        from apps.rooms import services

        parameters = inspect.signature(
            services.confirm_freelancer_readiness
        ).parameters
        self.assertEqual(list(parameters), ['member', 'actor'])
        self.assertIs(parameters['actor'].default, inspect.Parameter.empty)

    def test_facade_confirms_readiness_of_the_member(self):
        from apps.rooms import services

        slot = self.make_slot()
        member = self.assign(slot)
        self.assertTrue(services.confirm_freelancer_readiness(member, self.freelancer))
        member.refresh_from_db()
        self.assertEqual(member.ready_status, RoomMember.ReadyStatus.READY)

    def test_facade_keeps_the_permission_check(self):
        """Права не ослаблены: чужой пользователь готовность не подтверждает."""
        from apps.rooms import services

        member = self.assign(self.make_slot())
        with self.assertRaises(PermissionDenied):
            services.confirm_freelancer_readiness(member, self.director)
        member.refresh_from_db()
        self.assertEqual(member.ready_status, RoomMember.ReadyStatus.PENDING)

    def test_both_import_orders_work_in_a_fresh_interpreter(self):
        """Границу проверяем на чистом процессе, а не на уже прогретом.

        Порядок импорта не должен решать, поднимется ли приложение, поэтому
        оба направления проверяются отдельными процессами: внутри одного
        теста модули уже импортированы и цикл бы не проявился.
        """
        orders = (
            ('apps.rooms.services', 'apps.rooms.staffing.services'),
            ('apps.rooms.staffing.services', 'apps.rooms.services'),
        )
        for first, second in orders:
            with self.subTest(order=(first, second)):
                code = (
                    'import os, django;'
                    "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wowlance.settings');"
                    'django.setup();'
                    f'import {first};'
                    f'import {second};'
                    f'assert {first}, {second}'
                )
                result = subprocess.run(
                    [sys.executable, '-c', code],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


# ---------------------------------------------------------------------------
# 7. Человеческие названия функций вместо role_key
# ---------------------------------------------------------------------------


class SlotRoleLabelTests(RoomCompletionTestCase):
    def test_catalog_maps_known_keys_to_public_titles(self):
        self.assertEqual(catalog.role_label('seller_middle'), 'Сейлер Middle')
        self.assertEqual(catalog.role_label('linkedin_leadgen'), 'Лидген LinkedIn')

    def test_unknown_historical_key_falls_back_to_itself(self):
        """Слот старой комнаты не должен ронять страницу."""
        self.assertEqual(catalog.role_label('seller'), 'seller')

    def test_slot_card_exposes_the_public_label(self):
        card = selectors.slot_card_for(self.make_slot())
        self.assertEqual(card.role_label, 'Сейлер Middle')

    def test_team_page_shows_the_label_and_not_the_raw_key(self):
        self.make_slot()
        html = self.get(self.team_url, self.teamlead).content.decode()
        start = html.index('class="slot-cards"')
        block = html[start:]
        self.assertIn('Сейлер Middle', block)
        self.assertNotIn('>seller_middle ', block)

    def test_candidate_pool_page_shows_the_label(self):
        slot = self.make_slot()
        response = self.get(
            reverse(
                'rooms:room_slot_candidates',
                kwargs={'project_id': self.project.id, 'slot_id': slot.id},
            ),
            self.teamlead,
        )
        self.assertContains(response, 'Сейлер Middle')

    def test_page_with_unknown_role_key_still_renders(self):
        self.make_slot(role_key='seller', slot_index=1)
        response = self.get(self.team_url, self.teamlead)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'seller')


# ---------------------------------------------------------------------------
# 8. Канбан задач: четыре колонки на «Обзоре»
# ---------------------------------------------------------------------------


class OverviewKanbanPreviewTests(RoomCompletionTestCase):
    """Полная раскладка — в `apps.pipeline.tests_kanban`; здесь только Обзор."""

    def test_overview_preview_uses_the_same_four_columns(self):
        titles = [
            col['title']
            for col in self.get(self.overview_url, self.director)
            .context['kanban_preview']
        ]
        self.assertEqual(titles, ['К работе', 'В работе', 'На проверке', 'Готово'])

    def test_in_progress_task_is_visible_in_its_own_column(self):
        Task.objects.create(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.director,
            title='Обзвонить первый сегмент',
            status=Task.Status.IN_PROGRESS,
        )
        columns = {
            col['key']: col['tasks']
            for col in self.get(self.overview_url, self.director)
            .context['kanban_preview']
        }
        self.assertEqual(
            [task.title for task in columns['in_progress']],
            ['Обзвонить первый сегмент'],
        )
        self.assertEqual(columns['todo'], [])

    # --- превью по роли ---------------------------------------------------

    #: Подписи четырёх колонок общей доски. Фрилансер не должен видеть ни одной.
    KANBAN_COLUMN_TITLES = ('К работе', 'В работе', 'На проверке', 'Готово')

    def make_task(self, title, assignee, created_at=None, **fields):
        """Задача проекта с управляемым `created_at`.

        `created_at` — `auto_now_add`, поэтому в тесте он проставляется
        отдельным `update`. Иначе шесть задач, созданных в одну миллисекунду,
        дали бы неопределённый порядок, и проверка «показаны пять последних»
        зависела бы от удачи.
        """
        task = Task.objects.create(
            project=self.project,
            assignee=assignee,
            created_by=self.teamlead,
            title=title,
            **fields,
        )
        if created_at is not None:
            Task.objects.filter(pk=task.pk).update(created_at=created_at)
            task.refresh_from_db()
        return task

    def seed_mixed_tasks(self):
        """Шесть задач фрилансера и две чужие, с явным порядком по времени.

        Возвращает список своих задач от самой новой к самой старой — тот же
        порядок, в котором их отдаёт `Task.Meta.ordering = ['-created_at']`.
        """
        base = timezone.now()
        mine = [
            self.make_task(
                f'Моя задача {index}',
                self.freelancer,
                created_at=base - timedelta(hours=index),
            )
            for index in range(6)
        ]
        self.foreign = [
            self.make_task(
                'Чужая задача тимлида',
                self.teamlead,
                created_at=base - timedelta(minutes=30),
            ),
            self.make_task(
                'Чужая задача менеджера',
                self.manager,
                created_at=base - timedelta(minutes=45),
            ),
        ]
        return mine

    def test_freelancer_sees_only_his_own_five_latest_tasks(self):
        """«Мои задачи»: свои, не больше пяти, без чужой доски."""
        mine = self.seed_mixed_tasks()
        response = self.get(self.overview_url, self.freelancer)
        self.assertEqual(response.status_code, 200)

        self.assertTrue(response.context['is_freelancer_task_preview'])
        preview = response.context['my_tasks_preview']
        self.assertEqual(len(preview), 5)
        # Именно пять последних по общему порядку задач, а не случайные пять.
        self.assertEqual(
            [task.id for task in preview], [task.id for task in mine[:5]]
        )
        self.assertEqual({task.assignee_id for task in preview}, {self.freelancer.id})

    def test_freelancer_preview_markup_replaces_the_shared_board(self):
        self.seed_mixed_tasks()
        response = self.get(self.overview_url, self.freelancer)

        self.assertContains(response, 'Мои задачи')
        self.assertNotContains(response, 'Задачи (канбан)')
        for title in self.KANBAN_COLUMN_TITLES:
            with self.subTest(column=title):
                self.assertNotContains(response, title)
        self.assertEqual(response.context['kanban_preview'], [])

    def test_freelancer_does_not_see_foreign_tasks(self):
        mine = self.seed_mixed_tasks()
        response = self.get(self.overview_url, self.freelancer)

        for task in self.foreign:
            with self.subTest(task=task.title):
                self.assertNotContains(response, task.title)
        # Шестая своя задача не пропала из проекта — она просто не влезла
        # в превью; полный список остаётся на вкладке «Задачи».
        self.assertContains(response, mine[0].title)
        self.assertNotContains(response, mine[5].title)
        self.assertTrue(Task.objects.filter(pk=mine[5].pk).exists())

    def test_freelancer_without_tasks_gets_an_honest_empty_state(self):
        response = self.get(self.overview_url, self.freelancer)
        self.assertContains(response, 'Мои задачи')
        self.assertEqual(response.context['my_tasks_preview'], [])
        self.assertContains(response, 'Задач пока нет')

    def test_teamlead_and_director_keep_the_full_board(self):
        """Урезан только фрилансер: у тимлида и владельца доска прежняя."""
        self.seed_mixed_tasks()
        for user in (self.teamlead, self.director):
            with self.subTest(role=user.role):
                response = self.get(self.overview_url, user)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context['is_freelancer_task_preview'])
                self.assertContains(response, 'Задачи (канбан)')
                self.assertNotContains(response, 'Мои задачи')
                titles = [
                    col['title'] for col in response.context['kanban_preview']
                ]
                self.assertEqual(titles, list(self.KANBAN_COLUMN_TITLES))

    def test_teamlead_and_director_see_tasks_of_different_assignees(self):
        mine = self.seed_mixed_tasks()
        for user in (self.teamlead, self.director):
            with self.subTest(role=user.role):
                response = self.get(self.overview_url, user)
                shown = {
                    task.id
                    for col in response.context['kanban_preview']
                    for task in col['tasks']
                }
                self.assertLessEqual(
                    {task.id for task in mine} | {task.id for task in self.foreign},
                    shown,
                )
                assignees = {
                    task.assignee_id
                    for col in response.context['kanban_preview']
                    for task in col['tasks']
                }
                self.assertEqual(
                    assignees,
                    {self.freelancer.id, self.teamlead.id, self.manager.id},
                )


# ---------------------------------------------------------------------------
# 10. Правка вводных проекта директором
# ---------------------------------------------------------------------------


class ProjectVisionEditTests(RoomCompletionTestCase):
    #: Полный валидный POST правки вводных.
    PAYLOAD = {
        'offer': 'Новый оффер',
        'utp': 'Новое УТП',
        'audience': 'Новая ЦА',
        'hot_criteria': 'Новые критерии',
    }

    def setUp(self):
        super().setUp()
        self.save_composition(teamlead=1, seller_middle=2)

    def post_vision(self, user, data=None, project=None):
        self.client.force_login(user)
        url = (
            self.vision_url
            if project is None
            else reverse('rooms:room_vision_update', args=[project.id])
        )
        return self.client.post(url, data if data is not None else self.PAYLOAD)

    def input_data(self):
        self.project.refresh_from_db()
        return self.project.input_data

    # --- ключи ----------------------------------------------------------

    def test_service_uses_exactly_the_keys_the_overview_reads(self):
        """Новых ключей не изобретаем: это те же `input_data`, что и на «Обзоре»."""
        self.assertEqual(
            set(VISION_INPUT_KEYS), {'offer', 'utp', 'audience', 'hot_criteria'}
        )
        self.project.input_data = dict(
            self.project.input_data,
            offer='Проверочный оффер',
            utp='Проверочное УТП',
            audience='Проверочная ЦА',
            hot_criteria='Проверочные критерии',
        )
        self.project.save(update_fields=['input_data'])
        self.assertEqual(self.project.offer, 'Проверочный оффер')
        self.assertEqual(self.project.utp, 'Проверочное УТП')
        self.assertEqual(self.project.audience, 'Проверочная ЦА')
        self.assertEqual(self.project.hot_criteria, 'Проверочные критерии')

    # --- видимость кнопки ------------------------------------------------

    def test_owner_director_sees_the_edit_control(self):
        response = self.get(self.overview_url, self.director)
        self.assertTrue(response.context['can_edit_vision'])
        self.assertContains(response, 'Редактировать')
        self.assertContains(response, 'class="vision-form"')

    def test_other_roles_do_not_see_the_edit_control(self):
        for user in (self.teamlead, self.freelancer, self.manager):
            with self.subTest(role=user.role):
                response = self.get(self.overview_url, user)
                self.assertFalse(response.context['can_edit_vision'])
                self.assertIsNone(response.context['vision_form'])
                self.assertNotContains(response, 'class="vision-form"')

    def test_permission_helper_rejects_a_foreign_director(self):
        self.assertFalse(
            user_can_edit_project_vision(self.other_director, self.project)
        )
        self.assertTrue(user_can_edit_project_vision(self.director, self.project))

    # --- сохранение -------------------------------------------------------

    def test_owner_director_can_save(self):
        response = self.post_vision(self.director)
        self.assertRedirects(response, self.overview_url)
        data = self.input_data()
        self.assertEqual(data['offer'], 'Новый оффер')
        self.assertEqual(data['utp'], 'Новое УТП')
        self.assertEqual(data['audience'], 'Новая ЦА')
        self.assertEqual(data['hot_criteria'], 'Новые критерии')

    def test_foreign_director_gets_403(self):
        self.assertEqual(self.post_vision(self.other_director).status_code, 403)
        self.assertEqual(self.input_data()['offer'], 'Исходный оффер')

    def test_teamlead_freelancer_and_manager_get_403(self):
        for user in (self.teamlead, self.freelancer, self.manager):
            with self.subTest(role=user.role):
                self.assertEqual(self.post_vision(user).status_code, 403)
        self.assertEqual(self.input_data()['offer'], 'Исходный оффер')

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.post(self.vision_url, self.PAYLOAD)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_get_is_not_allowed_and_mutates_nothing(self):
        before = dict(self.input_data())
        self.client.force_login(self.director)
        self.assertEqual(self.client.get(self.vision_url).status_code, 405)
        self.assertEqual(self.input_data(), before)

    def test_opening_the_overview_mutates_nothing(self):
        before = dict(self.input_data())
        budget_before = self.project.budget
        self.get(self.overview_url, self.director)
        self.project.refresh_from_db()
        self.assertEqual(self.project.input_data, before)
        self.assertEqual(self.project.budget, budget_before)

    # --- merge-safety ------------------------------------------------------

    def test_functional_roles_survive_the_edit(self):
        before = get_project_composition(self.project)
        self.assertTrue(before)
        self.post_vision(self.director)
        self.assertEqual(get_project_composition(self.project), before)

    def test_other_input_data_keys_survive_the_edit(self):
        self.post_vision(self.director)
        self.assertEqual(self.input_data()['architecture'], 'cold_calling')

    def test_budget_is_not_touched(self):
        self.project.refresh_from_db()
        budget_before = self.project.budget
        self.post_vision(self.director)
        self.project.refresh_from_db()
        self.assertEqual(self.project.budget, budget_before)

    def test_extra_posted_keys_are_ignored(self):
        """Весь `input_data` из браузера не принимается."""
        self.post_vision(
            self.director,
            dict(self.PAYLOAD, architecture='hacked', functional_roles='[]'),
        )
        data = self.input_data()
        self.assertEqual(data['architecture'], 'cold_calling')
        self.assertTrue(get_project_composition(self.project))

    def test_invalid_form_saves_nothing(self):
        response = self.post_vision(self.director, dict(self.PAYLOAD, offer=''))
        self.assertRedirects(response, self.overview_url)
        self.assertEqual(self.input_data()['offer'], 'Исходный оффер')

    def test_service_refuses_a_user_without_rights(self):
        with self.assertRaises(PermissionDenied):
            update_project_vision(self.project, self.PAYLOAD, self.teamlead)
        self.assertEqual(self.input_data()['offer'], 'Исходный оффер')


# ---------------------------------------------------------------------------
# 11. Кнопка видеокомнаты Jitsi
# ---------------------------------------------------------------------------


class JitsiVideoRoomTests(RoomCompletionTestCase):
    def test_url_is_built_from_the_room_id(self):
        self.assertEqual(
            room_video_call_url(self.room),
            f'https://meet.jit.si/wowlance-room-{self.room.id}',
        )

    def test_url_is_https_on_meet_jit_si(self):
        self.assertTrue(room_video_call_url(self.room).startswith('https://'))
        self.assertEqual(JITSI_BASE_URL, 'https://meet.jit.si')

    def test_comms_page_renders_the_exact_room_id(self):
        response = self.get(self.comms_url, self.director)
        self.assertContains(response, f'wowlance-room-{self.room.id}')

    def test_link_opens_safely_in_a_new_tab(self):
        html = self.get(self.comms_url, self.director).content.decode()
        start = html.index('id="comms-video-link"')
        block = html[html.rindex('<a', 0, start):html.index('</a>', start)]
        self.assertIn('target="_blank"', block)
        self.assertIn('rel="noopener noreferrer"', block)

    def test_button_has_an_understandable_label(self):
        self.assertContains(
            self.get(self.comms_url, self.director), 'Открыть видеокомнату'
        )

    def test_old_placeholder_is_gone(self):
        html = self.get(self.comms_url, self.director).content.decode()
        self.assertNotIn('Следующий этап', html)

    def test_chat_section_is_untouched(self):
        response = self.get(self.comms_url, self.director)
        self.assertContains(response, 'Чат комнаты')
        self.assertContains(response, 'id="chat-messages-team"')
        self.assertContains(response, 'id="comms-team-chat"')

    def test_url_does_not_depend_on_request_data(self):
        """Адрес встречи не приходит из браузера: query-параметры игнорируются."""
        self.client.force_login(self.director)
        response = self.client.get(self.comms_url, {'video_call_url': 'http://evil'})
        self.assertContains(response, f'wowlance-room-{self.room.id}')
        self.assertNotContains(response, 'evil')

    def test_two_rooms_get_two_different_links(self):
        other = Project.objects.create(
            owner=self.director, name='Вторая комната', status=Project.Status.STAFFING,
        )
        other_room = ensure_room_for_project(other)
        self.assertNotEqual(
            room_video_call_url(self.room), room_video_call_url(other_room)
        )


# ---------------------------------------------------------------------------
# Регрессии границ: этот этап ничего не расширил
# ---------------------------------------------------------------------------


class BoundaryRegressionTests(RoomCompletionTestCase):
    def test_projection_still_ignores_teamlead_and_database_assistant(self):
        from apps.rooms.staffing.projection import PROJECTED_ROLE_KEYS

        self.assertNotIn('teamlead', PROJECTED_ROLE_KEYS)
        self.assertNotIn('database_assistant', PROJECTED_ROLE_KEYS)
        self.assertEqual(
            PROJECTED_ROLE_KEYS,
            frozenset({'seller_middle', 'seller_senior', 'linkedin_leadgen'}),
        )

    def test_planned_block_creates_no_slots_for_them(self):
        self.save_composition(teamlead=1, database_assistant=2)
        self.get(self.team_url, self.teamlead)
        self.assertFalse(
            RoomFunctionSlot.objects.filter(
                room=self.room, role_key__in=('teamlead', 'database_assistant')
            ).exists()
        )

    def test_start_calls_sla_is_still_twenty_four_hours(self):
        from apps.pipeline.services import START_CALLS_SLA

        self.assertEqual(START_CALLS_SLA, timedelta(hours=24))
        self.assertNotEqual(START_CALLS_SLA, SEARCH_SLA)

    def test_no_new_room_or_slot_appears_from_reading_pages(self):
        rooms_before = Room.objects.count()
        slots_before = RoomFunctionSlot.objects.count()
        self.get(self.overview_url, self.director)
        self.get(self.team_url, self.teamlead)
        self.get(self.comms_url, self.director)
        self.assertEqual(Room.objects.count(), rooms_before)
        self.assertEqual(RoomFunctionSlot.objects.count(), slots_before)
