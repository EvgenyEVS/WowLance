"""Подбор команды в комнату (staffing).

Публичный API модуля:

* `matching` — read-only Matching Engine (hard filters + ranking);
* `projection` — приведение слотов комнаты к купленному составу проекта;
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
from .projection import (
    PROJECTED_ROLE_KEYS,
    SlotProjectionError,
    SlotProjectionResult,
    is_projected_role,
    sync_functional_roles_to_slots,
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
    'PROJECTED_ROLE_KEYS',
    'STAFFING_MUTABLE_STATUSES',
    'SlotCard',
    'SlotProjectionError',
    'SlotProjectionResult',
    'StaffingError',
    'StaffingOutcome',
    'assign_candidate_to_slot',
    'auto_assign_best_candidate',
    'confirm_freelancer_readiness',
    'get_best_candidate',
    'get_next_candidate',
    'get_ranked_candidates',
    'is_functional_team_ready',
    'is_projected_role',
    'replace_slot_member',
    'slot_card_for',
    'slot_cards',
    'staffing_summary',
    'sync_functional_roles_to_slots',
    'sync_project_activation',
]
