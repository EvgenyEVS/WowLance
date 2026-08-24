"""Продуктовые пресеты комнаты.

Два независимых набора:

* `ARCHITECTURE_PRESETS` — заготовки проекта для Apply Architecture / wizard
  (название, тип, тариф, вводные);
* `FUNCTIONAL_ROLE_PACKAGES` — готовые составы функциональных ролей
  (Issue #11): «Быстрый старт», «Масштабирование», «Enterprise аутрич».

Пакет — это только заготовка состава, а не тариф и не сущность в БД: после
применения директор правит `count` вручную, и связь проекта с пакетом нигде
не хранится. Поэтому таблицы пакетов нет и админского редактирования
пакетов тоже нет.
"""

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from . import functional_roles
from .models import Project

__all__ = [
    'ARCHITECTURE_PRESETS',
    'FUNCTIONAL_ROLE_PACKAGES',
    'FunctionalRolePackage',
    'apply_preset_to_form_initial',
    'functional_role_package_composition',
    'get_architecture_preset',
    'get_functional_role_package',
]


ARCHITECTURE_PRESETS = {
    'cold_calling': {
        'key': 'cold_calling',
        'label': 'Cold Calling Machine',
        'short': 'Агрессивный outbound по базе для B2B.',
        'scales': ('startup', 'smb'),
        'price_label': '₽97 000',
        'launch_label': '24 часа',
        'project_name': 'Cold Calling Machine',
        'project_type': Project.Type.BASE,
        'seller_level': Project.SellerLevel.MIDDLE,
        'tariff_plan': 'launch',
        'budget': Decimal('97000'),
        'kpi_target': Decimal('20'),
        'input_data': {
            'offer': 'Исходящие звонки по базе клиента с целью назначить встречу / демо.',
            'utp': 'Команда продавцов + контроль скриншотами + SLA первых звонков 24 часа.',
            'audience': 'B2B ЛПР в сегменте клиента (база предоставляется заказчиком).',
            'hot_criteria': 'Запросил демо, согласился на встречу, попросил коммерческое предложение.',
            'architecture': 'cold_calling',
        },
    },
    'linkedin': {
        'key': 'linkedin',
        'label': 'LinkedIn Outreach',
        'short': 'Тёплый аутрич в LinkedIn с квалификацией Hot.',
        'scales': ('startup', 'smb', 'enterprise'),
        'price_label': '₽83 000',
        'launch_label': '24 часа',
        'project_name': 'LinkedIn Outreach',
        'project_type': Project.Type.LINKEDIN,
        'seller_level': Project.SellerLevel.MIDDLE,
        'tariff_plan': 'launch',
        'budget': Decimal('83000'),
        'kpi_target': Decimal('15'),
        'input_data': {
            'offer': 'Последовательность касаний в LinkedIn: connect → value → CTA.',
            'utp': 'Шаблоны сообщений, чеклисты и прозрачный лог действий.',
            'audience': 'ЛПР в LinkedIn по ICP заказчика.',
            'hot_criteria': 'Ответил с интересом, согласился на созвон, запросил материалы.',
            'architecture': 'linkedin',
        },
    },
    'scaleup': {
        'key': 'scaleup',
        'label': 'The Scale-Up Machine',
        'short': 'Полная комната: тимлид, senior-продавцы, задачи и лиды.',
        'scales': ('smb', 'enterprise'),
        'price_label': '₽137 000',
        'launch_label': '1 час',
        'project_name': 'Scale-Up Machine',
        'project_type': Project.Type.BASE,
        'seller_level': Project.SellerLevel.SENIOR,
        'tariff_plan': 'scale',
        'budget': Decimal('137000'),
        'kpi_target': Decimal('40'),
        'input_data': {
            'offer': 'Масштабируемые продажи: staffing, SLA, Hot handoff менеджеру.',
            'utp': 'Единая комната проекта с контролем качества и KPI для директора.',
            'audience': 'Целевой сегмент роста заказчика (расширение воронки).',
            'hot_criteria': 'Готов к сделке: бюджет, срок, ЛПР подтверждены.',
            'architecture': 'scaleup',
        },
    },
}


def get_architecture_preset(key: str | None) -> dict | None:
    if not key:
        return None
    return ARCHITECTURE_PRESETS.get(key)


def apply_preset_to_form_initial(preset: dict) -> dict:
    """Начальные значения для ProjectCreateForm."""
    data = preset['input_data']
    return {
        'name': preset['project_name'],
        'project_type': preset['project_type'],
        'seller_level': preset['seller_level'],
        'tariff_plan': preset['tariff_plan'],
        'budget': preset['budget'],
        'kpi_target': preset.get('kpi_target'),
        'offer': data.get('offer', ''),
        'utp': data.get('utp', ''),
        'audience': data.get('audience', ''),
        'hot_criteria': data.get('hot_criteria', ''),
    }


@dataclass(frozen=True)
class FunctionalRolePackage:
    """Готовый состав функциональных ролей."""

    key: str
    label: str
    #: role_key → count. Порядок словаря — порядок структурного каталога.
    composition: MappingProxyType


def _package(key: str, label: str, composition: dict[str, int]) -> FunctionalRolePackage:
    """Собирает пакет, проверяя его по структурному каталогу на импорте модуля.

    Опечатка в `role_key` или пакет без обязательного Teamlead падают при
    загрузке приложения, а не при первом клике директора по кнопке пакета.
    """
    unknown = set(composition) - set(functional_roles.FUNCTIONAL_ROLES)
    if unknown:
        raise ValueError(f'Пакет {key}: неизвестные role_key {sorted(unknown)}')
    missing_fixed = functional_roles.FIXED_ROLE_KEYS - set(composition)
    if missing_fixed:
        raise ValueError(
            f'Пакет {key}: отсутствуют обязательные роли {sorted(missing_fixed)}'
        )
    return FunctionalRolePackage(
        key=key,
        label=label,
        composition=MappingProxyType(dict(composition)),
    )


#: Коммерческие пакеты MVP (Issue #11).
FUNCTIONAL_ROLE_PACKAGES: MappingProxyType = MappingProxyType({
    package.key: package
    for package in (
        _package('quick_start', 'Быстрый старт', {
            'teamlead': 1,
            'seller_middle': 1,
        }),
        _package('scaling', 'Масштабирование', {
            'teamlead': 1,
            'seller_middle': 2,
            'linkedin_leadgen': 1,
        }),
        _package('enterprise', 'Enterprise аутрич', {
            'teamlead': 1,
            'seller_senior': 2,
            'linkedin_leadgen': 1,
        }),
    )
})


def get_functional_role_package(package_key: str) -> FunctionalRolePackage | None:
    """Пакет по ключу или None. Имя не сокращаем до `get_package`: рядом
    живёт `get_architecture_preset`, и две «просто заготовки» перепутать легко."""
    return FUNCTIONAL_ROLE_PACKAGES.get(package_key)


def functional_role_package_composition(package_key: str) -> list[dict]:
    """Состав пакета в формате входных данных `update_project_functional_roles`.

    Возвращает новый список словарей: вызывающий код может править его,
    не рискуя изменить сам пакет.
    """
    package = get_functional_role_package(package_key)
    if package is None:
        raise KeyError(f'Неизвестный пакет: {package_key!r}')
    return [
        {'role_key': role_key, 'count': count}
        for role_key, count in package.composition.items()
    ]
