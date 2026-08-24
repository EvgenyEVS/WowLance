"""Проекция состава функциональных ролей в слоты комнаты (Issue #11, этап
`functional_roles → RoomFunctionSlot`).

Покрывается только контур проекции:

* какие функции превращаются в слоты, а какие сознательно нет;
* идемпотентность синхронизации;
* увеличение состава: возврат закрытых слотов раньше создания новых;
* уменьшение состава: закрываются только пустые слоты и никогда не удаляются;
* занятый слот останавливает операцию целиком — вместе с составом и бюджетом;
* пакет и ручное изменение проходят одним и тем же путём;
* границы этапа: подбор не запускается, `matching` не трогается,
  новых миграций не появляется.

Правила подбора (`matching`), назначения (`staffing.services`) и экономика
состава (`unit_economics`) живут в своих тестах и здесь не переписываются.
"""

import inspect
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from apps.rooms import presets
from apps.rooms.configurator import EMPTY_VALUE, SEARCHING_LABEL
from apps.rooms.functional_roles import FUNCTIONAL_ROLES
from apps.rooms.models import (
    Project,
    RoomFunctionSlot,
    RoomMember,
    RoomSlotCandidate,
)
from apps.rooms.services import (
    apply_package_and_sync_slots,
    ensure_room_for_project,
    save_functional_roles_and_sync_slots,
)
from apps.rooms.staffing import projection
from apps.rooms.staffing.projection import (
    PROJECTED_ROLE_KEYS,
    SlotProjectionError,
    sync_functional_roles_to_slots,
)
from apps.rooms.unit_economics import FunctionalRolesError, get_project_composition
from apps.test_helpers import make_director, make_freelancer, make_teamlead


class ProjectionTestCase(TestCase):
    """Проект директора в статусе STAFFING с открытой комнатой."""

    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir@projection.test')
        self.teamlead = make_teamlead(email='tl@projection.test')
        self.freelancer = make_freelancer(email='fr1@projection.test')
        self.other_freelancer = make_freelancer(email='fr2@projection.test')

        self.project = Project.objects.create(
            owner=self.director,
            name='Проекция состава',
            status=Project.Status.STAFFING,
            input_data={'offer': 'Оффер', 'hot_criteria': 'Критерии'},
        )
        self.room = ensure_room_for_project(self.project)

    # --- хелперы --------------------------------------------------------

    def save(self, **counts):
        """Сохраняет состав продуктовой оркестрацией (снапшот + слоты)."""
        counts.setdefault('teamlead', 1)
        return save_functional_roles_and_sync_slots(
            self.project,
            [{'role_key': key, 'count': value} for key, value in counts.items()],
            self.director,
        )

    def slots(self, role_key=None, *, is_active=None):
        queryset = RoomFunctionSlot.objects.filter(room=self.room)
        if role_key is not None:
            queryset = queryset.filter(role_key=role_key)
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)
        return list(queryset.order_by('role_key', 'slot_index'))

    def indices(self, role_key, *, is_active=None):
        return [slot.slot_index for slot in self.slots(role_key, is_active=is_active)]

    def occupy(self, slot, user=None):
        """Сажает участника на слот напрямую: правила подбора здесь не тема."""
        return RoomMember.objects.create(
            room=self.room,
            user=user or self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=slot,
        )

    def composition(self):
        self.project.refresh_from_db()
        return {
            entry['role_key']: entry['count']
            for entry in get_project_composition(self.project)
        }

    def slot_state(self):
        """Снимок слотов для сравнения «до / после» неудачной операции."""
        return sorted(
            RoomFunctionSlot.objects.values_list(
                'id', 'role_key', 'slot_index', 'is_active',
                'required_level', 'required_channel',
            )
        )


# ---------------------------------------------------------------------------
# Создание слотов
# ---------------------------------------------------------------------------


class ProjectionCreateTests(ProjectionTestCase):
    def test_seller_middle_one_creates_single_active_slot(self):
        """1. seller_middle count=1 → один активный слот."""
        self.save(seller_middle=1)
        slots = self.slots('seller_middle')
        self.assertEqual(len(slots), 1)
        self.assertTrue(slots[0].is_active)
        self.assertEqual(slots[0].slot_index, 1)
        self.assertEqual(slots[0].room_id, self.room.id)

    def test_seller_middle_two_creates_indices_one_and_two(self):
        """2. count=2 → два слота с индексами 1 и 2."""
        self.save(seller_middle=2)
        self.assertEqual(self.indices('seller_middle'), [1, 2])

    def test_seller_senior_slot_uses_catalog_grade_and_channel(self):
        """3. Senior-сейлер: грейд и канал берутся из структурного каталога."""
        self.save(seller_senior=1)
        slot = self.slots('seller_senior')[0]
        role = FUNCTIONAL_ROLES['seller_senior']
        self.assertEqual(slot.required_level, RoomFunctionSlot.Grade.SENIOR)
        self.assertEqual(slot.required_channel, RoomFunctionSlot.Channel.COLD_CALLING)
        self.assertEqual((slot.required_level, slot.required_channel),
                         (role.grade, role.channel))

    def test_linkedin_leadgen_slot_uses_catalog_grade_and_channel(self):
        """4. LinkedIn-лидген: middle + канал linkedin из каталога."""
        self.save(linkedin_leadgen=1)
        slot = self.slots('linkedin_leadgen')[0]
        role = FUNCTIONAL_ROLES['linkedin_leadgen']
        self.assertEqual(slot.required_level, RoomFunctionSlot.Grade.MIDDLE)
        self.assertEqual(slot.required_channel, RoomFunctionSlot.Channel.LINKEDIN)
        self.assertEqual((slot.required_level, slot.required_channel),
                         (role.grade, role.channel))

    def test_teamlead_never_gets_a_slot(self):
        """5. Тимлид приходит ручным инвайтом — слота ему проекция не создаёт."""
        self.save(teamlead=1, seller_middle=1)
        self.assertEqual(self.slots('teamlead'), [])
        self.assertNotIn('teamlead', PROJECTED_ROLE_KEYS)

    def test_database_assistant_never_gets_a_slot(self):
        """6. Канал `base` не поддержан контрактом слота — слота нет."""
        self.save(database_assistant=2)
        self.assertEqual(self.slots('database_assistant'), [])
        self.assertNotIn('database_assistant', PROJECTED_ROLE_KEYS)

    def test_only_three_roles_are_projected(self):
        """Набор проецируемых функций выводится из каталога и enum модели."""
        self.assertEqual(
            sorted(PROJECTED_ROLE_KEYS),
            ['linkedin_leadgen', 'seller_middle', 'seller_senior'],
        )

    def test_database_assistant_channel_stays_out_of_slot_enum(self):
        """`base` не должен просочиться в enum слота ради проекции."""
        self.assertNotIn('base', RoomFunctionSlot.Channel.values)

    def test_slots_are_created_only_for_the_project_room(self):
        other = Project.objects.create(
            owner=self.director,
            name='Соседний проект',
            status=Project.Status.STAFFING,
        )
        other_room = ensure_room_for_project(other)
        self.save(seller_middle=2)
        self.assertEqual(
            RoomFunctionSlot.objects.filter(room=other_room).count(), 0
        )


# ---------------------------------------------------------------------------
# Идемпотентность
# ---------------------------------------------------------------------------


class ProjectionIdempotencyTests(ProjectionTestCase):
    def test_second_sync_creates_nothing(self):
        """7. Повторная синхронизация того же состава ничего не создаёт."""
        self.save(seller_middle=2, linkedin_leadgen=1)
        before = self.slot_state()

        result = sync_functional_roles_to_slots(self.project)

        self.assertFalse(result.changed)
        self.assertEqual(result.created_count, 0)
        self.assertEqual(self.slot_state(), before)

    def test_repeated_saves_never_duplicate_slot_indices(self):
        """8. Индексы слотов внутри функции остаются уникальными."""
        for _ in range(3):
            self.save(seller_middle=2, seller_senior=1)
        self.save(seller_middle=1, seller_senior=1)
        self.save(seller_middle=3, seller_senior=1)

        indices = self.indices('seller_middle')
        self.assertEqual(len(indices), len(set(indices)))
        self.assertEqual(self.indices('seller_middle', is_active=True), [1, 2, 3])

    def test_matching_state_is_not_rewritten(self):
        """9. Совпадающее состояние не переписывается: `updated_at` не меняется."""
        self.save(seller_middle=1)
        slot = self.slots('seller_middle')[0]
        updated_before = slot.updated_at

        result = sync_functional_roles_to_slots(self.project)
        slot.refresh_from_db()

        self.assertFalse(result.changed)
        self.assertEqual(slot.updated_at, updated_before)

    def test_saving_the_same_composition_again_is_a_noop_for_slots(self):
        self.save(seller_middle=2)
        before = self.slot_state()
        outcome = self.save(seller_middle=2)
        self.assertFalse(outcome.projection.changed)
        self.assertEqual(self.slot_state(), before)


# ---------------------------------------------------------------------------
# Увеличение состава и возврат закрытых слотов
# ---------------------------------------------------------------------------


class ProjectionIncreaseTests(ProjectionTestCase):
    def test_inactive_empty_slot_is_reactivated_before_creating_a_new_one(self):
        """10. Закрытый пустой слот возвращается раньше, чем создаётся новый."""
        self.save(seller_middle=2)
        self.save(seller_middle=1)
        self.assertEqual(self.indices('seller_middle', is_active=False), [2])

        outcome = self.save(seller_middle=2)

        self.assertEqual(outcome.projection.reactivated_count, 1)
        self.assertEqual(outcome.projection.created_count, 0)
        self.assertEqual(self.indices('seller_middle', is_active=True), [1, 2])
        self.assertEqual(RoomFunctionSlot.objects.filter(room=self.room).count(), 2)

    def test_candidate_history_survives_reactivation(self):
        """11. История кандидатов закрытого слота не теряется при возврате."""
        self.save(seller_middle=2)
        slot = self.slots('seller_middle')[1]
        candidate = RoomSlotCandidate.objects.create(
            slot=slot,
            candidate=self.freelancer,
            outcome=RoomSlotCandidate.Outcome.SKIPPED,
        )

        self.save(seller_middle=1)
        self.save(seller_middle=2)

        candidate.refresh_from_db()
        slot.refresh_from_db()
        self.assertTrue(slot.is_active)
        self.assertEqual(candidate.slot_id, slot.id)
        self.assertEqual(candidate.outcome, RoomSlotCandidate.Outcome.SKIPPED)
        self.assertEqual(RoomSlotCandidate.objects.filter(slot=slot).count(), 1)

    def test_reactivated_slot_matches_structural_catalog(self):
        """Вернувшийся слот приводится к актуальным требованиям каталога."""
        self.save(seller_middle=2)
        self.save(seller_middle=1)
        RoomFunctionSlot.objects.filter(
            room=self.room, role_key='seller_middle', slot_index=2
        ).update(
            required_level=RoomFunctionSlot.Grade.JUNIOR,
            required_channel=RoomFunctionSlot.Channel.ANY,
        )

        self.save(seller_middle=2)

        slot = RoomFunctionSlot.objects.get(
            room=self.room, role_key='seller_middle', slot_index=2
        )
        role = FUNCTIONAL_ROLES['seller_middle']
        self.assertEqual(slot.required_level, role.grade)
        self.assertEqual(slot.required_channel, role.channel)

    def test_new_slot_index_is_max_of_active_and_inactive_plus_one(self):
        """12. Новый индекс считается по всем слотам функции, включая закрытые."""
        self.save(seller_middle=1)
        RoomFunctionSlot.objects.create(
            room=self.room,
            role_key='seller_middle',
            slot_index=7,
            is_active=False,
            required_level=RoomFunctionSlot.Grade.MIDDLE,
            required_channel=RoomFunctionSlot.Channel.COLD_CALLING,
        )

        outcome = self.save(seller_middle=3)

        # Сначала вернулся закрытый №7, затем создан следующий за максимумом.
        self.assertEqual(outcome.projection.reactivated_count, 1)
        self.assertEqual(outcome.projection.created_count, 1)
        self.assertEqual(self.indices('seller_middle', is_active=True), [1, 7, 8])

    def test_occupied_inactive_slot_is_not_silently_reactivated(self):
        """13. Закрытый слот с исполнителем не возвращается молча."""
        self.save(seller_middle=2)
        occupied = self.slots('seller_middle')[1]
        self.occupy(occupied)
        RoomFunctionSlot.objects.filter(pk=occupied.pk).update(is_active=False)
        self.save(seller_middle=1)
        slots_before = self.slot_state()
        members_before = list(RoomMember.objects.values_list('id', 'function_slot_id'))

        with self.assertRaises(SlotProjectionError) as ctx:
            self.save(seller_middle=2)

        self.assertIn('занят исполнителем', str(ctx.exception))
        self.assertEqual(self.slot_state(), slots_before)
        self.assertEqual(
            list(RoomMember.objects.values_list('id', 'function_slot_id')),
            members_before,
        )
        self.assertEqual(self.composition()['seller_middle'], 1)

    def test_removed_role_slots_come_back_on_the_next_purchase(self):
        """Функцию можно убрать и вернуть — слот тот же самый."""
        self.save(seller_middle=1)
        slot_id = self.slots('seller_middle')[0].id
        self.save(seller_middle=0)
        self.assertEqual(self.indices('seller_middle', is_active=True), [])

        self.save(seller_middle=1)

        slots = self.slots('seller_middle', is_active=True)
        self.assertEqual([slot.id for slot in slots], [slot_id])


# ---------------------------------------------------------------------------
# Уменьшение состава
# ---------------------------------------------------------------------------


class ProjectionDecreaseTests(ProjectionTestCase):
    def test_highest_index_empty_slot_is_deactivated_first(self):
        """14. Закрывается пустой слот с наибольшим индексом."""
        self.save(seller_middle=3)

        outcome = self.save(seller_middle=2)

        self.assertEqual(outcome.projection.deactivated_count, 1)
        self.assertEqual(self.indices('seller_middle', is_active=True), [1, 2])
        self.assertEqual(self.indices('seller_middle', is_active=False), [3])

    def test_decrease_never_deletes_a_slot(self):
        """15. Слоты не удаляются: строка остаётся в БД закрытой."""
        self.save(seller_middle=2)
        slot_ids = {slot.id for slot in self.slots('seller_middle')}

        self.save(seller_middle=0)

        self.assertEqual(
            {slot.id for slot in self.slots('seller_middle')}, slot_ids
        )
        self.assertEqual(self.indices('seller_middle', is_active=True), [])

    def test_candidate_history_is_preserved_on_deactivation(self):
        """16. История кандидатов закрытого слота сохраняется."""
        self.save(seller_middle=1)
        slot = self.slots('seller_middle')[0]
        RoomSlotCandidate.objects.create(
            slot=slot,
            candidate=self.freelancer,
            outcome=RoomSlotCandidate.Outcome.DECLINED,
        )

        self.save(seller_middle=0)

        self.assertEqual(RoomSlotCandidate.objects.filter(slot=slot).count(), 1)
        self.assertEqual(
            RoomSlotCandidate.objects.get(slot=slot).outcome,
            RoomSlotCandidate.Outcome.DECLINED,
        )

    def test_assigned_slot_is_never_silently_deactivated(self):
        """17. При выборе между занятым и пустым закрывается пустой."""
        self.save(seller_middle=2)
        assigned, empty = self.slots('seller_middle')
        self.occupy(assigned)

        self.save(seller_middle=1)

        assigned.refresh_from_db()
        empty.refresh_from_db()
        self.assertTrue(assigned.is_active)
        self.assertFalse(empty.is_active)
        self.assertEqual(
            RoomMember.objects.get(function_slot=assigned).user_id,
            self.freelancer.id,
        )


class ProjectionAssignedDecreaseRollbackTests(ProjectionTestCase):
    """18–22. Уменьшение, требующее занятого слота, откатывает всю операцию."""

    def setUp(self):
        super().setUp()
        self.save(seller_middle=2)
        self.first, self.second = self.slots('seller_middle')
        self.occupy(self.first, self.freelancer)
        self.occupy(self.second, self.other_freelancer)

        self.composition_before = self.composition()
        self.project.refresh_from_db()
        self.budget_before = self.project.budget
        self.slots_before = self.slot_state()
        self.members_before = sorted(
            RoomMember.objects.values_list(
                'id', 'user_id', 'function_slot_id', 'role_in_room', 'ready_status'
            )
        )

    def decrease(self):
        return self.save(seller_middle=1)

    def test_decrease_requiring_an_assigned_slot_raises_domain_error(self):
        """18. Понятная доменная ошибка вместо тихого снятия человека."""
        with self.assertRaises(SlotProjectionError) as ctx:
            self.decrease()
        self.assertEqual(
            str(ctx.exception),
            'Сначала снимите исполнителя с функции «Сейлер Middle».',
        )

    def test_projection_error_is_a_functional_roles_error(self):
        """UI уже умеет показывать `FunctionalRolesError` — тип совместим."""
        self.assertTrue(issubclass(SlotProjectionError, FunctionalRolesError))
        with self.assertRaises(FunctionalRolesError):
            self.decrease()

    def test_error_rolls_back_project_input_data(self):
        """19. Состав в `input_data` остаётся прежним."""
        with self.assertRaises(SlotProjectionError):
            self.decrease()
        self.assertEqual(self.composition(), self.composition_before)
        self.assertEqual(self.composition()['seller_middle'], 2)

    def test_error_rolls_back_project_budget(self):
        """20. Бюджет проекта не меняется."""
        with self.assertRaises(SlotProjectionError):
            self.decrease()
        self.project.refresh_from_db()
        self.assertEqual(self.project.budget, self.budget_before)
        self.assertGreater(self.project.budget, Decimal('0'))

    def test_error_keeps_slots_unchanged(self):
        """21. Слоты остаются активными и в том же составе."""
        with self.assertRaises(SlotProjectionError):
            self.decrease()
        self.assertEqual(self.slot_state(), self.slots_before)
        self.assertEqual(self.indices('seller_middle', is_active=True), [1, 2])

    def test_error_keeps_room_members_unchanged(self):
        """22. Участники и их привязка к слотам не трогаются."""
        with self.assertRaises(SlotProjectionError):
            self.decrease()
        self.assertEqual(
            sorted(
                RoomMember.objects.values_list(
                    'id', 'user_id', 'function_slot_id', 'role_in_room', 'ready_status'
                )
            ),
            self.members_before,
        )

    def test_configurator_partial_shows_saved_state_after_error(self):
        """HTMX-ответ после ошибки показывает фактически сохранённый состав."""
        self.client.force_login(self.director)
        response = self.client.post(
            reverse('rooms:room_functional_roles_update', args=[self.project.id]),
            {'role_key': 'seller_middle', 'action': 'dec'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Сначала снимите исполнителя')
        rows = {row.role_key: row for row in response.context['fr_rows']}
        self.assertEqual(rows['seller_middle'].count, 2)
        self.assertEqual(self.composition()['seller_middle'], 2)
        self.assertEqual(self.slot_state(), self.slots_before)


# ---------------------------------------------------------------------------
# Пакеты
# ---------------------------------------------------------------------------


class ProjectionPackageTests(ProjectionTestCase):
    def apply(self, package_key):
        return apply_package_and_sync_slots(self.project, package_key, self.director)

    def active_map(self):
        result = {}
        for slot in self.slots(is_active=True):
            result[slot.role_key] = result.get(slot.role_key, 0) + 1
        return result

    def test_quick_start_creates_one_seller_middle_slot(self):
        """23. «Быстрый старт» → один слот seller_middle."""
        self.apply('quick_start')
        self.assertEqual(self.active_map(), {'seller_middle': 1})

    def test_scaling_creates_two_sellers_and_one_linkedin(self):
        """24. «Масштабирование» → 2 seller_middle + 1 linkedin_leadgen."""
        self.apply('scaling')
        self.assertEqual(
            self.active_map(), {'seller_middle': 2, 'linkedin_leadgen': 1}
        )
        self.assertEqual(self.indices('seller_middle', is_active=True), [1, 2])

    def test_enterprise_creates_two_seniors_and_one_linkedin(self):
        """25. «Enterprise аутрич» → 2 seller_senior + 1 linkedin_leadgen."""
        self.apply('enterprise')
        self.assertEqual(
            self.active_map(), {'seller_senior': 2, 'linkedin_leadgen': 1}
        )

    def test_packages_never_create_teamlead_slots(self):
        """У всех пакетов есть тимлид, но слота у него не появляется."""
        for package_key in presets.FUNCTIONAL_ROLE_PACKAGES:
            with self.subTest(package=package_key):
                self.apply(package_key)
                self.assertEqual(self.slots('teamlead'), [])

    def test_package_and_manual_paths_produce_the_same_slots(self):
        """26. Пакет и ручное сохранение проходят одной проекцией."""
        manual_project = Project.objects.create(
            owner=self.director,
            name='Ручной состав',
            status=Project.Status.STAFFING,
        )
        manual_room = ensure_room_for_project(manual_project)
        save_functional_roles_and_sync_slots(
            manual_project,
            presets.functional_role_package_composition('scaling'),
            self.director,
        )
        self.apply('scaling')

        def state(room):
            return sorted(
                RoomFunctionSlot.objects.filter(room=room).values_list(
                    'role_key', 'slot_index', 'is_active',
                    'required_level', 'required_channel',
                )
            )

        self.assertEqual(state(self.room), state(manual_room))

    def test_package_endpoint_creates_slots(self):
        """Продуктовый путь пакета (HTTP) тоже проецируется в слоты."""
        self.client.force_login(self.director)
        response = self.client.post(
            reverse(
                'rooms:room_functional_roles_apply_package', args=[self.project.id]
            ),
            {'package': 'scaling'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.active_map(), {'seller_middle': 2, 'linkedin_leadgen': 1}
        )

    def test_update_endpoint_creates_slots(self):
        """Числовой ввод / «+» / «−» ведут в ту же оркестрацию."""
        self.client.force_login(self.director)
        url = reverse('rooms:room_functional_roles_update', args=[self.project.id])
        self.client.post(
            url,
            {'role_key': 'seller_middle', 'action': 'set', 'count': '2'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(self.indices('seller_middle', is_active=True), [1, 2])

        self.client.post(
            url,
            {'role_key': 'seller_middle', 'action': 'inc'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(self.indices('seller_middle', is_active=True), [1, 2, 3])

        self.client.post(
            url,
            {'role_key': 'seller_middle', 'action': 'dec'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(self.indices('seller_middle', is_active=True), [1, 2])
        self.assertEqual(self.indices('seller_middle', is_active=False), [3])

    def test_remove_role_endpoint_closes_its_slots(self):
        """Удаление функции из состава закрывает её слоты, но не удаляет их."""
        self.client.force_login(self.director)
        url = reverse('rooms:room_functional_roles_update', args=[self.project.id])
        self.client.post(
            url,
            {'role_key': 'linkedin_leadgen', 'action': 'set', 'count': '1'},
            headers={'HX-Request': 'true'},
        )
        self.client.post(
            url,
            {'role_key': 'linkedin_leadgen', 'action': 'set', 'count': '0'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(self.indices('linkedin_leadgen', is_active=True), [])
        self.assertEqual(self.indices('linkedin_leadgen', is_active=False), [1])


# ---------------------------------------------------------------------------
# UI-регрессия конфигуратора
# ---------------------------------------------------------------------------


class ProjectionConfiguratorUiTests(ProjectionTestCase):
    def staffing_cell(self, response, role_key):
        html = response.content.decode()
        start = html.index(f'id="fr-row-{role_key}"')
        row = html[start:html.index('</tr>', start)]
        cell_start = row.index('class="fr-staffing ')
        return row[cell_start:row.index('</td>', cell_start)]

    def test_partial_after_save_shows_searching_for_the_new_slot(self):
        """27. Ответ на POST уже знает о только что созданном слоте."""
        self.client.force_login(self.director)
        response = self.client.post(
            reverse('rooms:room_functional_roles_update', args=[self.project.id]),
            {'role_key': 'seller_middle', 'action': 'set', 'count': '1'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(response.status_code, 200)
        cell = self.staffing_cell(response, 'seller_middle')
        self.assertIn(SEARCHING_LABEL, cell)
        rows = {row.role_key: row for row in response.context['fr_rows']}
        self.assertEqual(rows['seller_middle'].staffing.slots_total, 1)

    def test_overview_get_still_creates_no_slots(self):
        """28. GET «Обзора» остаётся read-only."""
        self.save(seller_middle=1)
        before = self.slot_state()
        members_before = RoomMember.objects.count()

        self.client.force_login(self.director)
        response = self.client.get(
            reverse('rooms:room_overview', args=[self.project.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.slot_state(), before)
        self.assertEqual(RoomMember.objects.count(), members_before)

    def test_overview_get_creates_no_slots_for_empty_composition(self):
        """GET проекта без состава тоже ничего не проецирует."""
        self.client.force_login(self.director)
        self.client.get(reverse('rooms:room_overview', args=[self.project.id]))
        self.assertEqual(RoomFunctionSlot.objects.count(), 0)

    def test_teamlead_and_database_assistant_stay_neutral(self):
        """29. У непроецируемых функций в колонке «Подбор» остаётся прочерк."""
        self.save(seller_middle=1, database_assistant=1)
        self.client.force_login(self.director)
        response = self.client.get(
            reverse('rooms:room_overview', args=[self.project.id])
        )

        rows = {row.role_key: row for row in response.context['fr_rows']}
        for role_key in ('teamlead', 'database_assistant'):
            with self.subTest(role_key=role_key):
                self.assertEqual(rows[role_key].staffing.slots_total, 0)
                self.assertEqual(rows[role_key].staffing.status, 'none')
                cell = self.staffing_cell(response, role_key)
                self.assertIn(EMPTY_VALUE, cell)
                self.assertNotIn(SEARCHING_LABEL, cell)


# ---------------------------------------------------------------------------
# Границы этапа
# ---------------------------------------------------------------------------


class ProjectionBoundaryTests(ProjectionTestCase):
    def test_projection_does_not_touch_matching(self):
        """30. Проекция не знает о движке подбора: правил она не дублирует.

        Проверяется код модуля, а не его docstring: границы модулей в
        документации упоминать нужно, а импортировать `matching` и повторять
        его фильтры — нет.
        """
        code = inspect.getsource(projection).replace(projection.__doc__, '')
        self.assertNotIn('matching', code)
        self.assertFalse(hasattr(projection, 'matching'))
        self.assertNotIn(
            'apps.rooms.staffing.matching',
            {module for module in dir(projection)},
        )

    def test_projection_never_assigns_anyone(self):
        """31. Ни назначений, ни истории кандидатов проекция не создаёт."""
        members_before = set(RoomMember.objects.values_list('id', flat=True))

        self.save(seller_middle=2, seller_senior=1, linkedin_leadgen=1)

        self.assertEqual(
            set(RoomMember.objects.values_list('id', flat=True)), members_before
        )
        self.assertEqual(RoomSlotCandidate.objects.count(), 0)
        self.assertFalse(
            RoomMember.objects.filter(function_slot__isnull=False).exists()
        )

    def test_no_new_migrations_are_required(self):
        """32. Проекция обходится существующей схемой БД."""
        out = StringIO()
        try:
            call_command(
                'makemigrations', 'rooms', '--check', '--dry-run', stdout=out
            )
        except SystemExit as exc:  # pragma: no cover - падает только при регрессии
            self.fail(f'Появились несозданные миграции rooms:\n{out.getvalue()}\n{exc}')

    def test_sync_creates_the_room_when_the_write_path_has_none(self):
        """Write-path без комнаты пользуется штатным `ensure_room_for_project`."""
        draft = Project.objects.create(
            owner=self.director,
            name='Черновик без комнаты',
            status=Project.Status.DRAFT,
        )
        self.assertIsNone(getattr(draft, 'room', None))

        save_functional_roles_and_sync_slots(
            draft,
            [{'role_key': 'teamlead', 'count': 1},
             {'role_key': 'seller_middle', 'count': 1}],
            self.director,
        )

        room = draft.room
        self.assertEqual(
            RoomFunctionSlot.objects.filter(room=room).count(), 1
        )
        self.assertTrue(
            RoomMember.objects.filter(room=room, user=self.director).exists()
        )
