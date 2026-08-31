"""Оценки окупаемости для дашборда директора.

Формулы и константы живут здесь: шаблон только показывает готовые поля.
Юнит-экономику и matching не пересчитываем — берём уже сохранённый
`Project.budget` и часы из снапшота `input_data['functional_roles']`.
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum

from .models import Project

# Ключ снапшота состава совпадает с unit_economics, но константу дублируем
# сознательно: модуль статистики не должен импортировать калькулятор состава.
FUNCTIONAL_ROLES_KEY = 'functional_roles'

# ---------------------------------------------------------------------------
# Константы оценки «классический найм vs WowLance»
# Источник: исходное ТЗ / продуктовые правки (отбор 5–14 дн., онбординг 3–7 дн.
# против ~1 часа staffing + 24 ч до первых звонков). Это ориентир платформы,
# не бухгалтерский факт и не KPI бухгалтерии заказчика.
# ---------------------------------------------------------------------------

CLASSIC_SELECTION_DAYS_MIN = 5
CLASSIC_SELECTION_DAYS_MAX = 14
CLASSIC_ONBOARDING_DAYS_MIN = 3
CLASSIC_ONBOARDING_DAYS_MAX = 7

# Середина диапазонов ТЗ: (5+14)/2 + (3+7)/2 = 14.5 календарных дня.
CLASSIC_HIRING_DAYS = Decimal(
    (
        (CLASSIC_SELECTION_DAYS_MIN + CLASSIC_SELECTION_DAYS_MAX) / 2
        + (CLASSIC_ONBOARDING_DAYS_MIN + CLASSIC_ONBOARDING_DAYS_MAX) / 2
    )
)

PLATFORM_STAFFING_HOURS = Decimal('1')
PLATFORM_TO_FIRST_CALLS_HOURS = Decimal('24')
HOURS_PER_DAY = Decimal('24')
PLATFORM_LAUNCH_DAYS = (
    PLATFORM_STAFFING_HOURS + PLATFORM_TO_FIRST_CALLS_HOURS
) / HOURS_PER_DAY

# Полная стоимость часа штатного сейла (оклад + налоги + нагрузка найма),
# ориентир для сравнения с бюджетом комнаты. Не прайс WowLance.
STAFF_FULL_COST_PER_HOUR_RUB = Decimal('800.00')

MONEY_ZERO = Decimal('0.00')


def _money(value) -> Decimal:
    if value is None:
        return MONEY_ZERO
    return Decimal(value).quantize(Decimal('0.01'))


def _composition_hours(project: Project) -> int:
    """Сумма часов из сохранённого снапшота состава; без live-каталога."""
    rows = (project.input_data or {}).get(FUNCTIONAL_ROLES_KEY) or []
    if not isinstance(rows, list):
        return 0
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            count = int(row.get('count') or 0)
            hours = int(row.get('hours_per_unit') or 0)
        except (TypeError, ValueError):
            continue
        if count > 0 and hours > 0:
            total += count * hours
    return total


def spent_budget_total(projects) -> Decimal:
    """Сумма Project.budget по проектам директора (снапшот, не live-прайс)."""
    total = projects.aggregate(total=Sum('budget'))['total']
    return _money(total)


def estimated_time_saved_days(launched_project_count: int) -> Decimal:
    """Дни, которые не ушли на классический найм, на каждый запущенный проект."""
    if launched_project_count <= 0:
        return Decimal('0')
    per_launch = CLASSIC_HIRING_DAYS - PLATFORM_LAUNCH_DAYS
    if per_launch < 0:
        per_launch = Decimal('0')
    return (per_launch * launched_project_count).quantize(Decimal('0.1'))


def estimated_money_saved(projects) -> Decimal:
    """Ставка штата × часы состава − бюджет комнаты, сумма по проектам."""
    saved = MONEY_ZERO
    for project in projects:
        hours = _composition_hours(project)
        if hours <= 0:
            continue
        staff_cost = STAFF_FULL_COST_PER_HOUR_RUB * Decimal(hours)
        delta = staff_cost - _money(project.budget)
        if delta > 0:
            saved += delta
    return _money(saved)


def director_finance_metrics(user) -> dict:
    """Четыре показателя финансовой полосы дашборда директора."""
    projects = Project.objects.filter(owner=user)
    launched = projects.exclude(status=Project.Status.DRAFT)
    spent = spent_budget_total(projects)
    return {
        'spent_total': spent,
        'spent_caption': 'по составу команды, тестовая оплата',
        'earned_total': MONEY_ZERO,
        'earned_caption': 'контур сделок не в этом релизе',
        'time_saved_days': estimated_time_saved_days(launched.count()),
        'time_saved_caption': 'оценка платформы',
        'money_saved_total': estimated_money_saved(projects),
        'money_saved_caption': 'оценка платформы',
    }
