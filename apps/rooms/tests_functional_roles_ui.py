"""UI конфигуратора функциональных ролей на «Обзоре» (Issue #11, UI-этап).

Покрывается только UI-контур поверх уже готового backend:

* рендер таблицы, публичных названий, экономики и итогов;
* editable / read-only по владельцу, роли и статусу проекта;
* фиксированный Тимлид в интерфейсе;
* автосохранение +/− / числового ввода, добавление и удаление функции;
* применение пакетов (в том числе LinkedIn);
* бюджет и KPI проекта после любого сохранения состава;
* HTMX-ответ как отдельный partial и fallback без HTMX;
* персистентность и регрессия соседних блоков комнаты.

Backend-правила (валидация, снапшот, бюджет, RBAC сервиса) живут в
`tests_functional_roles.py` и здесь не переписываются — проверяется, что UI
их не обходит и не дублирует.

Колонка «Подбор» читает только уже существующие слоты и ничего не создаёт.
Сама синхронизация состава со слотами (`functional_roles → RoomFunctionSlot`)
проверяется в `tests_functional_role_projection.py`; здесь — лишь то, что
продуктовые кнопки конфигуратора до неё доходят и не обходят её стороной.
"""

from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse

from apps.pipeline.models import Task
from apps.rooms import functional_roles as catalog
from apps.rooms import presets
from apps.rooms.configurator import (
    EMPTY_VALUE,
    HOT_UNIT,
    HOURS_UNIT,
    SEARCHING_LABEL,
    format_money,
)
from apps.rooms.models import (
    FunctionalRoleConfig,
    Project,
    Room,
    RoomFunctionSlot,
    RoomMember,
)
from apps.rooms.services import (
    add_freelancer_to_room,
    ensure_room_for_project,
    get_unit_economics_summary,
    update_project_functional_roles,
)
from apps.rooms.unit_economics import FUNCTIONAL_ROLES_KEY, get_project_composition
from apps.test_helpers import make_director, make_freelancer, make_teamlead, make_user
from apps.users.models import User

#: Публичные названия функций (Issue #11). Список берётся из каталога кода,
#: а не переписывается литералами: расхождение UI и каталога должно падать.
PUBLIC_TITLES = [role.label for role in catalog.FUNCTIONAL_ROLES.values()]

#: Корневой id единственного partial конфигуратора.
ROOT_ID = 'id="functional-roles-configurator"'


def composition_of(project) -> dict:
    """Сохранённый состав как `{role_key: count}` — прямо из проекта."""
    project.refresh_from_db()
    return {
        entry['role_key']: entry['count']
        for entry in get_project_composition(project)
    }


class ConfiguratorTestCase(TestCase):
    """Проект директора в статусе STAFFING с открытой комнатой."""

    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir@fr-ui.test')
        self.other_director = make_director(email='other-dir@fr-ui.test')
        self.teamlead = make_teamlead(email='tl@fr-ui.test')
        self.freelancer = make_freelancer(email='fr@fr-ui.test')
        self.manager = make_user(email='mng@fr-ui.test', role=User.Roles.MANAGER)

        self.project = Project.objects.create(
            owner=self.director,
            name='Комната конфигуратора',
            status=Project.Status.STAFFING,
            teamlead=self.teamlead,
            input_data={
                'offer': 'Оффер',
                'utp': 'УТП',
                'audience': 'Аудитория',
                'hot_criteria': 'Критерии',
            },
        )
        self.room = ensure_room_for_project(self.project)
        add_freelancer_to_room(self.room, self.freelancer)
        # У `RoleInRoom` варианта «менеджер» нет, а для теста важна платформенная
        # роль пользователя: менеджер внутри комнаты всё равно состав не правит.
        RoomMember.objects.create(
            room=self.room,
            user=self.manager,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )

        self.overview_url = reverse('rooms:room_overview', args=[self.project.id])
        self.update_url = reverse(
            'rooms:room_functional_roles_update', args=[self.project.id]
        )
        self.package_url = reverse(
            'rooms:room_functional_roles_apply_package', args=[self.project.id]
        )

    # --- хелперы --------------------------------------------------------

    def save_composition(self, **counts):
        """Сохраняет состав штатным сервисом (тесты не пишут `input_data` сами)."""
        update_project_functional_roles(
            self.project,
            [{'role_key': key, 'count': value} for key, value in counts.items()],
            self.director,
        )

    def login(self, user):
        self.client.force_login(user)

    def get_overview(self, user=None):
        if user is not None:
            self.login(user)
        return self.client.get(self.overview_url)

    def post_update(self, user=None, htmx=True, **data):
        if user is not None:
            self.login(user)
        headers = {'HX-Request': 'true'} if htmx else {}
        return self.client.post(self.update_url, data, headers=headers)

    def post_package(self, package, user=None, htmx=True):
        if user is not None:
            self.login(user)
        headers = {'HX-Request': 'true'} if htmx else {}
        return self.client.post(self.package_url, {'package': package}, headers=headers)


# ---------------------------------------------------------------------------
# 1–6. Рендер
# ---------------------------------------------------------------------------


class ConfiguratorRenderingTests(ConfiguratorTestCase):
    def test_overview_shows_configurator(self):
        """1. Конфигуратор появляется на вкладке «Обзор»."""
        response = self.get_overview(self.director)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ROOT_ID)
        self.assertContains(response, 'Функциональная команда / Юнит-экономика')

    def test_all_saved_roles_are_rendered(self):
        """2. Каждая сохранённая функция даёт строку таблицы."""
        self.save_composition(teamlead=1, seller_middle=2, linkedin_leadgen=1)
        response = self.get_overview(self.director)
        for role_key in ('teamlead', 'seller_middle', 'linkedin_leadgen'):
            self.assertContains(response, f'id="fr-row-{role_key}"')
        self.assertNotContains(response, 'id="fr-row-seller_senior"')

    def test_public_russian_titles_are_rendered(self):
        """3. Показываются публичные названия, а не role_key."""
        self.save_composition(**{key: 1 for key in catalog.FUNCTIONAL_ROLE_KEYS})
        response = self.get_overview(self.director)
        for title in PUBLIC_TITLES:
            self.assertContains(response, title)

    def test_cost_hours_productivity_and_hot_are_rendered(self):
        """4. Экономика строки берётся из снапшота сервера."""
        self.save_composition(teamlead=1, seller_middle=2)
        config = FunctionalRoleConfig.objects.get(role_key='seller_middle')
        response = self.get_overview(self.director)
        self.assertContains(response, format_money(config.monthly_cost))
        self.assertContains(response, config.productivity_text)
        self.assertContains(response, f'{config.monthly_hours} {HOURS_UNIT}')
        self.assertContains(response, f'{config.hot_leads_per_month} {HOT_UNIT}')

    def test_totals_are_rendered(self):
        """5. Итоги совпадают с `get_unit_economics_summary`."""
        self.save_composition(teamlead=1, seller_middle=2)
        summary = get_unit_economics_summary(self.project)
        response = self.get_overview(self.director)
        self.assertContains(response, format_money(summary.total_budget))
        self.assertContains(response, f'>{summary.total_hours}<')
        self.assertContains(response, f'>{summary.forecast_hot_leads}<')
        self.assertContains(response, format_money(summary.cpl))
        self.assertContains(response, 'Общий бюджет / мес')
        self.assertContains(response, 'Прогноз Hot leads / мес')

    def test_cpl_none_is_rendered_without_error(self):
        """6. Нулевой прогноз Hot — «—», а не ноль рублей и не падение."""
        self.save_composition(teamlead=1, database_assistant=1)
        summary = get_unit_economics_summary(self.project)
        self.assertIsNone(summary.cpl)
        response = self.get_overview(self.director)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'fr-total-cpl">{EMPTY_VALUE}<')

    def test_empty_composition_shows_configuration_state_with_zero_totals(self):
        """Пустой состав — понятный empty state, итоги всё равно на месте."""
        response = self.get_overview(self.director)
        self.assertContains(response, 'Функциональная команда ещё не собрана.')
        self.assertContains(response, format_money(0))
        self.assertContains(response, f'fr-total-cpl">{EMPTY_VALUE}<')

    def test_titles_are_not_duplicated_in_template_or_js(self):
        """Публичные названия живут только в каталоге кода."""
        for path in (
            'apps/rooms/templates/rooms/_unit_economics_table.html',
            'apps/rooms/templates/rooms/room_overview.html',
        ):
            with open(path, encoding='utf-8') as handle:
                markup = handle.read()
            for title in PUBLIC_TITLES:
                with self.subTest(path=path, title=title):
                    self.assertNotIn(title, markup)


# ---------------------------------------------------------------------------
# 7–13. Editable / read-only
# ---------------------------------------------------------------------------


class ConfiguratorAccessTests(ConfiguratorTestCase):
    def assert_editable(self, response):
        self.assertTrue(response.context['can_edit_functional_roles'])
        self.assertContains(response, 'Добавить функцию')
        self.assertContains(response, 'name="action" value="inc"')

    def assert_read_only(self, response):
        self.assertFalse(response.context['can_edit_functional_roles'])
        self.assertNotContains(response, 'Добавить функцию')
        self.assertNotContains(response, 'name="action" value="inc"')
        self.assertNotContains(response, 'name="action" value="dec"')
        self.assertNotContains(response, self.package_url)

    def test_owner_director_in_draft_sees_controls(self):
        """7. DRAFT + владелец-директор — редактируемо."""
        self.project.status = Project.Status.DRAFT
        self.project.save(update_fields=['status'])
        self.save_composition(teamlead=1, seller_middle=1)
        self.assert_editable(self.get_overview(self.director))

    def test_owner_director_in_staffing_sees_controls(self):
        """8. STAFFING + владелец-директор — редактируемо."""
        self.save_composition(teamlead=1, seller_middle=1)
        self.assert_editable(self.get_overview(self.director))

    def test_active_project_is_read_only(self):
        """9. ACTIVE — только чтение."""
        self.save_composition(teamlead=1, seller_middle=1)
        self.project.status = Project.Status.ACTIVE
        self.project.save(update_fields=['status'])
        self.assert_read_only(self.get_overview(self.director))

    def test_closed_statuses_are_read_only(self):
        """Прочие закрытые статусы — тоже только чтение."""
        self.save_composition(teamlead=1, seller_middle=1)
        for status in (
            Project.Status.ON_HOLD,
            Project.Status.COMPLETED,
            Project.Status.ARCHIVED,
            Project.Status.LAUNCHED,
        ):
            with self.subTest(status=status):
                self.project.status = status
                self.project.save(update_fields=['status'])
                self.assert_read_only(self.get_overview(self.director))

    def test_teamlead_is_read_only(self):
        """10. Тимлид состав не правит."""
        self.save_composition(teamlead=1, seller_middle=1)
        self.assert_read_only(self.get_overview(self.teamlead))

    def test_freelancer_is_read_only(self):
        """11. Фрилансер состав не правит."""
        self.save_composition(teamlead=1, seller_middle=1)
        self.assert_read_only(self.get_overview(self.freelancer))

    def test_freelancer_does_not_see_finance_metrics(self):
        """Фрилансер видит состав/лиды, без стоимости, часов, CPL, прогноза."""
        self.save_composition(teamlead=1, seller_middle=2)
        config = FunctionalRoleConfig.objects.get(role_key='seller_middle')
        summary = get_unit_economics_summary(self.project)
        response = self.get_overview(self.freelancer)

        self.assertFalse(response.context['can_view_unit_economics_finance'])
        self.assertFalse(response.context['can_view_composition_staffing'])
        self.assertContains(response, 'Состав команды')
        self.assertNotContains(response, 'Юнит-экономика')
        self.assertContains(response, config.productivity_text)
        self.assertContains(response, f'{config.hot_leads_per_month}')
        self.assertContains(response, 'Hot leads / мес')
        self.assertContains(response, 'Производительность')

        self.assertNotContains(response, 'Стоимость / мес')
        self.assertNotContains(response, 'Часы / мес')
        self.assertNotContains(response, 'Общий бюджет / мес')
        self.assertNotContains(response, 'Прогноз Hot leads / мес')
        self.assertNotContains(response, 'CPL')
        self.assertNotContains(response, format_money(config.monthly_cost))
        self.assertNotContains(response, format_money(summary.total_budget))
        if summary.cpl is not None:
            self.assertNotContains(response, format_money(summary.cpl))
        # Колонка подбора на Overview скрыта (заголовок «Подбор» в thead).
        self.assertNotContains(response, 'fr-staffing')
        self.assertNotRegex(response.content.decode(), r'<th[^>]*>\s*Подбор\s*</th>')
        # Блок «Подбор команды» на Overview (не путать со статусом STAFFING).
        self.assertNotContains(response, 'staffing-stats')
        self.assertNotContains(response, '<h3>Подбор команды</h3>')

    def test_freelancer_team_tab_hides_staffing(self):
        """На вкладке «Команда» фрилансер не видит подбор и слоты."""
        self.save_composition(teamlead=1, seller_middle=1)
        self.login(self.freelancer)
        response = self.client.get(
            reverse('rooms:room_team', args=[self.project.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['can_view_composition_staffing'])
        self.assertNotContains(response, 'Функциональные слоты')
        self.assertNotContains(response, 'в поиске')
        self.assertNotRegex(response.content.decode(), r'<th[^>]*>\s*Подбор\s*</th>')

    def test_teamlead_still_sees_finance_metrics(self):
        """Тимлид по-прежнему видит юнит-экономику (урезаем только фрилансера)."""
        self.save_composition(teamlead=1, seller_middle=1)
        summary = get_unit_economics_summary(self.project)
        response = self.get_overview(self.teamlead)
        self.assertTrue(response.context['can_view_unit_economics_finance'])
        self.assertContains(response, 'Юнит-экономика')
        self.assertContains(response, format_money(summary.total_budget))
        self.assertContains(response, 'CPL')

    def test_manager_is_read_only(self):
        """12. Менеджер состав не правит."""
        self.save_composition(teamlead=1, seller_middle=1)
        self.assert_read_only(self.get_overview(self.manager))

    def test_foreign_director_has_no_access_at_all(self):
        """13. Чужой директор не проходит даже проверку доступа к комнате."""
        self.save_composition(teamlead=1, seller_middle=1)
        self.login(self.other_director)
        self.assertEqual(self.client.get(self.overview_url).status_code, 403)

    def test_foreign_director_who_is_room_member_is_read_only(self):
        """Чужой директор внутри комнаты видит состав, но не правит его."""
        self.save_composition(teamlead=1, seller_middle=1)
        RoomMember.objects.create(
            room=self.room,
            user=self.other_director,
            role_in_room=RoomMember.RoleInRoom.FREELANCER,
        )
        self.assert_read_only(self.get_overview(self.other_director))

    def test_read_only_users_cannot_post_update(self):
        """Read-only роли не сохраняют состав и прямым POST."""
        self.save_composition(teamlead=1, seller_middle=1)
        for user in (self.teamlead, self.freelancer, self.manager):
            with self.subTest(user=user.email):
                response = self.post_update(
                    user=user, role_key='seller_middle', action='inc'
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(composition_of(self.project)['seller_middle'], 1)

    def test_foreign_director_cannot_post_update(self):
        """Чужой проект нельзя изменить POST-запросом."""
        self.save_composition(teamlead=1, seller_middle=1)
        response = self.post_update(
            user=self.other_director, role_key='seller_middle', action='inc'
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(composition_of(self.project)['seller_middle'], 1)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.post(self.update_url, {'role_key': 'seller_middle'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_active_project_post_does_not_change_composition(self):
        """ACTIVE: даже у владельца-директора POST ничего не сохраняет."""
        self.save_composition(teamlead=1, seller_middle=1)
        self.project.status = Project.Status.ACTIVE
        self.project.save(update_fields=['status'])
        response = self.post_update(
            user=self.director, role_key='seller_middle', action='inc'
        )
        self.assertContains(response, 'Состав команды можно менять только')
        self.assertEqual(composition_of(self.project)['seller_middle'], 1)

    def test_get_only_endpoints_reject_get(self):
        self.login(self.director)
        self.assertEqual(self.client.get(self.update_url).status_code, 405)
        self.assertEqual(self.client.get(self.package_url).status_code, 405)

    def test_csrf_protection_is_active(self):
        """CSRF не отключён: POST без токена не проходит."""
        self.save_composition(teamlead=1, seller_middle=1)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.director)
        response = csrf_client.post(
            self.update_url, {'role_key': 'seller_middle', 'action': 'inc'}
        )
        self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# 14–17. Тимлид
# ---------------------------------------------------------------------------


class ConfiguratorTeamleadTests(ConfiguratorTestCase):
    def setUp(self):
        super().setUp()
        self.save_composition(teamlead=1, seller_middle=1)

    def teamlead_row(self, response):
        html = response.content.decode()
        start = html.index('id="fr-row-teamlead"')
        return html[start:html.index('</tr>', start)]

    def test_teamlead_row_is_present(self):
        """14. Строка Тимлида есть в таблице."""
        response = self.get_overview(self.director)
        self.assertContains(response, 'id="fr-row-teamlead"')
        self.assertIn(
            catalog.FUNCTIONAL_ROLES['teamlead'].label, self.teamlead_row(response)
        )

    def test_teamlead_row_has_no_remove_control(self):
        """15. У Тимлида нет [✕], у необязательной функции — есть."""
        response = self.get_overview(self.director)
        self.assertNotIn('fr-remove', self.teamlead_row(response))

        html = response.content.decode()
        start = html.index('id="fr-row-seller_middle"')
        self.assertIn('fr-remove', html[start:html.index('</tr>', start)])

    def test_teamlead_step_buttons_are_disabled(self):
        """16. [+] и [−] Тимлида визуально отключены."""
        row = self.teamlead_row(self.get_overview(self.director))
        self.assertEqual(row.count('disabled aria-disabled="true"'), 2)
        self.assertIn('value="dec"', row)
        self.assertIn('value="inc"', row)

    def test_backend_still_rejects_teamlead_below_one(self):
        """17. Явный `teamlead=0` доходит до сервиса и отвергается им."""
        response = self.post_update(
            user=self.director, role_key='teamlead', action='set', count='0'
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'обязательна: минимум 1')
        self.assertEqual(composition_of(self.project)['teamlead'], 1)

    def test_teamlead_decrement_action_is_also_rejected(self):
        """Кнопка «−» Тимлида не помогает даже в обход disabled в разметке."""
        response = self.post_update(
            user=self.director, role_key='teamlead', action='dec'
        )
        self.assertContains(response, 'обязательна: минимум 1')
        self.assertEqual(composition_of(self.project)['teamlead'], 1)

    def test_teamlead_count_above_one_stays_allowed(self):
        """Числовой ввод Тимлида не запрещается новым UI-правилом."""
        response = self.post_update(
            user=self.director, role_key='teamlead', action='set', count='2'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(composition_of(self.project)['teamlead'], 2)

    def test_teamlead_numeric_input_has_min_one(self):
        row = self.teamlead_row(self.get_overview(self.director))
        self.assertIn('min="1"', row)


# ---------------------------------------------------------------------------
# 18–22. Количество
# ---------------------------------------------------------------------------


class ConfiguratorCountTests(ConfiguratorTestCase):
    def setUp(self):
        super().setUp()
        self.save_composition(teamlead=1, seller_middle=2)

    def test_increment_is_saved(self):
        """18. «+» сохраняет количество."""
        response = self.post_update(
            user=self.director, role_key='seller_middle', action='inc'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(composition_of(self.project)['seller_middle'], 3)

    def test_decrement_is_saved(self):
        """19. «−» сохраняет количество."""
        self.post_update(user=self.director, role_key='seller_middle', action='dec')
        self.assertEqual(composition_of(self.project)['seller_middle'], 1)

    def test_numeric_input_is_saved(self):
        """20. Числовой ввод сохраняет точное значение."""
        self.post_update(
            user=self.director, role_key='seller_middle', action='set', count='5'
        )
        self.assertEqual(composition_of(self.project)['seller_middle'], 5)

    def test_negative_count_is_rejected(self):
        """21. Отрицательное количество отвергается сервисом."""
        response = self.post_update(
            user=self.director, role_key='seller_middle', action='set', count='-1'
        )
        self.assertContains(response, 'не может быть отрицательным')
        self.assertEqual(composition_of(self.project)['seller_middle'], 2)

    def test_non_integer_count_is_rejected(self):
        response = self.post_update(
            user=self.director, role_key='seller_middle', action='set', count='1.5'
        )
        self.assertContains(response, 'должно быть целым числом')
        self.assertEqual(composition_of(self.project)['seller_middle'], 2)

    def test_decrement_never_goes_below_zero(self):
        """«−» на нуле не уводит количество в минус."""
        self.post_update(user=self.director, role_key='seller_senior', action='dec')
        self.assertNotIn('seller_senior', composition_of(self.project))

    def test_client_economics_are_ignored(self):
        """22. Присланные клиентом цена / часы / Hot не влияют ни на что."""
        config = FunctionalRoleConfig.objects.get(role_key='seller_middle')
        self.post_update(
            user=self.director,
            role_key='seller_middle',
            action='set',
            count='1',
            cost_per_unit='1.00',
            monthly_cost='1.00',
            hours_per_unit='999',
            kpi_leads_per_unit='999',
            hot_leads_per_month='999',
            total_budget='1.00',
        )
        self.project.refresh_from_db()
        summary = get_unit_economics_summary(self.project)
        row = next(r for r in summary.rows if r.role_key == 'seller_middle')
        self.assertEqual(row.monthly_cost, Decimal(config.monthly_cost))
        self.assertEqual(row.monthly_hours, config.monthly_hours)
        self.assertEqual(row.hot_leads_per_month, config.hot_leads_per_month)
        self.assertNotEqual(self.project.budget, Decimal('1.00'))

    def test_unknown_role_key_is_rejected(self):
        response = self.post_update(
            user=self.director, role_key='ceo', action='set', count='1'
        )
        self.assertContains(response, 'Неизвестная функция')
        self.assertEqual(composition_of(self.project)['seller_middle'], 2)

    def test_unknown_action_is_rejected(self):
        response = self.post_update(
            user=self.director, role_key='seller_middle', action='multiply', count='9'
        )
        self.assertContains(response, 'Неизвестное действие')
        self.assertEqual(composition_of(self.project)['seller_middle'], 2)

    def test_increment_is_computed_from_saved_state_not_from_client(self):
        """Клиентское `count` при «+» игнорируется: сервер считает сам."""
        self.post_update(
            user=self.director, role_key='seller_middle', action='inc', count='99'
        )
        self.assertEqual(composition_of(self.project)['seller_middle'], 3)


# ---------------------------------------------------------------------------
# 23–26. Добавление и удаление
# ---------------------------------------------------------------------------


class ConfiguratorAddRemoveTests(ConfiguratorTestCase):
    def setUp(self):
        super().setUp()
        self.save_composition(teamlead=1, seller_middle=1)

    def test_add_optional_role_with_count_one(self):
        """23. Добавление функции даёт count=1 и серверную экономику."""
        self.post_update(
            user=self.director, role_key='linkedin_leadgen', action='set', count='1'
        )
        composition = composition_of(self.project)
        self.assertEqual(composition['linkedin_leadgen'], 1)
        config = FunctionalRoleConfig.objects.get(role_key='linkedin_leadgen')
        row = next(
            r
            for r in get_unit_economics_summary(self.project).rows
            if r.role_key == 'linkedin_leadgen'
        )
        self.assertEqual(row.monthly_cost, Decimal(config.monthly_cost))

    def test_add_dropdown_only_offers_missing_optional_roles(self):
        """24. Уже добавленная функция и обязательный Тимлид в списке не предлагаются."""
        response = self.get_overview(self.director)
        available = [role.role_key for role in response.context['fr_available_roles']]
        self.assertNotIn('seller_middle', available)
        self.assertNotIn('teamlead', available)
        self.assertEqual(
            set(available), {'seller_senior', 'linkedin_leadgen', 'database_assistant'}
        )

    def test_adding_existing_role_does_not_create_second_row(self):
        """24. Повторное добавление меняет count, а не создаёт вторую строку."""
        self.post_update(
            user=self.director, role_key='seller_middle', action='set', count='1'
        )
        self.project.refresh_from_db()
        keys = [entry['role_key'] for entry in get_project_composition(self.project)]
        self.assertEqual(keys.count('seller_middle'), 1)

    def test_add_action_is_hidden_when_all_roles_are_present(self):
        """Все пять функций в составе — action «Добавить функцию» пропадает."""
        self.save_composition(**{key: 1 for key in catalog.FUNCTIONAL_ROLE_KEYS})
        response = self.get_overview(self.director)
        self.assertEqual(response.context['fr_available_roles'], [])
        self.assertNotContains(response, 'Добавить функцию')
        self.assertContains(response, 'Все функции каталога уже в составе')

    def test_remove_optional_role(self):
        """25. [✕] убирает необязательную функцию из состава."""
        response = self.post_update(
            user=self.director, role_key='seller_middle', action='set', count='0'
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('seller_middle', composition_of(self.project))
        self.assertNotContains(response, 'id="fr-row-seller_middle"')

    def test_remove_teamlead_is_impossible(self):
        """26. Тимлида убрать нельзя ни кнопкой, ни прямым POST."""
        response = self.post_update(
            user=self.director, role_key='teamlead', action='set', count='0'
        )
        self.assertContains(response, 'обязательна: минимум 1')
        self.assertEqual(composition_of(self.project)['teamlead'], 1)

    def test_remove_closes_the_slot_without_deleting_it(self):
        """Удаление функции закрывает её пустой слот, но строку не удаляет.

        Подробности проекции — в `tests_functional_role_projection.py`;
        здесь проверяется только то, что продуктовая кнопка «✕» до неё
        доходит.
        """
        slot = RoomFunctionSlot.objects.create(
            room=self.room, role_key='seller_middle', slot_index=1
        )
        self.post_update(
            user=self.director, role_key='seller_middle', action='set', count='0'
        )
        slot.refresh_from_db()
        self.assertFalse(slot.is_active)
        self.assertEqual(RoomFunctionSlot.objects.filter(room=self.room).count(), 1)

    def test_remove_is_blocked_when_someone_is_assigned(self):
        """Ожидание обновлено вместе с продуктом (Issue #11, финальный этап).

        Раньше на назначенной функции показывался браузерный confirm, после
        которого backend всё равно отказывал: удаление функции закрывает её
        слоты, а слот с исполнителем проекция не закрывает. Теперь кнопка
        удаления просто недоступна и объясняет причину — обещания, которое
        продукт не может выполнить, больше нет.

        Подробности этого UX — в `tests_issue11_tabs_completion`.
        """
        free = self.get_overview(self.director)
        self.assertNotContains(free, 'hx-confirm')
        self.assertNotContains(free, 'Сначала снимите исполнителя')

        slot = RoomFunctionSlot.objects.create(
            room=self.room, role_key='seller_middle', slot_index=1
        )
        RoomMember.objects.filter(room=self.room, user=self.freelancer).update(
            function_slot=slot, role_key='seller_middle'
        )
        assigned = self.get_overview(self.director)
        self.assertNotContains(assigned, 'hx-confirm')
        self.assertContains(assigned, 'Сначала снимите исполнителя')

    def test_first_save_adds_teamlead_automatically(self):
        """Первое сохранение пустого состава само добавляет Тимлида."""
        empty = Project.objects.create(
            owner=self.director,
            name='Пустой состав',
            status=Project.Status.STAFFING,
        )
        ensure_room_for_project(empty)
        self.assertEqual(get_project_composition(empty), [])
        self.login(self.director)
        self.client.post(
            reverse('rooms:room_functional_roles_update', args=[empty.id]),
            {'role_key': 'seller_middle', 'action': 'set', 'count': '1'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(composition_of(empty), {'teamlead': 1, 'seller_middle': 1})


# ---------------------------------------------------------------------------
# 27–31. Пакеты
# ---------------------------------------------------------------------------


class ConfiguratorPackageTests(ConfiguratorTestCase):
    def assert_package_applied(self, package_key):
        self.post_package(package_key, user=self.director)
        expected = dict(presets.FUNCTIONAL_ROLE_PACKAGES[package_key].composition)
        self.assertEqual(composition_of(self.project), expected)

    def test_all_packages_are_rendered_on_overview(self):
        """Кнопки пакетов строятся циклом по `presets`, вручную их не добавляют.

        Новый пакет обязан появиться на «Обзоре» сам, только за счёт записи
        в `FUNCTIONAL_ROLE_PACKAGES`.
        """
        self.save_composition(teamlead=1, seller_middle=1)
        response = self.get_overview(self.director)
        self.assertContains(response, 'Готовые пакеты')
        for package in presets.FUNCTIONAL_ROLE_PACKAGES.values():
            with self.subTest(package=package.key):
                self.assertContains(response, package.label)
                self.assertContains(response, f'value="{package.key}"')
        self.assertEqual(
            [p.key for p in response.context['fr_packages']],
            list(presets.FUNCTIONAL_ROLE_PACKAGES),
        )

    def test_linkedin_package_button_is_shown_on_overview(self):
        """Пакет LinkedIn виден рядом с Быстрым стартом / Масштабированием."""
        self.save_composition(teamlead=1, seller_middle=1)
        response = self.get_overview(self.director)
        self.assertContains(response, 'LinkedIn')
        self.assertContains(response, 'value="linkedin"')

    def test_quick_start_package_is_applied_exactly(self):
        """27."""
        self.assert_package_applied('quick_start')

    def test_scaling_package_is_applied_exactly(self):
        """28."""
        self.assert_package_applied('scaling')

    def test_enterprise_package_is_applied_exactly(self):
        """29."""
        self.assert_package_applied('enterprise')

    def test_linkedin_package_is_applied_exactly(self):
        """Пакет LinkedIn сохраняет ровно тимлида и лидген LinkedIn."""
        self.assert_package_applied('linkedin')
        self.assertEqual(
            composition_of(self.project), {'teamlead': 1, 'linkedin_leadgen': 1}
        )

    def test_linkedin_package_writes_budget_and_kpi_target(self):
        """83 000 ₽ и 8 Hot приходят из сводки, а не из пресета."""
        self.post_package('linkedin', user=self.director)
        self.project.refresh_from_db()
        summary = get_unit_economics_summary(self.project)

        self.assertEqual(self.project.budget, Decimal('83000.00'))
        self.assertEqual(self.project.kpi_target, Decimal('8'))
        self.assertEqual(summary.total_hours, 240)
        self.assertEqual(summary.cpl, Decimal('10375.00'))
        self.assertEqual(self.project.budget, summary.total_budget)
        self.assertEqual(self.project.kpi_target, summary.forecast_hot_leads)

    def test_linkedin_package_totals_are_rendered(self):
        """Итоги пакета показаны теми же цифрами, что сохранены в проекте."""
        response = self.post_package('linkedin', user=self.director)
        self.project.refresh_from_db()
        summary = get_unit_economics_summary(self.project)
        self.assertContains(response, format_money(summary.total_budget))
        self.assertContains(response, format_money(summary.cpl))
        self.assertContains(response, f'>{summary.forecast_hot_leads}<')

    def test_package_replaces_previous_composition_and_recomputes_totals(self):
        """30. Пакет заменяет состав целиком и пересчитывает итоги."""
        self.save_composition(teamlead=1, database_assistant=3)
        response = self.post_package('scaling', user=self.director)
        self.project.refresh_from_db()
        self.assertNotIn('database_assistant', composition_of(self.project))

        summary = get_unit_economics_summary(self.project)
        self.assertContains(response, format_money(summary.total_budget))
        self.assertEqual(self.project.budget, summary.total_budget)
        self.assertGreater(summary.forecast_hot_leads, 0)

    def test_read_only_users_cannot_apply_package(self):
        """31. Пакет недоступен всем, кроме владельца-директора."""
        self.save_composition(teamlead=1, seller_middle=1)
        for user in (self.teamlead, self.freelancer, self.manager, self.other_director):
            with self.subTest(user=user.email):
                self.assertEqual(self.post_package('scaling', user=user).status_code, 403)
        self.assertEqual(
            composition_of(self.project), {'teamlead': 1, 'seller_middle': 1}
        )

    def test_package_buttons_are_hidden_for_read_only_users(self):
        self.save_composition(teamlead=1, seller_middle=1)
        self.assertNotContains(self.get_overview(self.teamlead), self.package_url)

    def test_unknown_package_is_rejected(self):
        self.save_composition(teamlead=1, seller_middle=1)
        response = self.post_package('mega_pack', user=self.director)
        self.assertContains(response, 'Неизвестный пакет')
        self.assertEqual(
            composition_of(self.project), {'teamlead': 1, 'seller_middle': 1}
        )

    def test_package_composition_is_not_duplicated_in_markup(self):
        """Состав пакета в шаблон не копируется: уходит только его ключ."""
        with open(
            'apps/rooms/templates/rooms/_unit_economics_table.html', encoding='utf-8'
        ) as handle:
            markup = handle.read()
        for package in presets.FUNCTIONAL_ROLE_PACKAGES.values():
            with self.subTest(package=package.key):
                self.assertNotIn(package.label, markup)
                self.assertNotIn(package.key, markup)

    def test_composition_stays_editable_after_package(self):
        """После пакета необязательные роли правятся вручную."""
        self.post_package('scaling', user=self.director)
        self.post_update(user=self.director, role_key='seller_middle', action='inc')
        expected = presets.FUNCTIONAL_ROLE_PACKAGES['scaling'].composition['seller_middle']
        self.assertEqual(composition_of(self.project)['seller_middle'], expected + 1)


# ---------------------------------------------------------------------------
# KPI проекта на продуктовых путях конфигуратора
# ---------------------------------------------------------------------------


class ConfiguratorKpiTargetTests(ConfiguratorTestCase):
    """Любое сохранение состава через UI обновляет и бюджет, и `kpi_target`.

    Считает Hot по-прежнему сервер: клиент присылает только `role_key`,
    намерение и ключ пакета.
    """

    def assert_matches_summary(self):
        """Перечитывает проект и сверяет бюджет и KPI со сводкой.

        `refresh_from_db` обязателен: POST работал со своим экземпляром
        `Project`, и `self.project` в памяти остался с прежними значениями.
        """
        self.project.refresh_from_db()
        summary = get_unit_economics_summary(self.project)
        self.assertEqual(self.project.budget, summary.total_budget)
        self.assertEqual(self.project.kpi_target, summary.forecast_hot_leads)
        return summary

    def test_manual_count_input_updates_kpi_target(self):
        self.save_composition(teamlead=1, seller_middle=1)
        self.post_update(
            user=self.director, role_key='seller_middle', action='set', count='3'
        )
        self.assertEqual(self.assert_matches_summary().forecast_hot_leads, 30)
        self.assertEqual(self.project.kpi_target, Decimal('30'))

    def test_increment_updates_kpi_target(self):
        self.save_composition(teamlead=1, seller_middle=1)
        self.post_update(user=self.director, role_key='seller_middle', action='inc')
        self.assert_matches_summary()
        self.assertEqual(self.project.kpi_target, Decimal('20'))

    def test_decrement_updates_kpi_target(self):
        self.save_composition(teamlead=1, seller_middle=2)
        self.post_update(user=self.director, role_key='seller_middle', action='dec')
        self.assert_matches_summary()
        self.assertEqual(self.project.kpi_target, Decimal('10'))

    def test_adding_a_role_updates_kpi_target(self):
        self.save_composition(teamlead=1, seller_middle=1)
        self.post_update(
            user=self.director, role_key='linkedin_leadgen', action='set', count='1'
        )
        self.assert_matches_summary()
        self.assertEqual(self.project.kpi_target, Decimal('18'))

    def test_removing_a_role_updates_kpi_target(self):
        self.save_composition(teamlead=1, seller_middle=1, linkedin_leadgen=1)
        self.post_update(
            user=self.director, role_key='linkedin_leadgen', action='set', count='0'
        )
        self.assert_matches_summary()
        self.assertEqual(self.project.kpi_target, Decimal('10'))

    def test_every_package_updates_kpi_target(self):
        for key in presets.FUNCTIONAL_ROLE_PACKAGES:
            with self.subTest(package=key):
                self.post_package(key, user=self.director)
                self.assert_matches_summary()

    def test_linkedin_package_gives_kpi_target_eight(self):
        self.post_package('linkedin', user=self.director)
        self.project.refresh_from_db()
        self.assertEqual(self.project.kpi_target, Decimal('8'))
        self.assertEqual(self.project.budget, Decimal('83000.00'))

    def test_client_cannot_post_its_own_kpi_target(self):
        """Лишние поля формы игнорируются: KPI считает сервер."""
        self.save_composition(teamlead=1, seller_middle=1)
        self.post_update(
            user=self.director,
            role_key='seller_middle',
            action='inc',
            kpi_target='777',
            hot_leads_per_month='777',
            count='777',
        )
        self.assert_matches_summary()
        self.assertEqual(self.project.kpi_target, Decimal('20'))

    def test_client_cannot_post_its_own_kpi_target_with_a_package(self):
        self.post_package('linkedin', user=self.director)
        self.client.force_login(self.director)
        self.client.post(
            self.package_url,
            {'package': 'linkedin', 'kpi_target': '777', 'budget': '1'},
            headers={'HX-Request': 'true'},
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.kpi_target, Decimal('8'))
        self.assertEqual(self.project.budget, Decimal('83000.00'))

    def test_failed_save_leaves_budget_and_kpi_target_untouched(self):
        """Ошибка операции не двигает ни бюджет, ни KPI (обязательный тимлид)."""
        self.post_package('linkedin', user=self.director)
        self.project.refresh_from_db()

        response = self.post_update(
            user=self.director, role_key='teamlead', action='set', count='0'
        )
        self.assertEqual(response.status_code, 200)

        self.project.refresh_from_db()
        self.assertEqual(self.project.budget, Decimal('83000.00'))
        self.assertEqual(self.project.kpi_target, Decimal('8'))
        self.assertEqual(
            composition_of(self.project), {'teamlead': 1, 'linkedin_leadgen': 1}
        )

    def test_read_only_user_cannot_move_kpi_target(self):
        self.post_package('linkedin', user=self.director)
        self.assertEqual(
            self.post_package('enterprise', user=self.teamlead).status_code, 403
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.kpi_target, Decimal('8'))


# ---------------------------------------------------------------------------
# 32–35. HTMX
# ---------------------------------------------------------------------------


class ConfiguratorHtmxTests(ConfiguratorTestCase):
    def setUp(self):
        super().setUp()
        self.save_composition(teamlead=1, seller_middle=1)

    def test_update_returns_the_configurator_partial(self):
        """32. HTMX-ответ — тот же partial конфигуратора."""
        response = self.post_update(
            user=self.director, role_key='seller_middle', action='inc'
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'rooms/_unit_economics_table.html')
        self.assertContains(response, ROOT_ID)

    def test_partial_has_no_room_header_or_tabs(self):
        """33. Partial не тащит шапку комнаты и навигацию по вкладкам."""
        response = self.post_update(
            user=self.director, role_key='seller_middle', action='inc'
        )
        html = response.content.decode()
        self.assertNotIn('room-tabs', html)
        self.assertNotIn('page-header', html)
        self.assertNotIn('<!DOCTYPE', html)
        self.assertTemplateNotUsed(response, 'rooms/_room_header.html')
        self.assertTemplateNotUsed(response, 'base.html')

    def test_non_htmx_post_redirects_to_overview(self):
        """34. Fallback без HTMX: redirect на «Обзор» с flash."""
        response = self.post_update(
            user=self.director, role_key='seller_middle', action='inc', htmx=False
        )
        self.assertRedirects(response, self.overview_url)
        self.assertEqual(composition_of(self.project)['seller_middle'], 2)

    def test_non_htmx_package_post_redirects_to_overview(self):
        response = self.post_package('scaling', user=self.director, htmx=False)
        self.assertRedirects(response, self.overview_url)

    def test_autosave_needs_no_full_page_render(self):
        """35. Автосохранение не зависит от полной страницы «Обзора»."""
        response = self.post_update(
            user=self.director, role_key='seller_middle', action='inc'
        )
        self.assertTemplateNotUsed(response, 'rooms/room_overview.html')
        self.assertNotContains(response, 'Лента событий')
        self.assertNotContains(response, 'Задачи (канбан)')

    def test_partial_reflects_saved_state_after_error(self):
        """При ошибке partial показывает сохранённое, а не желаемое состояние."""
        response = self.post_update(
            user=self.director, role_key='seller_middle', action='set', count='-3'
        )
        self.assertContains(response, 'не может быть отрицательным')
        self.assertContains(response, 'value="1"')
        self.assertEqual(composition_of(self.project)['seller_middle'], 1)

    def test_overview_and_partial_share_one_template(self):
        """Обзор и HTMX-ответ используют один и тот же файл разметки."""
        self.assertTemplateUsed(
            self.get_overview(self.director), 'rooms/_unit_economics_table.html'
        )


# ---------------------------------------------------------------------------
# 36–38. Персистентность
# ---------------------------------------------------------------------------


class ConfiguratorPersistenceTests(ConfiguratorTestCase):
    def test_reload_shows_saved_composition(self):
        """36. После перезагрузки «Обзора» состав тот же."""
        self.post_update(
            user=self.director, role_key='seller_senior', action='set', count='2'
        )
        response = self.get_overview()
        self.assertContains(response, 'id="fr-row-seller_senior"')
        self.assertEqual(composition_of(self.project)['seller_senior'], 2)

    def test_budget_is_synced_with_project_budget(self):
        """37. `Project.budget` равен рассчитанному бюджету состава."""
        self.post_package('scaling', user=self.director)
        self.project.refresh_from_db()
        summary = get_unit_economics_summary(self.project)
        self.assertEqual(self.project.budget, summary.total_budget)
        self.assertGreater(self.project.budget, Decimal('0'))

    def test_other_input_data_keys_survive(self):
        """38. Оффер, УТП, ЦА и критерии Hot не затираются."""
        self.post_update(
            user=self.director, role_key='seller_middle', action='set', count='2'
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.input_data['offer'], 'Оффер')
        self.assertEqual(self.project.input_data['utp'], 'УТП')
        self.assertEqual(self.project.input_data['audience'], 'Аудитория')
        self.assertEqual(self.project.input_data['hot_criteria'], 'Критерии')
        self.assertIn(FUNCTIONAL_ROLES_KEY, self.project.input_data)


# ---------------------------------------------------------------------------
# 39–42. Регрессия
# ---------------------------------------------------------------------------


class ConfiguratorRegressionTests(ConfiguratorTestCase):
    def test_staffing_block_still_renders(self):
        """39. Блок подбора PR #16 остался на «Обзоре»."""
        RoomFunctionSlot.objects.create(
            room=self.room, role_key='seller_middle', slot_index=1
        )
        response = self.get_overview(self.director)
        self.assertContains(response, 'Подбор команды')
        self.assertContains(response, 'staffing-stats')
        self.assertContains(response, 'slot-card')
        self.assertEqual(response.context['staffing_summary']['total'], 1)

    def test_six_room_tabs_are_intact(self):
        """40. Навигация комнаты не сломана."""
        response = self.get_overview(self.director)
        self.assertContains(response, 'room-tabs')
        for label in ('Обзор', 'Команда', 'Задачи', 'Лиды', 'Материалы', 'Коммуникации'):
            self.assertContains(response, f'>{label}</a>')

    def test_chat_page_still_works(self):
        """40. Чат комнаты не затронут."""
        self.login(self.director)
        response = self.client.get(reverse('rooms:room_comms', args=[self.project.id]))
        self.assertEqual(response.status_code, 200)

    def test_tasks_and_leads_pages_still_work(self):
        """41. Задачи и Лиды не сломаны."""
        Task.objects.create(
            project=self.project,
            title='Задача',
            created_by=self.director,
            assignee=self.freelancer,
        )
        self.login(self.director)
        for name in ('pipeline:room_tasks', 'pipeline:room_leads'):
            with self.subTest(url=name):
                response = self.client.get(reverse(name, args=[self.project.id]))
                self.assertEqual(response.status_code, 200)
        self.assertContains(self.get_overview(), 'Задачи (канбан)')

    def test_overview_get_never_writes_functional_roles(self):
        """42. GET «Обзора» не создаёт состав и не меняет проект."""
        self.assertNotIn(FUNCTIONAL_ROLES_KEY, self.project.input_data)
        before_budget = self.project.budget
        before_updated = self.project.updated_at

        for user in (self.director, self.teamlead, self.freelancer, self.manager):
            with self.subTest(user=user.email):
                self.assertEqual(self.get_overview(user).status_code, 200)

        self.project.refresh_from_db()
        self.assertNotIn(FUNCTIONAL_ROLES_KEY, self.project.input_data)
        self.assertEqual(self.project.budget, before_budget)
        self.assertEqual(self.project.updated_at, before_updated)

    def test_overview_get_does_not_create_slots_or_members(self):
        """GET не создаёт слоты и не меняет состав комнаты."""
        slots_before = RoomFunctionSlot.objects.count()
        members_before = RoomMember.objects.count()
        rooms_before = Room.objects.count()
        self.get_overview(self.director)
        self.assertEqual(RoomFunctionSlot.objects.count(), slots_before)
        self.assertEqual(RoomMember.objects.count(), members_before)
        self.assertEqual(Room.objects.count(), rooms_before)

    def test_package_apply_goes_through_the_same_slot_projection(self):
        """Пакет — такой же write-path состава: слоты появляются и здесь.

        Сценария «кнопкой слоты создаются, пакетом — нет» существовать не
        должно. Сама проекция проверяется в
        `tests_functional_role_projection.py`.
        """
        self.post_package('enterprise', user=self.director)
        active = RoomFunctionSlot.objects.filter(room=self.room, is_active=True)
        self.assertEqual(active.filter(role_key='seller_senior').count(), 2)
        self.assertEqual(active.filter(role_key='linkedin_leadgen').count(), 1)
        self.assertEqual(active.filter(role_key='teamlead').count(), 0)


# ---------------------------------------------------------------------------
# Contract pass: экономика строки зависит от count
# ---------------------------------------------------------------------------


class ConfiguratorRowEconomicsTests(ConfiguratorTestCase):
    """Строка показывает и норматив на единицу, и итог для текущего count."""

    def setUp(self):
        super().setUp()
        self.save_composition(teamlead=1, seller_middle=2)
        self.config = FunctionalRoleConfig.objects.get(role_key='seller_middle')
        self.row = next(
            row
            for row in self.get_overview(self.director).context['fr_rows']
            if row.role_key == 'seller_middle'
        )
        self.response = self.get_overview(self.director)

    def seller_row_html(self):
        html = self.response.content.decode()
        start = html.index('id="fr-row-seller_middle"')
        return html[start:html.index('</tr>', start)]

    def test_row_subtotal_cost_is_cost_per_unit_times_count(self):
        expected = Decimal(self.config.monthly_cost) * 2
        self.assertEqual(self.row.count, 2)
        self.assertEqual(self.row.subtotal_cost, expected)
        self.assertContains(self.response, format_money(expected))
        self.assertIn(format_money(expected), self.seller_row_html())

    def test_row_subtotal_hours_is_hours_per_unit_times_count(self):
        expected = self.config.monthly_hours * 2
        self.assertEqual(self.row.subtotal_hours, expected)
        self.assertIn(f'{expected} {HOURS_UNIT}', self.seller_row_html())

    def test_row_subtotal_hot_is_kpi_per_unit_times_count(self):
        expected = self.config.hot_leads_per_month * 2
        self.assertEqual(self.row.subtotal_hot_leads, expected)
        self.assertIn(f'{expected} {HOT_UNIT}', self.seller_row_html())

    def test_row_shows_unit_rate_next_to_the_subtotal(self):
        """Пользователь видит и норматив, и результат: «62 000 ₽ × 2»."""
        row_html = self.seller_row_html()
        self.assertIn(f'{format_money(self.config.monthly_cost)} × 2', row_html)
        self.assertIn(f'{self.config.monthly_hours} {HOURS_UNIT} × 2', row_html)
        self.assertIn(f'{self.config.hot_leads_per_month} {HOT_UNIT} × 2', row_html)

    def test_productivity_is_not_multiplied(self):
        """Продуктивность остаётся текстом: её математику задаёт Product Owner."""
        row_html = self.seller_row_html()
        self.assertIn(self.config.productivity_text, row_html)
        self.assertNotIn(f'{self.config.productivity_text} ×', row_html)
        self.assertEqual(self.row.productivity_text, self.config.productivity_text)

    def test_subtotals_match_server_summary(self):
        """Показанные итоги строк складываются в тот же бюджет, что и сводка."""
        summary = get_unit_economics_summary(self.project)
        self.assertEqual(
            sum(row.subtotal_cost for row in summary.rows), summary.total_budget
        )
        self.assertEqual(
            sum(row.subtotal_hours for row in summary.rows), summary.total_hours
        )
        self.assertEqual(
            sum(row.subtotal_hot_leads for row in summary.rows),
            summary.forecast_hot_leads,
        )

    def test_subtotals_follow_count_change(self):
        """После «+» итоги строки пересчитаны сервером, а не клиентом."""
        self.post_update(user=self.director, role_key='seller_middle', action='inc')
        response = self.get_overview()
        row = next(
            r for r in response.context['fr_rows'] if r.role_key == 'seller_middle'
        )
        self.assertEqual(row.count, 3)
        self.assertEqual(row.subtotal_cost, Decimal(self.config.monthly_cost) * 3)
        self.assertContains(response, format_money(row.subtotal_cost))


# ---------------------------------------------------------------------------
# Contract pass: меню доступных функций показывает базовые нормативы
# ---------------------------------------------------------------------------


class ConfiguratorAvailableRolesTests(ConfiguratorTestCase):
    def setUp(self):
        super().setUp()
        self.save_composition(teamlead=1, seller_middle=1)

    def test_available_role_option_shows_rate_hours_and_hot(self):
        """Пункт меню несёт название, ставку, часы и Hot — всё с сервера."""
        response = self.get_overview(self.director)
        html = response.content.decode()
        start = html.index('id="fr-add-role"')
        select_html = html[start:html.index('</select>', start)]

        for available in response.context['fr_available_roles']:
            config = FunctionalRoleConfig.objects.get(role_key=available.role_key)
            with self.subTest(role_key=available.role_key):
                self.assertIn(available.label, select_html)
                self.assertIn(format_money(config.monthly_cost), select_html)
                self.assertIn(f'{config.monthly_hours} {HOURS_UNIT}', select_html)
                self.assertIn(f'{config.hot_leads_per_month} {HOT_UNIT}', select_html)
                self.assertIn(available.option_label, select_html)

    def test_option_value_is_only_role_key(self):
        """В форму уходит один role_key: экономика остаётся display-данными."""
        response = self.get_overview(self.director)
        html = response.content.decode()
        start = html.index('id="fr-add-role"')
        select_html = html[start:html.index('</select>', start)]
        for available in response.context['fr_available_roles']:
            self.assertIn(f'value="{available.role_key}"', select_html)

    def test_displayed_rates_come_from_the_admin_catalog(self):
        """Правка ставки в каталоге меняет подпись меню без релиза."""
        config = FunctionalRoleConfig.objects.get(role_key='seller_senior')
        config.monthly_cost = Decimal('99000.00')
        config.save(update_fields=['monthly_cost'])
        self.assertContains(
            self.get_overview(self.director), format_money(Decimal('99000.00'))
        )

    def test_add_menu_economics_are_not_trusted_on_post(self):
        """Присланная вместе с role_key ставка игнорируется сервисом."""
        config = FunctionalRoleConfig.objects.get(role_key='seller_senior')
        self.post_update(
            user=self.director,
            role_key='seller_senior',
            action='set',
            count='1',
            cost_per_unit='1.00',
            monthly_cost='1.00',
        )
        self.project.refresh_from_db()
        row = next(
            r
            for r in get_unit_economics_summary(self.project).rows
            if r.role_key == 'seller_senior'
        )
        self.assertEqual(row.monthly_cost, Decimal(config.monthly_cost))


# ---------------------------------------------------------------------------
# Contract pass: конфигуратор в верхней части «Обзора»
# ---------------------------------------------------------------------------


class ConfiguratorPositionTests(ConfiguratorTestCase):
    def test_configurator_is_above_the_main_overview_blocks(self):
        """Конфигуратор идёт до вводных, ленты, задач, подбора и команды."""
        self.save_composition(teamlead=1, seller_middle=1)
        RoomFunctionSlot.objects.create(
            room=self.room, role_key='seller_middle', slot_index=1
        )
        html = self.get_overview(self.director).content.decode()
        position = html.index(ROOT_ID)
        for marker in (
            'Вводные проекта',
            'Лента событий',
            'Задачи (канбан)',
            'staffing-stats',
            '<h3>Команда</h3>',
        ):
            with self.subTest(marker=marker):
                self.assertLess(position, html.index(marker))

    def test_room_header_and_tabs_stay_above_the_configurator(self):
        """Шапка комнаты и навигация по вкладкам остаются выше."""
        html = self.get_overview(self.director).content.decode()
        position = html.index(ROOT_ID)
        self.assertLess(html.index('class="page-header"'), position)
        self.assertLess(html.index('class="room-tabs"'), position)


# ---------------------------------------------------------------------------
# Contract pass: ячейка подбора — только фактические данные
# ---------------------------------------------------------------------------


class ConfiguratorStaffingCellTests(ConfiguratorTestCase):
    def setUp(self):
        super().setUp()
        self.save_composition(teamlead=1, seller_middle=1)

    def make_slot(self, role_key='seller_middle', slot_index=1):
        return RoomFunctionSlot.objects.create(
            room=self.room, role_key=role_key, slot_index=slot_index
        )

    def assign(self, slot, user=None):
        member = RoomMember.objects.get(room=self.room, user=user or self.freelancer)
        member.function_slot = slot
        member.save()
        return member

    def staffing_cell(self, response=None):
        html = (response or self.get_overview(self.director)).content.decode()
        start = html.index('id="fr-row-seller_middle"')
        row = html[start:html.index('</tr>', start)]
        cell_start = row.index('class="fr-staffing ')
        return row[cell_start:row.index('</td>', cell_start)]

    def test_assigned_slot_shows_the_assigned_member(self):
        """Слот с исполнителем показывает человека и ссылку на его профиль."""
        self.assign(self.make_slot())
        cell = self.staffing_cell()
        self.assertIn(self.freelancer.full_name, cell)
        self.assertIn(
            reverse('profiles:detail', args=[self.freelancer.id]), cell
        )
        self.assertNotIn(SEARCHING_LABEL, cell)

    def test_assigned_member_avatar_is_shown_when_profile_already_has_one(self):
        """Аватар берётся из уже существующего профиля, нового API не заводим."""
        profile = self.freelancer.freelancer_profile
        profile.avatar_url = 'https://example.com/avatar.png'
        profile.save(update_fields=['avatar_url'])
        self.assign(self.make_slot())
        cell = self.staffing_cell()
        self.assertIn('fr-staffing-avatar', cell)
        self.assertIn('https://example.com/avatar.png', cell)

    def test_member_without_avatar_still_renders_name_only(self):
        self.assign(self.make_slot())
        cell = self.staffing_cell()
        self.assertNotIn('fr-staffing-avatar', cell)
        self.assertIn(self.freelancer.full_name, cell)

    def test_empty_existing_slot_shows_searching(self):
        """Слот есть, исполнителя нет — «Идёт подбор»."""
        self.make_slot()
        cell = self.staffing_cell()
        self.assertIn(SEARCHING_LABEL, cell)
        self.assertNotIn(self.freelancer.full_name, cell)

    def test_role_without_any_slot_shows_neutral_dash(self):
        """Слотов нет — прочерк, а не намёк на запущенный подбор."""
        cell = self.staffing_cell()
        self.assertIn(EMPTY_VALUE, cell)
        self.assertNotIn(SEARCHING_LABEL, cell)
        self.assertNotIn('fr-staffing-list', cell)

    def test_mixed_slots_show_person_and_searching_side_by_side(self):
        self.assign(self.make_slot(slot_index=1))
        self.make_slot(slot_index=2)
        cell = self.staffing_cell()
        self.assertIn(self.freelancer.full_name, cell)
        self.assertIn(SEARCHING_LABEL, cell)

    def test_no_sla_countdown_is_rendered(self):
        """SLA-таймер — следующий этап Automation / SLA, здесь его нет."""
        self.assign(self.make_slot())
        cell = self.staffing_cell()
        self.assertNotIn('SLA', cell)
        self.assertNotIn('осталось', cell)

    def test_teamlead_and_database_assistant_get_no_invented_mapping(self):
        """Для этих функций слотов нет — значит прочерк, а не выдуманный статус."""
        self.save_composition(teamlead=1, database_assistant=1)
        response = self.get_overview(self.director)
        rows = {row.role_key: row for row in response.context['fr_rows']}
        for role_key in ('teamlead', 'database_assistant'):
            with self.subTest(role_key=role_key):
                self.assertEqual(rows[role_key].staffing.slots_total, 0)
                self.assertEqual(rows[role_key].staffing.status, 'none')

    def test_rendering_the_cell_creates_nothing(self):
        """GET со всеми состояниями ячейки не создаёт слотов и назначений."""
        self.assign(self.make_slot(slot_index=1))
        self.make_slot(slot_index=2)
        slots_before = list(
            RoomFunctionSlot.objects.values_list('id', 'role_key', 'is_active')
        )
        members_before = list(
            RoomMember.objects.values_list('id', 'function_slot_id', 'ready_status')
        )

        for user in (self.director, self.teamlead, self.freelancer):
            self.assertEqual(self.get_overview(user).status_code, 200)

        self.assertEqual(
            list(RoomFunctionSlot.objects.values_list('id', 'role_key', 'is_active')),
            slots_before,
        )
        self.assertEqual(
            list(
                RoomMember.objects.values_list('id', 'function_slot_id', 'ready_status')
            ),
            members_before,
        )

    def test_configurator_post_does_not_touch_existing_assignments(self):
        """Увеличение состава добавляет слот и не трогает уже назначенного.

        Проекция создаёт недостающий слот, но существующий занятый остаётся
        активным вместе со своим участником: подбор ничего не переигрывает.
        """
        slot = self.assign(self.make_slot()).function_slot
        self.post_update(user=self.director, role_key='seller_middle', action='inc')
        slot.refresh_from_db()
        self.assertTrue(slot.is_active)
        self.assertEqual(
            sorted(
                RoomFunctionSlot.objects.filter(
                    room=self.room, role_key='seller_middle', is_active=True
                ).values_list('slot_index', flat=True)
            ),
            [1, 2],
        )
        self.assertEqual(
            RoomMember.objects.get(user=self.freelancer).function_slot_id, slot.id
        )
