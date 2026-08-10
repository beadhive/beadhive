"""dispatch_caps — the pure decision core for the in-process concurrency + wall-time caps
(bh-e7r9q.3). Pure unit tests: no I/O, no subprocess, no `bd`/config plumbing — plain ints and
floats in, a `CapDecision` out.

Covers the acceptance bar directly: under cap, at cap, cap unset/non-positive reads as
unlimited, a wall-time breach, deny reasons are never silent, and (module-shape assertions)
nothing here persists to disk or denominates anything in currency.
"""

from __future__ import annotations

import inspect

from beadhive import dispatch_caps
from beadhive.dispatch_caps import (
    REASON_OK,
    REASON_SEATS_AT_CAP,
    REASON_WALL_TIME_EXCEEDED,
    Caps,
    check_admission,
    check_wall_time,
)

# ---- concurrency cap: check_admission -----------------------------------------


def test_under_cap_allows():
    caps = Caps(max_seats_in_flight=3)
    decision = check_admission(caps, in_flight=1)
    assert decision.allowed is True
    assert decision.reason == REASON_OK


def test_at_cap_denies():
    caps = Caps(max_seats_in_flight=3)
    decision = check_admission(caps, in_flight=3)
    assert decision.allowed is False
    assert decision.reason == REASON_SEATS_AT_CAP


def test_over_cap_denies():
    caps = Caps(max_seats_in_flight=3)
    decision = check_admission(caps, in_flight=4)
    assert decision.allowed is False
    assert decision.reason == REASON_SEATS_AT_CAP


def test_cap_zero_is_unlimited():
    caps = Caps(max_seats_in_flight=0)
    decision = check_admission(caps, in_flight=10_000)
    assert decision.allowed is True
    assert decision.reason == REASON_OK


def test_cap_negative_is_unlimited():
    caps = Caps(max_seats_in_flight=-1)
    decision = check_admission(caps, in_flight=10_000)
    assert decision.allowed is True
    assert decision.reason == REASON_OK


# ---- wall-time cap: check_wall_time --------------------------------------------


def test_wall_time_under_cap_allows():
    caps = Caps(max_run_wall_time_seconds=600)
    decision = check_wall_time(caps, elapsed_seconds=100.0)
    assert decision.allowed is True
    assert decision.reason == REASON_OK


def test_wall_time_breach_denies():
    caps = Caps(max_run_wall_time_seconds=600)
    decision = check_wall_time(caps, elapsed_seconds=600.0)
    assert decision.allowed is False
    assert decision.reason == REASON_WALL_TIME_EXCEEDED


def test_wall_time_breach_past_cap_denies():
    caps = Caps(max_run_wall_time_seconds=600)
    decision = check_wall_time(caps, elapsed_seconds=900.5)
    assert decision.allowed is False
    assert decision.reason == REASON_WALL_TIME_EXCEEDED


def test_wall_time_cap_zero_is_unlimited():
    caps = Caps(max_run_wall_time_seconds=0)
    decision = check_wall_time(caps, elapsed_seconds=1_000_000.0)
    assert decision.allowed is True
    assert decision.reason == REASON_OK


def test_wall_time_cap_negative_is_unlimited():
    caps = Caps(max_run_wall_time_seconds=-5)
    decision = check_wall_time(caps, elapsed_seconds=1_000_000.0)
    assert decision.allowed is True
    assert decision.reason == REASON_OK


# ---- deny reasons must surface, never silently -------------------------------


def test_seats_denial_is_not_silent():
    """bh-h2yc: a denial is a visible, machine-readable outcome, never a quiet no-op — the
    reason is a non-empty, stable code AND the detail is a non-empty human-readable string."""
    decision = check_admission(Caps(max_seats_in_flight=1), in_flight=1)
    assert decision.allowed is False
    assert decision.reason and decision.reason != REASON_OK
    assert decision.detail
    assert "1" in decision.detail  # names the actual counts, not a generic message


def test_wall_time_denial_is_not_silent():
    decision = check_wall_time(Caps(max_run_wall_time_seconds=30), elapsed_seconds=31.0)
    assert decision.allowed is False
    assert decision.reason and decision.reason != REASON_OK
    assert decision.detail
    assert "ladder" in decision.detail.lower()


def test_allow_also_carries_an_explicit_reason():
    """Allow is an explicit outcome too — never an implicit "no reason given"."""
    decision = check_admission(Caps(max_seats_in_flight=5), in_flight=0)
    assert decision.reason == REASON_OK
    assert decision.detail


# ---- nothing persists to disk, nothing denominated in currency ----------------


def test_caps_and_decision_are_frozen_plain_dataclasses():
    """No hidden mutable state to accidentally serialize/persist."""
    caps = Caps(max_seats_in_flight=1, max_run_wall_time_seconds=1)
    decision = check_admission(caps, in_flight=0)
    for obj in (caps, decision):
        try:
            obj.__dict__["x"] = 1  # frozen dataclasses raise on attribute assignment instead
        except Exception:
            pass
    import dataclasses

    assert dataclasses.is_dataclass(caps) and caps.__dataclass_params__.frozen
    assert dataclasses.is_dataclass(decision) and decision.__dataclass_params__.frozen


def test_module_imports_nothing_but_dataclasses():
    """No file/socket/subprocess/`bd` API is importable from this module — walk the actual
    `import` statements via `ast` rather than grepping prose (the docstring legitimately
    *talks about* subprocess/I/O while promising not to use it)."""
    import ast

    tree = ast.parse(inspect.getsource(dispatch_caps))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {"__future__", "dataclasses"}, imported


def test_decisions_never_denominate_in_currency():
    """Runtime output (the reason codes + the human-readable details a denial surfaces) never
    denominates anything in currency — this is the concurrency/wall-time caps module, not the
    budget governor (bh-3yoh owns that)."""
    banned = ("usd", "$", "cost_usd", "dollar")
    decisions = [
        check_admission(Caps(max_seats_in_flight=1), in_flight=0),
        check_admission(Caps(max_seats_in_flight=1), in_flight=1),
        check_wall_time(Caps(max_run_wall_time_seconds=10), elapsed_seconds=1.0),
        check_wall_time(Caps(max_run_wall_time_seconds=10), elapsed_seconds=11.0),
    ]
    for decision in decisions:
        text = f"{decision.reason} {decision.detail}".lower()
        for token in banned:
            assert token not in text, f"unexpected currency token {token!r} in {decision!r}"


def test_module_has_no_module_level_state_to_persist():
    """No module-level mutable container (a would-be in-memory cache someone could later wire
    to disk) — every public name is a pure function, a frozen dataclass, or a str constant."""
    import types

    for name, value in vars(dispatch_caps).items():
        if name.startswith("_") or isinstance(value, types.ModuleType):
            continue
        assert not isinstance(value, (list, dict, set)), (
            f"{name} is a mutable module-level container — dispatch_caps must hold no state"
        )
