"""Авто-подбор исполнителя на слот, открытый сохранением состава команды.

Что проверяется
---------------

Продуктовый сценарий демо: директор покупает функции, и система сама сажает
лучшего кандидата на каждый **только что появившийся** слот — без кнопки
«Подобрать лучшего». Точка входа одна:
`apps.rooms.services.save_functional_roles_and_sync_slots`, поэтому и ручное
изменение состава, и применение пакета проверяются через неё.

Что здесь сознательно **не** проверяется:

* правила подбора (hard filters, ranking) — у Matching Engine свои тесты
  (`tests_staffing_matching`), и формула порядка сюда не копируется: там,
  где нужен «top-1», ответ спрашивается у самого движка;
* жизненный цикл слотов (создание, реактивация, закрытие) — это
  `tests_functional_role_projection`;
* переход проекта в ACTIVE — это `tests_automation_sla`; здесь проверяется
  только то, что авто-подбор его **не** делает.

Границы модулей (ADR-001) остаются прежними: назначение идёт исключительно
через `staffing.services.auto_assign_best_candidate`, `projection` никого не
назначает, `matching` ничего не пишет.
"""

import inspect
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from apps.profiles.models import FreelancerProfile
from apps.rooms import presets
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
from apps.rooms.staffing import matching, projection
from apps.rooms.staffing.services import STAFFING_MUTABLE_STATUSES
from apps.rooms.unit_economics import COMPOSITION_EDITABLE_STATUSES
from apps.test_helpers import make_director, make_user
from apps.users.models import User

VIDEO_URL = 'https://youtu.be/auto-assign-demo'

#: Признаки каналов берутся из таблицы Matching Engine, а не пишутся здесь
#: строками: связь «канал слота → поле профиля» обязана существовать в одном
#: месте, и тест не должен становиться её второй копией
#: (`MatchingBoundaryTests.test_channel_filters_live_only_in_matching_module`).
COLD_CALLING_FIELD = matching.CHANNEL_REQUIREMENTS[
    RoomFunctionSlot.Channel.COLD_CALLING
]
LINKEDIN_FIELD = matching.CHANNEL_REQUIREMENTS[RoomFunctionSlot.Channel.LINKEDIN]

#: Путь для `mock.patch`. Оркестрация импортирует сервис **внутри функции**,
#: поэтому подменять нужно атрибут самого модуля подбора: копии имени в
#: `apps.rooms.services` не существует, а импорт разрешается на каждом вызове.
AUTO_ASSIGN_PATH = 'apps.rooms.staffing.services.auto_assign_best_candidate'


class AutoAssignOnSlotCreateTestCase(TestCase):
    """Проект директора в подборе с открытой комнатой и управляемым пулом."""

    def setUp(self):
        self.director = make_director(email='dir@autoassign.test')
        self.project = Project.objects.create(
            owner=self.director,
            name='Авто-подбор при создании слота',
            status=Project.Status.STAFFING,
            input_data={'offer': 'Оффер', 'hot_criteria': 'Критерии'},
        )
        self.room = ensure_room_for_project(self.project)
        self._candidate_seq = 0

    # --- пул кандидатов ---------------------------------------------------

    def make_candidate(
        self,
        *,
        level=FreelancerProfile.Level.MIDDLE,
        cold=False,
        linkedin=False,
        rating='4.00',
        **overrides,
    ):
        """Фрилансер с профилем, проходящим hard filters нужного канала.

        Поля профиля перечисляются явно, а не берутся из общей фабрики:
        тест обязан сам отвечать за то, подходит кандидат или нет, иначе
        «пустой пул» и «кандидат есть» перестанут быть различимы.
        """
        self._candidate_seq += 1
        user = make_user(
            email=f'cand{self._candidate_seq}@autoassign.test',
            role=User.Roles.FREELANCER,
            first_name=f'Кандидат{self._candidate_seq}',
            last_name='Тестовый',
        )
        fields = {
            'level': level,
            'is_available': True,
            'is_verified': True,
            'video_url': VIDEO_URL,
            'rating': Decimal(rating),
            'acceptance_rate': Decimal('90.00'),
            'experience_projects': 10,
            COLD_CALLING_FIELD: cold,
            LINKEDIN_FIELD: linkedin,
        }
        fields.update(overrides)
        FreelancerProfile.objects.create(user=user, **fields)
        return user

    # --- продуктовые действия ---------------------------------------------

    def save(self, **counts):
        """Сохранение состава продуктовой оркестрацией."""
        counts.setdefault('teamlead', 1)
        return save_functional_roles_and_sync_slots(
            self.project,
            [{'role_key': key, 'count': value} for key, value in counts.items()],
            self.director,
        )

    # --- состояние комнаты ------------------------------------------------

    def slots(self, role_key=None, *, is_active=None):
        queryset = RoomFunctionSlot.objects.filter(room=self.room)
        if role_key is not None:
            queryset = queryset.filter(role_key=role_key)
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)
        return list(queryset.order_by('role_key', 'slot_index'))

    def slot(self, role_key, slot_index=1):
        return RoomFunctionSlot.objects.get(
            room=self.room, role_key=role_key, slot_index=slot_index
        )

    def slot_members(self):
        """Участники, занимающие функциональные слоты этой комнаты."""
        return list(
            RoomMember.objects.filter(
                room=self.room, function_slot__isnull=False
            ).select_related('user', 'function_slot')
        )

    def member_on(self, slot):
        return RoomMember.objects.filter(function_slot=slot).first()

    def reference_best_candidate(self, *, level, channel):
        """Top-1 пула по мнению самого Matching Engine.

        Слот-эталон живёт в отдельной комнате: назначения основного проекта
        сузили бы его пул (вошедший в комнату кандидат из подбора выпадает),
        и сравнивать было бы уже не с чем. Порядок ranking тест не
        воспроизводит — он его спрашивает.
        """
        reference_project = Project.objects.create(
            owner=self.director,
            name='Эталон ранжирования',
            status=Project.Status.STAFFING,
        )
        reference_room = ensure_room_for_project(reference_project)
        reference_slot = RoomFunctionSlot.objects.create(
            room=reference_room,
            role_key='seller_middle',
            slot_index=1,
            required_level=level,
            required_channel=channel,
        )
        return matching.get_best_candidate(reference_slot)


# ---------------------------------------------------------------------------
# 1-2. Новый слот получает лучшего кандидата
# ---------------------------------------------------------------------------


class NewSlotAutoAssignTests(AutoAssignOnSlotCreateTestCase):
    def test_new_middle_slot_gets_the_candidate(self):
        """1. +1 seller_middle → слот создан и на нём сидит подходящий сейл."""
        candidate = self.make_candidate(cold=True)

        self.save(seller_middle=1)

        slot = self.slot('seller_middle')
        self.assertTrue(slot.is_active)
        member = self.member_on(slot)
        self.assertIsNotNone(member)
        self.assertEqual(member.user_id, candidate.id)

    def test_member_is_linked_to_that_exact_slot(self):
        """1. Связь участника со слотом, а не «просто добавлен в комнату»."""
        self.make_candidate(cold=True)

        self.save(seller_middle=1)

        slot = self.slot('seller_middle')
        members = self.slot_members()
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].function_slot_id, slot.id)
        self.assertEqual(members[0].room_id, self.room.id)

    def test_auto_assigned_member_is_a_freelancer_pending_readiness(self):
        """Авто-подбор не подтверждает готовность за человека."""
        self.make_candidate(cold=True)

        self.save(seller_middle=1)

        member = self.member_on(self.slot('seller_middle'))
        self.assertEqual(member.role_in_room, RoomMember.RoleInRoom.FREELANCER)
        self.assertEqual(member.ready_status, RoomMember.ReadyStatus.PENDING)

    def test_assignment_is_written_to_slot_history(self):
        """Назначение идёт штатным сервисом: история кандидата записана."""
        candidate = self.make_candidate(cold=True)

        self.save(seller_middle=1)

        record = RoomSlotCandidate.objects.get(
            slot=self.slot('seller_middle'), candidate=candidate
        )
        self.assertEqual(record.outcome, RoomSlotCandidate.Outcome.ASSIGNED)

    def test_top1_is_the_one_matching_engine_ranks_first(self):
        """2. Из нескольких подходящих назначается top-1 самого движка."""
        self.make_candidate(cold=True, rating='3.10')
        self.make_candidate(cold=True, rating='4.90')
        self.make_candidate(cold=True, rating='4.20')

        expected = self.reference_best_candidate(
            level=RoomFunctionSlot.Grade.MIDDLE,
            channel=RoomFunctionSlot.Channel.COLD_CALLING,
        )
        self.assertIsNotNone(expected)

        self.save(seller_middle=1)

        member = self.member_on(self.slot('seller_middle'))
        self.assertEqual(member.user_id, expected.user_id)

    def test_two_new_slots_get_two_different_people(self):
        """Пул общий: один человек не занимает два слота сразу."""
        self.make_candidate(cold=True, rating='4.90')
        self.make_candidate(cold=True, rating='4.50')

        self.save(seller_middle=2)

        members = self.slot_members()
        self.assertEqual(len(members), 2)
        self.assertEqual(len({member.user_id for member in members}), 2)
        self.assertEqual(
            sorted(member.function_slot.slot_index for member in members), [1, 2]
        )

    def test_unverified_candidate_is_never_auto_assigned(self):
        """Hard filters работают и на этом пути: модерация не обходится."""
        self.make_candidate(cold=True, is_verified=False)

        self.save(seller_middle=1)

        self.assertIsNone(self.member_on(self.slot('seller_middle')))


# ---------------------------------------------------------------------------
# 3. Пустой пул
# ---------------------------------------------------------------------------


class EmptyPoolTests(AutoAssignOnSlotCreateTestCase):
    def test_slot_is_created_even_without_candidates(self):
        """3. Некому — слот всё равно куплен и открыт."""
        self.save(seller_middle=1)

        self.assertTrue(self.slot('seller_middle').is_active)

    def test_no_member_appears_from_an_empty_pool(self):
        """3. Пустой пул никого не сажает и никого не выдумывает."""
        self.save(seller_middle=1)

        self.assertEqual(self.slot_members(), [])
        self.assertEqual(RoomSlotCandidate.objects.count(), 0)

    def test_composition_is_saved_despite_empty_pool(self):
        """3. Отсутствие кандидатов — не ошибка: состав и бюджет сохранены."""
        result = self.save(seller_middle=1)

        self.project.refresh_from_db()
        self.assertEqual(result.projection.created_count, 1)
        self.assertEqual(self.project.budget, result.summary.total_budget)
        self.assertGreater(self.project.budget, 0)

    def test_only_unsuitable_candidates_leave_the_slot_empty(self):
        """Кандидат не того канала слот не занимает."""
        self.make_candidate(linkedin=True, cold=False)

        self.save(seller_middle=1)

        self.assertIsNone(self.member_on(self.slot('seller_middle')))


# ---------------------------------------------------------------------------
# 4. Идемпотентность повторного сохранения
# ---------------------------------------------------------------------------


class RepeatedSaveTests(AutoAssignOnSlotCreateTestCase):
    def test_second_identical_save_creates_no_new_slot_or_member(self):
        """4. Повторное сохранение того же состава ничего не добавляет."""
        self.make_candidate(cold=True)
        self.make_candidate(cold=True)

        self.save(seller_middle=1)
        slots_before = {slot.id for slot in self.slots()}
        members_before = {member.id for member in self.slot_members()}

        self.save(seller_middle=1)

        self.assertEqual({slot.id for slot in self.slots()}, slots_before)
        self.assertEqual(
            {member.id for member in self.slot_members()}, members_before
        )

    def test_auto_assign_is_not_called_again_on_unchanged_composition(self):
        """4. Без новых слотов подбор не вызывается вообще, а не «вхолостую»."""
        self.make_candidate(cold=True)
        self.save(seller_middle=1)

        with mock.patch(AUTO_ASSIGN_PATH) as auto_assign:
            self.save(seller_middle=1)

        auto_assign.assert_not_called()

    def test_occupied_slot_is_not_reassigned_by_a_later_save(self):
        """Занятый слот не переназначается следующим сохранением состава."""
        self.make_candidate(cold=True, rating='4.10')
        self.save(seller_middle=1)
        member_before = self.member_on(self.slot('seller_middle'))

        # Более сильный кандидат появляется уже после назначения.
        self.make_candidate(cold=True, rating='4.99')
        self.save(seller_middle=1)

        member_after = self.member_on(self.slot('seller_middle'))
        self.assertEqual(member_after.id, member_before.id)


# ---------------------------------------------------------------------------
# 5. Уменьшение состава и закрытие функции
# ---------------------------------------------------------------------------


class DecreaseTests(AutoAssignOnSlotCreateTestCase):
    def test_decrease_does_not_call_auto_assign(self):
        """5. Уменьшение количества подбор не запускает."""
        self.save(seller_senior=2)

        with mock.patch(AUTO_ASSIGN_PATH) as auto_assign:
            self.save(seller_senior=1)

        auto_assign.assert_not_called()

    def test_deactivated_slot_gets_no_member(self):
        """5. Закрытый слот кандидата не получает даже при живом пуле."""
        self.save(seller_senior=2)
        self.make_candidate(level=FreelancerProfile.Level.SENIOR, cold=True)
        self.make_candidate(level=FreelancerProfile.Level.SENIOR, cold=True)

        self.save(seller_senior=1)

        closed = self.slot('seller_senior', slot_index=2)
        self.assertFalse(closed.is_active)
        self.assertIsNone(self.member_on(closed))

    def test_removing_a_role_calls_no_auto_assign(self):
        """9. Полное удаление функции из состава — тоже только закрытие."""
        self.save(seller_middle=1)

        with mock.patch(AUTO_ASSIGN_PATH) as auto_assign:
            self.save(seller_middle=0)

        auto_assign.assert_not_called()
        self.assertEqual(self.slots('seller_middle', is_active=True), [])


# ---------------------------------------------------------------------------
# 6. Реактивация закрытого слота
# ---------------------------------------------------------------------------


class ReactivationTests(AutoAssignOnSlotCreateTestCase):
    def reopen(self):
        """Слот куплен без кандидатов и затем закрыт."""
        self.save(seller_middle=1)
        self.save(seller_middle=0)

    def test_reactivated_slot_is_treated_as_newly_opened(self):
        """6. Вернувшийся слот — это открытие: подбор на него идёт."""
        self.reopen()
        candidate = self.make_candidate(cold=True)

        self.save(seller_middle=1)

        slot = self.slot('seller_middle')
        self.assertTrue(slot.is_active)
        member = self.member_on(slot)
        self.assertIsNotNone(member)
        self.assertEqual(member.user_id, candidate.id)

    def test_reactivation_reuses_the_slot_instead_of_creating_a_second_one(self):
        """6. Подбор идёт на тот же slot_index, второго слота не появляется."""
        self.reopen()
        self.make_candidate(cold=True)

        result = self.save(seller_middle=1)

        change = next(
            item for item in result.projection.changes
            if item.role_key == 'seller_middle'
        )
        self.assertEqual(change.reactivated, (1,))
        self.assertEqual(change.created, ())
        self.assertEqual(len(self.slots('seller_middle')), 1)

    def test_reactivation_does_not_reoffer_a_skipped_candidate(self):
        """6. История слота уважается: пропущенного повторно не предлагают.

        Правило живёт в `auto_assign_best_candidate` (`get_next_candidate`
        при непустой истории) — оркестрация его не переопределяет и не
        дублирует.
        """
        skipped = self.make_candidate(cold=True, rating='4.90')
        fresh = self.make_candidate(cold=True, rating='4.10')

        self.save(seller_middle=1)
        slot = self.slot('seller_middle')
        RoomMember.objects.filter(function_slot=slot).delete()
        RoomSlotCandidate.objects.filter(slot=slot, candidate=skipped).update(
            outcome=RoomSlotCandidate.Outcome.SKIPPED
        )
        self.save(seller_middle=0)

        self.save(seller_middle=1)

        member = self.member_on(self.slot('seller_middle'))
        self.assertIsNotNone(member)
        self.assertEqual(member.user_id, fresh.id)


# ---------------------------------------------------------------------------
# 7. Тимлид
# ---------------------------------------------------------------------------


class TeamleadTests(AutoAssignOnSlotCreateTestCase):
    def test_teamlead_in_composition_creates_no_slot(self):
        """7. Тимлид есть в составе, слота у него нет."""
        self.save(teamlead=1, seller_middle=1)

        self.assertEqual(self.slots('teamlead'), [])

    def test_teamlead_is_never_auto_assigned(self):
        """7. Ручной поток тимлида подбор не подменяет."""
        self.make_candidate(cold=True)

        self.save(teamlead=1)

        self.assertEqual(self.slot_members(), [])
        self.assertEqual(RoomSlotCandidate.objects.count(), 0)

    def test_auto_assign_is_never_called_for_teamlead_slots(self):
        """7. В подбор не уходит ни один слот с ключом тимлида."""
        self.make_candidate(cold=True)

        with mock.patch(AUTO_ASSIGN_PATH) as auto_assign:
            self.save(teamlead=1, seller_middle=1)

        role_keys = {call.args[0].role_key for call in auto_assign.call_args_list}
        self.assertEqual(role_keys, {'seller_middle'})


# ---------------------------------------------------------------------------
# 8. LinkedIn
# ---------------------------------------------------------------------------


class LinkedInSlotTests(AutoAssignOnSlotCreateTestCase):
    def test_linkedin_slot_gets_a_linkedin_candidate(self):
        """8. Новый linkedin_leadgen занимается сам."""
        candidate = self.make_candidate(linkedin=True)

        self.save(linkedin_leadgen=1)

        member = self.member_on(self.slot('linkedin_leadgen'))
        self.assertIsNotNone(member)
        self.assertEqual(member.user_id, candidate.id)

    def test_cold_only_candidate_does_not_take_a_linkedin_slot(self):
        """8. Канал слота решает, а не рейтинг: cold-сейл сюда не садится."""
        self.make_candidate(cold=True, linkedin=False, rating='5.00')
        linkedin_candidate = self.make_candidate(linkedin=True, rating='3.00')

        self.save(linkedin_leadgen=1)

        member = self.member_on(self.slot('linkedin_leadgen'))
        self.assertEqual(member.user_id, linkedin_candidate.id)

    def test_mixed_composition_fills_each_slot_from_its_own_channel(self):
        """Смешанный состав: каждый слот получает исполнителя своего канала."""
        cold_candidate = self.make_candidate(cold=True)
        linkedin_candidate = self.make_candidate(linkedin=True)

        self.save(seller_middle=1, linkedin_leadgen=1)

        self.assertEqual(
            self.member_on(self.slot('seller_middle')).user_id, cold_candidate.id
        )
        self.assertEqual(
            self.member_on(self.slot('linkedin_leadgen')).user_id,
            linkedin_candidate.id,
        )


# ---------------------------------------------------------------------------
# 9. Статус проекта
# ---------------------------------------------------------------------------


class ProjectStatusTests(AutoAssignOnSlotCreateTestCase):
    def test_project_stays_in_staffing_after_auto_assign(self):
        """9. Назначение не активирует проект."""
        self.make_candidate(cold=True)

        self.save(seller_middle=1)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)

    def test_full_composition_without_readiness_stays_in_staffing(self):
        """9. Даже когда все слоты заняты, ACTIVE без готовности не наступает."""
        self.make_candidate(cold=True)
        self.make_candidate(linkedin=True)

        self.save(seller_middle=1, linkedin_leadgen=1)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.STAFFING)
        members = self.slot_members()
        self.assertEqual(len(members), 2)
        self.assertTrue(
            all(
                member.ready_status == RoomMember.ReadyStatus.PENDING
                for member in members
            )
        )


# ---------------------------------------------------------------------------
# 10. Старый пустой слот (регрессия)
# ---------------------------------------------------------------------------


class OldEmptySlotRegressionTests(AutoAssignOnSlotCreateTestCase):
    def setUp(self):
        super().setUp()
        # Слот куплен, когда подходящих кандидатов не было, — и остался пустым.
        self.save(seller_middle=1)
        self.old_slot = self.slot('seller_middle')
        self.assertIsNone(self.member_on(self.old_slot))

    def test_old_empty_slot_is_not_filled_by_a_later_save_of_another_role(self):
        """10. Покупка другой функции не заполняет задним числом старый слот."""
        self.make_candidate(cold=True)
        self.make_candidate(linkedin=True)

        self.save(seller_middle=1, linkedin_leadgen=1)

        self.assertIsNone(self.member_on(self.old_slot))

    def test_the_newly_opened_slot_of_another_role_is_filled(self):
        """10. При этом новый слот другой функции подбор получает."""
        self.make_candidate(cold=True)
        linkedin_candidate = self.make_candidate(linkedin=True)

        self.save(seller_middle=1, linkedin_leadgen=1)

        member = self.member_on(self.slot('linkedin_leadgen'))
        self.assertIsNotNone(member)
        self.assertEqual(member.user_id, linkedin_candidate.id)

    def test_auto_assign_is_called_only_for_the_new_slot(self):
        """10. Старый слот в подбор не передаётся вообще."""
        self.make_candidate(cold=True)
        self.make_candidate(linkedin=True)

        with mock.patch(AUTO_ASSIGN_PATH) as auto_assign:
            self.save(seller_middle=1, linkedin_leadgen=1)

        passed = [call.args[0].id for call in auto_assign.call_args_list]
        self.assertEqual(passed, [self.slot('linkedin_leadgen').id])

    def test_second_seller_slot_does_not_pull_the_first_one_in(self):
        """10. Рост той же функции трогает только вновь созданный слот."""
        self.make_candidate(cold=True)
        self.make_candidate(cold=True)

        self.save(seller_middle=2)

        self.assertIsNone(self.member_on(self.old_slot))
        self.assertIsNotNone(
            self.member_on(self.slot('seller_middle', slot_index=2))
        )


# ---------------------------------------------------------------------------
# 11. Пакеты
# ---------------------------------------------------------------------------


class PackagePathTests(AutoAssignOnSlotCreateTestCase):
    def test_quick_start_package_auto_assigns_its_seller_slot(self):
        """11. Пакет идёт той же оркестрацией — подбор работает и здесь."""
        candidate = self.make_candidate(cold=True)

        apply_package_and_sync_slots(self.project, 'quick_start', self.director)

        member = self.member_on(self.slot('seller_middle'))
        self.assertIsNotNone(member)
        self.assertEqual(member.user_id, candidate.id)

    def test_scaling_package_fills_all_three_slots(self):
        """11. «Масштабирование»: 2 сейла + LinkedIn заняты за одно сохранение."""
        self.make_candidate(cold=True, rating='4.90')
        self.make_candidate(cold=True, rating='4.50')
        self.make_candidate(linkedin=True)

        apply_package_and_sync_slots(self.project, 'scaling', self.director)

        self.assertEqual(len(self.slot_members()), 3)
        self.assertEqual(len(self.slots('seller_middle', is_active=True)), 2)
        self.assertIsNotNone(self.member_on(self.slot('linkedin_leadgen')))

    def test_package_creates_no_teamlead_member(self):
        """11. Тимлид пакета в подбор не попадает и здесь."""
        self.make_candidate(cold=True)

        apply_package_and_sync_slots(self.project, 'quick_start', self.director)

        self.assertEqual(self.slots('teamlead'), [])
        self.assertEqual(
            {member.function_slot.role_key for member in self.slot_members()},
            {'seller_middle'},
        )

    def test_package_path_reaches_the_same_orchestration(self):
        """11. Отдельной реализации подбора у пакетов нет."""
        self.make_candidate(cold=True)

        with mock.patch(AUTO_ASSIGN_PATH) as auto_assign:
            apply_package_and_sync_slots(self.project, 'quick_start', self.director)

        self.assertEqual(auto_assign.call_count, 1)
        self.assertEqual(auto_assign.call_args.args[1], self.director)


# ---------------------------------------------------------------------------
# 12. Границы этапа
# ---------------------------------------------------------------------------


class AutoAssignBoundaryTests(AutoAssignOnSlotCreateTestCase):
    def test_composition_statuses_are_covered_by_staffing_statuses(self):
        """Инвариант, на котором держится узкий `except StaffingError`.

        Оркестрация не ловит отказ подбора по статусу проекта: любой статус,
        в котором разрешено менять состав, обязан быть статусом, в котором
        разрешён подбор. Разойдутся — это программная ошибка, и она должна
        падать, а не молча оставлять слоты пустыми.
        """
        self.assertLessEqual(
            set(COMPOSITION_EDITABLE_STATUSES), set(STAFFING_MUTABLE_STATUSES)
        )

    def test_orchestration_assigns_only_through_the_staffing_service(self):
        """Назначение идёт единственным сервисом подбора, а не своим кодом.

        При подменённом сервисе в комнате не появляется ни одного участника:
        значит второй, собственной ветки назначения в оркестрации нет.
        """
        self.make_candidate(cold=True)

        with mock.patch(AUTO_ASSIGN_PATH) as auto_assign:
            self.save(seller_middle=1)

        auto_assign.assert_called_once()
        self.assertEqual(self.slot_members(), [])

    def test_actor_is_the_user_who_saved_the_composition(self):
        """`actor` подбора — тот, кто сохранял состав, а не владелец «по умолчанию»."""
        self.make_candidate(cold=True)

        with mock.patch(AUTO_ASSIGN_PATH) as auto_assign:
            self.save(seller_middle=1)

        self.assertEqual(auto_assign.call_args.args[1], self.director)

    def test_projection_module_still_knows_nothing_about_assignment(self):
        """Проекция осталась модулем без назначений: подбор живёт в оркестрации."""
        code = inspect.getsource(projection).replace(projection.__doc__, '')

        self.assertNotIn('auto_assign', code)
        self.assertNotIn('RoomMember.objects.create', code)

    def test_packages_composition_is_still_server_side(self):
        """Пакет не превратился в отдельную ветку с собственным составом."""
        self.assertIn('quick_start', presets.FUNCTIONAL_ROLE_PACKAGES)
