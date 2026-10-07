"""Signed authority without expiry by default (bh-y929l, epic bh-taa04).

* the sentinel is finite and round-trips through canonical JSON, the Git ``authority.json`` /
  ``config.json`` encoding, float64/DOUBLE, BIGINT and ``datetime``;
* every operator mutation without ``--duration`` yields (or keeps) the sentinel on both backends;
* a 0.23.1-shaped verifier accepts a 0.24.0 record unchanged (frozen fixture: the verbatim
  v0.23.1 ``hq_authority_guard.py`` plus the v0.23.1 SQL ``verified_state_at`` checks);
* an explicit ``--duration`` still signs an expiring authority that fences after expiry.

The Dolt round trip and the SQL frame at t+400 d live in ``test_hq_authority_no_expiry_int.py``.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import re
import struct
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.test_hq_authority_backend import _prepared_hqs, apply, backend  # noqa: F401
from tests.test_hq_operator_expiry_preserved import (
    NOW,
    PATHS,
    WEEK,
    Operator,
    _drive,
    _plane,
    _record,
    _state,
)

from beadhive import gitref
from beadhive import hq_authority_ceiling as c
from beadhive import hq_authority_guard as guard
from beadhive.hq_authority_ceiling import AUTHORITY_NO_EXPIRY
from beadhive.hq_control_plane import ControlPlaneError
from beadhive.hq_sql_signatures import canonical

DAY = 86400
FIXTURE = Path(__file__).parent / "fixtures" / "authority_v0_23_1" / "hq_authority_guard.py.txt"


# --- the sentinel -------------------------------------------------------------------------


def test_sentinel_is_finite_far_future_and_fits_every_store():
    assert type(AUTHORITY_NO_EXPIRY) is int and math.isfinite(AUTHORITY_NO_EXPIRY)
    assert datetime.fromtimestamp(AUTHORITY_NO_EXPIRY, UTC) == datetime(2100, 1, 1, tzinfo=UTC)
    # Far beyond any realistic clock, yet a remaining-seconds count fits 32-bit unsigned.
    assert AUTHORITY_NO_EXPIRY - time.time() > 70 * 365 * DAY
    assert AUTHORITY_NO_EXPIRY - time.time() < 2**32
    # Exact in a float64 (Dolt DOUBLE, JSON readers) and in a signed BIGINT.
    assert float(AUTHORITY_NO_EXPIRY) == AUTHORITY_NO_EXPIRY
    assert struct.unpack("<d", struct.pack("<d", AUTHORITY_NO_EXPIRY))[0] == AUTHORITY_NO_EXPIRY
    assert AUTHORITY_NO_EXPIRY < 2**63
    # It is not a MySQL/Dolt TIMESTAMP (2038-01-19 cap) value: no authority column is one.
    assert AUTHORITY_NO_EXPIRY > 2**31


def test_sentinel_round_trips_canonical_json_and_git_encoding():
    state = {**_state(AUTHORITY_NO_EXPIRY, _record()), "issued_at": NOW}
    sql_body = canonical(state, limit=4 * 1024 * 1024)
    assert json.loads(sql_body) == state
    assert canonical(json.loads(sql_body), limit=4 * 1024 * 1024) == sql_body
    assert b"Infinity" not in (sql_body if isinstance(sql_body, bytes) else sql_body.encode())
    git_body = gitref.encode(state)
    assert json.loads(git_body)["expires_at"] == AUTHORITY_NO_EXPIRY
    assert gitref.encode(json.loads(git_body)) == git_body
    guard.validate_state(json.loads(git_body))


def test_signed_expiry_helper():
    assert c.signed_expiry(NOW) == AUTHORITY_NO_EXPIRY
    assert c.signed_expiry(NOW, 7 * DAY) == NOW + 7 * DAY
    assert c.signed_expiry(NOW, 3650 * DAY) == NOW + 3650 * DAY  # default ceiling: unlimited
    assert c.signed_expiry(NOW, 10**12) == AUTHORITY_NO_EXPIRY  # never past the sentinel
    with pytest.raises(ValueError, match="exceeds ceiling"):
        c.signed_expiry(NOW, 8 * DAY, c.resolve_ceiling(cli="7d"))
    with pytest.raises(ValueError):
        c.signed_expiry(NOW, 0)
    assert c.non_expiring(AUTHORITY_NO_EXPIRY) and not c.non_expiring(NOW + WEEK)
    # The (unchanged) operator-signed helper keeps a non-expiring record non-expiring.
    assert guard.operator_signed_expiry({"expires_at": AUTHORITY_NO_EXPIRY}, NOW) == (
        AUTHORITY_NO_EXPIRY
    )


# --- SQL operator mutations (fake operator, simulated clock) ------------------------------


@pytest.mark.parametrize("path", PATHS)
def test_sql_operator_mutation_keeps_no_expiry(path):
    before, after = _drive(path, AUTHORITY_NO_EXPIRY)
    assert after["expires_at"] == AUTHORITY_NO_EXPIRY
    assert after["revision"] == before["revision"] + 1
    guard.validate_state(after)


def test_sql_renew_without_duration_signs_no_expiry(monkeypatch):
    monkeypatch.setenv(c.ENV_VAR, "7d")  # a ceiling bounds explicit durations only
    operator = Operator(_state(NOW + WEEK, _record()))
    _plane(operator).renew(expected="head", operator_key="key")
    assert operator.published[-1]["expires_at"] == AUTHORITY_NO_EXPIRY
    _plane(operator).renew(expected="head", operator_key="key", duration=DAY)
    assert operator.published[-1]["expires_at"] == NOW + DAY
    with pytest.raises(ControlPlaneError, match="exceeds ceiling"):
        _plane(operator).renew(expected="head", operator_key="key", duration=8 * DAY)


# --- 0.23.1-shaped verifier compatibility -------------------------------------------------


@pytest.fixture(scope="module")
def guard_v0_23_1():
    """The verbatim v0.23.1 standalone guard, loaded as an independent module."""
    from beadhive import beadyard_identity

    spec = importlib.util.spec_from_loader("hq_authority_guard_v0_23_1", loader=None)
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("beadyard_identity")
    sys.modules["beadyard_identity"] = beadyard_identity  # the guard's pinned library
    try:
        exec(compile(FIXTURE.read_text(), str(FIXTURE), "exec"), module.__dict__)  # noqa: S102
        yield module
    finally:
        if previous is None:
            sys.modules.pop("beadyard_identity", None)
        else:
            sys.modules["beadyard_identity"] = previous


def _validate_sql_hive_policies_v0_23_1(policies, *, config_head, now, require_fresh=True):
    """Verbatim v0.23.1 ``hq_hive_policy.validate_sql_hive_policies`` (requires elided)."""
    if (
        not isinstance(config_head, str)
        or not re.fullmatch(r"[0-9a-v]{32}", config_head)
        or type(now) not in (int, float)
        or not math.isfinite(now)
        or not isinstance(policies, dict)
    ):
        raise ValueError("invalid SQL hive policy provenance")
    for prefix, item in policies.items():
        if (
            not isinstance(prefix, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]*", prefix)
            or not isinstance(item, dict)
            or set(item)
            != {"config_revision", "config_head", "valid_until", "requires", "evict_after_s"}
            or not isinstance(item["config_revision"], str)
            or not item["config_revision"]
            or item["config_head"] != config_head
            or type(item["valid_until"]) not in (int, float)
            or not math.isfinite(item["valid_until"])
            or require_fresh
            and now >= item["valid_until"]
            or type(item["evict_after_s"]) not in (int, float)
            or not math.isfinite(item["evict_after_s"])
            or item["evict_after_s"] <= 0
            or not isinstance(item["requires"], dict)
        ):
            raise ValueError("invalid canonical hive policy projection")


def _verified_state_at_v0_23_1(old_guard, state, policies, *, config_head, generation, now):
    """The v0.23.1 ``SqlRuntimeAuthority.verified_state_at`` checks after signature/floor."""
    old_guard.validate_state(state)
    _validate_sql_hive_policies_v0_23_1(
        policies, config_head=config_head, now=now, require_fresh=False
    )
    if any(policy["valid_until"] > state["expires_at"] for policy in policies.values()):
        raise ValueError("protected hive policy exceeds signed authority expiry")
    if (
        state["generation"] != generation
        or now < state["issued_at"] - 30
        or now >= state["expires_at"]
    ):
        raise ValueError("protected HQ runtime authority expired or inconsistent")
    return state


def _record_v024():
    """A 0.24.0 operator record: renewed without --duration, then cordoned (simulated)."""
    operator = Operator(_state(NOW + WEEK, _record()))
    _plane(operator).renew(expected="head", operator_key="key")
    operator.state = copy.deepcopy(operator.published[-1])
    operator.published.clear()
    operator.state["frames"]["frame"]["active"]["cordoned"] = True
    return operator.state


def test_v0_23_1_fixture_is_the_released_guard(guard_v0_23_1):
    assert guard_v0_23_1.DOMAIN == guard.DOMAIN and guard_v0_23_1.DOMAIN_V2 == guard.DOMAIN_V2
    assert "def validate_state(state):" in FIXTURE.read_text()


def test_v0_23_1_verifier_accepts_v0_24_0_record_unchanged(guard_v0_23_1):
    state = _record_v024()
    assert state["expires_at"] == AUTHORITY_NO_EXPIRY
    body = json.loads(canonical(state, limit=4 * 1024 * 1024))
    config_head = "0" * 32
    policies = {
        "bh": {
            "config_revision": "desired-1",
            "config_head": config_head,
            "valid_until": AUTHORITY_NO_EXPIRY,
            "requires": {},
            "evict_after_s": 900,
        }
    }
    guard_v0_23_1.validate_state(body)
    guard_v0_23_1.validate_hive_policies({"bh": {**policies["bh"], "config_head": ""}})
    for days in (0, 30, 400):
        _verified_state_at_v0_23_1(
            guard_v0_23_1,
            body,
            policies,
            config_head=config_head,
            generation="g",
            now=NOW + days * DAY,
        )
    # The 0.23.1 Git server guard's expiry check also passes (``expires_at <= time.time()``).
    assert not body["expires_at"] <= time.time()


def test_v0_23_1_verifier_still_rejects_what_it_always_rejected(guard_v0_23_1):
    state = _record_v024()
    for bad in (math.inf, float("nan"), state["issued_at"]):
        with pytest.raises(ValueError):
            guard_v0_23_1.validate_state({**state, "expires_at": bad})
    with pytest.raises(ValueError):
        guard_v0_23_1.validate_state({**state, "never_expires": True})


# --- Git backend --------------------------------------------------------------------------


def test_git_operator_mutations_without_duration_keep_no_expiry(backend, monkeypatch):  # noqa: F811
    monkeypatch.setenv(c.ENV_VAR, "7d")  # a ceiling bounds explicit durations only
    b = backend
    plane, key = b["plane"], str(b["operator"])
    head = plane.renew(expected=plane._read()[0], operator_key=key)
    _, state, _ = plane._read()
    assert state["expires_at"] == AUTHORITY_NO_EXPIRY
    for sequence in (1, 2, 3):  # operator-signed observations
        b["accept"](sequence)
    sha, state, _ = plane._read()
    assert sha != head and state["expires_at"] == AUTHORITY_NO_EXPIRY
    assert apply(b, "admit")["state"] == "active"  # operator-signed lifecycle
    sha, state, _ = plane._read()
    assert state["expires_at"] == AUTHORITY_NO_EXPIRY
    # Frame view: still valid at t+400 d with no operator action.
    real = plane.clock
    plane.clock = lambda: real() + 400 * DAY
    try:
        assert plane._read()[0] == sha
        assert plane.watch_state("frame-one") is not None
    finally:
        plane.clock = real
    # Opt-in expiry: capped by the ceiling, fenced after expiry.
    with pytest.raises(ControlPlaneError, match="exceeds ceiling"):
        plane.renew(expected=sha, operator_key=key, duration=8 * DAY)
    plane.renew(expected=sha, operator_key=key, duration=DAY)
    _, state, _ = plane._read()
    assert state["expires_at"] - state["issued_at"] == DAY
    plane.clock = lambda: state["expires_at"] + 1
    try:
        with pytest.raises(ControlPlaneError, match="expired"):
            plane._read()
    finally:
        plane.clock = real


def test_git_fleet_config_without_duration_never_expires(backend):  # noqa: F811
    from beadhive.modules.config.domain.ports import FleetConfigDocument

    b = backend
    documents = (FleetConfigDocument("fleet.yaml", "schema_version: 1\nmanaged_repos: []\n"),)
    first = (
        b["plane"]
        .config_store(operator_key=str(b["operator"]))
        .publish_snapshot(documents, expected_revision="")
    )
    assert first.valid_until == AUTHORITY_NO_EXPIRY
    _, state, _ = b["plane"].config_store()._read()
    state = json.loads(gitref.encode(state))
    assert state["expires_at"] == AUTHORITY_NO_EXPIRY
    guard.validate_config_state(state)
    second = (
        b["plane"]
        .config_store(operator_key=str(b["operator"]), duration=DAY)
        .publish_snapshot(documents, expected_revision=first.commit_revision)
    )
    assert second.valid_until == pytest.approx(time.time() + DAY, abs=120)
