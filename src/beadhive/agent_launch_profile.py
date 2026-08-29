"""Harness-agnostic agent launch profiles.

This module is the core contract for a requested agent launch.  Presentation hosts may extend
``AgentLaunchProfile`` with their own targeting fields, but core validation and command
construction deliberately know nothing about those hosts.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints, model_validator

ProfileVersion = Literal["1"]
NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
# ``hive_repair._PREFIX_RE`` owns the configured hive-prefix shape.  bd appends a lowercase
# alphanumeric issue token, then dot-separated lowercase alphanumeric tokens for child beads.
# Keep this identity exact: unlike display text it must not be whitespace-normalized.
BEAD_ID_PATTERN = r"^[a-z][a-z0-9-]*-[a-z0-9]+(?:\.[a-z0-9]+)*$"
BeadId = Annotated[str, StringConstraints(pattern=BEAD_ID_PATTERN)]


class BeadPolicy(StrEnum):
    """Whether a seat may be bound to an exact bead identity."""

    REQUIRED = "required"
    OPTIONAL = "optional"
    FORBIDDEN = "forbidden"


class Harness(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    OPENCODE = "opencode"


REQUIRED_BEAD_SEATS = frozenset({"developer", "dispatcher", "reviewer", "merger"})
OPTIONAL_BEAD_SEATS = frozenset({"planner", "analyst", "warden"})
FORBIDDEN_BEAD_SEATS = frozenset({"supervisor", "director", "custodian", "controller"})
KNOWN_SEATS = REQUIRED_BEAD_SEATS | OPTIONAL_BEAD_SEATS | FORBIDDEN_BEAD_SEATS


def bead_policy_for_seat(seat: str) -> BeadPolicy:
    """Return the declared bead policy for *seat*, refusing unknown capabilities."""

    if seat in REQUIRED_BEAD_SEATS:
        return BeadPolicy.REQUIRED
    if seat in OPTIONAL_BEAD_SEATS:
        return BeadPolicy.OPTIONAL
    if seat in FORBIDDEN_BEAD_SEATS:
        return BeadPolicy.FORBIDDEN
    raise ValueError(f"unknown seat {seat!r}")


class AgentLaunchProfile(BaseModel):
    """Version 1 request for launching one named Beadhive seat."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    version: ProfileVersion = "1"
    managed_bead: StrictBool
    bead: BeadId | None = None
    initial_seat: NonEmptyString
    available_seats: frozenset[NonEmptyString] | None = None
    harness: Harness
    model: NonEmptyString | None = None
    effort: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_policy(self) -> AgentLaunchProfile:
        available = self.available_seats
        if available is None:
            object.__setattr__(self, "available_seats", frozenset({self.initial_seat}))
            available = self.available_seats
        if self.initial_seat not in available:
            raise ValueError("initial_seat must be present in available_seats")
        unknown = set(available) - KNOWN_SEATS
        if unknown:
            raise ValueError(f"unknown available seat(s): {', '.join(sorted(unknown))}")

        policy = bead_policy_for_seat(self.initial_seat)
        if policy is BeadPolicy.REQUIRED and not self.managed_bead:
            raise ValueError(f"seat {self.initial_seat!r} requires a managed bead")
        if policy is BeadPolicy.FORBIDDEN and self.managed_bead:
            raise ValueError(f"seat {self.initial_seat!r} forbids a managed bead")
        if self.managed_bead and self.bead is None:
            raise ValueError("bead is required when managed_bead is true")
        if not self.managed_bead and self.bead is not None:
            raise ValueError("bead is forbidden when managed_bead is false")

        # A permitted seat switch must not silently change the launch's bead-binding contract.
        for seat in available:
            seat_policy = bead_policy_for_seat(seat)
            if seat_policy is BeadPolicy.REQUIRED and not self.managed_bead:
                raise ValueError(f"available seat {seat!r} requires a managed bead")
            if seat_policy is BeadPolicy.FORBIDDEN and self.managed_bead:
                raise ValueError(f"available seat {seat!r} forbids a managed bead")
        return self


class ResolvedAgentLaunchProfile(BaseModel):
    """Validated effective profile, including a safe executable argv."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    version: ProfileVersion = "1"
    managed_bead: bool
    bead: str | None
    initial_seat: str
    current_seat: str
    available_seats: frozenset[str]
    bead_policy: BeadPolicy
    harness: Harness
    model: str | None
    effort: str | None
    argv: tuple[str, ...]


class HarnessArgvAdapter:
    """Allowlisted translation from normalized launch choices to an executable argv."""

    harness: ClassVar[Harness]
    efforts: ClassVar[frozenset[str]] = frozenset()

    def build(self, *, seat: str, model: str | None, effort: str | None) -> tuple[str, ...]:
        raise NotImplementedError

    def normalize(self, value: str | None, *, field: str) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or normalized.startswith("-") or any(c in normalized for c in "\0\r\n"):
            raise ValueError(f"invalid {field}")
        return normalized

    def normalize_effort(self, effort: str | None) -> str | None:
        normalized = self.normalize(effort, field="effort")
        if normalized is None:
            return None
        normalized = normalized.lower()
        if normalized not in self.efforts:
            raise ValueError(f"effort {normalized!r} is not supported by {self.harness.value}")
        return normalized


class CodexArgvAdapter(HarnessArgvAdapter):
    harness = Harness.CODEX
    efforts = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})

    def build(self, *, seat: str, model: str | None, effort: str | None) -> tuple[str, ...]:
        argv = ["codex"]
        if model:
            argv.extend(("--model", model))
        if effort:
            argv.extend(("--config", f'model_reasoning_effort="{effort}"'))
        return tuple(argv)


class ClaudeArgvAdapter(HarnessArgvAdapter):
    harness = Harness.CLAUDE
    efforts = frozenset({"low", "medium", "high"})

    def build(self, *, seat: str, model: str | None, effort: str | None) -> tuple[str, ...]:
        argv = ["claude", "--agent", seat]
        if model:
            argv.extend(("--model", model))
        if effort:
            argv.extend(("--effort", effort))
        return tuple(argv)


class OpenCodeArgvAdapter(HarnessArgvAdapter):
    harness = Harness.OPENCODE

    def build(self, *, seat: str, model: str | None, effort: str | None) -> tuple[str, ...]:
        argv = ["opencode", "--agent", seat]
        if model:
            argv.extend(("--model", model))
        return tuple(argv)


HARNESS_ADAPTERS: dict[Harness, HarnessArgvAdapter] = {
    adapter.harness: adapter
    for adapter in (CodexArgvAdapter(), ClaudeArgvAdapter(), OpenCodeArgvAdapter())
}


def resolve_agent_launch_profile(
    profile: AgentLaunchProfile, *, current_seat: str | None = None
) -> ResolvedAgentLaunchProfile:
    """Authorize a seat selection and resolve normalized, allowlisted harness arguments."""

    seat = current_seat.strip() if current_seat is not None else profile.initial_seat
    if seat not in profile.available_seats:
        raise ValueError(f"seat {seat!r} is not authorized by available_seats")
    adapter = HARNESS_ADAPTERS[Harness(profile.harness)]
    model = adapter.normalize(profile.model, field="model")
    effort = adapter.normalize_effort(profile.effort)
    return ResolvedAgentLaunchProfile(
        managed_bead=profile.managed_bead,
        bead=profile.bead,
        initial_seat=profile.initial_seat,
        current_seat=seat,
        available_seats=profile.available_seats,
        bead_policy=bead_policy_for_seat(seat),
        harness=profile.harness,
        model=model,
        effort=effort,
        argv=adapter.build(seat=seat, model=model, effort=effort),
    )
