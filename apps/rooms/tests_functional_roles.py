"""Функциональные роли и юнит-экономика проекта (Issue #11, backend & data).

Покрывается только backend-контур:

* структурный каталог в коде и его инварианты;
* админский каталог бизнес-значений (`FunctionalRoleConfig`) и его запреты;
* сервис сохранения состава: RBAC, статусы, валидация, снапшот, бюджет;
* сервис юнит-экономики: суммы, CPL, отсутствие записи в БД;
* регрессия на затирание `input_data`.

UI конфигуратора, синхронизация `RoomFunctionSlot` и подбор в этот этап
не входят и здесь не проверяются.
"""

from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.rooms import functional_roles as catalog
from apps.rooms import presets
from apps.rooms.admin import FunctionalRoleConfigAdmin
from apps.rooms.forms import ProjectCreateForm
from apps.rooms.models import FunctionalRoleConfig, Project

# Публичная точка входа модуля ROOM — `apps.rooms.services` (контракт Issue #11).
# Тесты ходят именно через неё, чтобы реэкспорт не отвалился незаметно.
from apps.rooms.services import (
    get_unit_economics_summary,
    update_project_functional_roles,
)
from apps.rooms.unit_economics import (
    FUNCTIONAL_ROLES_KEY,
    FunctionalRolesError,
    apply_package_to_project,
    user_can_edit_functional_roles,
)
from apps.test_helpers import make_director, make_freelancer, make_teamlead, make_user
from apps.users.models import User


def make_manager(email='manager-fr@example.com'):
    return make_user(email=email, role=User.Roles.MANAGER)


def make_admin(email='admin-fr@example.com'):
    """Администратор платформы со входом в Django admin.

    `status=ACTIVE` обязателен: `User.save()` выводит `is_active` из статуса,
    и pending-суперпользователь не смог бы залогиниться в админку.
    """
    return make_user(
        email=email,
        role=User.Roles.ADMIN,
        status=User.Status.ACTIVE,
        is_staff=True,
        is_superuser=True,
    )


class FunctionalRoleCatalogTests(TestCase):
    """Структурный каталог в коде (`apps.rooms.functional_roles`)."""

    def test_catalog_has_exactly_five_system_roles(self):
        self.assertEqual(
            list(catalog.FUNCTIONAL_ROLE_KEYS),
            [
                'teamlead',
                'seller_middle',
                'seller_senior',
                'linkedin_leadgen',
                'database_assistant',
            ],
        )

    def test_structural_labels_are_public_ui_titles(self):
        """Issue #11 задаёт публичные названия ролей; role_key остаются английскими."""
        self.assertEqual(
            {key: role.label for key, role in catalog.FUNCTIONAL_ROLES.items()},
            {
                'teamlead': 'Тимлид проекта',
                'seller_middle': 'Сейлер Middle',
                'seller_senior': 'Сейлер Senior',
                'linkedin_leadgen': 'Лидген LinkedIn',
                'database_assistant': 'Ассистент базы',
            },
        )

    def test_role_keys_stay_stable_english_identifiers(self):
        """Переименование титулов не трогает стабильные ключи."""
        for role_key in catalog.FUNCTIONAL_ROLE_KEYS:
            with self.subTest(role_key=role_key):
                self.assertTrue(role_key.isascii())
                self.assertEqual(role_key, role_key.lower())

    def test_structural_grade_and_channel_are_correct(self):
        expected = {
            'teamlead': (None, catalog.CHANNEL_ANY),
            'seller_middle': (catalog.GRADE_MIDDLE, catalog.CHANNEL_COLD_CALLING),
            'seller_senior': (catalog.GRADE_SENIOR, catalog.CHANNEL_COLD_CALLING),
            'linkedin_leadgen': (catalog.GRADE_MIDDLE, catalog.CHANNEL_LINKEDIN),
            'database_assistant': (catalog.GRADE_JUNIOR, catalog.CHANNEL_BASE),
        }
        for role_key, (grade, channel) in expected.items():
            with self.subTest(role_key=role_key):
                role = catalog.FUNCTIONAL_ROLES[role_key]
                self.assertEqual(role.grade, grade)
                self.assertEqual(role.channel, channel)

    def test_database_assistant_channel_base_lives_only_in_structural_catalog(self):
        """`base` — канал каталога, а не enum слота: matching не расширяется."""
        from apps.rooms.models import RoomFunctionSlot

        self.assertEqual(
            catalog.FUNCTIONAL_ROLES['database_assistant'].channel,
            catalog.CHANNEL_BASE,
        )
        self.assertNotIn(
            catalog.CHANNEL_BASE,
            {choice[0] for choice in RoomFunctionSlot.Channel.choices},
        )

    def test_teamlead_is_fixed(self):
        self.assertTrue(catalog.FUNCTIONAL_ROLES['teamlead'].is_fixed)
        self.assertEqual(catalog.FIXED_ROLE_KEYS, frozenset({'teamlead'}))

    def test_other_roles_are_not_fixed(self):
        for role_key, role in catalog.FUNCTIONAL_ROLES.items():
            if role_key == 'teamlead':
                continue
            with self.subTest(role_key=role_key):
                self.assertFalse(role.is_fixed)


class FunctionalRoleConfigSeedTests(TestCase):
    """Data-миграция 0008: бизнес-значения руководителя."""

    def test_migration_seeds_five_records(self):
        self.assertEqual(FunctionalRoleConfig.objects.count(), 5)
        self.assertEqual(
            set(FunctionalRoleConfig.objects.values_list('role_key', flat=True)),
            set(catalog.FUNCTIONAL_ROLE_KEYS),
        )

    def test_migration_seeds_approved_business_values(self):
        expected = {
            'teamlead': (Decimal('35000.00'), 80, 'Стратегия, контроль SLA', 0),
            'seller_middle': (Decimal('62000.00'), 160, '60 звонков / день', 10),
            'seller_senior': (Decimal('85000.00'), 160, '80 звонков / день', 15),
            'linkedin_leadgen': (Decimal('48000.00'), 160, '40 касаний / день', 8),
            'database_assistant': (Decimal('28000.00'), 80, 'База, CRM, разметка', 0),
        }
        for role_key, (cost, hours, productivity, hot) in expected.items():
            with self.subTest(role_key=role_key):
                config = FunctionalRoleConfig.objects.get(role_key=role_key)
                self.assertEqual(config.monthly_cost, cost)
                self.assertEqual(config.monthly_hours, hours)
                self.assertEqual(config.productivity_text, productivity)
                self.assertEqual(config.hot_leads_per_month, hot)

    def test_structural_fields_are_exposed_read_only(self):
        config = FunctionalRoleConfig.objects.get(role_key='database_assistant')
        self.assertEqual(config.label, 'Ассистент базы')
        self.assertEqual(config.grade, catalog.GRADE_JUNIOR)
        self.assertEqual(config.channel, catalog.CHANNEL_BASE)
        self.assertFalse(config.is_fixed)
        self.assertTrue(
            FunctionalRoleConfig.objects.get(role_key='teamlead').is_fixed
        )


class FunctionalRoleConfigModelTests(TestCase):
    """Инварианты модели: чужой role_key, переименование, удаление."""

    def test_unknown_role_key_rejected_by_clean(self):
        config = FunctionalRoleConfig(role_key='sales_ninja', monthly_cost=1)
        with self.assertRaises(ValidationError) as ctx:
            config.full_clean()
        self.assertIn('role_key', ctx.exception.error_dict)

    def test_role_key_cannot_be_changed_after_creation(self):
        config = FunctionalRoleConfig.objects.get(role_key='teamlead')
        config.role_key = 'seller_senior'
        with self.assertRaises(ValidationError) as ctx:
            config.full_clean()
        self.assertIn('role_key', ctx.exception.error_dict)

    def test_system_role_cannot_be_deleted(self):
        config = FunctionalRoleConfig.objects.get(role_key='teamlead')
        with self.assertRaises(ValidationError):
            config.delete()
        self.assertEqual(FunctionalRoleConfig.objects.count(), 5)


class FunctionalRoleConfigAdminTests(TestCase):
    """Django admin: правим только бизнес-значения."""

    def setUp(self):
        self.admin_user = make_admin()
        self.model_admin = FunctionalRoleConfigAdmin(FunctionalRoleConfig, AdminSite())
        self.request = RequestFactory().get('/admin/')
        self.request.user = self.admin_user

    def test_cannot_add_sixth_system_role(self):
        self.assertFalse(self.model_admin.has_add_permission(self.request))

    def test_cannot_delete_system_role(self):
        config = FunctionalRoleConfig.objects.get(role_key='teamlead')
        self.assertFalse(self.model_admin.has_delete_permission(self.request, config))
        self.assertNotIn('delete_selected', self.model_admin.get_actions(self.request))

    def test_role_key_is_readonly_on_existing_record(self):
        config = FunctionalRoleConfig.objects.get(role_key='teamlead')
        self.assertIn('role_key', self.model_admin.get_readonly_fields(self.request, config))
        self.assertNotIn('role_key', self.model_admin.get_readonly_fields(self.request, None))

    def test_admin_shows_public_ui_title_as_structural_label(self):
        for role_key, title in (
            ('teamlead', 'Тимлид проекта'),
            ('seller_middle', 'Сейлер Middle'),
            ('seller_senior', 'Сейлер Senior'),
            ('linkedin_leadgen', 'Лидген LinkedIn'),
            ('database_assistant', 'Ассистент базы'),
        ):
            with self.subTest(role_key=role_key):
                config = FunctionalRoleConfig.objects.get(role_key=role_key)
                self.assertEqual(self.model_admin.label(config), title)

    def test_structural_fields_are_readonly(self):
        readonly = self.model_admin.get_readonly_fields(self.request, None)
        for name in ('label', 'grade_display', 'channel_display', 'is_fixed'):
            with self.subTest(field=name):
                self.assertIn(name, readonly)

    def test_admin_can_change_business_values(self):
        config = FunctionalRoleConfig.objects.get(role_key='seller_middle')
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse('admin:rooms_functionalroleconfig_change', args=[config.pk]),
            {
                'monthly_cost': '70000.00',
                'monthly_hours': '170',
                'productivity_text': '70 звонков / день',
                'hot_leads_per_month': '12',
            },
        )
        self.assertEqual(response.status_code, 302)

        config.refresh_from_db()
        self.assertEqual(config.monthly_cost, Decimal('70000.00'))
        self.assertEqual(config.monthly_hours, 170)
        self.assertEqual(config.productivity_text, '70 звонков / день')
        self.assertEqual(config.hot_leads_per_month, 12)

    def test_admin_post_cannot_change_role_key(self):
        config = FunctionalRoleConfig.objects.get(role_key='seller_middle')
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse('admin:rooms_functionalroleconfig_change', args=[config.pk]),
            {
                'role_key': 'seller_senior',
                'monthly_cost': '62000.00',
                'monthly_hours': '160',
                'productivity_text': '60 звонков / день',
                'hot_leads_per_month': '10',
            },
        )
        # Сохранение проходит успешно — просто `role_key` из запроса
        # игнорируется, а не роняет форму: поле readonly и в неё не входит.
        self.assertEqual(response.status_code, 302)
        config.refresh_from_db()
        self.assertEqual(config.role_key, 'seller_middle')

    def test_admin_add_view_is_forbidden(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('admin:rooms_functionalroleconfig_add'))
        self.assertEqual(response.status_code, 403)

    def test_admin_delete_view_is_forbidden(self):
        config = FunctionalRoleConfig.objects.get(role_key='teamlead')
        self.client.force_login(self.admin_user)
        response = self.client.get(
            reverse('admin:rooms_functionalroleconfig_delete', args=[config.pk])
        )
        self.assertEqual(response.status_code, 403)


class ServicesPublicContractTests(TestCase):
    """Issue #11: публичный API живёт в `apps.rooms.services`."""

    def test_services_module_exports_issue_11_functions(self):
        from apps.rooms.services import (
            get_unit_economics_summary as exported_summary,
            update_project_functional_roles as exported_update,
        )
        from apps.rooms import unit_economics

        # Реэкспорт, а не копия реализации: расходиться им нельзя.
        self.assertIs(exported_update, unit_economics.update_project_functional_roles)
        self.assertIs(exported_summary, unit_economics.get_unit_economics_summary)

    def test_importing_services_does_not_create_cycles(self):
        """Импорт фасада и staffing в любом порядке не должен падать."""
        import importlib

        for name in (
            'apps.rooms.services',
            'apps.rooms.unit_economics',
            'apps.rooms.presets',
            'apps.rooms.staffing',
        ):
            with self.subTest(module=name):
                self.assertIsNotNone(importlib.import_module(name))


class PackageTests(TestCase):
    """Коммерческие пакеты — константы продукта в `presets.py`, без таблицы в БД."""

    def test_packages_are_public_through_presets_module(self):
        """Контракт Issue #11 (WBS): пакеты доступны из `apps.rooms.presets`."""
        from apps.rooms.presets import (
            FUNCTIONAL_ROLE_PACKAGES,
            functional_role_package_composition,
            get_functional_role_package,
        )

        self.assertEqual(
            list(FUNCTIONAL_ROLE_PACKAGES), ['quick_start', 'scaling', 'enterprise']
        )
        self.assertIsNotNone(get_functional_role_package('quick_start'))
        self.assertIsNone(get_functional_role_package('нет такого'))
        self.assertTrue(functional_role_package_composition('scaling'))

    def test_package_labels(self):
        self.assertEqual(
            [p.label for p in presets.FUNCTIONAL_ROLE_PACKAGES.values()],
            ['Быстрый старт', 'Масштабирование', 'Enterprise аутрич'],
        )

    def test_quick_start_composition(self):
        self.assertEqual(
            dict(presets.FUNCTIONAL_ROLE_PACKAGES['quick_start'].composition),
            {'teamlead': 1, 'seller_middle': 1},
        )

    def test_scaling_composition(self):
        self.assertEqual(
            dict(presets.FUNCTIONAL_ROLE_PACKAGES['scaling'].composition),
            {'teamlead': 1, 'seller_middle': 2, 'linkedin_leadgen': 1},
        )

    def test_enterprise_composition(self):
        self.assertEqual(
            dict(presets.FUNCTIONAL_ROLE_PACKAGES['enterprise'].composition),
            {'teamlead': 1, 'seller_senior': 2, 'linkedin_leadgen': 1},
        )

    def test_no_package_model_exists(self):
        """Пакеты остаются константами: таблицы в БД для них нет."""
        from django.apps import apps as django_apps

        model_names = {m.__name__ for m in django_apps.get_app_config('rooms').get_models()}
        self.assertNotIn('Package', model_names)
        self.assertNotIn('FunctionalRolePackage', model_names)

    def test_architecture_presets_still_work(self):
        """Пакеты добавлены рядом, а не вместо существующих пресетов wizard."""
        self.assertEqual(
            set(presets.ARCHITECTURE_PRESETS), {'cold_calling', 'linkedin', 'scaleup'}
        )
        self.assertIsNotNone(presets.get_architecture_preset('cold_calling'))

    def test_every_package_satisfies_fixed_teamlead_rule(self):
        for key, package in presets.FUNCTIONAL_ROLE_PACKAGES.items():
            with self.subTest(package=key):
                self.assertGreaterEqual(package.composition.get('teamlead', 0), 1)

    def test_package_composition_is_input_ready_and_copied(self):
        composition = presets.functional_role_package_composition('quick_start')
        self.assertEqual(
            composition,
            [
                {'role_key': 'teamlead', 'count': 1},
                {'role_key': 'seller_middle', 'count': 1},
            ],
        )
        composition[0]['count'] = 99
        self.assertEqual(
            presets.FUNCTIONAL_ROLE_PACKAGES['quick_start'].composition['teamlead'], 1
        )


class FunctionalRolesServiceTestCase(TestCase):
    def setUp(self):
        self.director = make_director(email='dir-fr@example.com')
        self.project = Project.objects.create(
            owner=self.director,
            name='Проект с функциональными ролями',
            status=Project.Status.DRAFT,
            input_data={
                'offer': 'Оффер',
                'utp': 'УТП',
                'audience': 'Аудитория',
                'hot_criteria': 'Критерии',
            },
        )

    def quick_start(self):
        return [
            {'role_key': 'teamlead', 'count': 1},
            {'role_key': 'seller_middle', 'count': 1},
        ]


class UpdateFunctionalRolesRbacTests(FunctionalRolesServiceTestCase):
    def test_owner_director_can_save(self):
        summary = update_project_functional_roles(
            self.project, self.quick_start(), self.director
        )
        self.assertEqual(
            summary.composition,
            [
                {'role_key': 'teamlead', 'count': 1},
                {'role_key': 'seller_middle', 'count': 1},
            ],
        )

    def test_platform_admin_cannot_save(self):
        """Роль ADMIN обслуживает систему, а состав команды покупает директор."""
        with self.assertRaises(PermissionDenied):
            update_project_functional_roles(
                self.project, self.quick_start(), make_admin()
            )

    def test_owner_without_director_role_cannot_save(self):
        """Владения мало: право на состав даёт связка «владелец + директор»."""
        owner = self.project.owner
        owner.role = User.Roles.MANAGER
        owner.save(update_fields=['role'])
        self.project.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            update_project_functional_roles(self.project, self.quick_start(), owner)

    def test_permission_predicate_matrix(self):
        """Матрица прав целиком, включая анонима."""
        from django.contrib.auth.models import AnonymousUser

        allowed = [self.director]
        denied = [
            AnonymousUser(),
            make_admin(email='admin-matrix@example.com'),
            make_director(email='dir-matrix@example.com'),
            make_teamlead(email='tl-matrix@example.com'),
            make_manager(email='mgr-matrix@example.com'),
            make_freelancer(email='fl-matrix@example.com'),
        ]
        for user in allowed:
            with self.subTest(user=user, expected='allowed'):
                self.assertTrue(user_can_edit_functional_roles(user, self.project))
        for user in denied:
            with self.subTest(user=getattr(user, 'email', 'anonymous'), expected='denied'):
                self.assertFalse(user_can_edit_functional_roles(user, self.project))

    def test_teamlead_cannot_save(self):
        teamlead = make_teamlead(email='tl-fr@example.com')
        self.project.teamlead = teamlead
        self.project.save(update_fields=['teamlead'])
        with self.assertRaises(PermissionDenied):
            update_project_functional_roles(self.project, self.quick_start(), teamlead)

    def test_freelancer_cannot_save(self):
        with self.assertRaises(PermissionDenied):
            update_project_functional_roles(
                self.project, self.quick_start(), make_freelancer(email='fl-fr@example.com')
            )

    def test_manager_cannot_save(self):
        with self.assertRaises(PermissionDenied):
            update_project_functional_roles(
                self.project, self.quick_start(), make_manager()
            )

    def test_other_director_cannot_save(self):
        with self.assertRaises(PermissionDenied):
            update_project_functional_roles(
                self.project, self.quick_start(), make_director(email='dir2-fr@example.com')
            )


class UpdateFunctionalRolesStatusTests(FunctionalRolesServiceTestCase):
    def test_draft_is_editable(self):
        self.project.status = Project.Status.DRAFT
        self.project.save(update_fields=['status'])
        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.assertEqual(self.project.budget, Decimal('97000.00'))

    def test_staffing_is_editable(self):
        self.project.status = Project.Status.STAFFING
        self.project.save(update_fields=['status'])
        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.assertEqual(self.project.budget, Decimal('97000.00'))

    def test_active_is_not_editable(self):
        self.project.status = Project.Status.ACTIVE
        self.project.save(update_fields=['status'])
        with self.assertRaises(FunctionalRolesError):
            update_project_functional_roles(
                self.project, self.quick_start(), self.director
            )

    def test_closed_statuses_are_not_editable(self):
        for status in (
            Project.Status.LAUNCHED,
            Project.Status.ON_HOLD,
            Project.Status.COMPLETED,
            Project.Status.ARCHIVED,
        ):
            with self.subTest(status=status):
                self.project.status = status
                self.project.save(update_fields=['status'])
                with self.assertRaises(FunctionalRolesError):
                    update_project_functional_roles(
                        self.project, self.quick_start(), self.director
                    )


class UpdateFunctionalRolesValidationTests(FunctionalRolesServiceTestCase):
    def test_negative_count_rejected(self):
        with self.assertRaises(FunctionalRolesError):
            update_project_functional_roles(
                self.project,
                [
                    {'role_key': 'teamlead', 'count': 1},
                    {'role_key': 'seller_middle', 'count': -1},
                ],
                self.director,
            )

    def test_unknown_role_rejected(self):
        with self.assertRaises(FunctionalRolesError):
            update_project_functional_roles(
                self.project,
                [
                    {'role_key': 'teamlead', 'count': 1},
                    {'role_key': 'sales_ninja', 'count': 1},
                ],
                self.director,
            )

    def test_missing_teamlead_rejected(self):
        with self.assertRaises(FunctionalRolesError):
            update_project_functional_roles(
                self.project, [{'role_key': 'seller_middle', 'count': 1}], self.director
            )

    def test_teamlead_zero_rejected(self):
        with self.assertRaises(FunctionalRolesError):
            update_project_functional_roles(
                self.project,
                [
                    {'role_key': 'teamlead', 'count': 0},
                    {'role_key': 'seller_middle', 'count': 1},
                ],
                self.director,
            )

    def test_empty_composition_rejected(self):
        with self.assertRaises(FunctionalRolesError):
            update_project_functional_roles(self.project, [], self.director)

    def test_duplicate_role_key_rejected(self):
        with self.assertRaises(FunctionalRolesError):
            update_project_functional_roles(
                self.project,
                [
                    {'role_key': 'teamlead', 'count': 1},
                    {'role_key': 'seller_middle', 'count': 1},
                    {'role_key': 'seller_middle', 'count': 2},
                ],
                self.director,
            )

    def test_numeric_string_count_is_accepted(self):
        """Состав придёт из формы, где count всегда строка."""
        summary = update_project_functional_roles(
            self.project,
            [
                {'role_key': 'teamlead', 'count': '1'},
                {'role_key': 'seller_middle', 'count': ' 2 '},
            ],
            self.director,
        )
        self.assertEqual(
            summary.composition,
            [
                {'role_key': 'teamlead', 'count': 1},
                {'role_key': 'seller_middle', 'count': 2},
            ],
        )

    def test_negative_string_count_rejected(self):
        with self.assertRaises(FunctionalRolesError):
            update_project_functional_roles(
                self.project,
                [
                    {'role_key': 'teamlead', 'count': 1},
                    {'role_key': 'seller_middle', 'count': '-1'},
                ],
                self.director,
            )

    def test_non_integer_count_rejected(self):
        # '--5' и '٥' проходят наивную проверку через `lstrip('-').isdigit()`,
        # поэтому разбор строк проверяется на них явно.
        for bad in (1.5, 'два', None, True, '--5', '٥', '', '-', '1.0', [1]):
            with self.subTest(count=bad):
                with self.assertRaises(FunctionalRolesError):
                    update_project_functional_roles(
                        self.project,
                        [
                            {'role_key': 'teamlead', 'count': 1},
                            {'role_key': 'seller_middle', 'count': bad},
                        ],
                        self.director,
                    )

    def test_zero_count_removes_optional_role(self):
        update_project_functional_roles(
            self.project,
            [
                {'role_key': 'teamlead', 'count': 1},
                {'role_key': 'seller_middle', 'count': 1},
                {'role_key': 'linkedin_leadgen', 'count': 0},
            ],
            self.director,
        )
        self.assertEqual(
            [row['role_key'] for row in self.project.input_data[FUNCTIONAL_ROLES_KEY]],
            ['teamlead', 'seller_middle'],
        )

    def test_client_cannot_override_business_values(self):
        """Экономика берётся только с сервера, что бы ни прислал клиент."""
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
        seller = self.project.input_data[FUNCTIONAL_ROLES_KEY][1]
        self.assertEqual(seller['cost_per_unit'], '62000.00')
        self.assertEqual(seller['hours_per_unit'], 160)
        self.assertEqual(seller['kpi_leads_per_unit'], 10)
        self.assertEqual(seller['productivity_text'], '60 звонков / день')
        self.assertEqual(self.project.budget, Decimal('97000.00'))

    def test_mapping_input_is_accepted(self):
        summary = update_project_functional_roles(
            self.project, {'teamlead': 1, 'seller_middle': 2}, self.director
        )
        self.assertEqual(
            summary.composition,
            [
                {'role_key': 'teamlead', 'count': 1},
                {'role_key': 'seller_middle', 'count': 2},
            ],
        )


class UpdateFunctionalRolesStorageTests(FunctionalRolesServiceTestCase):
    def test_other_input_data_keys_are_preserved(self):
        self.project.input_data = {
            **self.project.input_data,
            'architecture': 'cold_calling',
            'notes': 'важная заметка',
        }
        self.project.save(update_fields=['input_data'])

        update_project_functional_roles(self.project, self.quick_start(), self.director)

        self.project.refresh_from_db()
        self.assertEqual(self.project.input_data['offer'], 'Оффер')
        self.assertEqual(self.project.input_data['utp'], 'УТП')
        self.assertEqual(self.project.input_data['audience'], 'Аудитория')
        self.assertEqual(self.project.input_data['hot_criteria'], 'Критерии')
        self.assertEqual(self.project.input_data['architecture'], 'cold_calling')
        self.assertEqual(self.project.input_data['notes'], 'важная заметка')
        self.assertIn(FUNCTIONAL_ROLES_KEY, self.project.input_data)

    def test_functional_roles_is_a_plain_list(self):
        """Контракт Issue #11: значение ключа — сам список, без обёртки."""
        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.project.refresh_from_db()

        stored = self.project.input_data[FUNCTIONAL_ROLES_KEY]
        self.assertIsInstance(stored, list)
        self.assertEqual(len(stored), 2)

    def test_snapshot_uses_exact_public_field_names(self):
        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.project.refresh_from_db()

        stored = self.project.input_data[FUNCTIONAL_ROLES_KEY]
        self.assertEqual(
            stored[0],
            {
                'id': 'role_teamlead',
                'role_key': 'teamlead',
                'title': 'Тимлид проекта',
                'count': 1,
                'cost_per_unit': '35000.00',
                'hours_per_unit': 80,
                'productivity_text': 'Стратегия, контроль SLA',
                'kpi_leads_per_unit': 0,
                'is_fixed': True,
            },
        )
        # Ни одна строка не должна нести лишних или внутренних имён полей.
        expected_keys = {
            'id', 'role_key', 'title', 'count', 'cost_per_unit', 'hours_per_unit',
            'productivity_text', 'kpi_leads_per_unit', 'is_fixed',
        }
        for entry in stored:
            with self.subTest(role_key=entry['role_key']):
                self.assertEqual(set(entry), expected_keys)

    def test_snapshot_id_is_stable_role_prefix(self):
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
        stored = self.project.input_data[FUNCTIONAL_ROLES_KEY]

        self.assertEqual(
            [entry['id'] for entry in stored],
            ['role_teamlead', 'role_seller_senior', 'role_linkedin_leadgen'],
        )
        for entry in stored:
            with self.subTest(role_key=entry['role_key']):
                self.assertEqual(entry['id'], f"role_{entry['role_key']}")

        # id не зависит от count и не меняется между сохранениями.
        first_ids = [entry['id'] for entry in stored]
        update_project_functional_roles(
            self.project,
            [
                {'role_key': 'teamlead', 'count': 1},
                {'role_key': 'seller_senior', 'count': 5},
                {'role_key': 'linkedin_leadgen', 'count': 3},
            ],
            self.director,
        )
        self.project.refresh_from_db()
        self.assertEqual(
            [entry['id'] for entry in self.project.input_data[FUNCTIONAL_ROLES_KEY]],
            first_ids,
        )

    def test_snapshot_carries_is_fixed_and_title(self):
        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.project.refresh_from_db()
        by_key = {e['role_key']: e for e in self.project.input_data[FUNCTIONAL_ROLES_KEY]}

        self.assertTrue(by_key['teamlead']['is_fixed'])
        self.assertFalse(by_key['seller_middle']['is_fixed'])
        self.assertEqual(by_key['seller_middle']['title'], 'Сейлер Middle')

    def test_snapshot_title_uses_public_ui_titles_for_every_role(self):
        update_project_functional_roles(
            self.project,
            [{'role_key': key, 'count': 1} for key in catalog.FUNCTIONAL_ROLE_KEYS],
            self.director,
        )
        self.project.refresh_from_db()
        titles = {
            entry['role_key']: entry['title']
            for entry in self.project.input_data[FUNCTIONAL_ROLES_KEY]
        }
        self.assertEqual(
            titles,
            {
                'teamlead': 'Тимлид проекта',
                'seller_middle': 'Сейлер Middle',
                'seller_senior': 'Сейлер Senior',
                'linkedin_leadgen': 'Лидген LinkedIn',
                'database_assistant': 'Ассистент базы',
            },
        )

    def test_snapshot_omits_structural_grade_and_channel(self):
        """Грейд и канал остаются в каталоге и добавляются в summary server-side."""
        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.project.refresh_from_db()

        for entry in self.project.input_data[FUNCTIONAL_ROLES_KEY]:
            with self.subTest(role_key=entry['role_key']):
                self.assertNotIn('grade', entry)
                self.assertNotIn('channel', entry)

    def test_snapshot_is_json_safe(self):
        import json

        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.project.refresh_from_db()

        stored = self.project.input_data[FUNCTIONAL_ROLES_KEY]
        # Снапшот должен переживать round-trip через JSON без потерь.
        self.assertEqual(json.loads(json.dumps(stored)), stored)

    def test_budget_is_synced_with_total(self):
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

    def test_budget_is_recomputed_on_each_save(self):
        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.project.refresh_from_db()
        self.assertEqual(self.project.budget, Decimal('97000.00'))

        update_project_functional_roles(
            self.project,
            [
                {'role_key': 'teamlead', 'count': 1},
                {'role_key': 'seller_middle', 'count': 2},
                {'role_key': 'linkedin_leadgen', 'count': 1},
            ],
            self.director,
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.budget, Decimal('207000.00'))

    def test_manual_budget_is_overridden_by_composition(self):
        """`Project.budget` перестаёт быть вторым ручным источником истины."""
        self.project.budget = Decimal('1.00')
        self.project.save(update_fields=['budget'])

        update_project_functional_roles(self.project, self.quick_start(), self.director)

        self.project.refresh_from_db()
        self.assertEqual(self.project.budget, Decimal('97000.00'))

    def test_no_room_function_slots_are_created(self):
        """Проекция состава в слоты — следующий этап, здесь её быть не должно."""
        from apps.rooms.models import RoomFunctionSlot

        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.assertEqual(
            RoomFunctionSlot.objects.filter(room__project=self.project).count(), 0
        )


class UnitEconomicsSummaryTests(FunctionalRolesServiceTestCase):
    def save_package(self, package_key):
        apply_package_to_project(self.project, package_key, self.director)
        self.project.refresh_from_db()
        return get_unit_economics_summary(self.project)

    def test_quick_start_numbers(self):
        summary = self.save_package('quick_start')
        self.assertEqual(summary.total_budget, Decimal('97000.00'))
        self.assertEqual(summary.total_hours, 240)
        self.assertEqual(summary.forecast_hot_leads, 10)
        self.assertEqual(summary.cpl, Decimal('9700.00'))

    def test_scaling_numbers(self):
        summary = self.save_package('scaling')
        self.assertEqual(summary.total_budget, Decimal('207000.00'))
        self.assertEqual(summary.total_hours, 560)
        self.assertEqual(summary.forecast_hot_leads, 28)
        # 207000 / 28 = 7392.857142…
        self.assertEqual(summary.cpl, Decimal('7392.86'))

    def test_enterprise_numbers(self):
        summary = self.save_package('enterprise')
        self.assertEqual(summary.total_budget, Decimal('253000.00'))
        self.assertEqual(summary.total_hours, 560)
        self.assertEqual(summary.forecast_hot_leads, 38)
        # 253000 / 38 = 6657.894736…
        self.assertEqual(summary.cpl, Decimal('6657.89'))

    def test_cpl_is_decimal_without_float_drift(self):
        summary = self.save_package('scaling')
        self.assertIsInstance(summary.cpl, Decimal)
        self.assertIsInstance(summary.total_budget, Decimal)
        self.assertEqual(
            summary.cpl,
            (Decimal('207000.00') / Decimal(28)).quantize(Decimal('0.01')),
        )

    def test_zero_hot_leads_gives_cpl_none(self):
        update_project_functional_roles(
            self.project,
            [
                {'role_key': 'teamlead', 'count': 1},
                {'role_key': 'database_assistant', 'count': 1},
            ],
            self.director,
        )
        self.project.refresh_from_db()
        summary = get_unit_economics_summary(self.project)

        self.assertEqual(summary.forecast_hot_leads, 0)
        self.assertIsNone(summary.cpl)
        self.assertEqual(summary.total_budget, Decimal('63000.00'))
        self.assertEqual(summary.total_hours, 160)

    def test_summary_reads_new_list_shape(self):
        """Сводка считает по списку публичных полей, а не по старой обёртке."""
        self.project.input_data = {
            **self.project.input_data,
            FUNCTIONAL_ROLES_KEY: [
                {
                    'id': 'role_teamlead',
                    'role_key': 'teamlead',
                    'title': 'Тимлид проекта',
                    'count': 1,
                    'cost_per_unit': '35000.00',
                    'hours_per_unit': 80,
                    'productivity_text': 'Стратегия, контроль SLA',
                    'kpi_leads_per_unit': 0,
                    'is_fixed': True,
                },
                {
                    'id': 'role_seller_middle',
                    'role_key': 'seller_middle',
                    'title': 'Сейлер Middle',
                    'count': 1,
                    'cost_per_unit': '62000.00',
                    'hours_per_unit': 160,
                    'productivity_text': '60 звонков / день',
                    'kpi_leads_per_unit': 10,
                    'is_fixed': False,
                },
            ],
        }
        self.project.save(update_fields=['input_data'])

        summary = get_unit_economics_summary(self.project)
        self.assertEqual(summary.total_budget, Decimal('97000.00'))
        self.assertEqual(summary.total_hours, 240)
        self.assertEqual(summary.forecast_hot_leads, 10)
        self.assertEqual(summary.cpl, Decimal('9700.00'))

    def test_old_wrapper_shape_is_ignored(self):
        """Снятая обёртка {version, roles} больше не читается как состав."""
        self.project.input_data = {
            **self.project.input_data,
            FUNCTIONAL_ROLES_KEY: {'version': 1, 'roles': [{'role_key': 'teamlead'}]},
        }
        self.project.save(update_fields=['input_data'])

        self.assertTrue(get_unit_economics_summary(self.project).is_empty)

    def test_rows_mix_snapshot_money_and_structural_catalog(self):
        summary = self.save_package('enterprise')
        rows = {row.role_key: row for row in summary.rows}

        teamlead = rows['teamlead']
        self.assertEqual(teamlead.label, 'Тимлид проекта')
        self.assertIsNone(teamlead.grade)
        self.assertEqual(teamlead.channel, catalog.CHANNEL_ANY)
        self.assertTrue(teamlead.is_fixed)
        self.assertEqual(teamlead.subtotal_cost, Decimal('35000.00'))

        senior = rows['seller_senior']
        self.assertEqual(senior.count, 2)
        self.assertEqual(senior.grade, catalog.GRADE_SENIOR)
        self.assertEqual(senior.channel, catalog.CHANNEL_COLD_CALLING)
        self.assertFalse(senior.is_fixed)
        self.assertEqual(senior.subtotal_cost, Decimal('170000.00'))
        self.assertEqual(senior.subtotal_hours, 320)
        self.assertEqual(senior.subtotal_hot_leads, 30)

    def test_summary_rows_use_public_ui_titles_for_every_role(self):
        update_project_functional_roles(
            self.project,
            [{'role_key': key, 'count': 1} for key in catalog.FUNCTIONAL_ROLE_KEYS],
            self.director,
        )
        self.project.refresh_from_db()
        summary = get_unit_economics_summary(self.project)

        self.assertEqual(
            {row.role_key: row.label for row in summary.rows},
            {
                'teamlead': 'Тимлид проекта',
                'seller_middle': 'Сейлер Middle',
                'seller_senior': 'Сейлер Senior',
                'linkedin_leadgen': 'Лидген LinkedIn',
                'database_assistant': 'Ассистент базы',
            },
        )

    def test_productivity_stays_text_and_no_numeric_reach_is_invented(self):
        summary = self.save_package('quick_start')
        rows = {row.role_key: row for row in summary.rows}

        self.assertEqual(rows['seller_middle'].productivity_text, '60 звонков / день')
        self.assertIsInstance(rows['seller_middle'].productivity_text, str)
        # Никакого суммарного «охвата»/«производительности» сервис не считает.
        for name in ('total_productivity', 'total_reach', 'forecast_touches'):
            with self.subTest(attribute=name):
                self.assertFalse(hasattr(summary, name))

    def test_summary_does_not_write_to_db(self):
        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.project.refresh_from_db()
        before = self.project.updated_at

        with self.assertNumQueries(0):
            get_unit_economics_summary(self.project)

        self.project.refresh_from_db()
        self.assertEqual(self.project.updated_at, before)

    def test_project_without_composition_gives_empty_summary(self):
        summary = get_unit_economics_summary(self.project)

        self.assertTrue(summary.is_empty)
        self.assertEqual(summary.rows, [])
        self.assertEqual(summary.total_budget, Decimal('0.00'))
        self.assertEqual(summary.total_hours, 0)
        self.assertEqual(summary.forecast_hot_leads, 0)
        self.assertIsNone(summary.cpl)

    def test_summary_on_get_does_not_create_composition(self):
        """GET не мутирует проект: состава по умолчанию не появляется."""
        get_unit_economics_summary(self.project)
        self.project.refresh_from_db()
        self.assertNotIn(FUNCTIONAL_ROLES_KEY, self.project.input_data)
        self.assertEqual(self.project.budget, Decimal('0.00'))


class SnapshotSemanticsTests(FunctionalRolesServiceTestCase):
    """Правка каталога администратором не переписывает согласованные проекты."""

    def test_admin_price_change_does_not_touch_saved_project(self):
        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.project.refresh_from_db()
        self.assertEqual(self.project.budget, Decimal('97000.00'))

        config = FunctionalRoleConfig.objects.get(role_key='seller_middle')
        config.monthly_cost = Decimal('99000.00')
        config.hot_leads_per_month = 20
        config.save(update_fields=['monthly_cost', 'hot_leads_per_month'])

        self.project.refresh_from_db()
        summary = get_unit_economics_summary(self.project)
        self.assertEqual(summary.total_budget, Decimal('97000.00'))
        self.assertEqual(summary.forecast_hot_leads, 10)
        self.assertEqual(self.project.budget, Decimal('97000.00'))

    def test_next_explicit_update_picks_up_new_catalog_values(self):
        update_project_functional_roles(self.project, self.quick_start(), self.director)

        config = FunctionalRoleConfig.objects.get(role_key='seller_middle')
        config.monthly_cost = Decimal('99000.00')
        config.hot_leads_per_month = 20
        config.productivity_text = '90 звонков / день'
        config.save(
            update_fields=['monthly_cost', 'hot_leads_per_month', 'productivity_text']
        )

        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.project.refresh_from_db()
        summary = get_unit_economics_summary(self.project)

        self.assertEqual(summary.total_budget, Decimal('134000.00'))
        self.assertEqual(summary.forecast_hot_leads, 20)
        self.assertEqual(self.project.budget, Decimal('134000.00'))
        rows = {row.role_key: row for row in summary.rows}
        self.assertEqual(rows['seller_middle'].productivity_text, '90 звонков / день')

    def test_other_projects_are_not_recalculated(self):
        """Массового ретроактивного пересчёта быть не должно."""
        other = Project.objects.create(
            owner=self.director,
            name='Второй проект',
            status=Project.Status.DRAFT,
        )
        update_project_functional_roles(self.project, self.quick_start(), self.director)
        update_project_functional_roles(other, self.quick_start(), self.director)

        config = FunctionalRoleConfig.objects.get(role_key='seller_middle')
        config.monthly_cost = Decimal('99000.00')
        config.save(update_fields=['monthly_cost'])

        update_project_functional_roles(self.project, self.quick_start(), self.director)

        other.refresh_from_db()
        self.assertEqual(other.budget, Decimal('97000.00'))
        self.assertEqual(
            get_unit_economics_summary(other).total_budget, Decimal('97000.00')
        )


class InputDataRegressionTests(FunctionalRolesServiceTestCase):
    """`ProjectCreateForm` больше не затирает `input_data` целиком."""

    def form_payload(self, **overrides):
        payload = {
            'name': 'Обновлённый проект',
            'project_type': Project.Type.BASE,
            'seller_level': Project.SellerLevel.MIDDLE,
            'tariff_plan': 'launch',
            'budget': '97000.00',
            'offer': 'Новый оффер',
            'utp': 'Новое УТП',
            'audience': 'Новая аудитория',
            'hot_criteria': 'Новые критерии',
        }
        payload.update(overrides)
        return payload

    def test_updating_inputs_keeps_functional_roles(self):
        update_project_functional_roles(self.project, self.quick_start(), self.director)
        self.project.refresh_from_db()
        saved_snapshot = self.project.input_data[FUNCTIONAL_ROLES_KEY]

        form = ProjectCreateForm(self.form_payload(), instance=self.project)
        self.assertTrue(form.is_valid(), form.errors)
        project = form.save()

        project.refresh_from_db()
        self.assertEqual(project.input_data[FUNCTIONAL_ROLES_KEY], saved_snapshot)
        self.assertEqual(project.input_data['offer'], 'Новый оффер')
        self.assertEqual(project.input_data['utp'], 'Новое УТП')
        self.assertEqual(project.input_data['audience'], 'Новая аудитория')
        self.assertEqual(project.input_data['hot_criteria'], 'Новые критерии')

    def test_updating_inputs_keeps_unrelated_keys(self):
        self.project.input_data = {
            **self.project.input_data,
            'architecture': 'cold_calling',
            'notes': 'заметка',
        }
        self.project.save(update_fields=['input_data'])

        form = ProjectCreateForm(self.form_payload(), instance=self.project)
        self.assertTrue(form.is_valid(), form.errors)
        project = form.save()

        project.refresh_from_db()
        self.assertEqual(project.input_data['architecture'], 'cold_calling')
        self.assertEqual(project.input_data['notes'], 'заметка')

    def test_new_project_still_gets_its_inputs(self):
        form = ProjectCreateForm(self.form_payload(name='Совсем новый проект'))
        self.assertTrue(form.is_valid(), form.errors)
        project = form.save(commit=False)
        project.owner = self.director
        project.save()

        project.refresh_from_db()
        self.assertEqual(
            project.input_data,
            {
                'offer': 'Новый оффер',
                'utp': 'Новое УТП',
                'audience': 'Новая аудитория',
                'hot_criteria': 'Новые критерии',
            },
        )
