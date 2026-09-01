"""Compatibility facade for the provider-neutral agents domain."""

from .modules.agents.domain.seat import SeatContract, seat_contract

__all__ = ["SeatContract", "seat_contract"]
