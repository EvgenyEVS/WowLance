"""Пресеты архитектуры продаж для Apply Architecture / wizard."""

from decimal import Decimal

from .models import Project


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
