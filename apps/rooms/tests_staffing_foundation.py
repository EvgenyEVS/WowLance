"""Фундамент данных staffing: функциональные слоты, привязка участников, кандидаты.

Проверяется только слой данных. Подбор (matching), ranking, «Другой сейлер»,
переходы статуса проекта и UI в этот этап не входят.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.rooms.models import (
    Project,
    Room,
    RoomFunctionSlot,
    RoomMember,
    RoomSlotCandidate,
)
from apps.rooms.services import add_freelancer_to_room, launch_project
from apps.test_helpers import make_director, make_freelancer, make_teamlead


class StaffingFoundationTestCase(TestCase):
    def setUp(self):
        self.director = make_director(email='dir-staffing@example.com')
        self.project = Project.objects.create(
            owner=self.director,
            name='Проект со слотами',
            status=Project.Status.STAFFING,
        )
        self.room = Room.objects.create(project=self.project)

    def make_slot(self, role_key='seller_middle', slot_index=1, **extra):
        return RoomFunctionSlot.objects.create(
            room=self.room,
            role_key=role_key,
            slot_index=slot_index,
            **extra,
        )


class RoomFunctionSlotTests(StaffingFoundationTestCase):
    def test_two_slots_of_same_role_coexist(self):
        first = self.make_slot(slot_index=1)
        second = self.make_slot(slot_index=2)

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.room.function_slots.count(), 2)
        self.assertEqual(
            list(
                self.room.function_slots.filter(role_key='seller_middle')
                .order_by('slot_index')
                .values_list('slot_index', flat=True)
            ),
            [1, 2],
        )

    def test_duplicate_room_role_key_slot_index_rejected(self):
        self.make_slot(slot_index=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_slot(slot_index=1)

    def test_same_slot_index_allowed_for_other_role_and_other_room(self):
        self.make_slot(role_key='seller_middle', slot_index=1)
        self.make_slot(role_key='researcher', slot_index=1)

        other_project = Project.objects.create(owner=self.director, name='Другой проект')
        other_room = Room.objects.create(project=other_project)
        RoomFunctionSlot.objects.create(
            room=other_room,
            role_key='seller_middle',
            slot_index=1,
        )
        self.assertEqual(RoomFunctionSlot.objects.count(), 3)

    def test_defaults_and_structured_requirements_are_filterable(self):
        default_slot = self.make_slot(slot_index=1)
        self.assertEqual(default_slot.required_level, RoomFunctionSlot.Grade.MIDDLE)
        self.assertEqual(default_slot.required_channel, RoomFunctionSlot.Channel.ANY)
        self.assertTrue(default_slot.is_active)

        cold_senior = self.make_slot(
            slot_index=2,
            required_level=RoomFunctionSlot.Grade.SENIOR,
            required_channel=RoomFunctionSlot.Channel.COLD_CALLING,
        )
        found = RoomFunctionSlot.objects.filter(
            room=self.room,
            required_level=RoomFunctionSlot.Grade.SENIOR,
            required_channel=RoomFunctionSlot.Channel.COLD_CALLING,
        )
        self.assertEqual(list(found), [cold_senior])

    def test_slot_index_zero_rejected_by_database(self):
        """CheckConstraint: нумерация слотов начинается с 1, ноль в БД не попадёт."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoomFunctionSlot.objects.create(
                    room=self.room,
                    role_key='seller_middle',
                    slot_index=0,
                )
        self.assertEqual(RoomFunctionSlot.objects.count(), 0)

    def test_empty_and_closed_slot_states(self):
        slot = self.make_slot()
        self.assertIsNone(slot.assigned_member)
        self.assertFalse(slot.is_filled)

        slot.is_active = False
        slot.save(update_fields=['is_active', 'updated_at'])
        slot.refresh_from_db()
        self.assertFalse(slot.is_active)
        self.assertFalse(slot.is_filled)


class RoomMemberSlotLinkTests(StaffingFoundationTestCase):
    def setUp(self):
        super().setUp()
        self.slot = self.make_slot()
        self.freelancer = make_freelancer(email='seller1@example.com')
        self.other_freelancer = make_freelancer(email='seller2@example.com')

    def test_member_occupies_slot_and_slot_sees_member(self):
        member = RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            role_key='seller_middle',
            function_slot=self.slot,
        )
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.assigned_member, member)
        self.assertTrue(self.slot.is_filled)

    def test_slot_cannot_be_taken_by_two_members(self):
        RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=self.slot,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoomMember.objects.create(
                    room=self.room,
                    user=self.other_freelancer,
                    role_in_room=RoomMember.RoleInRoom.FREELANCER,
                    function_slot=self.slot,
                )

    def test_members_without_slot_are_not_blocked(self):
        """Обратная совместимость: у нескольких участников function_slot = NULL."""
        first = RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )
        second = RoomMember.objects.create(
            room=self.room,
            user=self.other_freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )
        for member in (first, second):
            member.refresh_from_db()
            self.assertIsNone(member.function_slot)
            self.assertEqual(member.role_key, '')

    def test_existing_flow_creates_member_without_slot(self):
        """Существующий invite/manual flow не трогаем: слот не требуется."""
        project = Project.objects.create(owner=self.director, name='Старый поток')
        launch_project(project)
        member = add_freelancer_to_room(project.room, self.freelancer)
        self.assertIsNone(member.function_slot)
        self.assertEqual(member.role_key, '')
        self.assertEqual(member.role_in_room, RoomMember.RoleInRoom.FREELANCER)

    def test_role_in_room_and_role_key_are_independent(self):
        teamlead = make_teamlead(email='lead-slots@example.com')
        lead_member = RoomMember.objects.create(
            room=self.room,
            user=teamlead,
            role_in_room=RoomMember.RoleInRoom.TEAMLEAD,
            role_key='',
        )
        seller_member = RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            role_key='seller_middle',
            function_slot=self.slot,
        )
        # Права в комнате остаются за role_in_room, функция — за role_key.
        self.assertEqual(lead_member.role_in_room, RoomMember.RoleInRoom.TEAMLEAD)
        self.assertEqual(lead_member.role_key, '')
        self.assertIsNone(lead_member.function_slot)
        self.assertEqual(seller_member.role_in_room, RoomMember.RoleInRoom.FREELANCER)
        self.assertEqual(seller_member.role_key, 'seller_middle')

        # Директор тоже может закрывать функцию, не меняя своих прав.
        director_member = RoomMember.objects.create(
            room=self.room,
            user=self.director,
            role_in_room=RoomMember.RoleInRoom.DIRECTOR,
            role_key='seller_middle',
            function_slot=self.make_slot(slot_index=2),
        )
        self.assertEqual(director_member.role_in_room, RoomMember.RoleInRoom.DIRECTOR)
        self.assertEqual(director_member.role_key, 'seller_middle')

    def test_unique_room_user_constraint_still_applies(self):
        RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoomMember.objects.create(
                    room=self.room,
                    user=self.freelancer,
                    role_in_room=RoomMember.RoleInRoom.FREELANCER,
                )

    def test_slot_from_another_room_is_rejected_by_validation(self):
        other_project = Project.objects.create(owner=self.director, name='Чужой проект')
        other_room = Room.objects.create(project=other_project)
        foreign_slot = RoomFunctionSlot.objects.create(
            room=other_room,
            role_key='seller_middle',
            slot_index=1,
        )
        member = RoomMember(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=foreign_slot,
        )
        with self.assertRaises(ValidationError):
            member.full_clean()

    def test_matching_role_key_with_slot_is_valid(self):
        member = RoomMember(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            role_key=self.slot.role_key,
            function_slot=self.slot,
        )
        member.full_clean()

    def test_mismatched_role_key_with_slot_is_rejected(self):
        member = RoomMember(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            role_key='researcher',
            function_slot=self.slot,
        )
        with self.assertRaises(ValidationError) as ctx:
            member.full_clean()
        self.assertIn('role_key', ctx.exception.message_dict)

    def test_member_without_slot_stays_valid(self):
        member = RoomMember(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )
        member.full_clean()
        member.save()
        self.assertIsNone(member.function_slot)
        self.assertEqual(member.role_key, '')

    def test_empty_role_key_with_slot_is_still_allowed(self):
        """Пустой role_key — «не указан», а не противоречие: истина живёт в слоте."""
        member = RoomMember(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            role_key='',
            function_slot=self.slot,
        )
        member.full_clean()

    def test_create_with_slot_syncs_role_key(self):
        member = RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=self.slot,
        )
        member.refresh_from_db()
        self.assertEqual(member.role_key, self.slot.role_key)

    def test_reassigning_slot_updates_role_key(self):
        member = RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=self.slot,
        )
        researcher_slot = self.make_slot(role_key='researcher', slot_index=1)
        member.function_slot = researcher_slot
        member.save()
        member.refresh_from_db()
        self.assertEqual(member.role_key, 'researcher')

    def test_reassigning_slot_with_update_fields_updates_role_key(self):
        """update_fields не должен «потерять» синхронизацию role_key."""
        member = RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=self.slot,
        )
        researcher_slot = self.make_slot(role_key='researcher', slot_index=1)
        member.function_slot = researcher_slot
        member.save(update_fields=['function_slot'])
        member.refresh_from_db()
        self.assertEqual(member.role_key, 'researcher')

    def test_contradicting_role_key_cannot_be_persisted(self):
        """Слот seller_middle + role_key seller_senior обычным ORM не сохранить."""
        member = RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            role_key='seller_senior',
            function_slot=self.slot,
        )
        member.refresh_from_db()
        self.assertEqual(member.role_key, 'seller_middle')

        # Повторная попытка «протащить» несовпадение обычным save.
        member.role_key = 'seller_senior'
        member.save()
        member.refresh_from_db()
        self.assertEqual(member.role_key, 'seller_middle')

        self.assertFalse(
            RoomMember.objects.filter(
                function_slot=self.slot,
                role_key='seller_senior',
            ).exists()
        )

    def test_member_without_slot_keeps_own_role_key(self):
        """Сценарий без слота не изменился: role_key не навязывается и не стирается."""
        empty = RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )
        empty.refresh_from_db()
        self.assertIsNone(empty.function_slot)
        self.assertEqual(empty.role_key, '')

        custom = RoomMember.objects.create(
            room=self.room,
            user=self.other_freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            role_key='seller_senior',
        )
        custom.refresh_from_db()
        self.assertIsNone(custom.function_slot)
        self.assertEqual(custom.role_key, 'seller_senior')

    def test_deleting_slot_keeps_member_in_room(self):
        member = RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            role_key='seller_middle',
            function_slot=self.slot,
        )
        self.slot.delete()
        member.refresh_from_db()
        self.assertIsNone(member.function_slot)
        self.assertTrue(RoomMember.objects.filter(pk=member.pk).exists())


class RoomSlotCandidateTests(StaffingFoundationTestCase):
    def setUp(self):
        super().setUp()
        self.slot = self.make_slot()
        self.candidate = make_freelancer(email='cand1@example.com')

    def test_candidate_history_is_linked_to_slot_and_candidate(self):
        entry = RoomSlotCandidate.objects.create(
            slot=self.slot,
            candidate=self.candidate,
            actor=self.director,
        )
        self.assertEqual(entry.outcome, RoomSlotCandidate.Outcome.SHOWN)
        self.assertEqual(list(self.slot.candidates.all()), [entry])
        self.assertEqual(list(self.candidate.slot_candidacies.all()), [entry])
        self.assertEqual(entry.actor, self.director)
        self.assertIsNotNone(entry.created_at)
        self.assertIsNotNone(entry.updated_at)

    def test_outcomes_cover_shown_assigned_skipped_declined(self):
        entry = RoomSlotCandidate.objects.create(slot=self.slot, candidate=self.candidate)
        for outcome in (
            RoomSlotCandidate.Outcome.ASSIGNED,
            RoomSlotCandidate.Outcome.SKIPPED,
            RoomSlotCandidate.Outcome.DECLINED,
        ):
            entry.outcome = outcome
            entry.save(update_fields=['outcome', 'updated_at'])
            entry.refresh_from_db()
            self.assertEqual(entry.outcome, outcome)
        self.assertEqual(RoomSlotCandidate.objects.filter(slot=self.slot).count(), 1)

    def test_duplicate_candidate_for_same_slot_rejected(self):
        RoomSlotCandidate.objects.create(slot=self.slot, candidate=self.candidate)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoomSlotCandidate.objects.create(slot=self.slot, candidate=self.candidate)

    def test_same_candidate_allowed_on_another_slot(self):
        other_slot = self.make_slot(slot_index=2)
        RoomSlotCandidate.objects.create(slot=self.slot, candidate=self.candidate)
        RoomSlotCandidate.objects.create(slot=other_slot, candidate=self.candidate)
        self.assertEqual(self.candidate.slot_candidacies.count(), 2)

    def test_history_is_removed_with_slot(self):
        RoomSlotCandidate.objects.create(slot=self.slot, candidate=self.candidate)
        self.slot.delete()
        self.assertEqual(RoomSlotCandidate.objects.count(), 0)
