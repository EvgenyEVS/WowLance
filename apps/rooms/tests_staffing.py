"""Подбор в комнату: данные слота, назначение, auto top-1, замена, готовность.

Один файл на весь staffing-контур:

* инварианты схемы — уникальность слота, связь участник↔слот, история кандидата;
* `apps.rooms.staffing.services` — назначение, авто-подбор, «Другой сейлер»;
* переход STAFFING → ACTIVE по готовности всей команды.

Правила акторов (см. `_guard_staffing` в `apps.rooms.staffing.services`):

* ручной подбор, замена, пул — **тимлид** проекта (`user_can_manage_team`);
* авто-подбор при покупке состава — **владелец-директор**
  (`user_can_edit_functional_roles`), это отдельный путь и живёт в
  `tests_composition`.

Сами правила подбора (hard filters и ranking) покрыты в
`tests_staffing_matching` и здесь не дублируются.
"""

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from apps.rooms.models import (
    Project,
    RoomActivity,
    RoomFunctionSlot,
    RoomMember,
    RoomSlotCandidate,
)
from apps.rooms.services import add_freelancer_to_room, user_can_access_project
from apps.rooms.staffing.services import (
    StaffingError,
    assign_candidate_to_slot,
    auto_assign_best_candidate,
    confirm_freelancer_readiness,
    is_functional_team_ready,
    replace_slot_member,
)
from apps.test_helpers import make_staffed_project


class StaffingTestCase(TestCase):
    """Комната в подборе: два одинаковых слота и пул из пяти кандидатов."""

    def setUp(self):
        fixture = make_staffed_project(slots=2, candidates=5)
        self.project = fixture.project
        self.room = fixture.room
        self.director = fixture.director
        self.teamlead = fixture.teamlead
        self.slot, self.other_slot = fixture.slots
        self.candidates = fixture.candidates

    def assign_url(self, candidate, slot=None):
        return reverse(
            'rooms:room_slot_assign_candidate',
            kwargs={
                'project_id': self.project.id,
                'slot_id': (slot or self.slot).id,
                'candidate_id': candidate.id,
            },
        )


# ---------------------------------------------------------------------------
# 1-3. Инварианты схемы: слот, участник↔слот, история кандидата
# ---------------------------------------------------------------------------


class SlotDataInvariantsTests(StaffingTestCase):
    def test_slot_is_unique_per_room_role_key_and_index(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoomFunctionSlot.objects.create(
                    room=self.room,
                    role_key=self.slot.role_key,
                    slot_index=self.slot.slot_index,
                )

        # Уникальность именно тройная: другой ключ функции с тем же номером — ок.
        other_role = RoomFunctionSlot.objects.create(
            room=self.room,
            role_key='researcher',
            slot_index=self.slot.slot_index,
        )
        self.assertNotEqual(other_role.id, self.slot.id)

    def test_slot_cannot_be_taken_by_two_members(self):
        RoomMember.objects.create(
            room=self.room,
            user=self.candidates[0],
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=self.slot,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoomMember.objects.create(
                    room=self.room,
                    user=self.candidates[1],
                    role_in_room=RoomMember.RoleInRoom.FREELANCER,
                    function_slot=self.slot,
                )

    def test_candidate_is_unique_per_slot(self):
        RoomSlotCandidate.objects.create(
            slot=self.slot,
            candidate=self.candidates[0],
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoomSlotCandidate.objects.create(
                    slot=self.slot,
                    candidate=self.candidates[0],
                )

        # Тот же человек на другом слоте — не дубль: история ведётся по слоту.
        RoomSlotCandidate.objects.create(
            slot=self.other_slot,
            candidate=self.candidates[0],
        )
        self.assertEqual(self.candidates[0].slot_candidacies.count(), 2)


# ---------------------------------------------------------------------------
# 4-9. Авто-подбор, замена, независимость слотов
# ---------------------------------------------------------------------------


class AutoAssignAndReplaceTests(StaffingTestCase):
    def test_auto_assign_takes_the_top_ranked_candidate_on_a_new_slot(self):
        outcome = auto_assign_best_candidate(self.slot, self.teamlead)

        self.assertEqual(outcome.code, 'assigned')
        self.assertEqual(outcome.member.user_id, self.candidates[0].id)
        self.assertEqual(outcome.member.function_slot_id, self.slot.id)

    def test_replace_gives_the_next_candidate_and_never_returns_the_first(self):
        assign_candidate_to_slot(self.slot, self.candidates[0], self.teamlead)
        # Готовность снятого кандидата не должна утечь новому исполнителю.
        RoomMember.objects.filter(user=self.candidates[0]).update(
            ready_status=RoomMember.ReadyStatus.READY,
        )

        outcome = replace_slot_member(self.slot, self.teamlead)

        self.assertEqual(outcome.code, 'replaced')
        self.assertEqual(outcome.member.user_id, self.candidates[1].id)
        self.assertEqual(outcome.member.ready_status, RoomMember.ReadyStatus.PENDING)

        skipped = RoomSlotCandidate.objects.get(
            slot=self.slot, candidate=self.candidates[0]
        )
        self.assertEqual(skipped.outcome, RoomSlotCandidate.Outcome.SKIPPED)
        self.assertFalse(
            RoomMember.objects.filter(room=self.room, user=self.candidates[0]).exists()
        )
        self.assertFalse(user_can_access_project(self.candidates[0], self.project))

        # Снятый №1 не возвращается и следующей заменой.
        second = replace_slot_member(self.slot, self.teamlead)
        self.assertEqual(second.member.user_id, self.candidates[2].id)

    def test_same_role_slots_get_different_people(self):
        first = auto_assign_best_candidate(self.slot, self.teamlead)
        second = auto_assign_best_candidate(self.other_slot, self.teamlead)

        self.assertEqual(first.member.user_id, self.candidates[0].id)
        self.assertEqual(second.member.user_id, self.candidates[1].id)

    def test_history_of_one_slot_does_not_affect_another(self):
        RoomSlotCandidate.objects.create(
            slot=self.slot,
            candidate=self.candidates[0],
            outcome=RoomSlotCandidate.Outcome.SKIPPED,
        )

        outcome = auto_assign_best_candidate(self.other_slot, self.teamlead)

        self.assertEqual(outcome.member.user_id, self.candidates[0].id)
        self.assertEqual(RoomSlotCandidate.objects.filter(slot=self.slot).count(), 1)
        self.assertEqual(
            RoomSlotCandidate.objects.filter(slot=self.other_slot).count(), 1
        )


class ExhaustedPoolTests(TestCase):
    """Пул исчерпан или пуст.

    Отдельный класс намеренно: hard filters исключают только участников
    *этой* комнаты, поэтому кандидаты соседней фикстуры остались бы годными
    и пул перестал бы быть пустым. Здесь в базе нет никого лишнего.
    """

    def test_replace_without_next_candidate_keeps_current_member(self):
        solo = make_staffed_project(slots=1, candidates=1, prefix='solo-')
        only_candidate = solo.candidates[0]
        assign_candidate_to_slot(solo.slots[0], only_candidate, solo.teamlead)
        member = RoomMember.objects.get(function_slot=solo.slots[0])
        RoomMember.objects.filter(pk=member.pk).update(
            ready_status=RoomMember.ReadyStatus.READY,
        )

        outcome = replace_slot_member(solo.slots[0], solo.teamlead)

        self.assertEqual(outcome.code, 'no_candidates')
        member.refresh_from_db()
        self.assertEqual(member.user_id, only_candidate.id)
        self.assertEqual(member.ready_status, RoomMember.ReadyStatus.READY)
        self.assertEqual(
            RoomSlotCandidate.objects.get(
                slot=solo.slots[0], candidate=only_candidate
            ).outcome,
            RoomSlotCandidate.Outcome.ASSIGNED,
        )

    def test_auto_assign_on_empty_pool_leaves_the_slot_empty(self):
        empty = make_staffed_project(slots=1, candidates=0, prefix='empty-')

        outcome = auto_assign_best_candidate(empty.slots[0], empty.teamlead)

        self.assertEqual(outcome.code, 'no_candidates')
        self.assertIsNone(outcome.member)
        self.assertFalse(
            RoomMember.objects.filter(function_slot=empty.slots[0]).exists()
        )
        empty.project.refresh_from_db()
        self.assertEqual(empty.project.status, Project.Status.STAFFING)


# ---------------------------------------------------------------------------
# 10-11, 22. Готовность команды и переход в ACTIVE
# ---------------------------------------------------------------------------


class ReadinessActivationTests(StaffingTestCase):
    def test_all_slots_ready_activate_the_project(self):
        first = assign_candidate_to_slot(self.slot, self.candidates[0], self.teamlead)
        second = assign_candidate_to_slot(
            self.other_slot, self.candidates[1], self.teamlead
        )

        confirm_freelancer_readiness(first, first.user)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)

        confirm_freelancer_readiness(second, second.user)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

    def test_single_ready_member_keeps_the_project_in_staffing(self):
        member = assign_candidate_to_slot(self.slot, self.candidates[0], self.teamlead)
        assign_candidate_to_slot(self.other_slot, self.candidates[1], self.teamlead)

        confirm_freelancer_readiness(member, member.user)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)

    def test_empty_active_slot_blocks_activation(self):
        member = assign_candidate_to_slot(self.slot, self.candidates[0], self.teamlead)

        confirm_freelancer_readiness(member, member.user)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertFalse(is_functional_team_ready(self.room))


# ---------------------------------------------------------------------------
# 12-15. Кто имеет право на ручной подбор
# ---------------------------------------------------------------------------


class StaffingPermissionTests(StaffingTestCase):
    def test_freelancer_post_to_slot_assign_is_forbidden(self):
        add_freelancer_to_room(self.room, self.candidates[0])
        self.client.force_login(self.candidates[0])

        response = self.client.post(self.assign_url(self.candidates[1]))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            RoomMember.objects.filter(user=self.candidates[1]).exists()
        )

    def test_teamlead_assigns_candidate_to_slot(self):
        member = assign_candidate_to_slot(self.slot, self.candidates[0], self.teamlead)

        self.assertEqual(member.room_id, self.room.id)
        self.assertEqual(member.user_id, self.candidates[0].id)
        self.assertEqual(member.role_in_room, RoomMember.RoleInRoom.FREELANCER)

    def test_plain_freelancer_cannot_assign_via_service(self):
        intruder = self.candidates[1]
        add_freelancer_to_room(self.room, intruder)

        with self.assertRaises(PermissionDenied):
            assign_candidate_to_slot(self.slot, self.candidates[0], intruder)

        self.assertFalse(
            RoomMember.objects.filter(room=self.room, user=self.candidates[0]).exists()
        )

    def test_owner_director_cannot_manage_slots_manually(self):
        """Операционка комнаты — у тимлида: владелец покупает состав, но не сажает.

        Обратная сторона `for_composition_autofill`: авто-подбор при покупке
        функции идёт от директора, а кнопки «Команды» — только от тимлида.
        """
        with self.assertRaises(PermissionDenied):
            assign_candidate_to_slot(self.slot, self.candidates[0], self.director)

        self.assertFalse(
            RoomMember.objects.filter(room=self.room, user=self.candidates[0]).exists()
        )
        self.assertFalse(RoomSlotCandidate.objects.filter(slot=self.slot).exists())


# ---------------------------------------------------------------------------
# 16-21. Сервис назначения: запись, проверки, границы
# ---------------------------------------------------------------------------


class AssignmentServiceTests(StaffingTestCase):
    def test_new_member_gets_slot_role_key_and_pending_status(self):
        member = assign_candidate_to_slot(self.slot, self.candidates[0], self.teamlead)

        self.assertEqual(member.function_slot_id, self.slot.id)
        self.assertEqual(member.role_key, self.slot.role_key)
        self.assertEqual(member.ready_status, RoomMember.ReadyStatus.PENDING)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.assigned_member.id, member.id)

    def test_assignment_writes_candidate_history_and_activity(self):
        assign_candidate_to_slot(self.slot, self.candidates[0], self.teamlead)

        history = RoomSlotCandidate.objects.get(
            slot=self.slot, candidate=self.candidates[0]
        )
        self.assertEqual(history.outcome, RoomSlotCandidate.Outcome.ASSIGNED)
        self.assertEqual(history.actor_id, self.teamlead.id)
        self.assertTrue(
            RoomActivity.objects.filter(
                room=self.room,
                event_type=RoomActivity.EventType.MEMBER_ADDED,
            ).exists()
        )

    def test_ineligible_candidate_is_rejected_by_the_server(self):
        # Кандидат подходил на момент показа, но успел стать недоступным.
        profile = self.candidates[0].freelancer_profile
        profile.is_available = False
        profile.save(update_fields=['is_available'])

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(self.slot, self.candidates[0], self.teamlead)

        self.assertFalse(
            RoomMember.objects.filter(user=self.candidates[0]).exists()
        )

    def test_occupied_slot_is_not_assigned_twice(self):
        assign_candidate_to_slot(self.slot, self.candidates[0], self.teamlead)

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(self.slot, self.candidates[1], self.teamlead)

        self.assertEqual(
            RoomMember.objects.filter(room=self.room, function_slot=self.slot).count(),
            1,
        )

    def test_assignment_alone_does_not_activate_the_project(self):
        assign_candidate_to_slot(self.slot, self.candidates[0], self.teamlead)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)

    def test_staffing_is_closed_for_a_non_staffing_project(self):
        self.project.status = Project.Status.ARCHIVED
        self.project.save(update_fields=['status'])

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(self.slot, self.candidates[0], self.teamlead)

        self.assertFalse(
            RoomMember.objects.filter(user=self.candidates[0]).exists()
        )
