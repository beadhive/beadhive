"""Compatibility facade for the state capability's activity projection contract."""

from .modules.state.domain.activity import (
    SEAT_CANCELLED_STATE,
    AgentRunState,
    AgentRunSummary,
    Freshness,
    state_for_seat_harvested,
)

__all__ = [
    "AgentRunState",
    "AgentRunSummary",
    "Freshness",
    "SEAT_CANCELLED_STATE",
    "state_for_seat_harvested",
]
