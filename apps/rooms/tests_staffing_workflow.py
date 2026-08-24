"""Рабочий сценарий подбора: назначение, auto top-1, замена, пул, готовность, UI.

Проверяется слой операций поверх read-only Matching Engine:

* `apps.rooms.staffing.services` — транзакционные операции над слотами;
* `apps.rooms.staffing.selectors` — данные карточек слотов для UI;
* views вкладки «Команда», страницы пула кандидатов и подтверждения готовности.

Сам подбор (hard filters и ranking) покрыт в `tests_staffing_matching`
и здесь не дублируется — тесты опираются на его результат.
"""

from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase
from django.urls import reverse

from apps.profiles.models import FreelancerProfile
from apps.rooms.models import (
    Project,
    Room,
    RoomActivity,
    RoomFunctionSlot,
    RoomMember,
    RoomSlotCandidate,
)
from apps.rooms.services import (
    add_freelancer_to_room,
    user_can_access_project,
)
from apps.rooms.staffing import selectors
from apps.rooms.staffing.matching import CHANNEL_REQUIREMENTS
from apps.rooms.staffing.services import (
    StaffingError,
    assign_candidate_to_slot,
    auto_assign_best_candidate,
    confirm_freelancer_readiness,
    is_functional_team_ready,
    replace_slot_member,
)
from apps.test_helpers import make_director, make_teamlead, make_user
from apps.users.models import User

VIDEO_URL = 'https://youtu.be/demo-presentation'
PASSWORD = 'TestPass123!'


class StaffingWorkflowTestCase(TestCase):
    """Комната в статусе подбора, один слот и пул кандидатов.

    Пользователи создаются один раз на класс: хеширование пароля — самая
    дорогая часть подготовки, а профили собираются под сценарий теста.
    """

    @classmethod
    def setUpTestData(cls):
        cls.director = make_director(email='dir-workflow@example.com', password=PASSWORD)
        cls.teamlead = make_teamlead(email='tl-workflow@example.com', password=PASSWORD)
        cls.project = Project.objects.create(
            owner=cls.director,
            name='Проект подбора команды',
            status=Project.Status.STAFFING,
            teamlead=cls.teamlead,
        )
        cls.room = Room.objects.create(project=cls.project)
        RoomMember.objects.create(
            room=cls.room,
            user=cls.director,
            role_in_room=RoomMember.RoleInRoom.DIRECTOR,
        )
        RoomMember.objects.create(
            room=cls.room,
            user=cls.teamlead,
            role_in_room=RoomMember.RoleInRoom.TEAMLEAD,
        )
        cls.slot = RoomFunctionSlot.objects.create(
            room=cls.room,
            role_key='seller',
            slot_index=1,
            required_level=RoomFunctionSlot.Grade.MIDDLE,
        )
        cls.users = [
            make_user(
                email=f'workflow-cand{index}@example.com',
                role=User.Roles.FREELANCER,
                password=PASSWORD,
                first_name=f'Кандидат{index}',
            )
            for index in range(1, 6)
        ]

    def make_profile(self, user, **overrides):
        """Профиль, по умолчанию проходящий все hard filters слота."""
        fields = {
            'level': FreelancerProfile.Level.MIDDLE,
            'is_available': True,
            'is_verified': True,
            'video_url': VIDEO_URL,
            'rating': Decimal('4.50'),
            'acceptance_rate': Decimal('90.00'),
            'experience_projects': 10,
        }
        # Признаки каналов берутся из таблицы Matching Engine, а не пишутся
        # здесь строками: канал слота и поле профиля должны оставаться
        # связанными в одном месте (matching.CHANNEL_REQUIREMENTS).
        for field in CHANNEL_REQUIREMENTS.values():
            fields[field] = True
        fields.update(overrides)
        return FreelancerProfile.objects.create(user=user, **fields)

    def make_ranked_pool(self, count=3):
        """Кандидаты с убывающим рейтингом: users[0] — лучший."""
        return [
            self.make_profile(user, rating=Decimal('5.00') - index)
            for index, user in enumerate(self.users[:count])
        ]

    def second_slot(self, **overrides):
        """Второй такой же слот: одинаковая роль, другой slot_index."""
        fields = {
            'room': self.room,
            'role_key': 'seller',
            'slot_index': 2,
            'required_level': RoomFunctionSlot.Grade.MIDDLE,
        }
        fields.update(overrides)
        return RoomFunctionSlot.objects.create(**fields)


class AssignmentServiceTests(StaffingWorkflowTestCase):
    def test_director_can_assign_candidate(self):
        profile = self.make_profile(self.users[0])

        member = assign_candidate_to_slot(self.slot, profile.user, self.director)

        self.assertEqual(member.room_id, self.room.id)
        self.assertEqual(member.user_id, profile.user.id)
        self.assertEqual(member.role_in_room, RoomMember.RoleInRoom.FREELANCER)

    def test_teamlead_can_assign_candidate(self):
        profile = self.make_profile(self.users[0])

        member = assign_candidate_to_slot(self.slot, profile.user, self.teamlead)

        self.assertEqual(member.user_id, profile.user.id)

    def test_plain_freelancer_cannot_assign(self):
        profile = self.make_profile(self.users[0])
        intruder = self.make_profile(self.users[1])
        add_freelancer_to_room(self.room, intruder.user)

        with self.assertRaises(PermissionDenied):
            assign_candidate_to_slot(self.slot, profile.user, intruder.user)

        self.assertFalse(
            RoomMember.objects.filter(room=self.room, user=profile.user).exists()
        )

    def test_new_member_gets_slot_role_key_and_pending_status(self):
        profile = self.make_profile(self.users[0])

        member = assign_candidate_to_slot(self.slot, profile.user, self.director)

        self.assertEqual(member.function_slot_id, self.slot.id)
        self.assertEqual(member.role_key, self.slot.role_key)
        self.assertEqual(member.ready_status, RoomMember.ReadyStatus.PENDING)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.assigned_member.id, member.id)

    def test_assignment_writes_history_and_activity(self):
        profile = self.make_profile(self.users[0])

        assign_candidate_to_slot(self.slot, profile.user, self.director)

        history = RoomSlotCandidate.objects.get(slot=self.slot, candidate=profile.user)
        self.assertEqual(history.outcome, RoomSlotCandidate.Outcome.ASSIGNED)
        self.assertEqual(history.actor_id, self.director.id)
        self.assertTrue(
            RoomActivity.objects.filter(
                room=self.room,
                event_type=RoomActivity.EventType.MEMBER_ADDED,
            ).exists()
        )

    def test_ineligible_candidate_is_rejected_by_server(self):
        profile = self.make_profile(self.users[0])
        # Кандидат подходил на момент показа, но успел стать недоступным.
        profile.is_available = False
        profile.save(update_fields=['is_available'])

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(self.slot, profile.user, self.director)

        self.assertFalse(RoomMember.objects.filter(user=profile.user).exists())

    def test_candidate_without_profile_is_rejected(self):
        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(self.slot, self.users[0], self.director)

    def test_existing_room_member_is_not_assigned_again(self):
        profile = self.make_profile(self.users[0])
        add_freelancer_to_room(self.room, profile.user)

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(self.slot, profile.user, self.director)

        self.assertEqual(
            RoomMember.objects.filter(room=self.room, user=profile.user).count(),
            1,
        )

    def test_occupied_slot_is_not_assigned_twice(self):
        first, second = self.make_ranked_pool(2)
        assign_candidate_to_slot(self.slot, first.user, self.director)

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(self.slot, second.user, self.director)

        self.assertEqual(
            RoomMember.objects.filter(room=self.room, function_slot=self.slot).count(),
            1,
        )

    def test_assignment_alone_does_not_activate_project(self):
        profile = self.make_profile(self.users[0])

        assign_candidate_to_slot(self.slot, profile.user, self.director)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)

    def test_staffing_is_closed_for_non_staffing_project(self):
        profile = self.make_profile(self.users[0])
        self.project.status = Project.Status.ARCHIVED
        self.project.save(update_fields=['status'])

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(self.slot, profile.user, self.director)

        self.assertFalse(RoomMember.objects.filter(user=profile.user).exists())

    def test_closed_slot_is_not_staffed(self):
        profile = self.make_profile(self.users[0])
        self.slot.is_active = False
        self.slot.save(update_fields=['is_active'])

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(self.slot, profile.user, self.director)

    def test_failed_assignment_leaves_no_partial_state(self):
        profile = self.make_profile(self.users[0], video_url='')

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(self.slot, profile.user, self.director)

        self.assertFalse(RoomSlotCandidate.objects.filter(slot=self.slot).exists())
        self.assertFalse(RoomMember.objects.filter(user=profile.user).exists())
        self.assertFalse(
            RoomActivity.objects.filter(
                room=self.room,
                event_type=RoomActivity.EventType.MEMBER_ADDED,
            ).exists()
        )


class AutoAssignTests(StaffingWorkflowTestCase):
    def test_auto_assign_takes_top_ranked_candidate(self):
        pool = self.make_ranked_pool(3)

        outcome = auto_assign_best_candidate(self.slot, self.director)

        self.assertEqual(outcome.code, 'assigned')
        self.assertEqual(outcome.member.user_id, pool[0].user.id)

    def test_auto_assign_on_empty_pool_returns_result_without_changes(self):
        outcome = auto_assign_best_candidate(self.slot, self.director)

        self.assertEqual(outcome.code, 'no_candidates')
        self.assertIsNone(outcome.member)
        self.assertFalse(RoomMember.objects.filter(function_slot=self.slot).exists())
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)

    def test_auto_assign_does_not_offer_skipped_candidate_again(self):
        pool = self.make_ranked_pool(2)
        RoomSlotCandidate.objects.create(
            slot=self.slot,
            candidate=pool[0].user,
            outcome=RoomSlotCandidate.Outcome.SKIPPED,
        )

        outcome = auto_assign_best_candidate(self.slot, self.director)

        self.assertEqual(outcome.member.user_id, pool[1].user.id)

    def test_auto_assign_requires_manage_rights(self):
        self.make_ranked_pool(1)
        stranger = make_user(email='stranger-auto@example.com', role=User.Roles.FREELANCER)

        with self.assertRaises(PermissionDenied):
            auto_assign_best_candidate(self.slot, stranger)


class ReplaceSlotMemberTests(StaffingWorkflowTestCase):
    def test_replace_gives_next_candidate_by_ranking(self):
        pool = self.make_ranked_pool(3)
        auto_assign_best_candidate(self.slot, self.director)

        outcome = replace_slot_member(self.slot, self.director)

        self.assertEqual(outcome.code, 'replaced')
        self.assertEqual(outcome.member.user_id, pool[1].user.id)
        self.assertEqual(outcome.member.function_slot_id, self.slot.id)
        self.assertEqual(outcome.member.ready_status, RoomMember.ReadyStatus.PENDING)

    def test_replaced_candidate_becomes_skipped_and_loses_room_access(self):
        pool = self.make_ranked_pool(2)
        assign_candidate_to_slot(self.slot, pool[0].user, self.director)
        # Готовность снятого кандидата не должна утечь новому исполнителю.
        RoomMember.objects.filter(user=pool[0].user).update(
            ready_status=RoomMember.ReadyStatus.READY,
        )

        replace_slot_member(self.slot, self.director)

        skipped = RoomSlotCandidate.objects.get(slot=self.slot, candidate=pool[0].user)
        self.assertEqual(skipped.outcome, RoomSlotCandidate.Outcome.SKIPPED)
        self.assertFalse(
            RoomMember.objects.filter(room=self.room, user=pool[0].user).exists()
        )
        self.assertFalse(user_can_access_project(pool[0].user, self.project))
        new_member = RoomMember.objects.get(function_slot=self.slot)
        self.assertEqual(new_member.user_id, pool[1].user.id)
        self.assertEqual(new_member.ready_status, RoomMember.ReadyStatus.PENDING)

    def test_replace_logs_removal_and_addition(self):
        self.make_ranked_pool(2)
        auto_assign_best_candidate(self.slot, self.director)

        replace_slot_member(self.slot, self.director)

        self.assertTrue(
            RoomActivity.objects.filter(
                room=self.room,
                event_type=RoomActivity.EventType.MEMBER_REMOVED,
            ).exists()
        )
        self.assertEqual(
            RoomActivity.objects.filter(
                room=self.room,
                event_type=RoomActivity.EventType.MEMBER_ADDED,
            ).count(),
            2,
        )

    def test_replace_without_next_candidate_keeps_current_member(self):
        only = self.make_profile(self.users[0])
        assign_candidate_to_slot(self.slot, only.user, self.director)
        member = RoomMember.objects.get(function_slot=self.slot)
        RoomMember.objects.filter(pk=member.pk).update(
            ready_status=RoomMember.ReadyStatus.READY,
        )

        outcome = replace_slot_member(self.slot, self.director)

        self.assertEqual(outcome.code, 'no_candidates')
        member.refresh_from_db()
        self.assertEqual(member.user_id, only.user.id)
        self.assertEqual(member.ready_status, RoomMember.ReadyStatus.READY)
        self.assertEqual(
            RoomSlotCandidate.objects.get(slot=self.slot, candidate=only.user).outcome,
            RoomSlotCandidate.Outcome.ASSIGNED,
        )

    def test_replace_creates_new_member_row_instead_of_editing_user(self):
        pool = self.make_ranked_pool(2)
        assign_candidate_to_slot(self.slot, pool[0].user, self.director)
        old_member_id = RoomMember.objects.get(function_slot=self.slot).id

        replace_slot_member(self.slot, self.director)

        self.assertFalse(RoomMember.objects.filter(id=old_member_id).exists())
        self.assertEqual(RoomMember.objects.filter(function_slot=self.slot).count(), 1)

    def test_replace_on_empty_slot_is_an_error(self):
        self.make_ranked_pool(1)

        with self.assertRaises(StaffingError):
            replace_slot_member(self.slot, self.director)

    def test_replace_requires_manage_rights(self):
        pool = self.make_ranked_pool(2)
        assign_candidate_to_slot(self.slot, pool[0].user, self.director)

        with self.assertRaises(PermissionDenied):
            replace_slot_member(self.slot, pool[0].user)


class IndependentSlotsTests(StaffingWorkflowTestCase):
    """Два одинаковых `seller_middle` слота живут своей историей."""

    def test_same_role_slots_get_different_people(self):
        pool = self.make_ranked_pool(3)
        other = self.second_slot()

        first = auto_assign_best_candidate(self.slot, self.director)
        second = auto_assign_best_candidate(other, self.director)

        self.assertEqual(first.member.user_id, pool[0].user.id)
        self.assertEqual(second.member.user_id, pool[1].user.id)

    def test_history_of_one_slot_does_not_affect_another(self):
        pool = self.make_ranked_pool(3)
        other = self.second_slot()
        RoomSlotCandidate.objects.create(
            slot=self.slot,
            candidate=pool[0].user,
            outcome=RoomSlotCandidate.Outcome.SKIPPED,
        )

        outcome = auto_assign_best_candidate(other, self.director)

        self.assertEqual(outcome.member.user_id, pool[0].user.id)
        self.assertEqual(RoomSlotCandidate.objects.filter(slot=self.slot).count(), 1)
        self.assertEqual(RoomSlotCandidate.objects.filter(slot=other).count(), 1)

    def test_slots_with_different_channel_use_their_own_requirements(self):
        cold_field = CHANNEL_REQUIREMENTS[RoomFunctionSlot.Channel.COLD_CALLING]
        linkedin_field = CHANNEL_REQUIREMENTS[RoomFunctionSlot.Channel.LINKEDIN]
        cold_only = self.make_profile(
            self.users[0],
            rating=Decimal('4.00'),
            **{cold_field: True, linkedin_field: False},
        )
        linkedin_only = self.make_profile(
            self.users[1],
            rating=Decimal('5.00'),
            **{cold_field: False, linkedin_field: True},
        )
        self.slot.required_channel = RoomFunctionSlot.Channel.COLD_CALLING
        self.slot.save(update_fields=['required_channel'])
        linkedin_slot = self.second_slot(
            required_channel=RoomFunctionSlot.Channel.LINKEDIN,
        )

        cold_outcome = auto_assign_best_candidate(self.slot, self.director)
        linkedin_outcome = auto_assign_best_candidate(linkedin_slot, self.director)

        self.assertEqual(cold_outcome.member.user_id, cold_only.user.id)
        self.assertEqual(linkedin_outcome.member.user_id, linkedin_only.user.id)


class ReadinessActivationTests(StaffingWorkflowTestCase):
    def _assign(self, slot, profile):
        return assign_candidate_to_slot(slot, profile.user, self.director)

    def test_single_ready_member_does_not_activate_project_with_two_slots(self):
        pool = self.make_ranked_pool(2)
        other = self.second_slot()
        member = self._assign(self.slot, pool[0])
        self._assign(other, pool[1])

        confirm_freelancer_readiness(member, member.user)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)

    def test_empty_slot_blocks_activation(self):
        pool = self.make_ranked_pool(2)
        self.second_slot()
        member = self._assign(self.slot, pool[0])

        confirm_freelancer_readiness(member, member.user)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertFalse(is_functional_team_ready(self.room))

    def test_declined_member_blocks_activation(self):
        pool = self.make_ranked_pool(2)
        other = self.second_slot()
        ready_member = self._assign(self.slot, pool[0])
        declined_member = self._assign(other, pool[1])
        RoomMember.objects.filter(pk=declined_member.pk).update(
            ready_status=RoomMember.ReadyStatus.DECLINED,
        )

        confirm_freelancer_readiness(ready_member, ready_member.user)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)

    def test_all_slots_filled_and_ready_activate_project(self):
        pool = self.make_ranked_pool(2)
        other = self.second_slot()
        first = self._assign(self.slot, pool[0])
        second = self._assign(other, pool[1])

        confirm_freelancer_readiness(first, first.user)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)

        confirm_freelancer_readiness(second, second.user)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

    def test_repeated_confirmation_is_idempotent(self):
        profile = self.make_profile(self.users[0])
        member = self._assign(self.slot, profile)

        self.assertTrue(confirm_freelancer_readiness(member, member.user))
        self.assertFalse(confirm_freelancer_readiness(member, member.user))

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)
        self.assertEqual(
            RoomActivity.objects.filter(
                room=self.room,
                event_type=RoomActivity.EventType.READY,
            ).count(),
            2,  # готовность участника + активация проекта, по одному разу
        )

    def test_director_and_teamlead_without_slot_do_not_block_activation(self):
        profile = self.make_profile(self.users[0])
        member = self._assign(self.slot, profile)
        # Директор и тимлид в комнате есть, их ready_status остаётся pending.
        self.assertEqual(
            RoomMember.objects.filter(
                room=self.room,
                ready_status=RoomMember.ReadyStatus.PENDING,
            )
            .exclude(pk=member.pk)
            .count(),
            2,
        )

        confirm_freelancer_readiness(member, member.user)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

    def test_room_without_active_slots_is_never_ready(self):
        self.slot.is_active = False
        self.slot.save(update_fields=['is_active'])

        self.assertFalse(is_functional_team_ready(self.room))

    def test_adding_freelancer_no_longer_activates_project(self):
        """Старое поведение «первый фрилансер → ACTIVE» удалено."""
        profile = self.make_profile(self.users[0])

        add_freelancer_to_room(self.room, profile.user, actor=self.director)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)

    def test_readiness_is_confirmed_only_by_the_member(self):
        profile = self.make_profile(self.users[0])
        member = self._assign(self.slot, profile)

        with self.assertRaises(PermissionDenied):
            confirm_freelancer_readiness(member, self.director)

    def test_readiness_is_for_freelancers_only(self):
        teamlead_member = RoomMember.objects.get(room=self.room, user=self.teamlead)

        with self.assertRaises(StaffingError):
            confirm_freelancer_readiness(teamlead_member, self.teamlead)


class SlotSelectorTests(StaffingWorkflowTestCase):
    def test_cards_show_status_person_and_metrics(self):
        profile = self.make_profile(self.users[0])
        other = self.second_slot()
        assign_candidate_to_slot(self.slot, profile.user, self.director)

        cards = {card.slot.id: card for card in selectors.slot_cards(self.room)}

        filled = cards[self.slot.id]
        self.assertEqual(filled.status, 'assigned')
        self.assertEqual(filled.profile.id, profile.id)
        self.assertIsNotNone(filled.assigned_at)
        self.assertEqual(cards[other.id].status, 'empty')
        self.assertIsNone(cards[other.id].member)

    def test_ready_and_declined_statuses(self):
        pool = self.make_ranked_pool(2)
        other = self.second_slot()
        ready = assign_candidate_to_slot(self.slot, pool[0].user, self.director)
        declined = assign_candidate_to_slot(other, pool[1].user, self.director)
        RoomMember.objects.filter(pk=ready.pk).update(
            ready_status=RoomMember.ReadyStatus.READY,
        )
        RoomMember.objects.filter(pk=declined.pk).update(
            ready_status=RoomMember.ReadyStatus.DECLINED,
        )

        statuses = {card.slot.id: card.status for card in selectors.slot_cards(self.room)}

        self.assertEqual(statuses[self.slot.id], 'ready')
        self.assertEqual(statuses[other.id], 'declined')

    def test_closed_slots_are_not_shown(self):
        closed = self.second_slot()
        closed.is_active = False
        closed.save(update_fields=['is_active'])

        cards = selectors.slot_cards(self.room)

        self.assertEqual([card.slot.id for card in cards], [self.slot.id])

    def test_summary_counts_total_filled_ready_and_searching(self):
        pool = self.make_ranked_pool(2)
        second = self.second_slot()
        self.second_slot(slot_index=3)
        ready = assign_candidate_to_slot(self.slot, pool[0].user, self.director)
        assign_candidate_to_slot(second, pool[1].user, self.director)
        RoomMember.objects.filter(pk=ready.pk).update(
            ready_status=RoomMember.ReadyStatus.READY,
        )

        summary = selectors.staffing_summary(selectors.slot_cards(self.room))

        self.assertEqual(summary['total'], 3)
        self.assertEqual(summary['filled'], 2)
        self.assertEqual(summary['ready'], 1)
        self.assertEqual(summary['searching'], 1)
        self.assertFalse(summary['complete'])

    def test_slot_cards_do_not_make_a_query_per_slot(self):
        pool = self.make_ranked_pool(3)
        for index, profile in enumerate(pool, start=1):
            slot = self.slot if index == 1 else self.second_slot(slot_index=index)
            assign_candidate_to_slot(slot, profile.user, self.director)

        with self.assertNumQueries(1):
            cards = selectors.slot_cards(self.room)
            names = [(card.member.user.full_name, card.profile.rating) for card in cards]

        self.assertEqual(len(names), 3)


class CandidatePoolViewTests(StaffingWorkflowTestCase):
    def setUp(self):
        self.client = Client()

    def pool_url(self, slot=None):
        return reverse(
            'rooms:room_slot_candidates',
            kwargs={'project_id': self.project.id, 'slot_id': (slot or self.slot).id},
        )

    def assign_url(self, candidate, slot=None):
        return reverse(
            'rooms:room_slot_assign_candidate',
            kwargs={
                'project_id': self.project.id,
                'slot_id': (slot or self.slot).id,
                'candidate_id': candidate.id,
            },
        )

    def test_director_sees_only_matching_candidates_in_ranked_order(self):
        pool = self.make_ranked_pool(3)
        self.make_profile(self.users[3], is_verified=False)
        self.make_profile(self.users[4], level=FreelancerProfile.Level.SENIOR)
        self.client.force_login(self.director)

        response = self.client.get(self.pool_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [profile.id for profile in response.context['page_obj']],
            [profile.id for profile in pool],
        )

    def test_teamlead_has_access(self):
        self.make_ranked_pool(1)
        self.client.force_login(self.teamlead)

        response = self.client.get(self.pool_url())

        self.assertEqual(response.status_code, 200)

    def test_freelancer_member_gets_403(self):
        profile = self.make_profile(self.users[0])
        add_freelancer_to_room(self.room, profile.user)
        self.client.force_login(profile.user)

        response = self.client.get(self.pool_url())

        self.assertEqual(response.status_code, 403)

    def test_outsider_gets_403(self):
        stranger = make_user(email='pool-stranger@example.com', role=User.Roles.FREELANCER)
        self.client.force_login(stranger)

        response = self.client.get(self.pool_url())

        self.assertEqual(response.status_code, 403)

    def test_pool_excludes_candidates_already_seen_on_this_slot(self):
        pool = self.make_ranked_pool(3)
        RoomSlotCandidate.objects.create(
            slot=self.slot,
            candidate=pool[0].user,
            outcome=RoomSlotCandidate.Outcome.SKIPPED,
        )
        self.client.force_login(self.director)

        response = self.client.get(self.pool_url())

        self.assertEqual(
            [profile.id for profile in response.context['page_obj']],
            [pool[1].id, pool[2].id],
        )

    def test_history_of_another_slot_does_not_shrink_the_pool(self):
        pool = self.make_ranked_pool(2)
        other = self.second_slot()
        RoomSlotCandidate.objects.create(
            slot=other,
            candidate=pool[0].user,
            outcome=RoomSlotCandidate.Outcome.SKIPPED,
        )
        self.client.force_login(self.director)

        response = self.client.get(self.pool_url())

        self.assertEqual(
            [profile.id for profile in response.context['page_obj']],
            [pool[0].id, pool[1].id],
        )

    def test_pool_page_does_not_write_candidate_history(self):
        self.make_ranked_pool(3)
        self.client.force_login(self.director)

        self.client.get(self.pool_url())

        self.assertEqual(RoomSlotCandidate.objects.count(), 0)

    def test_pool_is_paginated_by_twenty(self):
        extra_users = [
            make_user(
                email=f'pool-extra{index}@example.com',
                role=User.Roles.FREELANCER,
            )
            for index in range(21)
        ]
        for index, user in enumerate(extra_users):
            self.make_profile(user, rating=Decimal('4.00'), experience_projects=index)
        self.client.force_login(self.director)

        first = self.client.get(self.pool_url())
        second = self.client.get(self.pool_url() + '?page=2')

        self.assertEqual(len(first.context['page_obj'].object_list), 20)
        self.assertEqual(len(second.context['page_obj'].object_list), 1)

    def test_manual_assign_creates_member_and_redirects_to_team(self):
        pool = self.make_ranked_pool(2)
        self.client.force_login(self.director)

        response = self.client.post(self.assign_url(pool[1].user))

        self.assertRedirects(
            response,
            reverse('rooms:room_team', kwargs={'project_id': self.project.id}),
        )
        member = RoomMember.objects.get(function_slot=self.slot)
        self.assertEqual(member.user_id, pool[1].user.id)
        self.assertEqual(member.ready_status, RoomMember.ReadyStatus.PENDING)

    def test_manual_assign_rechecks_eligibility_on_post(self):
        profile = self.make_profile(self.users[0])
        self.client.force_login(self.director)
        # Пул был показан, затем кандидат перестал подходить.
        profile.is_available = False
        profile.save(update_fields=['is_available'])

        response = self.client.post(self.assign_url(profile.user))

        self.assertRedirects(response, self.pool_url())
        self.assertFalse(RoomMember.objects.filter(user=profile.user).exists())

    def test_freelancer_cannot_assign_by_direct_post(self):
        pool = self.make_ranked_pool(2)
        add_freelancer_to_room(self.room, pool[0].user)
        self.client.force_login(pool[0].user)

        response = self.client.post(self.assign_url(pool[1].user))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(RoomMember.objects.filter(user=pool[1].user).exists())

    def test_direct_post_cannot_staff_archived_project(self):
        pool = self.make_ranked_pool(1)
        self.project.status = Project.Status.ARCHIVED
        self.project.save(update_fields=['status'])
        self.client.force_login(self.director)

        response = self.client.post(self.assign_url(pool[0].user))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(RoomMember.objects.filter(user=pool[0].user).exists())


class TeamStaffingUiTests(StaffingWorkflowTestCase):
    def setUp(self):
        self.client = Client()

    def team_url(self):
        return reverse('rooms:room_team', kwargs={'project_id': self.project.id})

    def replace_url(self, slot=None):
        return reverse(
            'rooms:room_slot_replace',
            kwargs={'project_id': self.project.id, 'slot_id': (slot or self.slot).id},
        )

    def auto_url(self, slot=None):
        return reverse(
            'rooms:room_slot_auto_assign',
            kwargs={'project_id': self.project.id, 'slot_id': (slot or self.slot).id},
        )

    def test_team_page_shows_slot_cards_with_actions_for_director(self):
        self.make_ranked_pool(1)
        self.client.force_login(self.director)

        response = self.client.get(self.team_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Функциональные слоты')
        self.assertContains(response, f'slot-card-{self.slot.id}')
        self.assertContains(response, 'Подобрать лучшего')
        self.assertContains(response, 'Выбрать из пула')

    def test_team_page_shows_replace_action_for_filled_slot(self):
        self.make_ranked_pool(2)
        auto_assign_best_candidate(self.slot, self.director)
        self.client.force_login(self.director)

        response = self.client.get(self.team_url())

        self.assertContains(response, 'Другой сейлер')
        self.assertNotContains(response, 'Подобрать лучшего')

    def test_freelancer_sees_slots_without_staffing_actions(self):
        pool = self.make_ranked_pool(2)
        assign_candidate_to_slot(self.slot, pool[0].user, self.director)
        self.client.force_login(pool[0].user)

        response = self.client.get(self.team_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'slot-card-{self.slot.id}')
        self.assertNotContains(response, 'Другой сейлер')
        self.assertNotContains(response, 'Подобрать лучшего')
        self.assertNotContains(response, 'Выбрать из пула')

    def test_actions_are_hidden_when_project_is_no_longer_staffing(self):
        self.make_ranked_pool(1)
        self.project.status = Project.Status.ACTIVE
        self.project.save(update_fields=['status'])
        self.client.force_login(self.director)

        response = self.client.get(self.team_url())

        self.assertFalse(response.context['can_staff_slots'])
        self.assertNotContains(response, 'Подобрать лучшего')

    def test_htmx_replace_returns_slot_card_partial(self):
        pool = self.make_ranked_pool(2)
        auto_assign_best_candidate(self.slot, self.director)
        self.client.force_login(self.director)

        response = self.client.post(self.replace_url(), headers={'hx-request': 'true'})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'rooms/_slot_card.html')
        self.assertTemplateNotUsed(response, 'base.html')
        self.assertContains(response, f'slot-card-{self.slot.id}')
        self.assertContains(response, pool[1].user.full_name)

    def test_plain_post_replace_redirects_to_team(self):
        self.make_ranked_pool(2)
        auto_assign_best_candidate(self.slot, self.director)
        self.client.force_login(self.director)

        response = self.client.post(self.replace_url())

        self.assertRedirects(response, self.team_url())

    def test_htmx_auto_assign_without_candidates_reports_in_partial(self):
        self.client.force_login(self.director)

        response = self.client.post(self.auto_url(), headers={'hx-request': 'true'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Подходящие кандидаты не найдены')
        self.assertFalse(RoomMember.objects.filter(function_slot=self.slot).exists())

    def test_plain_post_auto_assign_redirects_and_fills_slot(self):
        pool = self.make_ranked_pool(2)
        self.client.force_login(self.director)

        response = self.client.post(self.auto_url())

        self.assertRedirects(response, self.team_url())
        self.assertEqual(
            RoomMember.objects.get(function_slot=self.slot).user_id,
            pool[0].user.id,
        )

    def test_freelancer_cannot_replace_by_direct_post(self):
        pool = self.make_ranked_pool(2)
        assign_candidate_to_slot(self.slot, pool[0].user, self.director)
        self.client.force_login(pool[0].user)

        response = self.client.post(self.replace_url())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            RoomMember.objects.get(function_slot=self.slot).user_id,
            pool[0].user.id,
        )

    def test_overview_shows_read_only_staffing_summary(self):
        pool = self.make_ranked_pool(2)
        self.second_slot()
        assign_candidate_to_slot(self.slot, pool[0].user, self.director)
        self.client.force_login(self.director)

        response = self.client.get(
            reverse('rooms:room_overview', kwargs={'project_id': self.project.id}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Подбор команды')
        self.assertEqual(response.context['staffing_summary']['total'], 2)
        self.assertEqual(response.context['staffing_summary']['filled'], 1)
        self.assertEqual(response.context['staffing_summary']['searching'], 1)
        # Обзор — только чтение: управляющих действий подбора там нет.
        self.assertNotContains(response, 'Другой сейлер')


class StaffingConcurrencyTests(StaffingWorkflowTestCase):
    def test_two_fast_posts_do_not_create_two_members_on_one_slot(self):
        self.make_ranked_pool(2)
        client = Client()
        client.force_login(self.director)
        url = reverse(
            'rooms:room_slot_auto_assign',
            kwargs={'project_id': self.project.id, 'slot_id': self.slot.id},
        )

        client.post(url)
        client.post(url)

        self.assertEqual(
            RoomMember.objects.filter(room=self.room, function_slot=self.slot).count(),
            1,
        )

    def test_second_assignment_of_same_candidate_is_rejected(self):
        profile = self.make_profile(self.users[0])
        assign_candidate_to_slot(self.slot, profile.user, self.director)
        other = self.second_slot()

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(other, profile.user, self.director)

        self.assertEqual(
            RoomMember.objects.filter(room=self.room, user=profile.user).count(),
            1,
        )
