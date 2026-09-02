"""Read-only Matching Engine: hard filters, исключения, история, ranking, API.

Проверяется только подбор кандидатов на `RoomFunctionSlot`. Назначение в
комнату, запись истории кандидатов, «Другой сейлер» и переходы статуса
проекта живут в `tests_staffing`; здесь остаётся один контракт — какой пул
и в каком порядке движок отдаёт, ничего при этом не меняя.

Границы модулей (ADR-001) проверяются в `apps.profiles.tests_boundaries`
и здесь не дублируются.
"""

from decimal import Decimal

from django.db.models import QuerySet
from django.test import TestCase

from apps.profiles.models import FreelancerProfile
from apps.rooms.models import (
    Project,
    Room,
    RoomFunctionSlot,
    RoomMember,
    RoomSlotCandidate,
)
from apps.rooms.staffing import get_best_candidate, get_next_candidate
from apps.rooms.staffing.matching import get_ranked_candidates
from apps.test_helpers import make_director, make_user
from apps.users.models import User

VIDEO_URL = 'https://youtu.be/demo-presentation'


class MatchingTestCase(TestCase):
    """Общая заготовка: комната со слотом и пул пользователей без профилей.

    Пользователи создаются один раз на класс (хеширование пароля — самая
    дорогая часть), а профили собираются в каждом тесте под его сценарий.
    """

    @classmethod
    def setUpTestData(cls):
        cls.director = make_director(email='dir-matching@example.com')
        cls.project = Project.objects.create(
            owner=cls.director,
            name='Проект подбора',
            status=Project.Status.STAFFING,
        )
        cls.room = Room.objects.create(project=cls.project)
        cls.slot = RoomFunctionSlot.objects.create(
            room=cls.room,
            role_key='seller',
            slot_index=1,
        )
        cls.users = [
            make_user(email=f'cand{index}@example.com', role=User.Roles.FREELANCER)
            for index in range(1, 9)
        ]
        cls.pending_user = make_user(
            email='pending@example.com',
            role=User.Roles.FREELANCER,
            status=User.Status.PENDING,
        )
        cls.blocked_user = make_user(
            email='blocked@example.com',
            role=User.Roles.FREELANCER,
            status=User.Status.BLOCKED,
        )

    def make_profile(self, user, **overrides):
        """Профиль, по умолчанию проходящий все hard filters слота."""
        fields = {
            'level': FreelancerProfile.Level.MIDDLE,
            'is_available': True,
            'is_verified': True,
            'video_url': VIDEO_URL,
            # Оба канала True: тесты, не проверяющие канал, из-за него не падают.
            'does_cold_calling': True,
            'does_linkedin_outreach': True,
            'rating': Decimal('4.50'),
            'acceptance_rate': Decimal('90.00'),
            'experience_projects': 10,
        }
        fields.update(overrides)
        return FreelancerProfile.objects.create(user=user, **fields)

    def ranked(self, slot=None, **kwargs):
        return list(get_ranked_candidates(slot or self.slot, **kwargs))


class HardFilterTests(MatchingTestCase):
    """Кого движок в пул не берёт вообще."""

    def test_hard_filters_reject_inactive_unverified_videoless_wrong_grade_wrong_channel_and_room_members(self):
        # Слот требует MIDDLE и холодные звонки: годен ровно один профиль,
        # у каждого остального ровно одна дисквалифицирующая причина.
        self.assertEqual(self.slot.required_level, RoomFunctionSlot.Grade.MIDDLE)
        self.slot.required_channel = RoomFunctionSlot.Channel.COLD_CALLING
        self.slot.save(update_fields=['required_channel', 'updated_at'])

        fit = self.make_profile(self.users[0])
        rejected = {
            'user is pending': self.make_profile(self.pending_user),
            'user is blocked': self.make_profile(self.blocked_user),
            'not available': self.make_profile(self.users[1], is_available=False),
            'not verified': self.make_profile(self.users[2], is_verified=False),
            'no video': self.make_profile(self.users[3], video_url=''),
            'wrong grade': self.make_profile(
                self.users[4],
                level=FreelancerProfile.Level.JUNIOR,
            ),
            'wrong channel': self.make_profile(self.users[5], does_cold_calling=False),
            'already in this room': self.make_profile(self.users[6]),
        }
        RoomMember.objects.create(
            room=self.room,
            user=rejected['already in this room'].user,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )

        pool = self.ranked()
        self.assertEqual(pool, [fit])
        for reason, profile in rejected.items():
            with self.subTest(rejected=reason):
                self.assertNotIn(profile, pool)

    def test_closed_slot_has_no_candidates(self):
        """Слот с is_active=False в подборе не участвует (см. модель слота)."""
        self.make_profile(self.users[0])
        self.slot.is_active = False
        self.slot.save(update_fields=['is_active', 'updated_at'])

        self.assertEqual(self.ranked(), [])
        self.assertIsNone(get_best_candidate(self.slot))
        self.assertIsNone(get_next_candidate(self.slot))

    def test_member_of_another_room_is_not_excluded(self):
        other_project = Project.objects.create(
            owner=self.director,
            name='Другой проект',
            status=Project.Status.STAFFING,
        )
        other_room = Room.objects.create(project=other_project)
        busy_elsewhere = self.make_profile(self.users[0])
        RoomMember.objects.create(
            room=other_room,
            user=busy_elsewhere.user,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )

        self.assertEqual(self.ranked(), [busy_elsewhere])


class RankingTests(MatchingTestCase):
    """Порядок пула: рейтинг → acceptance → опыт."""

    def test_full_ranking_order(self):
        third = self.make_profile(
            self.users[0],
            rating=Decimal('4.00'),
            acceptance_rate=Decimal('99.00'),
            experience_projects=99,
        )
        second = self.make_profile(
            self.users[1],
            rating=Decimal('5.00'),
            acceptance_rate=Decimal('50.00'),
            experience_projects=99,
        )
        first = self.make_profile(
            self.users[2],
            rating=Decimal('5.00'),
            acceptance_rate=Decimal('80.00'),
            experience_projects=1,
        )

        self.assertEqual(self.ranked(), [first, second, third])

    def test_ranking_is_stable_for_equal_metrics(self):
        for user in self.users[:4]:
            self.make_profile(
                user,
                rating=Decimal('4.00'),
                acceptance_rate=Decimal('80.00'),
                experience_projects=7,
            )

        first_run = [profile.pk for profile in get_ranked_candidates(self.slot)]
        second_run = [profile.pk for profile in get_ranked_candidates(self.slot)]

        self.assertEqual(len(first_run), 4)
        self.assertEqual(first_run, second_run)
        self.assertEqual(get_best_candidate(self.slot).pk, first_run[0])


class CandidateHistoryTests(MatchingTestCase):
    """История показов сужает пул «следующего», но только своего слота."""

    def test_next_candidate_after_the_seen_ones(self):
        seen = self.make_profile(self.users[0], rating=Decimal('5.00'))
        fresh = self.make_profile(self.users[1], rating=Decimal('4.00'))
        RoomSlotCandidate.objects.create(slot=self.slot, candidate=seen.user)

        self.assertEqual(self.ranked(), [seen, fresh])
        self.assertEqual(self.ranked(exclude_seen=True), [fresh])
        self.assertEqual(get_best_candidate(self.slot), seen)
        self.assertEqual(get_next_candidate(self.slot), fresh)

    def test_history_of_another_slot_does_not_shrink_the_pool(self):
        neighbour_slot = RoomFunctionSlot.objects.create(
            room=self.room,
            role_key='seller',
            slot_index=2,
        )
        other_project = Project.objects.create(
            owner=self.director,
            name='Проект соседей',
            status=Project.Status.STAFFING,
        )
        foreign_slot = RoomFunctionSlot.objects.create(
            room=Room.objects.create(project=other_project),
            role_key='seller',
            slot_index=1,
        )
        seen_next_door = self.make_profile(self.users[0], rating=Decimal('5.00'))
        seen_in_another_room = self.make_profile(self.users[1], rating=Decimal('4.00'))
        RoomSlotCandidate.objects.create(
            slot=neighbour_slot,
            candidate=seen_next_door.user,
        )
        RoomSlotCandidate.objects.create(
            slot=foreign_slot,
            candidate=seen_in_another_room.user,
        )

        self.assertEqual(
            self.ranked(exclude_seen=True),
            [seen_next_door, seen_in_another_room],
        )
        self.assertEqual(get_next_candidate(self.slot), seen_next_door)

    def test_every_outcome_excludes_candidate_from_the_next_pool(self):
        outcomes = [
            RoomSlotCandidate.Outcome.SHOWN,
            RoomSlotCandidate.Outcome.ASSIGNED,
            RoomSlotCandidate.Outcome.SKIPPED,
            RoomSlotCandidate.Outcome.DECLINED,
        ]
        seen_profiles = []
        for index, outcome in enumerate(outcomes):
            profile = self.make_profile(self.users[index], rating=Decimal('5.00'))
            RoomSlotCandidate.objects.create(
                slot=self.slot,
                candidate=profile.user,
                outcome=outcome,
            )
            seen_profiles.append(profile)
        fresh = self.make_profile(self.users[4], rating=Decimal('3.00'))

        for outcome, profile in zip(outcomes, seen_profiles):
            with self.subTest(outcome=outcome):
                self.assertNotIn(profile, self.ranked(exclude_seen=True))
        self.assertEqual(self.ranked(exclude_seen=True), [fresh])
        self.assertEqual(get_next_candidate(self.slot), fresh)
        # Пул best-кандидата историю не учитывает: там всё ещё лидируют 5.00.
        self.assertIn(get_best_candidate(self.slot), seen_profiles)


class PublicApiTests(MatchingTestCase):
    """Публичный контракт `apps.rooms.staffing`: пул, лучший, следующий."""

    def test_ranked_candidates_is_a_lazy_queryset(self):
        queryset = get_ranked_candidates(self.slot)

        self.assertIsInstance(queryset, QuerySet)
        self.assertIsNone(queryset._result_cache)

    def test_best_candidate_is_first_of_ranked(self):
        self.make_profile(self.users[0], rating=Decimal('4.00'))
        top = self.make_profile(self.users[1], rating=Decimal('4.80'))

        self.assertEqual(get_best_candidate(self.slot), top)
        self.assertEqual(get_best_candidate(self.slot), self.ranked()[0])

    def test_empty_pool_returns_none_instead_of_raising(self):
        self.assertEqual(self.ranked(), [])
        self.assertIsNone(get_best_candidate(self.slot))
        self.assertIsNone(get_next_candidate(self.slot))

    def test_matching_has_no_side_effects(self):
        candidate = self.make_profile(self.users[0], rating=Decimal('4.90'))
        self.make_profile(self.users[1], rating=Decimal('4.10'))
        RoomSlotCandidate.objects.create(slot=self.slot, candidate=candidate.user)
        history_snapshot = list(
            RoomSlotCandidate.objects.values_list(
                'id', 'slot_id', 'candidate_id', 'outcome'
            )
        )
        members_before = RoomMember.objects.count()

        self.ranked()
        self.ranked(exclude_seen=True)
        get_best_candidate(self.slot)
        get_next_candidate(self.slot)

        self.project.refresh_from_db()
        self.slot.refresh_from_db()
        self.assertEqual(RoomMember.objects.count(), members_before)
        self.assertEqual(
            list(
                RoomSlotCandidate.objects.values_list(
                    'id', 'slot_id', 'candidate_id', 'outcome'
                )
            ),
            history_snapshot,
        )
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        self.assertIsNone(self.slot.assigned_member)
        self.assertTrue(self.slot.is_active)
