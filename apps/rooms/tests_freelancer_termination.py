"""Расторжение с фрилансером: доменные инварианты (этап 3.1).

Проверяется только `apps.rooms.termination`: автомат статусов, архивация
членства, срок ответа и письмо в поддержку при протесте. HTTP-контур, формы,
модалка, чат кейса и блокировки работы появятся на следующих этапах и
проверяются своими тестами — здесь их нет намеренно.

Ключевые инварианты, ради которых файл существует:

* завершение расторжения **не удаляет** `RoomMember` — строка остаётся, а
  вместе с ней вся история человека в проекте;
* открытый кейс на пару комната+фрилансер ровно один;
* `appeal_pending` не завершается по дедлайну никогда;
* протест не переводит кейс в `appeal_pending` без ушедшего письма;
* переход из терминального статуса — ошибка, а не тихий no-op.
"""

import re
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.core.mail import EmailMultiAlternatives
from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.utils import timezone

from django.test import Client
from django.urls import reverse

from apps.pipeline.models import FreelancerAccrual, Lead, Report, Task
from apps.pipeline.services import create_task
from apps.rooms.admin import FreelancerTerminationAdmin
from apps.rooms.chat import CHAT_MESSAGE_MAX_LENGTH
from apps.rooms.onboarding import freelancer_metrics
from apps.rooms.models import (
    FreelancerTermination,
    Project,
    RoomActivity,
    RoomChatMessage,
    RoomFunctionSlot,
    RoomMember,
    TerminationMessage,
)
from apps.rooms.forms import AddFreelancerForm
from apps.rooms.services import (
    add_freelancer_to_room,
    assign_teamlead,
    launch_project,
    reactivate_room_member,
    user_can_work_in_room,
    user_is_archived_member,
)
from apps.rooms.staffing import selectors
from apps.rooms.staffing.matching import get_ranked_candidates
from apps.rooms.staffing.services import (
    StaffingError,
    assign_candidate_to_slot,
    auto_assign_best_candidate,
    replace_slot_member,
)
from apps.rooms.views import _members_with_termination
from apps.rooms.termination import (
    TERMINATION_NOTICE_DAYS,
    InvalidTerminationTransition,
    TerminationAlreadyOpen,
    TerminationError,
    appeal_termination,
    complete_termination,
    finalize_expired_terminations,
    initiate_termination,
    post_termination_message,
    recent_termination_messages,
    revoke_termination,
)
from apps.test_helpers import (
    make_director,
    make_freelancer,
    make_staffed_project,
    make_teamlead,
    make_user,
)
from apps.users.models import User

#: Причина длиннее `TERMINATION_REASON_MIN_LENGTH`: короткая отсекается
#: валидацией, а эти тесты проверяют не её, а переходы автомата.
REASON = 'Систематически срывает сроки и не выходит на связь по задачам.'

#: Ссылка на кейс в админке, которую протест кладёт в письмо поддержке.
#: Собирает её вызывающий код (у домена нет `request`), поэтому в тестах
#: это просто фиксированная строка.
ADMIN_URL = 'https://wowlance.test/admin/rooms/freelancertermination/1/change/'


class TerminationDomainTestCase(TestCase):
    """Комната в подборе: тимлид, занятый слот и фрилансер на нём."""

    def setUp(self):
        fixture = make_staffed_project(slots=1, candidates=1)
        self.project = fixture.project
        self.room = fixture.room
        self.teamlead = fixture.teamlead
        self.slot = fixture.slots[0]
        self.freelancer = fixture.candidates[0]
        self.member = RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=self.slot,
        )

    def initiate(self):
        return initiate_termination(
            room=self.room,
            member=self.member,
            initiated_by=self.teamlead,
            reason=REASON,
        )


class InitiateTerminationTests(TerminationDomainTestCase):
    def test_valid_notice_opens_case_and_keeps_member_working(self):
        """Уведомление заводит кейс, но состав команды не меняет."""
        case = self.initiate()

        self.assertEqual(case.status, FreelancerTermination.Status.NOTICE_SENT)
        self.assertEqual(
            case.deadline_at - case.initiated_at,
            timedelta(days=TERMINATION_NOTICE_DAYS),
        )
        self.assertIsNone(case.completed_at)
        self.assertIsNone(case.revoked_at)

        # Слот ещё занят: место освобождается только при завершении кейса.
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertIsNone(self.member.left_at)
        self.assertEqual(self.member.function_slot_id, self.slot.id)

    def test_second_open_case_is_not_created(self):
        """Повторное уведомление отдаёт существующий кейс, а не заводит второй."""
        first = self.initiate()

        with self.assertRaises(TerminationAlreadyOpen) as ctx:
            self.initiate()

        self.assertEqual(ctx.exception.case.pk, first.pk)
        self.assertEqual(FreelancerTermination.objects.count(), 1)


class InitiateTerminationValidationTests(TerminationDomainTestCase):
    """Отказы `initiate_termination`: кейс не заводится ни в одном случае."""

    def assertNoCaseCreated(self):
        self.assertFalse(FreelancerTermination.objects.exists())

    def test_short_reason_is_rejected(self):
        with self.assertRaises(TerminationError):
            initiate_termination(
                room=self.room,
                member=self.member,
                initiated_by=self.teamlead,
                reason='Не сработались',
            )

        self.assertNoCaseCreated()
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertEqual(self.member.function_slot_id, self.slot.id)

    def test_member_of_another_room_is_rejected(self):
        other = make_staffed_project(slots=1, candidates=1, prefix='other')
        foreign_member = RoomMember.objects.create(
            room=other.room,
            user=other.candidates[0],
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )

        with self.assertRaises(TerminationError):
            initiate_termination(
                room=self.room,
                member=foreign_member,
                initiated_by=self.teamlead,
                reason=REASON,
            )

        self.assertNoCaseCreated()

    def test_non_freelancer_member_is_rejected(self):
        """Расторжение — не универсальная кнопка удаления: тимлид не подходит."""
        teamlead_member = RoomMember.objects.get(
            room=self.room, user=self.teamlead,
        )

        with self.assertRaises(TerminationError):
            initiate_termination(
                room=self.room,
                member=teamlead_member,
                initiated_by=self.teamlead,
                reason=REASON,
            )

        self.assertNoCaseCreated()

    def test_only_project_teamlead_may_initiate(self):
        """Владелец проекта операционкой команды не управляет."""
        director = self.project.owner

        with self.assertRaises(PermissionDenied):
            initiate_termination(
                room=self.room,
                member=self.member,
                initiated_by=director,
                reason=REASON,
            )

        self.assertNoCaseCreated()

    def test_archived_member_cannot_be_terminated_again(self):
        self.member.is_active = False
        self.member.save(update_fields=['is_active'])

        with self.assertRaises(TerminationError):
            initiate_termination(
                room=self.room,
                member=self.member,
                initiated_by=self.teamlead,
                reason=REASON,
            )

        self.assertNoCaseCreated()


class CompleteTerminationTests(TerminationDomainTestCase):
    def test_complete_archives_membership_without_deleting_it(self):
        """Членство уходит в архив: строка на месте, слот свободен."""
        case = self.initiate()

        complete_termination(case)

        case.refresh_from_db()
        self.assertEqual(case.status, FreelancerTermination.Status.COMPLETED)
        self.assertIsNotNone(case.completed_at)
        self.assertIsNone(case.revoked_at)

        # Главный инвариант: RoomMember.delete() не вызывался.
        self.assertTrue(
            RoomMember.objects.filter(pk=self.member.pk).exists()
        )
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertIsNotNone(self.member.left_at)
        self.assertIsNone(self.member.function_slot_id)
        self.assertEqual(
            self.member.role_in_room, RoomMember.RoleInRoom.FREELANCER
        )

    def test_activity_records_removal_without_the_reason_text(self):
        """В ленту идёт короткое событие, а не формулировка тимлида."""
        case = self.initiate()

        complete_termination(case)

        activity = RoomActivity.objects.filter(
            room=self.room,
            event_type=RoomActivity.EventType.MEMBER_REMOVED,
        ).first()
        self.assertIsNotNone(activity)
        self.assertIn(self.freelancer.full_name, activity.message)
        self.assertNotIn(REASON, activity.message)


class RevokeTerminationTests(TerminationDomainTestCase):
    def test_revoke_closes_case_and_leaves_the_team_untouched(self):
        case = self.initiate()

        revoke_termination(case)

        case.refresh_from_db()
        self.assertEqual(case.status, FreelancerTermination.Status.REVOKED)
        self.assertIsNotNone(case.revoked_at)
        self.assertIsNone(case.completed_at)

        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertIsNone(self.member.left_at)
        self.assertEqual(self.member.function_slot_id, self.slot.id)


class ExpiredTerminationTests(TerminationDomainTestCase):
    def test_expired_notice_is_finalized(self):
        case = self.initiate()
        case.deadline_at = timezone.now() - timedelta(hours=1)
        case.save(update_fields=['deadline_at'])

        self.assertEqual(finalize_expired_terminations(), 1)

        case.refresh_from_db()
        self.assertEqual(case.status, FreelancerTermination.Status.COMPLETED)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)

    def test_appeal_pending_never_expires(self):
        """Оспоренный кейс ждёт решения поддержки, а не таймер."""
        case = self.initiate()
        appeal_termination(case, admin_url=ADMIN_URL)
        # Срок ответа давно прошёл — для `notice_sent` это был бы авто-уход.
        FreelancerTermination.objects.filter(pk=case.pk).update(
            deadline_at=timezone.now() - timedelta(hours=1),
        )

        self.assertEqual(finalize_expired_terminations(), 0)

        case.refresh_from_db()
        self.assertEqual(
            case.status, FreelancerTermination.Status.APPEAL_PENDING
        )
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)


class AppealTerminationTests(TerminationDomainTestCase):
    """Протест: письмо в поддержку обязательно, иначе статус не меняется."""

    def test_valid_appeal_notifies_support_and_keeps_the_member_in_place(self):
        case = self.initiate()

        appeal_termination(case, admin_url=ADMIN_URL)

        case.refresh_from_db()
        self.assertEqual(
            case.status, FreelancerTermination.Status.APPEAL_PENDING
        )
        self.assertIsNotNone(case.appealed_at)

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [settings.SUPPORT_EMAIL])
        self.assertEqual(message.from_email, settings.DEFAULT_FROM_EMAIL)
        # Проверяется обязательный смысл письма, а не его вёрстка.
        payload = f'{message.subject}\n{message.body}'
        for required in (
            self.project.name,
            self.freelancer.email,
            self.teamlead.email,
            REASON,
            ADMIN_URL,
        ):
            with self.subTest(required=required):
                self.assertIn(required, payload)

        # Человек ещё в команде: протест блокирует работу, а не членство.
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertIsNone(self.member.left_at)
        self.assertEqual(self.member.function_slot_id, self.slot.id)

    def test_second_appeal_is_rejected_and_sends_nothing(self):
        case = self.initiate()
        appeal_termination(case, admin_url=ADMIN_URL)

        with self.assertRaises(InvalidTerminationTransition):
            appeal_termination(case, admin_url=ADMIN_URL)

        self.assertEqual(len(mail.outbox), 1)

    def test_appeal_after_complete_is_rejected_and_sends_nothing(self):
        case = self.initiate()
        complete_termination(case)

        with self.assertRaises(InvalidTerminationTransition):
            appeal_termination(case, admin_url=ADMIN_URL)

        self.assertEqual(mail.outbox, [])

    def test_failed_delivery_rolls_the_appeal_back(self):
        """Упавшая отправка не оставляет кейс в «ждём поддержку»."""
        case = self.initiate()

        with patch.object(
            EmailMultiAlternatives, 'send', side_effect=RuntimeError('smtp down'),
        ):
            with self.assertRaises(RuntimeError):
                appeal_termination(case, admin_url=ADMIN_URL)

        case.refresh_from_db()
        self.assertEqual(case.status, FreelancerTermination.Status.NOTICE_SENT)
        self.assertIsNone(case.appealed_at)

        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertIsNone(self.member.left_at)
        self.assertEqual(self.member.function_slot_id, self.slot.id)


class TerminationChatTests(TerminationDomainTestCase):
    """Приватный тред кейса: участники, статус кейса, длина текста."""

    def setUp(self):
        super().setUp()
        self.case = self.initiate()

    def test_freelancer_posts_into_the_thread(self):
        message = post_termination_message(
            self.case, author=self.freelancer, text='Не согласен со сроками.',
        )

        self.assertEqual(message.case_id, self.case.pk)
        self.assertEqual(message.author_id, self.freelancer.id)
        self.assertEqual(message.text, 'Не согласен со сроками.')
        self.assertEqual(TerminationMessage.objects.count(), 1)

    def test_initiating_teamlead_posts_into_the_thread(self):
        message = post_termination_message(
            self.case, author=self.teamlead, text='Готов обсудить в звонке.',
        )

        self.assertEqual(message.author_id, self.teamlead.id)
        self.assertEqual(TerminationMessage.objects.count(), 1)

    def test_outsider_cannot_post(self):
        """Директор проекта в тред расторжения не входит."""
        with self.assertRaises(PermissionDenied):
            post_termination_message(
                self.case, author=self.project.owner, text='А что тут у вас?',
            )

        self.assertEqual(TerminationMessage.objects.count(), 0)

    def test_whitespace_only_message_is_rejected(self):
        with self.assertRaises(TerminationError):
            post_termination_message(
                self.case, author=self.freelancer, text='   \n\t  ',
            )

        self.assertEqual(TerminationMessage.objects.count(), 0)

    def test_message_longer_than_the_limit_is_rejected(self):
        with self.assertRaises(TerminationError):
            post_termination_message(
                self.case,
                author=self.freelancer,
                text='я' * (CHAT_MESSAGE_MAX_LENGTH + 1),
            )

        self.assertEqual(TerminationMessage.objects.count(), 0)

    def test_message_of_exactly_the_limit_is_accepted(self):
        text = 'я' * CHAT_MESSAGE_MAX_LENGTH

        message = post_termination_message(
            self.case, author=self.freelancer, text=text,
        )

        self.assertEqual(len(message.text), CHAT_MESSAGE_MAX_LENGTH)

    def test_appeal_pending_thread_still_accepts_messages(self):
        """Пока ждём поддержку, переписка не замирает."""
        appeal_termination(self.case, admin_url=ADMIN_URL)

        post_termination_message(
            self.case, author=self.freelancer, text='Жду решения поддержки.',
        )

        self.assertEqual(TerminationMessage.objects.count(), 1)

    def test_completed_case_rejects_new_messages(self):
        complete_termination(self.case)

        with self.assertRaises(TerminationError):
            post_termination_message(
                self.case, author=self.freelancer, text='Ещё пара слов.',
            )

        self.assertEqual(TerminationMessage.objects.count(), 0)

    def test_revoked_case_rejects_new_messages(self):
        revoke_termination(self.case)

        with self.assertRaises(TerminationError):
            post_termination_message(
                self.case, author=self.freelancer, text='Ещё пара слов.',
            )

        self.assertEqual(TerminationMessage.objects.count(), 0)


class TerminationChatHistoryTests(TerminationDomainTestCase):
    """Селектор ленты: свой кейс, хронология, лимит."""

    def setUp(self):
        super().setUp()
        self.case = self.initiate()

    def post_at(self, text, *, minutes_ago):
        """Сообщение с явным временем: порядок ленты не должен зависеть от
        разрешения системных часов."""
        message = post_termination_message(
            self.case, author=self.freelancer, text=text,
        )
        TerminationMessage.objects.filter(pk=message.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes_ago),
        )
        return message

    def test_history_is_chronological_and_scoped_to_the_case(self):
        self.post_at('первое', minutes_ago=30)
        self.post_at('второе', minutes_ago=20)
        self.post_at('третье', minutes_ago=10)

        # Чужой кейс в другой комнате: его сообщения в ленту попадать не должны.
        other = make_staffed_project(slots=1, candidates=1, prefix='hist')
        other_member = RoomMember.objects.create(
            room=other.room,
            user=other.candidates[0],
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )
        other_case = initiate_termination(
            room=other.room,
            member=other_member,
            initiated_by=other.teamlead,
            reason=REASON,
        )
        post_termination_message(
            other_case, author=other.candidates[0], text='чужое',
        )

        texts = [m.text for m in recent_termination_messages(self.case)]
        self.assertEqual(texts, ['первое', 'второе', 'третье'])

    def test_limit_returns_the_newest_messages(self):
        self.post_at('первое', minutes_ago=30)
        self.post_at('второе', minutes_ago=20)
        self.post_at('третье', minutes_ago=10)

        texts = [m.text for m in recent_termination_messages(self.case, limit=2)]
        self.assertEqual(texts, ['второе', 'третье'])

    def test_non_positive_limit_is_rejected(self):
        with self.assertRaises(TerminationError):
            recent_termination_messages(self.case, limit=0)


class WorkBlockingTestCase(TestCase):
    """Живая комната, где фрилансер реально работает.

    Отдельная фикстура от `TerminationDomainTestCase`: там комната в подборе
    и проверяется домен, а здесь нужен запущенный проект с задачей, лидами и
    включённым чатом, чтобы блокировка проверялась по настоящим HTTP-путям.
    """

    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir@block.test')
        self.teamlead = make_teamlead(email='tl@block.test')
        self.freelancer = make_freelancer(email='fr@block.test')
        self.project = Project.objects.create(
            owner=self.director,
            name='Проект блокировки',
            input_data={
                'offer': 'Оффер',
                'audience': 'ЦА',
                'hot_criteria': 'Запросил демо',
            },
            status=Project.Status.DRAFT,
        )
        launch_project(self.project)
        assign_teamlead(self.project, self.teamlead)
        add_freelancer_to_room(self.project.room, self.freelancer)
        self.project.refresh_from_db()
        self.room = self.project.room
        self.member = RoomMember.objects.get(
            room=self.room, user=self.freelancer,
        )
        self.task = create_task(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.teamlead,
            title='Обзвон базы',
        )

    def initiate(self):
        return initiate_termination(
            room=self.room,
            member=self.member,
            initiated_by=self.teamlead,
            reason=REASON,
        )

    def url(self, name, **kwargs):
        return reverse(name, kwargs={'project_id': self.project.id, **kwargs})

    def post_task_start(self):
        return self.client.post(
            self.url('pipeline:task_start', task_id=self.task.id)
        )

    def post_lead_create(self):
        return self.client.post(
            self.url('pipeline:lead_create'),
            {
                'name': 'Контакт',
                'phone': '+70000000000',
                'source': Lead.Source.BASE,
                'qualification_status': Lead.Qualification.COLD,
            },
        )

    def post_team_chat(self, text='Сообщение команде'):
        return self.client.post(self.url('rooms:room_chat_send'), {'text': text})

    def post_readiness(self):
        return self.client.post(self.url('rooms:room_confirm_ready'))

    def post_task_close(self, task=None):
        return self.client.post(
            self.url('pipeline:task_close', task_id=(task or self.task).id)
        )


class WorkBlockingTests(WorkBlockingTestCase):
    """Серверная блокировка записи: 403 по настоящим endpoint'ам."""

    def test_notice_sent_blocks_every_write_path(self):
        self.initiate()
        self.client.force_login(self.freelancer)

        checks = {
            'task_start': self.post_task_start,
            'lead_create': self.post_lead_create,
            'team_chat_send': self.post_team_chat,
            'confirm_ready': self.post_readiness,
        }
        for name, request in checks.items():
            with self.subTest(endpoint=name):
                self.assertEqual(request().status_code, 403)

        # Ни одна запись не состоялась.
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.NEW)
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(
            RoomChatMessage.objects.filter(author=self.freelancer).exists()
        )
        self.member.refresh_from_db()
        self.assertNotEqual(
            self.member.ready_status, RoomMember.ReadyStatus.READY
        )

    def test_appeal_pending_keeps_the_block(self):
        case = self.initiate()
        appeal_termination(case, admin_url=ADMIN_URL)
        self.client.force_login(self.freelancer)

        self.assertEqual(self.post_task_start().status_code, 403)
        self.assertEqual(self.post_team_chat().status_code, 403)

    def test_archived_member_cannot_write(self):
        case = self.initiate()
        complete_termination(case)
        self.client.force_login(self.freelancer)

        self.assertFalse(
            user_can_work_in_room(self.freelancer, self.project)
        )
        self.assertEqual(self.post_task_start().status_code, 403)
        self.assertEqual(self.post_lead_create().status_code, 403)
        # Членство архивировано, а не удалено.
        self.assertTrue(RoomMember.objects.filter(pk=self.member.pk).exists())

    def test_revoke_restores_the_ability_to_work(self):
        case = self.initiate()
        revoke_termination(case)
        self.client.force_login(self.freelancer)

        self.assertTrue(user_can_work_in_room(self.freelancer, self.project))
        self.assertEqual(self.post_task_start().status_code, 302)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.IN_PROGRESS)

    def test_gate_does_not_touch_non_freelancer_members(self):
        """Гейт молчит там, где строки фрилансера нет: тимлид и менеджер."""
        self.initiate()
        manager = make_user(email='mgr@block.test', role=User.Roles.MANAGER)

        for label, user in (
            ('teamlead', self.teamlead),
            ('manager_without_membership', manager),
        ):
            with self.subTest(user=label):
                self.assertTrue(user_can_work_in_room(user, self.project))

    def test_team_chat_stays_readable_during_the_case(self):
        """Блокируется запись, а не чтение: человек заходит и видит комнату."""
        self.initiate()
        self.client.force_login(self.freelancer)

        response = self.client.get(self.url('rooms:room_chat_messages'))

        self.assertEqual(response.status_code, 200)


class TerminationNoticeHttpTests(WorkBlockingTestCase):
    """Форма тимлида: «Удалить» фрилансера больше не значит delete."""

    #: Точная подпись из задания — проверяется приёмкой, поэтому константа.
    SUBMIT_LABEL = 'Отправить фрилансеру уведомление о расторжении'

    def setUp(self):
        super().setUp()
        self.remove_url = self.url(
            'rooms:room_remove_member', member_id=self.member.id,
        )
        self.form_url = self.url(
            'rooms:room_termination_form', member_id=self.member.id,
        )
        self.client.force_login(self.teamlead)

    def assertNothingHappened(self):
        self.assertFalse(FreelancerTermination.objects.exists())
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)

    def test_post_without_reason_is_rejected(self):
        response = self.client.post(self.remove_url, {})

        self.assertEqual(response.status_code, 400)
        self.assertNothingHappened()

    def test_post_with_short_reason_is_rejected(self):
        response = self.client.post(self.remove_url, {'reason': 'коротко'})

        self.assertEqual(response.status_code, 400)
        self.assertNothingHappened()

    def test_valid_reason_opens_the_case_and_keeps_the_member(self):
        response = self.client.post(self.remove_url, {'reason': REASON})

        self.assertEqual(response.status_code, 302)
        case = FreelancerTermination.objects.get()
        self.assertEqual(case.status, FreelancerTermination.Status.NOTICE_SENT)

        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertIsNone(self.member.left_at)

    def test_second_notice_does_not_create_a_second_case(self):
        self.client.post(self.remove_url, {'reason': REASON})

        response = self.client.post(
            self.remove_url, {'reason': 'Вторая попытка расторжения подряд.'},
        )

        self.assertEqual(FreelancerTermination.objects.count(), 1)
        self.assertRedirects(response, self.form_url)

    def test_form_page_offers_the_reason_field_and_exact_button(self):
        response = self.client.get(self.form_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'textarea')
        self.assertContains(response, self.SUBMIT_LABEL)

    def test_form_page_shows_the_open_case_instead_of_a_new_form(self):
        self.initiate()

        response = self.client.get(self.form_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, REASON)
        # Второй кейс отсюда не завести: формы новой причины больше нет.
        self.assertNotContains(response, self.SUBMIT_LABEL)

    def test_team_page_links_freelancer_row_to_the_termination_form(self):
        response = self.client.get(self.url('rooms:room_team'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.form_url)
        # Удаление фрилансера больше не идёт через POST-форму с confirm.
        self.assertNotContains(response, f'action="{self.remove_url}"')

    def test_reason_is_escaped_on_the_case_page(self):
        payload = '<script>alert(1)</script>'
        self.client.post(
            self.remove_url,
            {'reason': f'{payload} нарушение договорённостей по проекту'},
        )

        response = self.client.get(self.form_url)

        self.assertNotContains(response, payload)
        self.assertContains(response, '&lt;script&gt;')

    def test_freelancer_cannot_open_the_teamlead_form(self):
        self.client.force_login(self.freelancer)

        self.assertEqual(self.client.get(self.form_url).status_code, 403)

    def test_director_cannot_initiate_termination(self):
        self.client.force_login(self.director)

        response = self.client.post(self.remove_url, {'reason': REASON})

        self.assertEqual(response.status_code, 403)
        self.assertNothingHappened()

    def test_platform_admin_cannot_open_the_form(self):
        """Общий manage-team RBAC пускает admin, форма расторжения — нет.

        Уведомление отправляет тимлид проекта, и интерфейс не должен
        показывать форму тому, кому доменный сервис откажет.
        """
        admin = make_user(email='admin@notice.test', role=User.Roles.ADMIN)
        self.client.force_login(admin)

        self.assertEqual(self.client.get(self.form_url).status_code, 403)


class TerminationModalTests(WorkBlockingTestCase):
    """Блокирующая модалка: где показывается, что в ней и когда исчезает."""

    #: Устойчивый признак модалки в разметке — её backdrop.
    MARKER = 'termination-modal-backdrop'

    def get_as_freelancer(self, name, **kwargs):
        self.client.force_login(self.freelancer)
        return self.client.get(self.url(name, **kwargs))

    def test_modal_appears_on_every_room_surface(self):
        self.initiate()

        for name in (
            'rooms:room_overview',
            'pipeline:room_tasks',
            'rooms:room_documents',
            'pipeline:room_leads',
            'rooms:room_comms',
        ):
            with self.subTest(page=name):
                response = self.get_as_freelancer(name)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, self.MARKER)

    def test_modal_is_absent_outside_the_room(self):
        self.initiate()
        self.client.force_login(self.freelancer)

        for label, url in (
            ('dashboard', reverse('core:home')),
            ('project_list', reverse('rooms:project_list')),
        ):
            with self.subTest(page=label):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, self.MARKER)

    def test_foreign_room_shows_no_modal_and_leaks_no_reason(self):
        self.initiate()
        other = make_staffed_project(slots=1, candidates=1, prefix='foreign')
        RoomMember.objects.create(
            room=other.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )
        self.client.force_login(self.freelancer)

        response = self.client.get(
            reverse('rooms:room_overview', kwargs={'project_id': other.project.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.MARKER)
        self.assertNotContains(response, REASON)

    def test_teamlead_never_sees_the_blocking_modal(self):
        self.initiate()
        self.client.force_login(self.teamlead)

        response = self.client.get(self.url('rooms:room_overview'))

        self.assertNotContains(response, self.MARKER)

    def test_notice_modal_carries_case_details_and_chat(self):
        case = self.initiate()

        response = self.get_as_freelancer('rooms:room_overview')

        for fragment in (
            REASON,
            self.project.name,
            case.deadline_at.strftime('%d.%m.%Y'),
            'role="dialog"',
            'aria-modal="true"',
            self.url('rooms:room_termination_messages', case_id=case.id),
            self.url('rooms:room_termination_send', case_id=case.id),
            'every 7s',
            f'maxlength="{CHAT_MESSAGE_MAX_LENGTH}"',
        ):
            with self.subTest(fragment=fragment):
                self.assertContains(response, fragment)

    def test_notice_modal_has_no_dismiss_control(self):
        self.initiate()

        response = self.get_as_freelancer('rooms:room_overview')
        body = response.content.decode()
        modal = body[body.index(self.MARKER):]

        for forbidden in ('Закрыть', 'data-dismiss', 'aria-label="Close"'):
            with self.subTest(control=forbidden):
                self.assertNotIn(forbidden, modal)

    def test_appeal_modal_says_support_is_deciding(self):
        case = self.initiate()
        appeal_termination(case, admin_url=ADMIN_URL)

        response = self.get_as_freelancer('rooms:room_overview')

        self.assertContains(response, self.MARKER)
        self.assertContains(response, REASON)
        self.assertContains(response, 'Ожидайте ответа поддержки')

    def test_modal_disappears_after_revoke(self):
        revoke_termination(self.initiate())

        response = self.get_as_freelancer('rooms:room_overview')

        self.assertNotContains(response, self.MARKER)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        # Операционная навигация вернулась.
        self.assertContains(response, self.url('rooms:room_documents'))

    def test_archived_overview_has_no_modal_and_no_operational_tabs(self):
        complete_termination(self.initiate())

        response = self.get_as_freelancer('rooms:room_overview')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.MARKER)
        for name in (
            'rooms:room_documents',
            'pipeline:room_leads',
            'rooms:room_comms',
        ):
            with self.subTest(tab=name):
                self.assertNotContains(response, self.url(name))

    def test_readiness_button_hidden_during_case_and_archive(self):
        ready_action = self.url('rooms:room_confirm_ready')

        case = self.initiate()
        self.assertNotContains(
            self.get_as_freelancer('rooms:room_overview'), ready_action,
        )

        complete_termination(case)
        self.assertNotContains(
            self.get_as_freelancer('rooms:room_overview'), ready_action,
        )

    def test_modal_escapes_reason_markup(self):
        payload = '<script>alert(1)</script>'
        self.client.force_login(self.teamlead)
        self.client.post(
            self.url('rooms:room_remove_member', member_id=self.member.id),
            {'reason': f'{payload} систематические срывы сроков'},
        )

        response = self.get_as_freelancer('rooms:room_overview')

        self.assertContains(response, self.MARKER)
        self.assertNotContains(response, payload)
        self.assertContains(response, '&lt;script&gt;')


class LazyTerminationExpiryTests(WorkBlockingTestCase):
    """Срок «три дня» закрывается на заходе в комнату, а не только по cron."""

    def setUp(self):
        super().setUp()
        # Слот нужен, чтобы проверить его освобождение при авто-завершении:
        # базовая фикстура добавляет фрилансера в комнату без слота.
        self.slot = RoomFunctionSlot.objects.create(
            room=self.room, role_key='seller', slot_index=1,
        )
        self.member.function_slot = self.slot
        self.member.save()

    def expire(self, case):
        FreelancerTermination.objects.filter(pk=case.pk).update(
            deadline_at=timezone.now() - timedelta(hours=1),
        )

    def test_overview_finalizes_the_expired_notice(self):
        case = self.initiate()
        self.expire(case)
        self.client.force_login(self.freelancer)

        response = self.client.get(self.url('rooms:room_overview'))

        self.assertEqual(response.status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.status, FreelancerTermination.Status.COMPLETED)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertIsNone(self.member.function_slot_id)
        self.assertNotContains(response, TerminationModalTests.MARKER)

    def test_operational_get_finalizes_and_blocks_on_the_same_request(self):
        """Порядок критичен: гейт архива обязан увидеть уже закрытый кейс."""
        case = self.initiate()
        self.expire(case)
        self.client.force_login(self.freelancer)

        response = self.client.get(self.url('pipeline:room_tasks'))

        self.assertEqual(response.status_code, 403)
        case.refresh_from_db()
        self.assertEqual(case.status, FreelancerTermination.Status.COMPLETED)

    def test_chat_poll_finalizes_and_closes_the_thread(self):
        case = self.initiate()
        self.expire(case)
        self.client.force_login(self.freelancer)

        response = self.client.get(
            self.url('rooms:room_termination_messages', case_id=case.id)
        )

        self.assertIn(response.status_code, (403, 404))
        case.refresh_from_db()
        self.assertEqual(case.status, FreelancerTermination.Status.COMPLETED)

    def test_appeal_pending_survives_a_past_deadline(self):
        case = self.initiate()
        appeal_termination(case, admin_url=ADMIN_URL)
        self.expire(case)
        self.client.force_login(self.freelancer)

        response = self.client.get(self.url('rooms:room_overview'))

        self.assertEqual(response.status_code, 200)
        case.refresh_from_db()
        self.assertEqual(
            case.status, FreelancerTermination.Status.APPEAL_PENDING
        )
        self.assertContains(response, TerminationModalTests.MARKER)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertEqual(self.member.function_slot_id, self.slot.id)


class TerminationChatHttpTests(WorkBlockingTestCase):
    """HTTP-контур треда расторжения: доступ, запись, изоляция от чата комнаты."""

    def setUp(self):
        super().setUp()
        self.case = self.initiate()
        self.messages_url = self.url(
            'rooms:room_termination_messages', case_id=self.case.id,
        )
        self.send_url = self.url(
            'rooms:room_termination_send', case_id=self.case.id,
        )

    def send(self, text='Обсудим сроки.'):
        return self.client.post(self.send_url, {'text': text})

    # --- доступ -----------------------------------------------------------

    def test_initiating_teamlead_reads_the_thread(self):
        self.client.force_login(self.teamlead)

        self.assertEqual(self.client.get(self.messages_url).status_code, 200)

    def test_freelancer_reads_the_thread(self):
        self.client.force_login(self.freelancer)

        self.assertEqual(self.client.get(self.messages_url).status_code, 200)

    def test_outsiders_cannot_read_the_thread(self):
        outsider = make_freelancer(email='out@thread.test')

        for label, user in (('director', self.director), ('outsider', outsider)):
            with self.subTest(user=label):
                self.client.force_login(user)
                self.assertIn(
                    self.client.get(self.messages_url).status_code, (403, 404),
                )

    def test_outsider_cannot_post(self):
        self.client.force_login(self.director)

        response = self.send('Дайте посмотреть, что там у вас.')

        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(TerminationMessage.objects.count(), 0)

    # --- запись -----------------------------------------------------------

    def test_freelancer_posts_and_sees_the_message(self):
        self.client.force_login(self.freelancer)

        response = self.send('Не согласен с причиной.')

        self.assertEqual(response.status_code, 200)
        message = TerminationMessage.objects.get()
        self.assertEqual(message.case_id, self.case.pk)
        self.assertEqual(message.author_id, self.freelancer.id)
        self.assertEqual(message.text, 'Не согласен с причиной.')
        self.assertContains(response, 'Не согласен с причиной.')

    def test_initiating_teamlead_posts(self):
        self.client.force_login(self.teamlead)

        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(TerminationMessage.objects.count(), 1)

    def test_too_long_message_is_rejected(self):
        self.client.force_login(self.freelancer)

        response = self.send('я' * (CHAT_MESSAGE_MAX_LENGTH + 1))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(TerminationMessage.objects.count(), 0)

    def test_whitespace_message_is_rejected(self):
        self.client.force_login(self.freelancer)

        response = self.send('   \n  ')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(TerminationMessage.objects.count(), 0)

    # --- статусы кейса ----------------------------------------------------

    def test_appeal_pending_thread_stays_open(self):
        appeal_termination(self.case, admin_url=ADMIN_URL)
        self.client.force_login(self.freelancer)

        self.assertEqual(self.client.get(self.messages_url).status_code, 200)
        self.assertEqual(self.send('Жду решения поддержки.').status_code, 200)

    def assertThreadIsClosed(self):
        self.client.force_login(self.freelancer)
        self.assertIn(self.client.get(self.messages_url).status_code, (403, 404))
        self.assertIn(self.send().status_code, (403, 404))
        self.assertEqual(TerminationMessage.objects.count(), 0)

    def test_completed_case_thread_is_unavailable(self):
        complete_termination(self.case)

        self.assertThreadIsClosed()

    def test_revoked_case_thread_is_unavailable(self):
        revoke_termination(self.case)

        self.assertThreadIsClosed()

    # --- изоляция ---------------------------------------------------------

    def test_thread_works_even_when_room_chat_is_disabled(self):
        """Переписка о расторжении обязательна и настройкой чата не гасится."""
        self.room.chat_enabled = False
        self.room.save(update_fields=['chat_enabled'])
        self.client.force_login(self.freelancer)

        self.assertEqual(self.client.get(self.messages_url).status_code, 200)
        self.assertEqual(self.send().status_code, 200)

    def test_thread_message_does_not_touch_the_team_chat(self):
        before = RoomChatMessage.objects.count()
        self.client.force_login(self.freelancer)

        self.send()

        self.assertEqual(RoomChatMessage.objects.count(), before)
        self.assertEqual(TerminationMessage.objects.count(), 1)

    def test_message_markup_is_escaped(self):
        self.client.force_login(self.freelancer)

        response = self.send('<script>alert(1)</script>')

        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, '&lt;script&gt;')

    # --- разметка HTMX ----------------------------------------------------

    def test_form_page_renders_the_htmx_chat_for_the_initiator(self):
        self.client.force_login(self.teamlead)

        response = self.client.get(
            self.url('rooms:room_termination_form', member_id=self.member.id)
        )

        for fragment in (
            self.messages_url,
            self.send_url,
            'every 7s',
            'name="text"',
            f'maxlength="{CHAT_MESSAGE_MAX_LENGTH}"',
        ):
            with self.subTest(fragment=fragment):
                self.assertContains(response, fragment)


class TeamleadRemovalRegressionTests(WorkBlockingTestCase):
    """Удаление тимлида задачей расторжения не меняется."""

    def test_teamlead_member_is_still_deleted(self):
        teamlead_member = RoomMember.objects.get(
            room=self.room, user=self.teamlead,
        )
        self.client.force_login(self.teamlead)

        response = self.client.post(
            self.url('rooms:room_remove_member', member_id=teamlead_member.id)
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            RoomMember.objects.filter(pk=teamlead_member.pk).exists()
        )
        self.project.refresh_from_db()
        self.assertIsNone(self.project.teamlead_id)
        self.assertFalse(FreelancerTermination.objects.exists())


class TaskCloseBlockingTests(WorkBlockingTestCase):
    """Закрытие задачи — тоже работа.

    `close_task` разрешает закрытие исполнителю, поэтому без отдельного гейта
    отстранённый фрилансер закрывал бы свою задачу прямым POST, минуя уже
    закрытую для него карточку задачи.
    """

    def assertTaskStillOpen(self):
        self.task.refresh_from_db()
        self.assertNotEqual(self.task.status, Task.Status.CLOSED)
        self.assertIsNone(self.task.closed_at)

    def test_notice_sent_blocks_task_close(self):
        self.initiate()
        self.client.force_login(self.freelancer)

        self.assertEqual(self.post_task_close().status_code, 403)
        self.assertTaskStillOpen()

    def test_appeal_pending_blocks_task_close(self):
        case = self.initiate()
        appeal_termination(case, admin_url=ADMIN_URL)
        self.client.force_login(self.freelancer)

        self.assertEqual(self.post_task_close().status_code, 403)
        self.assertTaskStillOpen()

    def test_archived_member_blocks_task_close(self):
        complete_termination(self.initiate())
        self.client.force_login(self.freelancer)

        self.assertEqual(self.post_task_close().status_code, 403)
        self.assertTaskStillOpen()

    def test_revoke_restores_task_close(self):
        """Отзыв возвращает обычное поведение — закрытие снова работает."""
        chore = create_task(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.teamlead,
            title='Задача без отчёта',
            report_required=False,
        )
        revoke_termination(self.initiate())
        self.client.force_login(self.freelancer)

        self.assertEqual(self.post_task_close(chore).status_code, 302)

        chore.refresh_from_db()
        self.assertEqual(chore.status, Task.Status.CLOSED)

    def test_teamlead_still_closes_a_departed_freelancer_task(self):
        """Задачи ушедшего закрывает тимлид — гейт его не касается."""
        chore = create_task(
            project=self.project,
            assignee=self.freelancer,
            created_by=self.teamlead,
            title='Хвост после расторжения',
            report_required=False,
        )
        complete_termination(self.initiate())
        self.client.force_login(self.teamlead)

        self.assertEqual(self.post_task_close(chore).status_code, 302)

        chore.refresh_from_db()
        self.assertEqual(chore.status, Task.Status.CLOSED)


class ArchivedReadOnlyAccessTests(WorkBlockingTestCase):
    """Архивный фрилансер: только «Обзор» и свои прошлые цифры.

    Операционные вкладки закрыты и по прямой ссылке — вкладки в шапке пока
    не прячутся, защита здесь серверная.
    """

    def setUp(self):
        super().setUp()
        self.lead = Lead.objects.create(
            project=self.project,
            creator=self.freelancer,
            source=Lead.Source.BASE,
            qualification_status=Lead.Qualification.COLD,
        )

    def archive(self):
        complete_termination(self.initiate())
        self.client.force_login(self.freelancer)

    def operational_urls(self):
        return {
            'room_team': self.url('rooms:room_team'),
            'room_documents': self.url('rooms:room_documents'),
            'room_comms': self.url('rooms:room_comms'),
            'room_chat_messages': self.url('rooms:room_chat_messages'),
            'room_tasks': self.url('pipeline:room_tasks'),
            'task_detail': self.url(
                'pipeline:task_detail', task_id=self.task.id,
            ),
            'room_leads': self.url('pipeline:room_leads'),
            'lead_detail': self.url(
                'pipeline:lead_detail', lead_id=self.lead.id,
            ),
        }

    def test_archived_keeps_overview_and_own_numbers(self):
        self.archive()

        for name, url in (
            ('room_overview', self.url('rooms:room_overview')),
            (
                'freelancer_project_accruals',
                self.url('pipeline:freelancer_project_accruals'),
            ),
        ):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_archived_is_blocked_on_every_operational_url(self):
        self.archive()

        for name, url in self.operational_urls().items():
            with self.subTest(page=name):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_open_case_still_opens_operational_pages(self):
        """Архивный гейт не должен превратиться в гейт открытого кейса."""
        self.initiate()
        self.client.force_login(self.freelancer)

        for name in ('room_tasks', 'room_documents', 'room_comms'):
            with self.subTest(page=name):
                response = self.client.get(self.operational_urls()[name])
                self.assertEqual(response.status_code, 200)

    def test_appeal_pending_still_opens_operational_pages(self):
        case = self.initiate()
        appeal_termination(case, admin_url=ADMIN_URL)
        self.client.force_login(self.freelancer)

        response = self.client.get(self.url('rooms:room_documents'))

        self.assertEqual(response.status_code, 200)

    def test_gate_does_not_block_teamlead_or_director(self):
        self.archive()

        self.client.force_login(self.teamlead)
        self.assertEqual(
            self.client.get(self.url('rooms:room_team')).status_code, 200,
        )

        self.client.force_login(self.director)
        self.assertEqual(
            self.client.get(self.url('rooms:room_overview')).status_code, 200,
        )


class WorkPredicateTests(WorkBlockingTestCase):
    """Предикаты фасада отдельно от HTTP."""

    def test_active_member_without_case_can_work(self):
        self.assertTrue(user_can_work_in_room(self.freelancer, self.project))
        self.assertFalse(user_is_archived_member(self.freelancer, self.project))

    def test_open_case_blocks_and_revoke_restores(self):
        case = self.initiate()
        self.assertFalse(user_can_work_in_room(self.freelancer, self.project))

        appeal_termination(case, admin_url=ADMIN_URL)
        self.assertFalse(user_can_work_in_room(self.freelancer, self.project))

        revoke_termination(case)
        self.assertTrue(user_can_work_in_room(self.freelancer, self.project))
        self.assertFalse(user_is_archived_member(self.freelancer, self.project))

    def test_completed_case_archives_the_member(self):
        complete_termination(self.initiate())

        self.assertFalse(user_can_work_in_room(self.freelancer, self.project))
        self.assertTrue(user_is_archived_member(self.freelancer, self.project))


class TerminationTransitionTests(TerminationDomainTestCase):
    def test_revoke_after_complete_is_rejected(self):
        """Ошибка workflow не маскируется no-op'ом: архив нельзя «отозвать»."""
        case = self.initiate()
        complete_termination(case)

        with self.assertRaises(InvalidTerminationTransition):
            revoke_termination(case)

    def test_complete_after_revoke_is_rejected(self):
        case = self.initiate()
        revoke_termination(case)

        with self.assertRaises(InvalidTerminationTransition):
            complete_termination(case)

class TerminationActionTestCase(WorkBlockingTestCase):
    """Общая фикстура трёх действий по открытому кейсу.

    Отличий от базовой две, и обе нужны каждому классу ниже: занятый слот —
    чтобы «Покинуть проект» доказуемо освобождал место (базовая фикстура
    сажает фрилансера в комнату без слота), и посторонний фрилансер — чтобы
    проверять защиту самих POST-адресов, а не только скрытые кнопки.
    """

    def setUp(self):
        super().setUp()
        self.slot = RoomFunctionSlot.objects.create(
            room=self.room, role_key='seller', slot_index=1,
        )
        self.member.function_slot = self.slot
        self.member.save(update_fields=['function_slot'])
        self.outsider = make_freelancer(email='outsider@block.test')
        self.case = self.initiate()

    def action_url(self, name, case=None):
        return self.url(name, case_id=(case or self.case).id)

    def post_action(self, name, user, case=None):
        self.client.force_login(user)
        return self.client.post(self.action_url(name, case=case))

    def assertCaseStatus(self, status):
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, status)

    def assertMemberStillOnTheTeam(self):
        """Человек в команде и на своём месте: ни архива, ни свободного слота."""
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertIsNone(self.member.left_at)
        self.assertEqual(self.member.function_slot_id, self.slot.id)

    def assertMemberStaysArchived(self):
        """Архив не отменяется задним числом: строка на месте, но неактивна."""
        self.assertTrue(RoomMember.objects.filter(pk=self.member.pk).exists())
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertIsNotNone(self.member.left_at)
        self.assertIsNone(self.member.function_slot_id)


class TerminationLeaveHttpTests(TerminationActionTestCase):
    """«Покинуть проект»: единственный статус, права и сохранение истории."""

    def test_freelancer_leaves_and_history_survives(self):
        response = self.post_action('rooms:room_termination_leave', self.freelancer)

        self.assertRedirects(response, reverse('rooms:project_list'))
        self.assertCaseStatus(FreelancerTermination.Status.COMPLETED)

        # Строка членства не удалена — архивирована.
        self.assertTrue(RoomMember.objects.filter(pk=self.member.pk).exists())
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertIsNotNone(self.member.left_at)
        self.assertIsNone(self.member.function_slot_id)

        # Смоук истории: задача человека осталась на месте (полная приёмка —
        # отдельным этапом, здесь проверяется только отсутствие удалений).
        self.assertTrue(Task.objects.filter(pk=self.task.pk).exists())
        self.assertTrue(
            Task.objects.filter(assignee=self.freelancer).exists()
        )

    def test_modal_disappears_after_leaving(self):
        self.post_action('rooms:room_termination_leave', self.freelancer)

        response = self.client.get(self.url('rooms:room_overview'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, TerminationModalTests.MARKER)
        self.assertFalse(user_can_work_in_room(self.freelancer, self.project))
        self.assertTrue(user_is_archived_member(self.freelancer, self.project))

    def test_nobody_else_may_leave_for_the_freelancer(self):
        for label, user in (
            ('teamlead', self.teamlead),
            ('director', self.director),
            ('outsider', self.outsider),
        ):
            with self.subTest(user=label):
                response = self.post_action(
                    'rooms:room_termination_leave', user,
                )
                self.assertEqual(response.status_code, 403)

        self.assertCaseStatus(FreelancerTermination.Status.NOTICE_SENT)
        self.assertMemberStillOnTheTeam()

    def test_leave_is_blocked_while_the_appeal_is_pending(self):
        """Протест уже у поддержки: закрыть кейс в обход неё нельзя."""
        appeal_termination(self.case, admin_url=ADMIN_URL)

        response = self.post_action('rooms:room_termination_leave', self.freelancer)

        self.assertEqual(response.status_code, 400)
        self.assertCaseStatus(FreelancerTermination.Status.APPEAL_PENDING)
        self.assertMemberStillOnTheTeam()

    def test_completed_case_cannot_be_left_twice(self):
        complete_termination(self.case)

        response = self.post_action('rooms:room_termination_leave', self.freelancer)

        self.assertEqual(response.status_code, 400)
        self.assertCaseStatus(FreelancerTermination.Status.COMPLETED)
        self.assertMemberStaysArchived()

    def test_revoked_case_cannot_be_left(self):
        revoke_termination(self.case)

        response = self.post_action('rooms:room_termination_leave', self.freelancer)

        self.assertEqual(response.status_code, 400)
        self.assertCaseStatus(FreelancerTermination.Status.REVOKED)
        self.assertMemberStillOnTheTeam()

    def test_action_is_post_only(self):
        self.client.force_login(self.freelancer)

        response = self.client.get(self.action_url('rooms:room_termination_leave'))

        self.assertEqual(response.status_code, 405)
        self.assertCaseStatus(FreelancerTermination.Status.NOTICE_SENT)

    def test_case_of_another_project_is_not_reachable_by_address(self):
        """Подмена проекта в адресе не даёт добраться до чужого кейса."""
        other = make_staffed_project(slots=1, candidates=1, prefix='other')
        self.client.force_login(self.freelancer)

        response = self.client.post(
            reverse(
                'rooms:room_termination_leave',
                kwargs={
                    'project_id': other.project.id,
                    'case_id': self.case.id,
                },
            )
        )

        self.assertEqual(response.status_code, 404)
        self.assertCaseStatus(FreelancerTermination.Status.NOTICE_SENT)


class TerminationAppealHttpTests(TerminationActionTestCase):
    """«Опротестовать решение»: письмо поддержке со ссылкой на кейс в админке."""

    def admin_path(self):
        return reverse(
            'admin:rooms_freelancertermination_change', args=[self.case.pk],
        )

    def test_freelancer_appeal_notifies_support(self):
        response = self.post_action('rooms:room_termination_appeal', self.freelancer)

        self.assertRedirects(response, self.url('rooms:room_overview'))
        self.assertCaseStatus(FreelancerTermination.Status.APPEAL_PENDING)
        self.case.refresh_from_db()
        self.assertIsNotNone(self.case.appealed_at)

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [settings.SUPPORT_EMAIL])
        # Ссылка на конкретный кейс, и она абсолютная: письмо читают вне
        # браузера пользователя, и относительный путь там бесполезен.
        self.assertIn(self.admin_path(), message.body)
        self.assertRegex(
            message.body, r'https?://[^\s]+' + re.escape(self.admin_path()),
        )

        # Протест блокирует работу, а не членство: место остаётся за человеком.
        self.assertMemberStillOnTheTeam()

    def test_nobody_else_may_appeal(self):
        for label, user in (
            ('teamlead', self.teamlead),
            ('director', self.director),
            ('outsider', self.outsider),
        ):
            with self.subTest(user=label):
                response = self.post_action(
                    'rooms:room_termination_appeal', user,
                )
                self.assertEqual(response.status_code, 403)

        self.assertEqual(mail.outbox, [])
        self.assertCaseStatus(FreelancerTermination.Status.NOTICE_SENT)
        self.assertMemberStillOnTheTeam()

    def test_second_appeal_sends_nothing(self):
        self.post_action('rooms:room_termination_appeal', self.freelancer)

        response = self.post_action('rooms:room_termination_appeal', self.freelancer)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(mail.outbox), 1)
        self.assertCaseStatus(FreelancerTermination.Status.APPEAL_PENDING)

    def test_completed_case_cannot_be_appealed(self):
        complete_termination(self.case)

        response = self.post_action('rooms:room_termination_appeal', self.freelancer)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(mail.outbox, [])
        self.assertCaseStatus(FreelancerTermination.Status.COMPLETED)
        self.assertMemberStaysArchived()

    def test_failed_delivery_is_not_reported_as_success(self):
        """Упавшая отправка не оставляет кейс в «ждём поддержку»."""
        self.client.force_login(self.freelancer)

        with patch.object(
            EmailMultiAlternatives, 'send', side_effect=RuntimeError('smtp down'),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    self.action_url('rooms:room_termination_appeal')
                )

        self.assertCaseStatus(FreelancerTermination.Status.NOTICE_SENT)
        self.case.refresh_from_db()
        self.assertIsNone(self.case.appealed_at)
        self.assertMemberStillOnTheTeam()

    def test_action_is_post_only(self):
        self.client.force_login(self.freelancer)

        response = self.client.get(self.action_url('rooms:room_termination_appeal'))

        self.assertEqual(response.status_code, 405)
        self.assertEqual(mail.outbox, [])


class TerminationRevokeHttpTests(TerminationActionTestCase):
    """«Отозвать увольнение»: право текущего тимлида проекта, и только его."""

    def test_current_teamlead_revokes_the_notice(self):
        response = self.post_action('rooms:room_termination_revoke', self.teamlead)

        self.assertRedirects(response, self.url('rooms:room_team'))
        self.assertCaseStatus(FreelancerTermination.Status.REVOKED)
        self.assertMemberStillOnTheTeam()
        self.assertTrue(user_can_work_in_room(self.freelancer, self.project))

        self.client.force_login(self.freelancer)
        overview = self.client.get(self.url('rooms:room_overview'))
        self.assertNotContains(overview, TerminationModalTests.MARKER)

    def test_current_teamlead_revokes_an_appealed_case(self):
        appeal_termination(self.case, admin_url=ADMIN_URL)

        response = self.post_action('rooms:room_termination_revoke', self.teamlead)

        self.assertEqual(response.status_code, 302)
        self.assertCaseStatus(FreelancerTermination.Status.REVOKED)
        self.assertMemberStillOnTheTeam()

        self.client.force_login(self.freelancer)
        overview = self.client.get(self.url('rooms:room_overview'))
        self.assertNotContains(overview, TerminationModalTests.MARKER)

    def test_only_the_current_project_teamlead_may_revoke(self):
        """Ни фрилансер, ни директор, ни платформенный admin, ни чужой тимлид."""
        platform_admin = make_user(
            email='admin@block.test', role=User.Roles.ADMIN,
        )
        other_teamlead = make_teamlead(email='tl2@block.test')

        for label, user in (
            ('freelancer', self.freelancer),
            ('director', self.director),
            ('platform_admin', platform_admin),
            ('foreign_teamlead', other_teamlead),
        ):
            with self.subTest(user=label):
                response = self.post_action(
                    'rooms:room_termination_revoke', user,
                )
                self.assertEqual(response.status_code, 403)

        self.assertCaseStatus(FreelancerTermination.Status.NOTICE_SENT)
        self.assertMemberStillOnTheTeam()

    def test_replaced_teamlead_loses_the_right_to_revoke(self):
        """Право у того, кто ведёт команду сейчас, а не у автора уведомления."""
        successor = make_teamlead(email='tl-new@block.test')
        # Прямая замена поля: проверяется правило доступа, а не сервис
        # назначения тимлида со всеми его побочными эффектами.
        self.project.teamlead = successor
        self.project.save(update_fields=['teamlead'])

        refused = self.post_action('rooms:room_termination_revoke', self.teamlead)
        self.assertEqual(refused.status_code, 403)
        self.assertCaseStatus(FreelancerTermination.Status.NOTICE_SENT)

        allowed = self.post_action('rooms:room_termination_revoke', successor)
        self.assertEqual(allowed.status_code, 302)
        self.assertCaseStatus(FreelancerTermination.Status.REVOKED)
        self.assertMemberStillOnTheTeam()

    def test_completed_case_is_never_reactivated(self):
        complete_termination(self.case)

        response = self.post_action('rooms:room_termination_revoke', self.teamlead)

        self.assertEqual(response.status_code, 400)
        self.assertCaseStatus(FreelancerTermination.Status.COMPLETED)
        self.assertMemberStaysArchived()
        self.assertFalse(user_can_work_in_room(self.freelancer, self.project))

    def test_revoked_case_cannot_be_revoked_twice(self):
        revoke_termination(self.case)

        response = self.post_action('rooms:room_termination_revoke', self.teamlead)

        self.assertEqual(response.status_code, 400)
        self.assertCaseStatus(FreelancerTermination.Status.REVOKED)

    def test_action_is_post_only(self):
        self.client.force_login(self.teamlead)

        response = self.client.get(self.action_url('rooms:room_termination_revoke'))

        self.assertEqual(response.status_code, 405)
        self.assertCaseStatus(FreelancerTermination.Status.NOTICE_SENT)


class TerminationModalActionsTests(TerminationActionTestCase):
    """Две кнопки фрилансера в модалке: точные подписи и настоящие POST-формы."""

    #: Подписи проверяются приёмкой дословно, поэтому они константы.
    LEAVE_LABEL = 'Покинуть проект'
    APPEAL_LABEL = 'Опротестовать решение'

    def overview_as_freelancer(self):
        self.client.force_login(self.freelancer)
        return self.client.get(self.url('rooms:room_overview'))

    def assertPostForm(self, html, action_url, label):
        """Форма именно POST, с csrf_token и нужной подписью внутри."""
        match = re.search(
            r'<form[^>]*method="post"[^>]*action="%s"[^>]*>(.*?)</form>'
            % re.escape(action_url),
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match, f'Нет POST-формы на {action_url}')
        body = match.group(1)
        self.assertIn('csrfmiddlewaretoken', body)
        self.assertIn(label, body)

    def test_notice_modal_offers_both_actions(self):
        response = self.overview_as_freelancer()
        html = response.content.decode()

        self.assertContains(response, TerminationModalTests.MARKER)
        self.assertContains(response, self.LEAVE_LABEL)
        self.assertContains(response, self.APPEAL_LABEL)
        self.assertPostForm(
            html,
            self.action_url('rooms:room_termination_leave'),
            self.LEAVE_LABEL,
        )
        self.assertPostForm(
            html,
            self.action_url('rooms:room_termination_appeal'),
            self.APPEAL_LABEL,
        )

    def test_appeal_modal_drops_both_actions(self):
        self.post_action('rooms:room_termination_appeal', self.freelancer)

        response = self.overview_as_freelancer()

        self.assertContains(response, TerminationModalTests.MARKER)
        self.assertContains(response, 'Ожидайте ответа поддержки')
        self.assertNotContains(response, self.LEAVE_LABEL)
        self.assertNotContains(response, self.APPEAL_LABEL)
        self.assertNotContains(
            response, self.action_url('rooms:room_termination_leave'),
        )
        self.assertNotContains(
            response, self.action_url('rooms:room_termination_appeal'),
        )
        # Переписка с тимлидом при этом остаётся доступной.
        self.assertContains(
            response, self.action_url('rooms:room_termination_send'),
        )


class TerminationRevokeButtonTests(TerminationActionTestCase):
    """Кнопка отзыва на странице расторжения у тимлида."""

    LABEL = 'Отозвать увольнение'

    def setUp(self):
        super().setUp()
        self.form_url = self.url(
            'rooms:room_termination_form', member_id=self.member.id,
        )

    def get_form_page(self, user=None):
        self.client.force_login(user or self.teamlead)
        return self.client.get(self.form_url)

    def test_open_case_page_shows_a_working_revoke_form(self):
        response = self.get_form_page()
        html = response.content.decode()
        revoke_url = self.action_url('rooms:room_termination_revoke')

        self.assertContains(response, self.LABEL)
        match = re.search(
            r'<form[^>]*method="post"[^>]*action="%s"[^>]*>(.*?)</form>'
            % re.escape(revoke_url),
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        self.assertIn('csrfmiddlewaretoken', match.group(1))
        self.assertIn(self.LABEL, match.group(1))

    def test_revoke_button_stays_while_the_case_is_appealed(self):
        appeal_termination(self.case, admin_url=ADMIN_URL)

        response = self.get_form_page()

        self.assertContains(response, self.LABEL)
        self.assertContains(
            response, self.action_url('rooms:room_termination_revoke'),
        )

    def test_no_revoke_button_after_the_case_is_closed(self):
        revoke_termination(self.case)

        response = self.get_form_page()

        self.assertNotContains(response, self.LABEL)
        self.assertNotContains(
            response, self.action_url('rooms:room_termination_revoke'),
        )

class ArchiveSurfaceTests(WorkBlockingTestCase):
    """Поверхности архива после завершённого расторжения.

    Проверяется то, что человек видит и чего больше не видит: список комнат,
    оперативный состав команды, страница расторжения, «Обзор» и плитка
    дашборда. Блокировки записи проверены раньше (этапы 4.2 и 7) и здесь не
    дублируются.
    """

    #: Точный заголовок секции архива в списке комнат.
    ARCHIVE_HEADING = 'Архивные комнаты'

    #: Устойчивые маркеры разметки — по ним режутся нужные куски страницы,
    #: чтобы не проверять имя человека глобальным `assertNotContains`.
    ARCHIVE_SECTION_MARKER = 'id="archived-rooms"'
    MEMBERS_TABLE_MARKER = 'id="room-members"'
    ARCHIVE_NOTICE_MARKER = 'room-archive-notice'

    OPERATIONAL_TABS = (
        'rooms:room_team',
        'rooms:room_documents',
        'rooms:room_comms',
        'pipeline:room_tasks',
        'pipeline:room_leads',
    )

    def archive_the_freelancer(self):
        """Полный путь до архива: уведомление → завершение расторжения."""
        return complete_termination(self.initiate())

    def project_list_as(self, user):
        self.client.force_login(user)
        return self.client.get(reverse('rooms:project_list'))

    def section_of(self, response, marker, closing='</table>'):
        """Кусок страницы от маркера до конца его таблицы."""
        html = response.content.decode()
        start = html.index(marker)
        return html[start:html.index(closing, start)]

    # -- A. Список комнат --------------------------------------------------

    def test_active_room_stays_operational_and_archive_section_is_absent(self):
        response = self.project_list_as(self.freelancer)

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.project, list(response.context['projects']))
        self.assertEqual(list(response.context['archived_projects']), [])
        self.assertNotContains(response, self.ARCHIVE_HEADING)

    def test_completed_case_moves_the_room_into_the_archive_section(self):
        self.archive_the_freelancer()

        response = self.project_list_as(self.freelancer)

        self.assertNotIn(self.project, list(response.context['projects']))
        self.assertIn(self.project, list(response.context['archived_projects']))
        self.assertContains(response, self.ARCHIVE_HEADING)
        self.assertContains(response, self.project.name)

    def test_archived_row_leads_to_the_overview_only(self):
        self.archive_the_freelancer()

        response = self.project_list_as(self.freelancer)
        section = self.section_of(response, self.ARCHIVE_SECTION_MARKER)

        self.assertIn(self.url('rooms:room_overview'), section)
        for name in self.OPERATIONAL_TABS:
            with self.subTest(tab=name):
                self.assertNotIn(self.url(name), section)

    # -- B. Оперативный состав команды ------------------------------------

    def test_archived_member_disappears_from_the_team_tab(self):
        teamlead_member = RoomMember.objects.get(
            room=self.room, user=self.teamlead,
        )
        self.archive_the_freelancer()
        self.client.force_login(self.teamlead)

        response = self.client.get(self.url('rooms:room_team'))

        self.assertEqual(response.status_code, 200)
        member_ids = [item.id for item in response.context['members']]
        self.assertNotIn(self.member.id, member_ids)
        # Активный состав не пострадал: тимлид на месте.
        self.assertIn(teamlead_member.id, member_ids)
        # И в самой таблице участников человека тоже нет.
        table = self.section_of(response, self.MEMBERS_TABLE_MARKER)
        self.assertNotIn(self.freelancer.email, table)
        self.assertIn(self.teamlead.email, table)

        # Строка членства сохранена — она ушла в архив, а не удалена.
        self.assertTrue(RoomMember.objects.filter(pk=self.member.pk).exists())
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)

    def test_shared_members_helper_hides_the_archive_on_both_paths(self):
        """Вкладка «Команда» и OOB-обновление после подбора — один хелпер.

        Поэтому фильтр проверяется на нём, а не сборкой полного сценария
        подбора: разойтись между обычным и HTMX-рендером он не может.
        """
        self.archive_the_freelancer()

        members = _members_with_termination(self.room)

        self.assertNotIn(self.member.pk, [item.pk for item in members])
        self.assertIn(
            RoomMember.objects.get(room=self.room, user=self.teamlead).pk,
            [item.pk for item in members],
        )

    # -- D. Страница расторжения ------------------------------------------

    def test_termination_page_is_gone_for_an_archived_member(self):
        self.archive_the_freelancer()
        self.client.force_login(self.teamlead)

        response = self.client.get(
            self.url('rooms:room_termination_form', member_id=self.member.id)
        )

        self.assertEqual(response.status_code, 404)

    def test_new_notice_for_an_archived_member_creates_nothing(self):
        """Прямой POST мимо страницы тоже не заводит второй кейс."""
        self.archive_the_freelancer()
        self.client.force_login(self.teamlead)

        response = self.client.post(
            self.url('rooms:room_remove_member', member_id=self.member.id),
            {'reason': REASON},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            FreelancerTermination.objects.filter(
                room=self.room, freelancer=self.freelancer,
            ).count(),
            1,
        )
        self.assertTrue(RoomMember.objects.filter(pk=self.member.pk).exists())

    # -- E. «Обзор» архивной комнаты --------------------------------------

    def test_archived_overview_is_read_only_but_complete(self):
        self.archive_the_freelancer()
        self.client.force_login(self.freelancer)

        response = self.client.get(self.url('rooms:room_overview'))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_archived_member'])
        self.assertContains(response, self.ARCHIVE_NOTICE_MARKER)
        self.assertContains(response, 'Комната в архиве')
        # Свои цифры и вводные проекта на месте.
        self.assertIsNotNone(response.context['my_project_stats'])
        self.assertContains(response, 'freelancer-project-stats')
        self.assertContains(response, 'Вводные проекта')
        # Прошлые задачи остались текстом: превью не навигация, а история.
        self.assertContains(response, self.task.title)
        # Операционной навигации нет (гейт стоял и раньше — здесь только UI).
        for name in self.OPERATIONAL_TABS:
            with self.subTest(tab=name):
                self.assertNotContains(response, self.url(name))

    def test_active_overview_has_no_archive_notice(self):
        self.client.force_login(self.freelancer)

        response = self.client.get(self.url('rooms:room_overview'))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['is_archived_member'])
        self.assertNotContains(response, self.ARCHIVE_NOTICE_MARKER)
        # Действующему участнику превью задач остаётся навигацией.
        self.assertContains(response, self.url('pipeline:room_tasks'))

    # -- F. Плитка дашборда ------------------------------------------------

    def test_dashboard_room_count_ignores_the_archive(self):
        self.assertEqual(
            freelancer_metrics(self.freelancer)['rooms_count'], 1,
        )

        self.archive_the_freelancer()

        self.assertEqual(
            freelancer_metrics(self.freelancer)['rooms_count'], 0,
        )
        # Метрика считает членство, а не удаляет его.
        self.assertTrue(RoomMember.objects.filter(pk=self.member.pk).exists())

    # -- G. Регрессия остальных ролей -------------------------------------

    def test_project_list_semantics_are_unchanged_for_other_roles(self):
        self.archive_the_freelancer()

        for label, user in (
            ('teamlead', self.teamlead),
            ('director', self.director),
        ):
            with self.subTest(role=label):
                response = self.project_list_as(user)
                self.assertEqual(response.status_code, 200)
                self.assertIn(self.project, list(response.context['projects']))
                self.assertEqual(
                    list(response.context['archived_projects']), [],
                )
                self.assertNotContains(response, self.ARCHIVE_HEADING)

class MemberReactivationTests(TerminationDomainTestCase):
    """Повторный найм: тот же `RoomMember`, а не второй участник.

    Фикстура удобна именно этим: в комнате есть слот, годный под кандидата
    профиль и активное членство на слоте — то есть всё, что нужно, чтобы
    провести человека через расторжение и вернуть обратно и формой, и
    подбором.
    """

    def archive_member(self, *, ready=True):
        """Полный путь в архив: готовность → уведомление → завершение.

        Готовность выставляется намеренно: сброс `ready_status` при
        возвращении иначе был бы проверен на значении по умолчанию и ничего
        не доказывал.
        """
        if ready:
            self.member.ready_status = RoomMember.ReadyStatus.READY
            self.member.save(update_fields=['ready_status'])
        complete_termination(self.initiate())
        self.member.refresh_from_db()
        return self.member

    def member_added_events(self):
        return RoomActivity.objects.filter(
            room=self.room, event_type=RoomActivity.EventType.MEMBER_ADDED,
        ).count()

    def assertBackOnTheTeam(self, member, *, slot=None):
        """Общий набор признаков вернувшегося участника."""
        self.assertEqual(member.pk, self.member.pk)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertIsNone(self.member.left_at)
        self.assertEqual(
            self.member.ready_status, RoomMember.ReadyStatus.PENDING,
        )
        self.assertEqual(
            self.member.role_in_room, RoomMember.RoleInRoom.FREELANCER,
        )
        if slot is not None:
            self.assertEqual(self.member.function_slot_id, slot.pk)

    # -- A. Центральный хелпер --------------------------------------------

    def test_reactivation_reuses_the_same_row(self):
        self.archive_member()
        joined_at = self.member.joined_at
        rows_before = RoomMember.objects.count()
        events_before = self.member_added_events()

        result = reactivate_room_member(
            self.member,
            actor=self.teamlead,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )

        self.assertBackOnTheTeam(result)
        self.assertEqual(RoomMember.objects.count(), rows_before)
        # Дата первого вступления историческая и вторым заходом не сдвигается.
        self.assertEqual(self.member.joined_at, joined_at)
        # Возвращение — событие комнаты, и оно записано существующим типом.
        self.assertEqual(self.member_added_events(), events_before + 1)

    def test_reactivation_of_an_active_member_changes_no_lifecycle(self):
        """Повторный вызов не сбрасывает готовность работающему человеку."""
        self.member.ready_status = RoomMember.ReadyStatus.READY
        self.member.save(update_fields=['ready_status'])
        rows_before = RoomMember.objects.count()
        events_before = self.member_added_events()

        result = reactivate_room_member(self.member, actor=self.teamlead)

        self.assertEqual(result.pk, self.member.pk)
        self.assertEqual(RoomMember.objects.count(), rows_before)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertEqual(self.member.ready_status, RoomMember.ReadyStatus.READY)
        self.assertEqual(self.member_added_events(), events_before)

    def test_reactivation_can_seat_the_member_on_a_slot(self):
        self.archive_member()
        self.assertIsNone(self.member.function_slot_id)

        reactivate_room_member(
            self.member,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            slot=self.slot,
        )

        self.assertBackOnTheTeam(self.member, slot=self.slot)
        # `role_key` держит в согласии со слотом сама модель.
        self.assertEqual(self.member.role_key, self.slot.role_key)

    # -- B. Добавление в комнату ------------------------------------------

    def test_add_freelancer_reactivates_the_archived_row(self):
        self.archive_member()
        rows_before = RoomMember.objects.count()
        events_before = self.member_added_events()

        member = add_freelancer_to_room(
            self.room, self.freelancer, actor=self.teamlead,
        )

        self.assertBackOnTheTeam(member)
        self.assertEqual(RoomMember.objects.count(), rows_before)
        self.assertEqual(self.member_added_events(), events_before + 1)

    def test_add_freelancer_keeps_the_semantics_for_an_active_member(self):
        self.member.ready_status = RoomMember.ReadyStatus.READY
        self.member.save(update_fields=['ready_status'])
        rows_before = RoomMember.objects.count()
        events_before = self.member_added_events()

        member = add_freelancer_to_room(self.room, self.freelancer)

        self.assertEqual(member.pk, self.member.pk)
        self.assertEqual(RoomMember.objects.count(), rows_before)
        self.assertEqual(self.member_added_events(), events_before)
        self.member.refresh_from_db()
        self.assertEqual(self.member.ready_status, RoomMember.ReadyStatus.READY)

    def test_add_freelancer_still_creates_a_first_time_member(self):
        """Обычный путь не изменился: новый человек и событие ленты."""
        newcomer = make_freelancer(email='newcomer@staffed.test')
        rows_before = RoomMember.objects.count()
        events_before = self.member_added_events()

        member = add_freelancer_to_room(
            self.room, newcomer, actor=self.teamlead,
        )

        self.assertEqual(RoomMember.objects.count(), rows_before + 1)
        self.assertEqual(self.member_added_events(), events_before + 1)
        self.assertTrue(member.is_active)
        self.assertEqual(
            member.role_in_room, RoomMember.RoleInRoom.FREELANCER,
        )
        self.assertEqual(member.ready_status, RoomMember.ReadyStatus.PENDING)

    # -- C. Форма добавления ----------------------------------------------

    def test_form_hides_an_active_member_and_offers_an_archived_one(self):
        active_choices = AddFreelancerForm(room=self.room).fields['freelancer']
        self.assertNotIn(self.freelancer, list(active_choices.queryset))

        self.archive_member()

        archived_choices = AddFreelancerForm(room=self.room).fields['freelancer']
        self.assertIn(self.freelancer, list(archived_choices.queryset))

    # -- D. Подбор --------------------------------------------------------

    def test_active_member_stays_out_of_the_candidate_pool(self):
        pool = [item.user for item in get_ranked_candidates(self.slot)]

        self.assertNotIn(self.freelancer, pool)

    def test_archived_member_returns_to_the_candidate_pool(self):
        self.archive_member()

        pool = [item.user for item in get_ranked_candidates(self.slot)]

        self.assertIn(self.freelancer, pool)

    def test_open_case_keeps_the_member_out_of_the_pool(self):
        """Идёт расторжение — членство ещё активно, и кандидатом он не стал."""
        self.initiate()

        pool = [item.user for item in get_ranked_candidates(self.slot)]

        self.assertNotIn(self.freelancer, pool)

    # -- E. Назначение на слот --------------------------------------------

    def test_assign_reactivates_the_archived_member_on_the_slot(self):
        self.archive_member()
        rows_before = RoomMember.objects.count()

        member = assign_candidate_to_slot(
            self.slot, self.freelancer, self.teamlead,
        )

        self.assertBackOnTheTeam(member, slot=self.slot)
        self.assertEqual(RoomMember.objects.count(), rows_before)
        self.assertEqual(self.member.role_key, self.slot.role_key)

    def test_assign_still_rejects_an_active_member(self):
        spare_slot = RoomFunctionSlot.objects.create(
            room=self.room,
            role_key=self.slot.role_key,
            slot_index=self.slot.slot_index + 1,
            required_level=self.slot.required_level,
        )
        rows_before = RoomMember.objects.count()

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(spare_slot, self.freelancer, self.teamlead)

        self.assertEqual(RoomMember.objects.count(), rows_before)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertEqual(self.member.function_slot_id, self.slot.pk)

    def test_assign_refuses_an_occupied_slot_before_any_reactivation(self):
        """Занятый слот остаётся отказом: реактивация его не «перебивает»."""
        self.archive_member()
        newcomer = self.room.members.create(
            user=make_freelancer(email='sitter@staffed.test'),
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=self.slot,
        )

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(self.slot, self.freelancer, self.teamlead)

        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        newcomer.refresh_from_db()
        self.assertEqual(newcomer.function_slot_id, self.slot.pk)

    # -- F. Повторное расторжение -----------------------------------------

    def test_rehired_member_can_be_terminated_again(self):
        first = self.initiate()
        complete_termination(first)
        self.member.refresh_from_db()

        reactivate_room_member(
            self.member,
            actor=self.teamlead,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            slot=self.slot,
        )
        self.member.refresh_from_db()

        second = initiate_termination(
            room=self.room,
            member=self.member,
            initiated_by=self.teamlead,
            reason=REASON,
        )

        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(second.member_id, self.member.pk)
        self.assertEqual(
            second.status, FreelancerTermination.Status.NOTICE_SENT,
        )
        # Прошлый кейс остался в истории нетронутым.
        first.refresh_from_db()
        self.assertEqual(
            first.status, FreelancerTermination.Status.COMPLETED,
        )
        self.assertEqual(
            FreelancerTermination.objects.filter(
                room=self.room, freelancer=self.freelancer,
            ).count(),
            2,
        )

    # -- G. Назначение тимлидом -------------------------------------------

    def test_assign_teamlead_never_leaves_the_row_archived(self):
        self.archive_member()
        rows_before = RoomMember.objects.count()

        member = assign_teamlead(
            self.project, self.freelancer, actor=self.project.owner,
        )

        self.assertEqual(member.pk, self.member.pk)
        self.assertEqual(RoomMember.objects.count(), rows_before)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertIsNone(self.member.left_at)
        self.assertEqual(
            self.member.role_in_room, RoomMember.RoleInRoom.TEAMLEAD,
        )

class ActiveRestaffingTerminationTests(TestCase):
    """Слот во время открытого кейса и после завершённого расторжения.

    Своя фикстура, а не `TerminationDomainTestCase`: нужен пул из нескольких
    подходящих кандидатов (замена, повторный подбор, матрица статусов) и
    HTTP-клиент для проверки самой карточки слота.
    """

    def setUp(self):
        self.client = Client()
        fixture = make_staffed_project(slots=1, candidates=4, prefix='restaff')
        self.project = fixture.project
        self.room = fixture.room
        self.director = fixture.director
        self.teamlead = fixture.teamlead
        self.slot = fixture.slots[0]
        self.pool = fixture.candidates
        self.freelancer = self.pool[0]
        self.replacement = self.pool[1]
        self.member = RoomMember.objects.create(
            room=self.room,
            user=self.freelancer,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=self.slot,
        )
        self.replace_url = reverse(
            'rooms:room_slot_replace',
            kwargs={'project_id': self.project.id, 'slot_id': self.slot.id},
        )
        self.auto_assign_url = reverse(
            'rooms:room_slot_auto_assign',
            kwargs={'project_id': self.project.id, 'slot_id': self.slot.id},
        )

    # -- вспомогательное ---------------------------------------------------

    def initiate(self):
        return initiate_termination(
            room=self.room,
            member=self.member,
            initiated_by=self.teamlead,
            reason=REASON,
        )

    def set_status(self, status):
        """Статус проекта ставится напрямую.

        Переход `STAFFING → ACTIVE` делает `sync_project_activation` по
        готовности всей команды, и эта задача его не меняет: здесь
        проверяется подбор **на уже запущенном** проекте, а не сам переход.
        """
        self.project.status = status
        self.project.save(update_fields=['status'])

    def spare_slot(self, index):
        return RoomFunctionSlot.objects.create(
            room=self.room,
            role_key=self.slot.role_key,
            slot_index=index,
            required_level=self.slot.required_level,
        )

    def team_page(self):
        self.client.force_login(self.teamlead)
        return self.client.get(
            reverse('rooms:room_team', kwargs={'project_id': self.project.id})
        )

    def card(self):
        """Карточка слота тем же селектором, что отдаёт HTMX-ответ."""
        return selectors.slot_card_for(self.slot)

    def assertStillOnTheSlot(self):
        self.assertTrue(RoomMember.objects.filter(pk=self.member.pk).exists())
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertEqual(self.member.function_slot_id, self.slot.pk)

    # -- A. Гард открытого кейса ------------------------------------------

    def test_replace_is_blocked_while_the_notice_is_sent(self):
        self.initiate()

        with self.assertRaises(StaffingError):
            replace_slot_member(self.slot, self.teamlead)

        self.assertStillOnTheSlot()

    def test_replace_is_blocked_while_the_appeal_is_pending(self):
        appeal_termination(self.initiate(), admin_url=ADMIN_URL)

        with self.assertRaises(StaffingError):
            replace_slot_member(self.slot, self.teamlead)

        self.assertStillOnTheSlot()

    def test_replace_works_again_after_revoke(self):
        revoke_termination(self.initiate())

        outcome = replace_slot_member(self.slot, self.teamlead)

        self.assertEqual(outcome.code, 'replaced')
        self.assertEqual(outcome.member.user, self.replacement)

    # -- B. Карточка слота -------------------------------------------------

    def test_slot_card_hides_replace_while_the_case_is_open(self):
        self.initiate()

        response = self.team_page()

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.replace_url)
        self.assertNotContains(response, 'Другой сейлер')
        self.assertContains(response, 'Идёт расторжение')
        # Тот же флаг получает и HTMX-ответ подбора (OOB-обновление карточки).
        card = self.card()
        self.assertTrue(card.has_open_termination)
        self.assertFalse(card.can_replace_member)

    def test_slot_card_returns_the_replace_control_after_revoke(self):
        revoke_termination(self.initiate())

        response = self.team_page()

        self.assertContains(response, self.replace_url)
        self.assertContains(response, 'Другой сейлер')
        card = self.card()
        self.assertFalse(card.has_open_termination)
        self.assertTrue(card.can_replace_member)

    def test_slot_cards_stay_at_two_queries_for_any_number_of_slots(self):
        """Признак расторжения считается один раз на комнату, а не на карточку.

        Два запроса: слоты с участником и профилем (`select_related`) и один
        bulk-набор открытых кейсов комнаты. Рост числа слотов запросов не
        добавляет — иначе вкладка «Команда» получила бы N+1.
        """
        for index in range(2, 6):
            self.spare_slot(index)
        self.initiate()

        with self.assertNumQueries(2):
            cards = selectors.slot_cards(self.room)
            flagged = [card for card in cards if card.has_open_termination]

        self.assertEqual(len(cards), 5)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].member.user, self.freelancer)

    # -- C. ACTIVE + пустой слот ------------------------------------------

    def test_active_empty_slot_can_be_staffed(self):
        complete_termination(self.initiate())
        self.set_status(Project.Status.ACTIVE)

        member = assign_candidate_to_slot(
            self.slot, self.replacement, self.teamlead,
        )

        self.assertEqual(member.function_slot_id, self.slot.pk)
        self.assertTrue(member.is_active)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

    def test_active_empty_slot_allows_auto_assign(self):
        complete_termination(self.initiate())
        self.set_status(Project.Status.ACTIVE)

        outcome = auto_assign_best_candidate(self.slot, self.teamlead)

        self.assertTrue(outcome.assigned)
        self.assertEqual(outcome.member.function_slot_id, self.slot.pk)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)

    def test_active_empty_slot_shows_the_fill_controls(self):
        complete_termination(self.initiate())
        self.set_status(Project.Status.ACTIVE)

        response = self.team_page()

        self.assertContains(response, self.auto_assign_url)
        self.assertContains(response, 'Подобрать лучшего')
        self.assertContains(response, 'Выбрать из пула')

    # -- D. ACTIVE + занятый слот -----------------------------------------

    def test_active_occupied_slot_still_refuses_ordinary_replace(self):
        self.set_status(Project.Status.ACTIVE)

        with self.assertRaises(StaffingError):
            replace_slot_member(self.slot, self.teamlead)

        self.assertStillOnTheSlot()

    def test_active_occupied_slot_refuses_assignment_over_the_current_member(self):
        """Послабление для пустого слота не открыло назначение поверх занятого."""
        self.set_status(Project.Status.ACTIVE)

        with self.assertRaises(StaffingError):
            assign_candidate_to_slot(self.slot, self.replacement, self.teamlead)

        self.assertStillOnTheSlot()
        self.assertFalse(
            RoomMember.objects.filter(
                room=self.room, user=self.replacement,
            ).exists()
        )

    def test_active_occupied_slot_shows_no_replace_control(self):
        self.set_status(Project.Status.ACTIVE)

        response = self.team_page()

        self.assertNotContains(response, self.replace_url)
        self.assertNotContains(response, 'Другой сейлер')

    # -- E. Расторжение → повторный подбор на ACTIVE ----------------------

    def test_completed_termination_frees_the_slot_on_an_active_project(self):
        """Полный путь: ACTIVE → расторжение → штатный подбор нового человека."""
        self.set_status(Project.Status.ACTIVE)
        case = self.initiate()
        complete_termination(case)

        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertIsNone(self.member.function_slot_id)
        self.assertIsNone(selectors.slot_card_for(self.slot).member)

        # Штатный HTTP-путь тимлида: пул кандидатов → ручное назначение.
        self.client.force_login(self.teamlead)
        pool_url = reverse(
            'rooms:room_slot_candidates',
            kwargs={'project_id': self.project.id, 'slot_id': self.slot.id},
        )
        self.assertEqual(self.client.get(pool_url).status_code, 200)

        response = self.client.post(
            reverse(
                'rooms:room_slot_assign_candidate',
                kwargs={
                    'project_id': self.project.id,
                    'slot_id': self.slot.id,
                    'candidate_id': self.replacement.id,
                },
            )
        )

        self.assertEqual(response.status_code, 302)
        new_member = RoomMember.objects.get(
            room=self.room, user=self.replacement,
        )
        self.assertEqual(new_member.function_slot_id, self.slot.pk)
        self.assertTrue(new_member.is_active)

        # Проект остался запущенным, а история ушедшего — на месте.
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, Project.Status.ACTIVE)
        self.assertTrue(RoomMember.objects.filter(pk=self.member.pk).exists())
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        case.refresh_from_db()
        self.assertEqual(case.status, FreelancerTermination.Status.COMPLETED)

    # -- F. Матрица статусов ----------------------------------------------

    def test_empty_slot_assignment_matrix_by_project_status(self):
        """DRAFT/STAFFING/ACTIVE заполняют пустой слот, закрытые статусы — нет."""
        for index, status in enumerate(
            (Project.Status.DRAFT, Project.Status.STAFFING, Project.Status.ACTIVE)
        ):
            with self.subTest(status=status, expected='allowed'):
                self.set_status(status)
                slot = self.spare_slot(index + 2)
                member = assign_candidate_to_slot(
                    slot, self.pool[index + 1], self.teamlead,
                )
                self.assertEqual(member.function_slot_id, slot.pk)

        for status in (
            Project.Status.COMPLETED,
            Project.Status.ARCHIVED,
            Project.Status.ON_HOLD,
        ):
            with self.subTest(status=status, expected='forbidden'):
                self.set_status(status)
                slot = self.spare_slot(90)
                with self.assertRaisesMessage(
                    StaffingError, 'пока проект набирает команду',
                ):
                    assign_candidate_to_slot(slot, self.pool[0], self.teamlead)
                slot.delete()

class SupportDecisionTestCase(TestCase):
    """Общая фикстура решений поддержки и пакетной команды.

    Каждый кейс живёт в своей комнате: частичный уникальный индекс
    разрешает паре комната+фрилансер только один открытый кейс, поэтому
    набрать выборку из разных статусов можно лишь разными комнатами.
    """

    def make_case(self, prefix, *, status=FreelancerTermination.Status.NOTICE_SENT):
        """Комната с фрилансером на слоте и кейсом в нужном статусе.

        Статусы выставляются доменными операциями, а не `update()`:
        фикстура не должна уметь то, чего не умеет продукт.
        """
        fixture = make_staffed_project(slots=1, candidates=1, prefix=prefix)
        member = RoomMember.objects.create(
            room=fixture.room,
            user=fixture.candidates[0],
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
            function_slot=fixture.slots[0],
        )
        case = initiate_termination(
            room=fixture.room,
            member=member,
            initiated_by=fixture.teamlead,
            reason=REASON,
        )
        if status == FreelancerTermination.Status.APPEAL_PENDING:
            appeal_termination(case, admin_url=ADMIN_URL)
        elif status == FreelancerTermination.Status.COMPLETED:
            complete_termination(case)
        elif status == FreelancerTermination.Status.REVOKED:
            revoke_termination(case)
        case.refresh_from_db()
        member.refresh_from_db()
        return case, member

    def expire(self, case):
        """Сдвигает срок ответа в прошлое, не трогая статус."""
        FreelancerTermination.objects.filter(pk=case.pk).update(
            deadline_at=timezone.now() - timedelta(hours=1),
        )

    def assertStatus(self, case, status):
        case.refresh_from_db()
        self.assertEqual(case.status, status)

    def assertStillWorking(self, member):
        """Человек в команде и на своём слоте: расторжение его не задело."""
        member.refresh_from_db()
        self.assertTrue(member.is_active)
        self.assertIsNone(member.left_at)
        self.assertIsNotNone(member.function_slot_id)

    def assertArchived(self, member):
        """Членство архивировано, но строка на месте, а слот свободен."""
        self.assertTrue(RoomMember.objects.filter(pk=member.pk).exists())
        member.refresh_from_db()
        self.assertFalse(member.is_active)
        self.assertIsNotNone(member.left_at)
        self.assertIsNone(member.function_slot_id)


class SupportAdminActionTests(SupportDecisionTestCase):
    """Решение поддержки по протесту — два действия списка в админке.

    Действия вызываются напрямую у `ModelAdmin`: проверяется их контракт, а
    не то, как Django рисует страницу списка и проверяет права staff — это
    его собственная, уже покрытая ответственность.
    """

    def setUp(self):
        self.model_admin = FreelancerTerminationAdmin(
            FreelancerTermination, AdminSite(),
        )
        self.support = make_user(
            email='support@wowlance.test',
            role=User.Roles.ADMIN,
            is_staff=True,
        )

    def build_request(self):
        request = RequestFactory().post('/admin/rooms/freelancertermination/')
        request.user = self.support
        # `message_user` пишет во фреймворк сообщений: в обход middleware
        # хранилище подставляется вручную.
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def run_action(self, name, cases):
        """Действие поверх выборки из переданных кейсов."""
        queryset = FreelancerTermination.objects.filter(
            pk__in=[case.pk for case in cases]
        )
        getattr(self.model_admin, name)(self.build_request(), queryset)

    # -- A. Оставить в силе ------------------------------------------------

    def test_uphold_completes_an_appealed_case(self):
        case, member = self.make_case(
            'uphold', status=FreelancerTermination.Status.APPEAL_PENDING,
        )
        activities_before = RoomActivity.objects.filter(
            room=case.room, event_type=RoomActivity.EventType.MEMBER_REMOVED,
        ).count()

        self.run_action('uphold_termination', [case])

        self.assertStatus(case, FreelancerTermination.Status.COMPLETED)
        self.assertArchived(member)
        # Событие ленты пишет доменная операция — тем же путём, что уход по
        # кнопке и по сроку; второго события админка не добавляет.
        self.assertEqual(
            RoomActivity.objects.filter(
                room=case.room,
                event_type=RoomActivity.EventType.MEMBER_REMOVED,
            ).count(),
            activities_before + 1,
        )

    def test_uphold_ignores_a_notice_that_is_not_appealed(self):
        case, member = self.make_case('uphold-notice')

        self.run_action('uphold_termination', [case])

        self.assertStatus(case, FreelancerTermination.Status.NOTICE_SENT)
        self.assertStillWorking(member)

    def test_uphold_ignores_terminal_cases(self):
        for label, status in (
            ('completed', FreelancerTermination.Status.COMPLETED),
            ('revoked', FreelancerTermination.Status.REVOKED),
        ):
            with self.subTest(status=label):
                case, member = self.make_case(f'uphold-{label}', status=status)

                self.run_action('uphold_termination', [case])

                self.assertStatus(case, status)
                if status == FreelancerTermination.Status.COMPLETED:
                    self.assertArchived(member)
                else:
                    self.assertStillWorking(member)

    # -- B. Отклонить расторжение -----------------------------------------

    def test_reject_revokes_an_appealed_case(self):
        case, member = self.make_case(
            'reject', status=FreelancerTermination.Status.APPEAL_PENDING,
        )

        self.run_action('reject_termination', [case])

        self.assertStatus(case, FreelancerTermination.Status.REVOKED)
        self.assertStillWorking(member)
        self.assertTrue(
            user_can_work_in_room(member.user, case.room.project)
        )

    def test_reject_ignores_a_notice_that_is_not_appealed(self):
        case, member = self.make_case('reject-notice')

        self.run_action('reject_termination', [case])

        self.assertStatus(case, FreelancerTermination.Status.NOTICE_SENT)
        self.assertStillWorking(member)

    def test_reject_never_reactivates_a_completed_case(self):
        case, member = self.make_case(
            'reject-done', status=FreelancerTermination.Status.COMPLETED,
        )

        self.run_action('reject_termination', [case])

        self.assertStatus(case, FreelancerTermination.Status.COMPLETED)
        self.assertArchived(member)
        self.assertFalse(
            user_can_work_in_room(member.user, case.room.project)
        )

    # -- C. Смешанная выборка и гонка --------------------------------------

    def test_mixed_selection_decides_only_the_appealed_case(self):
        appealed, appealed_member = self.make_case(
            'mixed-appeal', status=FreelancerTermination.Status.APPEAL_PENDING,
        )
        notice, notice_member = self.make_case('mixed-notice')
        done, done_member = self.make_case(
            'mixed-done', status=FreelancerTermination.Status.COMPLETED,
        )

        self.run_action('uphold_termination', [appealed, notice, done])

        self.assertStatus(appealed, FreelancerTermination.Status.COMPLETED)
        self.assertArchived(appealed_member)
        self.assertStatus(notice, FreelancerTermination.Status.NOTICE_SENT)
        self.assertStillWorking(notice_member)
        self.assertStatus(done, FreelancerTermination.Status.COMPLETED)
        self.assertArchived(done_member)

    def test_mixed_selection_rejects_only_the_appealed_case(self):
        appealed, appealed_member = self.make_case(
            'mixed2-appeal', status=FreelancerTermination.Status.APPEAL_PENDING,
        )
        notice, notice_member = self.make_case('mixed2-notice')

        self.run_action('reject_termination', [appealed, notice])

        self.assertStatus(appealed, FreelancerTermination.Status.REVOKED)
        self.assertStillWorking(appealed_member)
        self.assertStatus(notice, FreelancerTermination.Status.NOTICE_SENT)
        self.assertStillWorking(notice_member)

    def test_case_decided_elsewhere_is_skipped_not_crashed(self):
        """Гонка: кейс решили между отрисовкой списка и нажатием кнопки.

        Действию отдаются уже прочитанные объекты со старым статусом —
        ровно то, что видит админка. Домен перепроверяет состояние под
        блокировкой, и запись просто пропускается.
        """
        case, member = self.make_case(
            'race', status=FreelancerTermination.Status.APPEAL_PENDING,
        )
        stale = list(FreelancerTermination.objects.filter(pk=case.pk))
        revoke_termination(case)

        self.model_admin.uphold_termination(self.build_request(), stale)

        self.assertStatus(case, FreelancerTermination.Status.REVOKED)
        self.assertStillWorking(member)


class FinalizeExpiredTerminationsCommandTests(SupportDecisionTestCase):
    """`manage.py finalize_expired_terminations`: пакетная половина срока.

    Ленивое закрытие срока на заходе в комнату проверяется своими тестами
    (`LazyTerminationExpiryTests`) и здесь не дублируется — проверяется сама
    команда: что она берёт домен, считает и печатает число.
    """

    def run_command(self):
        out = StringIO()
        call_command('finalize_expired_terminations', stdout=out)
        return out.getvalue()

    def test_expired_notice_is_finalized_and_counted(self):
        case, member = self.make_case('cmd-expired')
        self.expire(case)

        output = self.run_command()

        self.assertIn('1', output)
        self.assertStatus(case, FreelancerTermination.Status.COMPLETED)
        self.assertArchived(member)

    def test_notice_within_the_deadline_is_untouched(self):
        case, member = self.make_case('cmd-future')

        output = self.run_command()

        self.assertIn('0', output)
        self.assertStatus(case, FreelancerTermination.Status.NOTICE_SENT)
        self.assertStillWorking(member)

    def test_appealed_case_never_expires(self):
        case, member = self.make_case(
            'cmd-appeal', status=FreelancerTermination.Status.APPEAL_PENDING,
        )
        self.expire(case)

        output = self.run_command()

        self.assertIn('0', output)
        self.assertStatus(case, FreelancerTermination.Status.APPEAL_PENDING)
        self.assertStillWorking(member)

    def test_every_expired_case_is_finalized(self):
        cases = []
        for index in range(3):
            case, member = self.make_case(f'cmd-batch{index}')
            self.expire(case)
            cases.append((case, member))

        output = self.run_command()

        self.assertIn('3', output)
        for case, member in cases:
            self.assertStatus(case, FreelancerTermination.Status.COMPLETED)
            self.assertArchived(member)

    def test_second_run_finalizes_nothing(self):
        case, _member = self.make_case('cmd-repeat')
        self.expire(case)
        self.assertIn('1', self.run_command())
        completed_at = FreelancerTermination.objects.get(pk=case.pk).completed_at

        output = self.run_command()

        self.assertIn('0', output)
        self.assertStatus(case, FreelancerTermination.Status.COMPLETED)
        # Повторный прогон не переписывает момент завершения.
        self.assertEqual(
            FreelancerTermination.objects.get(pk=case.pk).completed_at,
            completed_at,
        )

class AcceptanceNoticeModalSurfacesTests(TerminationActionTestCase):
    """Приёмка №3: причина и обе кнопки на каждой поверхности комнаты.

    Существующие тесты доказывают модалку на пяти поверхностях
    (`TerminationModalTests.test_modal_appears_on_every_room_surface`) и её
    полное содержимое на «Обзоре». Здесь закрывается ровно то, чего в них
    нет: причина и **точные** подписи действий на «Задачах» и «Материалах»,
    то есть там, куда фрилансер попадёт, минуя «Обзор».
    """

    SURFACES = (
        'rooms:room_overview',
        'pipeline:room_tasks',
        'rooms:room_documents',
    )

    def test_every_room_surface_carries_the_reason_and_both_actions(self):
        self.client.force_login(self.freelancer)

        for name in self.SURFACES:
            with self.subTest(page=name):
                response = self.client.get(self.url(name))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, TerminationModalTests.MARKER)
                self.assertContains(response, REASON)
                self.assertContains(response, 'Покинуть проект')
                self.assertContains(response, 'Опротестовать решение')
                self.assertContains(
                    response, self.action_url('rooms:room_termination_leave'),
                )
                self.assertContains(
                    response, self.action_url('rooms:room_termination_appeal'),
                )

    def test_teamlead_sees_no_blocking_modal_on_the_same_surfaces(self):
        self.client.force_login(self.teamlead)

        for name in self.SURFACES:
            with self.subTest(page=name):
                response = self.client.get(self.url(name))

                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, TerminationModalTests.MARKER)
                self.assertNotContains(response, 'Покинуть проект')
                self.assertNotContains(response, 'Опротестовать решение')


class AcceptanceLeavePreservesHistoryTests(TerminationActionTestCase):
    """Приёмка №5: уход из проекта не стирает ничего из истории человека.

    Один сквозной сценарий вместо набора точечных smoke-проверок: до ухода
    заводится вся история фрилансера по проекту, уход выполняется настоящим
    HTTP-действием кнопки «Покинуть проект», после чего проверяется и
    состояние членства, и сохранность каждой записи, и поверхности архива.

    Исторические объекты создаются моделями напрямую: сценарий проверяет
    последствия ухода, а не то, как эти записи заводятся в интерфейсе.
    Само действие ухода при этом идёт именно через endpoint.
    """

    def setUp(self):
        super().setUp()
        self.lead = Lead.objects.create(
            project=self.project,
            creator=self.freelancer,
            source=Lead.Source.BASE,
            qualification_status=Lead.Qualification.WARM,
        )
        self.report = Report.objects.create(
            task=self.task,
            author=self.freelancer,
            content_text='Обзвонил 40 контактов, 3 назначенные встречи.',
            review_status=Report.ReviewStatus.APPROVED,
        )
        self.accrual = FreelancerAccrual.objects.create(
            freelancer=self.freelancer,
            project=self.project,
            report=self.report,
            amount=Decimal('120.00'),
            title='Принятый отчёт по обзвону',
        )
        self.team_message = RoomChatMessage.objects.create(
            room=self.room,
            author=self.freelancer,
            text='Сообщение команде до ухода.',
            channel=RoomChatMessage.Channel.TEAM,
        )

    def test_leaving_the_project_preserves_the_whole_history(self):
        response = self.post_action(
            'rooms:room_termination_leave', self.freelancer,
        )

        # --- кейс и членство ---------------------------------------------
        self.assertEqual(response.status_code, 302)
        self.assertCaseStatus(FreelancerTermination.Status.COMPLETED)

        self.assertTrue(RoomMember.objects.filter(pk=self.member.pk).exists())
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertIsNotNone(self.member.left_at)
        self.assertIsNone(self.member.function_slot_id)

        # --- слот действительно пуст --------------------------------------
        self.assertFalse(
            RoomMember.objects.filter(function_slot=self.slot).exists()
        )
        self.assertIsNone(selectors.slot_card_for(self.slot).member)

        # --- ни одна историческая запись не удалена ------------------------
        for label, queryset in (
            ('RoomMember', RoomMember.objects.filter(pk=self.member.pk)),
            ('Task', Task.objects.filter(pk=self.task.pk)),
            ('Lead', Lead.objects.filter(pk=self.lead.pk)),
            ('Report', Report.objects.filter(pk=self.report.pk)),
            (
                'FreelancerAccrual',
                FreelancerAccrual.objects.filter(pk=self.accrual.pk),
            ),
            (
                'RoomChatMessage',
                RoomChatMessage.objects.filter(pk=self.team_message.pk),
            ),
        ):
            with self.subTest(model=label):
                self.assertTrue(queryset.exists())

        # --- и ни одна не переписана --------------------------------------
        self.task.refresh_from_db()
        # Задача не переназначена автоматически: исполнитель остаётся
        # историческим, а статус — прежним.
        self.assertEqual(self.task.assignee_id, self.freelancer.id)
        self.assertEqual(self.task.status, Task.Status.NEW)

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.creator_id, self.freelancer.id)

        self.report.refresh_from_db()
        self.assertEqual(self.report.author_id, self.freelancer.id)
        self.assertEqual(
            self.report.review_status, Report.ReviewStatus.APPROVED,
        )

        self.accrual.refresh_from_db()
        self.assertEqual(self.accrual.amount, Decimal('120.00'))
        self.assertEqual(self.accrual.report_id, self.report.pk)
        self.assertEqual(self.accrual.freelancer_id, self.freelancer.id)

        self.team_message.refresh_from_db()
        self.assertEqual(self.team_message.author_id, self.freelancer.id)
        self.assertEqual(
            self.team_message.channel, RoomChatMessage.Channel.TEAM,
        )

        # --- поверхности после ухода --------------------------------------
        self.client.force_login(self.teamlead)
        team = self.client.get(self.url('rooms:room_team'))
        self.assertEqual(team.status_code, 200)
        self.assertNotIn(
            self.member.id, [item.id for item in team.context['members']],
        )

        self.client.force_login(self.freelancer)
        projects = self.client.get(reverse('rooms:project_list'))
        self.assertEqual(projects.status_code, 200)
        self.assertNotIn(self.project, list(projects.context['projects']))
        self.assertIn(
            self.project, list(projects.context['archived_projects']),
        )
        self.assertContains(projects, 'Архивные комнаты')

        overview = self.client.get(self.url('rooms:room_overview'))
        self.assertEqual(overview.status_code, 200)
        self.assertNotContains(overview, TerminationModalTests.MARKER)
