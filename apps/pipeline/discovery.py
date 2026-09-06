"""Чеклист фактов разговора по лиду (BANT + next step).

Галочки — доказательства для фрилансера и тимлида. Они **не** двигают
`qualification_status` и не вызывают `set_lead_qualification`: Hot по-прежнему
ставит только тимлид.
"""

from __future__ import annotations

from typing import Mapping

# Группы в порядке показа на карточке: (id легенды, русская подпись группы,
# список (key, подпись галочки)).
DISCOVERY_GROUPS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        'need',
        'Need',
        (
            ('need_task', 'Есть конкретная задача, не «просто смотрю»'),
            ('need_icp', 'Задача похожа на оффер / ЦА этого проекта'),
        ),
    ),
    (
        'authority',
        'Authority',
        (
            ('auth_talked', 'Говорили с ЛПР или с тем, кто влияет на решение'),
            ('auth_signer', 'Понятно, кто подпишет (имя или роль)'),
        ),
    ),
    (
        'budget',
        'Budget',
        (
            ('budget_named', 'Назвал порядок суммы или что бюджет есть'),
            ('budget_fit', 'Порядок суммы нам подходит (не только «пришлите прайс»)'),
        ),
    ),
    (
        'timeline',
        'Timeline',
        (
            ('time_has', 'Есть срок «нужно к дате / в этом квартале»'),
            ('time_soon', 'Срок близкий, не «когда-нибудь»'),
        ),
    ),
    (
        'talk',
        'Диалог (тепло, ещё не сделка)',
        (
            ('talk_replied', 'Был ответ на касание или живой разговор'),
            ('talk_questions', 'Задаёт вопросы / сравнивает / готов слушать'),
            ('talk_materials', 'Запросил материалы, кейс или прайс — без демо/договора'),
        ),
    ),
    (
        'next',
        'Следующий шаг (намерение купить)',
        (
            ('next_demo', 'Просит демо или встречу на конкретные дни'),
            ('next_proposal', 'Просит КП, договор или счёт'),
            ('next_this_week', 'Сказал «давайте на этой неделе» / «нужно внедрить к …»'),
            ('next_leaning', 'Бюджет согласован и склоняется к нам'),
        ),
    ),
)

DISCOVERY_KEYS: tuple[str, ...] = tuple(
    key for _gid, _label, fields in DISCOVERY_GROUPS for key, _caption in fields
)

DISCOVERY_LABELS: dict[str, str] = {
    key: caption for _gid, _label, fields in DISCOVERY_GROUPS for key, caption in fields
}

TALK_KEYS: tuple[str, ...] = ('talk_replied', 'talk_questions', 'talk_materials')
NEXT_KEYS: tuple[str, ...] = (
    'next_demo',
    'next_proposal',
    'next_this_week',
    'next_leaning',
)

HINT_COLD = 'cold'
HINT_WARM = 'warm'
HINT_HOT_READY = 'hot_ready'
HINT_THIN = 'thin'

HINT_TEXTS: dict[str, str] = {
    HINT_COLD: (
        'По фактам это Cold: квалификация и прогрев, интереса к сделке ещё нет.'
    ),
    HINT_WARM: (
        'Похоже на Warm: интерес есть, дожмите ЛПР, бюджет и срок.'
    ),
    HINT_HOT_READY: (
        'Готово отдать тимлиду на Hot: есть потребность, срок и конкретный '
        'следующий шаг. Статус Hot ставит только тимлид.'
    ),
    HINT_THIN: (
        'Фактов мало для тёплого: отметьте боль или факт диалога.'
    ),
}


def discovery_flag(checks: Mapping | None, key: str) -> bool:
    """Отсутствие ключа = нет (неотмеченная галочка)."""
    if not checks:
        return False
    return bool(checks.get(key))


def normalize_discovery_checks(raw: Mapping | None) -> dict[str, bool]:
    """Словарь по всем стабильным ключам: True/False, без чужих полей."""
    raw = raw or {}
    return {key: bool(raw.get(key)) for key in DISCOVERY_KEYS}


def discovery_hint_key(checks: Mapping | None) -> str:
    """Ключ подсказки по фактам чеклиста (не статус колонки)."""
    checks = checks or {}
    need_task = discovery_flag(checks, 'need_task')
    any_talk = any(discovery_flag(checks, key) for key in TALK_KEYS)
    any_next = any(discovery_flag(checks, key) for key in NEXT_KEYS)
    time_ok = discovery_flag(checks, 'time_has') or discovery_flag(checks, 'time_soon')

    if need_task and time_ok and any_next:
        return HINT_HOT_READY
    if need_task and any_talk and not any_next:
        return HINT_WARM
    if not any_talk and not need_task:
        return HINT_COLD
    return HINT_THIN


def discovery_hint_text(checks: Mapping | None) -> str:
    return HINT_TEXTS[discovery_hint_key(checks)]
