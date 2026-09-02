"""Состав функциональных ролей проекта: покупка функций, экономика, слоты.

Один файл на весь composition-контур:

* RBAC и статусы — состав меняет только владелец-директор в DRAFT/STAFFING;
* снапшот в `input_data`, бюджет и KPI как производные состава;
* проекция состава в `RoomFunctionSlot`: +1, −1, идемпотентность, откат;
* авто-подбор на только что открытый слот.

КРИТИЧНО про актора. Здесь всюду **владелец-директор**: право даёт
`user_can_edit_functional_roles` (владелец + роль DIRECTOR). Это отдельный
путь от ручного подбора, где актор — тимлид проекта
(`user_can_manage_team`); ручные операции над слотами живут в
`tests_staffing` и здесь не проверяются.

Правила подбора (hard filters, ranking) — в `tests_staffing_matching`,
переход проекта в ACTIVE — в `tests_automation_sla`.
"""

from decimal import Decimal

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from apps.rooms import presets
from apps.rooms.models import Project, RoomFunctionSlot, RoomMember
from apps.rooms.services import (
    apply_package_and_sync_slots,
    get_unit_economics_summary,
    save_functional_roles_and_sync_slots,
    update_project_functional_roles,
)
from apps.rooms.staffing.projection import (
    SlotProjectionError,
    sync_functional_roles_to_slots,
)
from apps.rooms.unit_economics import (
    FUNCTIONAL_ROLES_KEY,
    FunctionalRolesError,
    get_project_composition,
    user_can_edit_functional_roles,
)
from apps.test_helpers import (
    make_director,
    make_freelancer,
    make_staffed_project,
    make_user,
)
from apps.users.models import User


class CompositionTestCase(TestCase):
    """Проект директора в подборе, комната открыта, слотов ещё нет.

    `slots=0` принципиально: создание слотов — предмет тестов этого файла,
    фикстура не имеет права подготовить их заранее.
    """

    CANDIDATES = 0

    def setUp(self):
        fixture = make_staffed_project(slots=0, candidates=self.CANDIDATES)
        self.project = fixture.project
        self.room = fixture.room
        self.director = fixture.director
        self.teamlead = fixture.teamlead
        self.candidates = fixture.candidates

    # --- продуктовое действие ---------------------------------------------

    def save(self, **counts):
        """Сохранение состава продуктовой оркестрацией: снапшот + слоты."""
        counts.setdefault('teamlead', 1)
        return save_functional_roles_and_sync_slots(
            self.project,
            [{'role_key': key, 'count': value} for key, value in counts.items()],
            self.director,
        )

    # --- состояние ---------------------------------------------------------

    def slots(self, role_key=None, *, is_active=None):
        queryset = RoomFunctionSlot.objects.filter(room=self.room)
        if role_key is not None:
            queryset = queryset.filter(role_key=role_key)
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)
        return list(queryset.order_by('role_key', 'slot_index'))

    def indices(self, role_key, *, is_active=None):
        return [slot.slot_index for slot in self.slots(role_key, is_active=is_active)]

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

    def occupy(self, slot, user):
        """Сажает участника на слот напрямую: правила подбора здесь не тема."""
        return RoomMember.objects.create(
            room=self.room,
            user=user,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=slot,
        )


# ---------------------------------------------------------------------------
# 1-2. Кто и когда меняет состав
# ---------------------------------------------------------------------------


class CompositionRbacTests(CompositionTestCase):
    def quick_start(self):
        return [
            {'role_key': 'teamlead', 'count': 1},
            {'role_key': 'seller_middle', 'count': 1},
        ]

    def test_only_owner_director_can_save_the_composition(self):
        summary = update_project_functional_roles(
            self.project, self.quick_start(), self.director
        )
        self.assertEqual(
            [row['role_key'] for row in summary.composition],
            ['teamlead', 'seller_middle'],
        )
        self.assertTrue(user_can_edit_functional_roles(self.director, self.project))

        denied = {
            'anonymous': AnonymousUser(),
            'platform_admin': make_user(
                email='admin-comp@example.com', role=User.Roles.ADMIN
            ),
            'project_teamlead': self.teamlead,
            'other_director': make_director(email='dir2-comp@example.com'),
            'manager': make_user(
                email='mgr-comp@example.com', role=User.Roles.MANAGER
            ),
            'freelancer': make_freelancer(email='fl-comp@example.com'),
        }
        for label, user in denied.items():
            with self.subTest(actor=label):
                self.assertFalse(
                    user_can_edit_functional_roles(user, self.project)
                )
                with self.assertRaises(PermissionDenied):
                    update_project_functional_roles(
                        self.project, self.quick_start(), user
                    )

    def test_composition_is_editable_in_draft_and_staffing_but_not_in_active(self):
        for status in (Project.Status.DRAFT, Project.Status.STAFFING):
            with self.subTest(status=status, expected='editable'):
                self.project.status = status
                self.project.save(update_fields=['status'])
                update_project_functional_roles(
                    self.project, self.quick_start(), self.director
                )
                self.project.refresh_from_db()
                self.assertEqual(self.project.budget, Decimal('97000.00'))

        self.project.status = Project.Status.ACTIVE
        self.project.save(update_fields=['status'])
        with self.assertRaises(FunctionalRolesError):
            update_project_functional_roles(
                self.project, self.quick_start(), self.director
            )


# ---------------------------------------------------------------------------
# 3-4, 14-17. Снапшот, бюджет, KPI и защита серверных значений
# ---------------------------------------------------------------------------


class CompositionStorageTests(CompositionTestCase):
    def quick_start(self):
        return [
            {'role_key': 'teamlead', 'count': 1},
            {'role_key': 'seller_middle', 'count': 1},
        ]

    def test_saving_composition_keeps_offer_and_other_input_data_keys(self):
        self.project.input_data = {
            'offer': 'Оффер',
            'utp': 'УТП',
            'audience': 'Аудитория',
            'hot_criteria': 'Критерии',
            'architecture': 'cold_calling',
            'notes': 'важная заметка',
        }
        self.project.save(update_fields=['input_data'])

        update_project_functional_roles(
            self.project, self.quick_start(), self.director
        )

        self.project.refresh_from_db()
        self.assertEqual(self.project.input_data['offer'], 'Оффер')
        self.assertEqual(self.project.input_data['utp'], 'УТП')
        self.assertEqual(self.project.input_data['audience'], 'Аудитория')
        self.assertEqual(self.project.input_data['hot_criteria'], 'Критерии')
        self.assertEqual(self.project.input_data['architecture'], 'cold_calling')
        self.assertEqual(self.project.input_data['notes'], 'важная заметка')
        self.assertIn(FUNCTIONAL_ROLES_KEY, self.project.input_data)

    def test_budget_is_the_sum_of_the_composition(self):
        update_project_functional_roles(
            self.project,
            [
                {'role_key': 'teamlead', 'count': 1},
                {'role_key': 'seller_senior', 'count': 2},
                {'role_key': 'linkedin_leadgen', 'count': 1},
            ],
            self.director,
        )

        self.project.refresh_from_db()
        summary = get_unit_economics_summary(self.project)
        self.assertEqual(self.project.budget, Decimal('253000.00'))
        self.assertEqual(self.project.budget, summary.total_budget)

    def test_client_cannot_override_business_values(self):
        update_project_functional_roles(
            self.project,
            [
                {'role_key': 'teamlead', 'count': 1},
                {
                    'role_key': 'seller_middle',
                    'count': 1,
                    'monthly_cost': '1.00',
                    'monthly_hours': 1,
                    'hot_leads_per_month': 9999,
                    'productivity_text': 'взломано',
                },
            ],
            self.director,
        )

        self.project.refresh_from_db()
        seller = self.project.input_data[FUNCTIONAL_ROLES_KEY][1]
        self.assertEqual(seller['cost_per_unit'], '62000.00')
        self.assertEqual(seller['hours_per_unit'], 160)
        self.assertEqual(seller['kpi_leads_per_unit'], 10)
        self.assertEqual(self.project.budget, Decimal('97000.00'))

    def test_missing_teamlead_is_rejected(self):
        with self.assertRaises(FunctionalRolesError):
            update_project_functional_roles(
                self.project,
                [{'role_key': 'seller_middle', 'count': 1}],
                self.director,
            )

    def test_negative_count_is_rejected(self):
        with self.assertRaises(FunctionalRolesError):
            update_project_functional_roles(
                self.project,
                [
                    {'role_key': 'teamlead', 'count': 1},
                    {'role_key': 'seller_middle', 'count': -1},
                ],
                self.director,
            )

    def test_manual_kpi_target_is_overridden_by_the_composition(self):
        self.project.kpi_target = Decimal('999')
        self.project.save(update_fields=['kpi_target'])

        apply_package_and_sync_slots(self.project, 'linkedin', self.director)

        self.project.refresh_from_db()
        self.assertEqual(self.project.kpi_target, Decimal('8'))


# ---------------------------------------------------------------------------
# 5-10, 18. Проекция состава в слоты комнаты
# ---------------------------------------------------------------------------


class SlotProjectionTests(CompositionTestCase):
    def test_teamlead_and_database_assistant_get_no_slots(self):
        self.save(teamlead=1, database_assistant=2, seller_middle=1)

        self.assertEqual(self.slots('teamlead'), [])
        self.assertEqual(self.slots('database_assistant'), [])
        self.assertEqual(self.indices('seller_middle', is_active=True), [1])

    def test_increment_creates_a_new_active_slot(self):
        self.save(seller_middle=1)
        self.assertEqual(self.indices('seller_middle', is_active=True), [1])

        outcome = self.save(seller_middle=2)

        self.assertEqual(outcome.projection.created_count, 1)
        self.assertEqual(self.indices('seller_middle', is_active=True), [1, 2])
        self.assertTrue(all(slot.is_active for slot in self.slots('seller_middle')))

    def test_decrement_deactivates_the_empty_slot_with_the_highest_index(self):
        self.save(seller_middle=3)

        outcome = self.save(seller_middle=2)

        self.assertEqual(outcome.projection.deactivated_count, 1)
        self.assertEqual(self.indices('seller_middle', is_active=True), [1, 2])
        self.assertEqual(self.indices('seller_middle', is_active=False), [3])

    def test_decrement_of_an_occupied_slot_raises_and_rolls_back_everything(self):
        self.save(seller_middle=2)
        first, second = self.slots('seller_middle')
        self.occupy(first, make_freelancer(email='occupant1@composition.test'))
        self.occupy(second, make_freelancer(email='occupant2@composition.test'))

        composition_before = self.composition()
        self.project.refresh_from_db()
        budget_before = self.project.budget
        slots_before = self.slot_state()
        members_before = sorted(
            RoomMember.objects.values_list('id', 'user_id', 'function_slot_id')
        )

        with self.assertRaises(SlotProjectionError):
            self.save(seller_middle=1)

        # Доменная ошибка совместима с тем, что уже умеет показывать UI.
        self.assertTrue(issubclass(SlotProjectionError, FunctionalRolesError))
        self.assertEqual(self.composition(), composition_before)
        self.assertEqual(self.composition()['seller_middle'], 2)
        self.project.refresh_from_db()
        self.assertEqual(self.project.budget, budget_before)
        self.assertGreater(self.project.budget, Decimal('0'))
        self.assertEqual(self.slot_state(), slots_before)
        self.assertEqual(self.indices('seller_middle', is_active=True), [1, 2])
        self.assertEqual(
            sorted(RoomMember.objects.values_list('id', 'user_id', 'function_slot_id')),
            members_before,
        )

    def test_package_and_manual_paths_produce_the_same_slots(self):
        manual = make_staffed_project(slots=0, prefix='manual-')
        save_functional_roles_and_sync_slots(
            manual.project,
            presets.functional_role_package_composition('scaling'),
            manual.director,
        )

        apply_package_and_sync_slots(self.project, 'scaling', self.director)

        def state(room):
            return sorted(
                RoomFunctionSlot.objects.filter(room=room).values_list(
                    'role_key', 'slot_index', 'is_active',
                    'required_level', 'required_channel',
                )
            )

        self.assertEqual(state(self.room), state(manual.room))
        self.assertNotEqual(state(self.room), [])

    def test_overview_get_creates_no_slots(self):
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

    def test_second_sync_of_the_same_composition_creates_nothing(self):
        self.save(seller_middle=2, linkedin_leadgen=1)
        before = self.slot_state()

        result = sync_functional_roles_to_slots(self.project)

        self.assertFalse(result.changed)
        self.assertEqual(result.created_count, 0)
        self.assertEqual(self.slot_state(), before)


# ---------------------------------------------------------------------------
# 12. Пустой пул кандидатов
# ---------------------------------------------------------------------------


class EmptyPoolAutofillTests(CompositionTestCase):
    """Кандидатов в базе нет вовсе — покупка функции обязана это пережить."""

    def test_empty_pool_leaves_slot_empty_but_saves_the_composition(self):
        outcome = self.save(seller_middle=1)

        slot = self.slots('seller_middle')[0]
        self.assertTrue(slot.is_active)
        self.assertEqual(outcome.projection.created_count, 1)
        self.assertFalse(
            RoomMember.objects.filter(function_slot__isnull=False).exists()
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.budget, outcome.summary.total_budget)
        self.assertGreater(self.project.budget, Decimal('0'))


# ---------------------------------------------------------------------------
# 11, 13. Авто-подбор на только что открытый слот
# ---------------------------------------------------------------------------


class CompositionAutofillTests(CompositionTestCase):
    """Пул есть: покупка функции сама сажает лучшего кандидата.

    Актор — владелец-директор: это `for_composition_autofill`-путь, а не
    кнопки «Команды».
    """

    CANDIDATES = 3

    def test_new_slot_gets_the_top_ranked_candidate(self):
        self.save(seller_middle=1)

        slot = self.slots('seller_middle')[0]
        member = RoomMember.objects.filter(function_slot=slot).first()
        self.assertIsNotNone(member)
        self.assertEqual(member.user_id, self.candidates[0].id)
        self.assertEqual(member.room_id, self.room.id)
        self.assertEqual(member.ready_status, RoomMember.ReadyStatus.PENDING)

    def test_teamlead_is_never_auto_assigned(self):
        self.save(teamlead=1)

        self.assertEqual(self.slots('teamlead'), [])
        self.assertFalse(
            RoomMember.objects.filter(function_slot__isnull=False).exists()
        )
        self.assertFalse(
            RoomMember.objects.filter(
                room=self.room, user__in=self.candidates
            ).exists()
        )
