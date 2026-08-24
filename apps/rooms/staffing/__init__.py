"""Подбор команды в комнату (staffing).

Публичный API модуля:

* `matching` — read-only Matching Engine (hard filters + ranking);
* `services` — транзакционные операции над слотами и готовностью команды;
* `selectors` — read-only сборка карточек слотов для UI.

Единственный источник правил подбора — `matching.py`; во views и шаблонах
фильтры не дублируются.
"""

from .matching import (
    CANDIDATE_ORDERING,
    CHANNEL_REQUIREMENTS,
    get_best_candidate,
    get_next_candidate,
    get_ranked_candidates,
)
from .selectors import SlotCard, slot_card_for, slot_cards, staffing_summary
from .services import (
    STAFFING_MUTABLE_STATUSES,
    StaffingError,
    StaffingOutcome,
    assign_candidate_to_slot,
    auto_assign_best_candidate,
    confirm_freelancer_readiness,
    is_functional_team_ready,
    replace_slot_member,
    sync_project_activation,
)

__all__ = [
    'CANDIDATE_ORDERING',
    'CHANNEL_REQUIREMENTS',
    'STAFFING_MUTABLE_STATUSES',
    'SlotCard',
    'StaffingError',
    'StaffingOutcome',
    'assign_candidate_to_slot',
    'auto_assign_best_candidate',
    'confirm_freelancer_readiness',
    'get_best_candidate',
    'get_next_candidate',
    'get_ranked_candidates',
    'is_functional_team_ready',
    'replace_slot_member',
    'slot_card_for',
    'slot_cards',
    'staffing_summary',
    'sync_project_activation',
]
