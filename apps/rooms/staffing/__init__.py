"""Подбор команды в комнату (staffing).

Публичный API этапа — read-only Matching Engine из `matching.py`.
"""

from .matching import (
    CANDIDATE_ORDERING,
    CHANNEL_REQUIREMENTS,
    get_best_candidate,
    get_next_candidate,
    get_ranked_candidates,
)

__all__ = [
    'CANDIDATE_ORDERING',
    'CHANNEL_REQUIREMENTS',
    'get_best_candidate',
    'get_next_candidate',
    'get_ranked_candidates',
]
