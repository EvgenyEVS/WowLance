"""Read-only Matching Engine: hard filters, исключения, история, ranking, API.

Проверяется только подбор кандидатов на `RoomFunctionSlot`. Назначение в
комнату, запись истории кандидатов, «Другой сейлер», переходы статуса проекта
и UI в этот этап не входят — их отсутствие тоже проверяется (движок read-only).
"""

import ast
from decimal import Decimal
from pathlib import Path

from django.db.models import QuerySet
from django.test import SimpleTestCase, TestCase

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
            for index in range(1, 7)
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

    @staticmethod
    def where_clause(queryset):
        """Условия и сортировка скомпилированного SQL, без списка колонок SELECT.

        `select_related` тянет в SELECT все колонки профиля, поэтому проверять
        «поле не участвует в подборе» можно только по этой части запроса.
        """
        return str(queryset.query).split(' WHERE ', 1)[1]


class HardFilterTests(MatchingTestCase):
    def test_active_user_passes_and_inactive_statuses_do_not(self):
        active = self.make_profile(self.users[0])
        self.make_profile(self.pending_user)
        self.make_profile(self.blocked_user)

        self.assertEqual(self.ranked(), [active])

    def test_unavailable_profile_does_not_pass(self):
        available = self.make_profile(self.users[0])
        self.make_profile(self.users[1], is_available=False)

        self.assertEqual(self.ranked(), [available])

    def test_unverified_profile_does_not_pass(self):
        verified = self.make_profile(self.users[0])
        self.make_profile(self.users[1], is_verified=False)

        self.assertEqual(self.ranked(), [verified])

    def test_empty_video_url_does_not_pass(self):
        with_video = self.make_profile(self.users[0])
        self.make_profile(self.users[1], video_url='')

        self.assertEqual(self.ranked(), [with_video])

    def test_only_required_grade_passes(self):
        middle = self.make_profile(self.users[0], level=FreelancerProfile.Level.MIDDLE)
        self.make_profile(self.users[1], level=FreelancerProfile.Level.JUNIOR)
        self.make_profile(self.users[2], level=FreelancerProfile.Level.SENIOR)

        self.assertEqual(self.slot.required_level, RoomFunctionSlot.Grade.MIDDLE)
        self.assertEqual(self.ranked(), [middle])

    def test_senior_slot_selects_senior_profile(self):
        senior = self.make_profile(self.users[0], level=FreelancerProfile.Level.SENIOR)
        self.make_profile(self.users[1], level=FreelancerProfile.Level.MIDDLE)
        self.slot.required_level = RoomFunctionSlot.Grade.SENIOR
        self.slot.save(update_fields=['required_level', 'updated_at'])

        self.assertEqual(self.ranked(), [senior])

    def test_cold_calling_slot_requires_cold_calling_flag(self):
        cold = self.make_profile(
            self.users[0],
            does_cold_calling=True,
            does_linkedin_outreach=False,
        )
        self.make_profile(
            self.users[1],
            does_cold_calling=False,
            does_linkedin_outreach=True,
        )
        self.slot.required_channel = RoomFunctionSlot.Channel.COLD_CALLING
        self.slot.save(update_fields=['required_channel', 'updated_at'])

        self.assertEqual(self.ranked(), [cold])

    def test_linkedin_slot_requires_linkedin_flag(self):
        linkedin = self.make_profile(
            self.users[0],
            does_cold_calling=False,
            does_linkedin_outreach=True,
        )
        self.make_profile(
            self.users[1],
            does_cold_calling=True,
            does_linkedin_outreach=False,
        )
        self.slot.required_channel = RoomFunctionSlot.Channel.LINKEDIN
        self.slot.save(update_fields=['required_channel', 'updated_at'])

        self.assertEqual(self.ranked(), [linkedin])

    def test_candidate_with_both_channels_fits_both_slots(self):
        universal = self.make_profile(
            self.users[0],
            does_cold_calling=True,
            does_linkedin_outreach=True,
        )
        cold_slot = RoomFunctionSlot.objects.create(
            room=self.room,
            role_key='seller',
            slot_index=2,
            required_channel=RoomFunctionSlot.Channel.COLD_CALLING,
        )
        linkedin_slot = RoomFunctionSlot.objects.create(
            room=self.room,
            role_key='seller',
            slot_index=3,
            required_channel=RoomFunctionSlot.Channel.LINKEDIN,
        )

        self.assertEqual(self.ranked(cold_slot), [universal])
        self.assertEqual(self.ranked(linkedin_slot), [universal])

    def test_any_channel_slot_does_not_restrict_by_channel(self):
        cold_only = self.make_profile(
            self.users[0],
            does_cold_calling=True,
            does_linkedin_outreach=False,
            rating=Decimal('5.00'),
        )
        linkedin_only = self.make_profile(
            self.users[1],
            does_cold_calling=False,
            does_linkedin_outreach=True,
            rating=Decimal('4.00'),
        )
        no_channel = self.make_profile(
            self.users[2],
            does_cold_calling=False,
            does_linkedin_outreach=False,
            rating=Decimal('3.00'),
        )

        self.assertEqual(self.slot.required_channel, RoomFunctionSlot.Channel.ANY)
        self.assertEqual(self.ranked(), [cold_only, linkedin_only, no_channel])

    def test_skills_json_does_not_affect_matching(self):
        with_skills = self.make_profile(
            self.users[0],
            skills=['Холодные звонки', 'LinkedIn', 'SPIN'],
            rating=Decimal('4.00'),
        )
        without_skills = self.make_profile(
            self.users[1],
            skills=[],
            rating=Decimal('3.00'),
        )
        irrelevant_skills = self.make_profile(
            self.users[2],
            skills=['Вёрстка'],
            rating=Decimal('2.00'),
        )

        self.assertEqual(
            self.ranked(),
            [with_skills, without_skills, irrelevant_skills],
        )
        self.assertNotIn('skills', self.where_clause(get_ranked_candidates(self.slot)))

    def test_closed_slot_has_no_candidates(self):
        """Слот с is_active=False в подборе не участвует (см. модель слота)."""
        self.make_profile(self.users[0])
        self.slot.is_active = False
        self.slot.save(update_fields=['is_active', 'updated_at'])

        self.assertEqual(self.ranked(), [])
        self.assertIsNone(get_best_candidate(self.slot))
        self.assertIsNone(get_next_candidate(self.slot))


class RoomExclusionTests(MatchingTestCase):
    def test_existing_room_member_is_excluded(self):
        member_profile = self.make_profile(self.users[0], rating=Decimal('5.00'))
        free_profile = self.make_profile(self.users[1], rating=Decimal('4.00'))
        RoomMember.objects.create(
            room=self.room,
            user=member_profile.user,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )

        self.assertEqual(self.ranked(), [free_profile])

    def test_member_occupying_another_slot_of_same_room_is_excluded(self):
        other_slot = RoomFunctionSlot.objects.create(
            room=self.room,
            role_key='seller',
            slot_index=2,
        )
        busy_profile = self.make_profile(self.users[0], rating=Decimal('5.00'))
        free_profile = self.make_profile(self.users[1], rating=Decimal('4.00'))
        RoomMember.objects.create(
            room=self.room,
            user=busy_profile.user,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=other_slot,
        )

        self.assertEqual(self.ranked(), [free_profile])

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


class CandidateHistoryTests(MatchingTestCase):
    def test_candidate_with_history_on_this_slot_is_excluded_from_next_pool(self):
        seen = self.make_profile(self.users[0], rating=Decimal('5.00'))
        fresh = self.make_profile(self.users[1], rating=Decimal('4.00'))
        RoomSlotCandidate.objects.create(slot=self.slot, candidate=seen.user)

        self.assertEqual(self.ranked(), [seen, fresh])
        self.assertEqual(self.ranked(exclude_seen=True), [fresh])
        self.assertEqual(get_best_candidate(self.slot), seen)
        self.assertEqual(get_next_candidate(self.slot), fresh)

    def test_history_of_another_slot_does_not_exclude_candidate(self):
        other_slot = RoomFunctionSlot.objects.create(
            room=self.room,
            role_key='seller',
            slot_index=2,
        )
        candidate = self.make_profile(self.users[0])
        RoomSlotCandidate.objects.create(slot=other_slot, candidate=candidate.user)

        self.assertEqual(self.ranked(exclude_seen=True), [candidate])
        self.assertEqual(get_next_candidate(self.slot), candidate)
        self.assertIsNone(get_next_candidate(other_slot))

    def test_history_of_slot_in_another_room_does_not_exclude_candidate(self):
        other_project = Project.objects.create(
            owner=self.director,
            name='Проект соседей',
            status=Project.Status.STAFFING,
        )
        other_room = Room.objects.create(project=other_project)
        foreign_slot = RoomFunctionSlot.objects.create(
            room=other_room,
            role_key='seller',
            slot_index=1,
        )
        candidate = self.make_profile(self.users[0])
        RoomSlotCandidate.objects.create(slot=foreign_slot, candidate=candidate.user)

        self.assertEqual(get_next_candidate(self.slot), candidate)

    def test_every_outcome_excludes_candidate_from_next_pool(self):
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

        self.assertEqual(self.ranked(exclude_seen=True), [fresh])
        self.assertEqual(get_next_candidate(self.slot), fresh)
        # Пул best-кандидата историю не учитывает: там всё ещё лидируют 5.00.
        self.assertIn(get_best_candidate(self.slot), seen_profiles)


class RankingTests(MatchingTestCase):
    def test_higher_rating_goes_first(self):
        low = self.make_profile(self.users[0], rating=Decimal('3.10'))
        high = self.make_profile(self.users[1], rating=Decimal('4.90'))

        self.assertEqual(self.ranked(), [high, low])

    def test_acceptance_rate_breaks_equal_rating(self):
        worse = self.make_profile(
            self.users[0],
            rating=Decimal('4.50'),
            acceptance_rate=Decimal('70.00'),
        )
        better = self.make_profile(
            self.users[1],
            rating=Decimal('4.50'),
            acceptance_rate=Decimal('95.50'),
        )

        self.assertEqual(self.ranked(), [better, worse])

    def test_experience_projects_breaks_equal_rating_and_acceptance(self):
        fewer_projects = self.make_profile(
            self.users[0],
            rating=Decimal('4.50'),
            acceptance_rate=Decimal('90.00'),
            experience_projects=3,
        )
        more_projects = self.make_profile(
            self.users[1],
            rating=Decimal('4.50'),
            acceptance_rate=Decimal('90.00'),
            experience_projects=42,
        )

        self.assertEqual(self.ranked(), [more_projects, fewer_projects])

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

    def test_equal_metrics_keep_stable_order(self):
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

    def test_ordering_is_done_by_database(self):
        sql = str(get_ranked_candidates(self.slot).query)
        order_by = sql.split('ORDER BY')[-1]

        self.assertIn('ORDER BY', sql)
        self.assertLess(order_by.index('rating'), order_by.index('acceptance_rate'))
        self.assertLess(
            order_by.index('acceptance_rate'),
            order_by.index('experience_projects'),
        )
        self.assertLess(order_by.index('experience_projects'), order_by.rindex('id'))
        self.assertEqual(order_by.count('DESC'), 3)


class PublicApiTests(MatchingTestCase):
    def test_best_candidate_is_first_of_ranked(self):
        self.make_profile(self.users[0], rating=Decimal('4.00'))
        top = self.make_profile(self.users[1], rating=Decimal('4.80'))

        self.assertEqual(get_best_candidate(self.slot), top)
        self.assertEqual(get_best_candidate(self.slot), self.ranked()[0])

    def test_empty_pool_returns_none_instead_of_raising(self):
        self.assertEqual(self.ranked(), [])
        self.assertIsNone(get_best_candidate(self.slot))
        self.assertIsNone(get_next_candidate(self.slot))

    def test_next_candidate_is_none_when_everyone_is_already_in_history(self):
        only = self.make_profile(self.users[0])
        RoomSlotCandidate.objects.create(slot=self.slot, candidate=only.user)

        self.assertEqual(get_best_candidate(self.slot), only)
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


class MatchingQuerysetTests(MatchingTestCase):
    def test_hard_filters_are_executed_by_sql(self):
        where = self.where_clause(get_ranked_candidates(self.slot, exclude_seen=True))

        for column in ('status', 'is_available', 'is_verified', 'video_url', 'level'):
            self.assertIn(column, where)
        # Оба исключения — подзапросы БД, а не фильтрация списка в Python.
        self.assertEqual(where.count('NOT EXISTS'), 2)
        self.assertIn('rooms_roommember', where)
        self.assertIn('rooms_roomslotcandidate', where)

    def test_candidate_history_is_not_joined_for_best_candidate(self):
        where = self.where_clause(get_ranked_candidates(self.slot))

        self.assertEqual(where.count('NOT EXISTS'), 1)
        self.assertIn('rooms_roommember', where)
        self.assertNotIn('rooms_roomslotcandidate', where)

    def test_channel_requirement_is_executed_by_sql(self):
        self.slot.required_channel = RoomFunctionSlot.Channel.COLD_CALLING
        cold_where = self.where_clause(get_ranked_candidates(self.slot))
        self.slot.required_channel = RoomFunctionSlot.Channel.LINKEDIN
        linkedin_where = self.where_clause(get_ranked_candidates(self.slot))
        self.slot.required_channel = RoomFunctionSlot.Channel.ANY
        any_where = self.where_clause(get_ranked_candidates(self.slot))

        self.assertIn('does_cold_calling', cold_where)
        self.assertNotIn('does_linkedin_outreach', cold_where)
        self.assertIn('does_linkedin_outreach', linkedin_where)
        self.assertNotIn('does_cold_calling', linkedin_where)
        self.assertNotIn('does_cold_calling', any_where)
        self.assertNotIn('does_linkedin_outreach', any_where)

    def test_result_is_lazy_queryset(self):
        queryset = get_ranked_candidates(self.slot)

        self.assertIsInstance(queryset, QuerySet)
        self.assertIsNone(queryset._result_cache)

    def test_best_candidate_is_selected_by_database_limit(self):
        for index, user in enumerate(self.users[:3]):
            self.make_profile(user, rating=Decimal('3.00') + index)

        with self.assertNumQueries(1):
            best = get_best_candidate(self.slot)
        self.assertEqual(best.rating, Decimal('5.00'))

    def test_user_is_prefetched_without_n_plus_one(self):
        for user in self.users[:3]:
            self.make_profile(user)

        with self.assertNumQueries(1):
            names = [
                profile.user.full_name for profile in get_ranked_candidates(self.slot)
            ]
        self.assertEqual(len(names), 3)


class MatchingBoundaryTests(SimpleTestCase):
    """Границы модулей (ADR-001) для кода подбора."""

    APPS_DIR = Path(__file__).resolve().parent.parent

    @staticmethod
    def _imported_modules(path: Path):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield alias.name
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield node.module

    def _offenders(self, package_dir: Path, forbidden):
        offenders = []
        for path in sorted(package_dir.rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            for module in self._imported_modules(path):
                if any(
                    module == name or module.startswith(name + '.')
                    for name in forbidden
                ):
                    offenders.append(f'{path.relative_to(self.APPS_DIR)}: {module}')
        return offenders

    def test_staffing_does_not_import_pipeline(self):
        offenders = self._offenders(
            self.APPS_DIR / 'rooms' / 'staffing',
            ('apps.pipeline',),
        )

        self.assertEqual(
            offenders,
            [],
            'Подбор не должен зависеть от pipeline: ' + ', '.join(offenders),
        )

    def test_profiles_does_not_import_rooms(self):
        offenders = self._offenders(
            self.APPS_DIR / 'profiles',
            ('apps.rooms', 'apps.pipeline'),
        )

        self.assertEqual(
            offenders,
            [],
            'BIZ не должен зависеть от ROOM: ' + ', '.join(offenders),
        )

    def test_channel_filters_live_only_in_matching_module(self):
        """ROOM не дублирует фильтры подбора во views/services.

        Сами поля принадлежат BIZ (`apps.profiles`) — там они и упоминаются
        легально. Проверяем сторону ROOM: единственное место, где ROOM знает
        о каналах фрилансера, — `matching.py`.
        """
        markers = ('does_cold_calling', 'does_linkedin_outreach')
        allowed = {
            Path('rooms/staffing/matching.py'),
            Path('rooms/tests_staffing_matching.py'),
        }
        offenders = []
        for app_name in ('rooms', 'pipeline', 'core', 'users'):
            for path in sorted((self.APPS_DIR / app_name).rglob('*.py')):
                if {'__pycache__', 'migrations'} & set(path.parts):
                    continue
                relative = path.relative_to(self.APPS_DIR)
                if relative in allowed:
                    continue
                source = path.read_text(encoding='utf-8')
                if any(marker in source for marker in markers):
                    offenders.append(str(relative))

        self.assertEqual(
            offenders,
            [],
            'Фильтр по каналу должен жить только в matching.py: ' + ', '.join(offenders),
        )
