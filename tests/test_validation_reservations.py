"""Default per-key reservations, overrides and the grant environment (bh-9oc32)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from beadhive import validation_ledger
from beadhive.validation_reservations import (
    DEFAULT_EXPECTED_SECONDS,
    DEFAULT_RESERVATIONS,
    FALLBACK,
    expected_seconds,
    grant_environ,
    resolve_reservation,
)
from beadhive_validation_broker import Capacity, Grant

ROOT = Path(__file__).parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_attest_catalog", ROOT / "scripts" / "check_attest_catalog.py"
)
assert _SPEC and _SPEC.loader
_CATALOG = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CATALOG)
MIB = 1024 * 1024


def _cfg(key: str, resources: dict | None) -> dict:
    entry = {"name": key, "cmd": f"just attest-{key}"}
    if resources is not None:
        entry["resources"] = resources
    return {"work": {"attest": {"keys": [entry]}}}


def _grant(cpu: float = 6, pools: dict | None = None) -> Grant:
    return Grant("lease-1", "job-1", Capacity(cpu, 512 * MIB, pools or {"dolt-servers": 2}))


@pytest.mark.parametrize("key", sorted(_CATALOG.KEY_RECIPES))
def test_every_live_catalog_key_resolves_a_reservation(key: str) -> None:
    reservation = resolve_reservation(key)

    assert key in DEFAULT_RESERVATIONS
    assert 0 < reservation.cpu_min <= reservation.cpu_max
    assert reservation.memory_bytes > 0


def test_measured_profiles_match_the_adr_table() -> None:
    stateful = resolve_reservation("stateful")
    assert (stateful.cpu_min, stateful.cpu_max) == (8, 16)
    assert stateful.memory_bytes == 8704 * MIB
    assert dict(stateful.pools) == {"dolt-servers": 2}
    assert dict(resolve_reservation("integration").pools) == {"dolt-servers": 4}
    assert dict(resolve_reservation("bd-cli").pools) == {"dolt-servers": 10}
    assert dict(resolve_reservation("docs").pools) == {}
    assert DEFAULT_EXPECTED_SECONDS["stateful"] == 332.0


def test_an_unknown_key_resolves_the_fallback() -> None:
    assert resolve_reservation("brand-new-key") is FALLBACK
    assert expected_seconds("brand-new-key") > 0


def test_an_override_replaces_the_default_wholesale() -> None:
    cfg = _cfg("stateful", {"cpu_min": 2, "cpu_max": 4, "memory_mb": 100})

    reservation = resolve_reservation("stateful", cfg)

    assert (reservation.cpu_min, reservation.cpu_max) == (2, 4)
    assert reservation.memory_bytes == 100 * MIB
    assert dict(reservation.pools) == {}  # replaced, not merged with the default pool
    assert expected_seconds("stateful", cfg) == 332.0


def test_an_override_can_set_expected_wall_and_pools() -> None:
    cfg = _cfg(
        "docs",
        {"cpu_min": 1, "cpu_max": 1, "memory_mb": 64, "pools": {"x": 1}, "expected_s": 5},
    )

    assert dict(resolve_reservation("docs", cfg).pools) == {"x": 1}
    assert expected_seconds("docs", cfg) == 5.0


def test_absent_resources_use_the_default() -> None:
    assert (
        resolve_reservation("stateful", _cfg("stateful", None)) == DEFAULT_RESERVATIONS["stateful"]
    )


def test_override_for_another_key_leaves_this_key_default() -> None:
    cfg = _cfg("docs", {"cpu_min": 1, "cpu_max": 1, "memory_mb": 64})

    assert resolve_reservation("unit", cfg) == DEFAULT_RESERVATIONS["unit"]


def test_an_inverted_override_is_rejected_by_the_contract() -> None:
    from beadhive.modules.config.contracts import AttestKeyConfig

    with pytest.raises(ValueError, match="cpu_min <= cpu_max"):
        AttestKeyConfig(name="x", cmd="c", resources={"cpu_min": 4, "cpu_max": 2, "memory_mb": 1})


def test_grant_environ_is_clean_and_allow_listed() -> None:
    base = {
        "PATH": "/bin",
        "HOME": "/h",
        "VIRTUAL_ENV": "/venv",
        "GITHUB_TOKEN": "secret",
        "UV_CACHE_DIR": "/c",
        "BH_GRANT_CPUS": "99",
        "BH_VALIDATION_LEASE": "stale",
    }

    env = grant_environ(_grant(6), base)

    assert env["PATH"] == "/bin" and env["UV_CACHE_DIR"] == "/c"
    assert "VIRTUAL_ENV" not in env and "GITHUB_TOKEN" not in env
    assert env["BH_GRANT_CPUS"] == "6"
    assert env["BH_VALIDATION_LEASE"] == "lease-1"
    assert env["BH_GRANT_DOLT_SERVERS"] == "2"


def test_the_grant_never_changes_cmd_hash() -> None:
    cmd = "just attest-stateful"
    before = validation_ledger.cmd_hash(cmd)

    env = grant_environ(_grant(4), {"PATH": "/bin"})

    assert env["BH_GRANT_CPUS"] == "4"
    assert validation_ledger.cmd_hash(cmd) == before
    assert validation_ledger.cmd_hash(cmd) == validation_ledger.cmd_hash(cmd)
    # the command text carries no grant: the recipe reads BH_GRANT_CPUS from its own shell
    assert "BH_GRANT" not in cmd


def _expanded_recipe(recipe: str) -> str:
    """The recipe as just would run it (variables expanded, nothing executed)."""
    result = subprocess.run(
        ["just", "--justfile", str(ROOT / "justfile"), "--dry-run", recipe],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        env={"PATH": os.environ["PATH"], "HOME": "/tmp"},
    )
    return result.stderr


def _shell_expand(expression: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["sh", "-c", f'printf %s "{expression}"'],
        check=True,
        text=True,
        capture_output=True,
        env={"PATH": os.environ["PATH"], **env},
    )
    return result.stdout


@pytest.mark.parametrize(
    ("recipe", "default"),
    [
        ("stateful-native", "16"),
        ("root-composition-native", "16"),
        ("test-integration-land", "16"),
        ("bd-cli-check", "auto"),
        ("packages-check", "auto"),
    ],
)
def test_recipes_honour_the_grant_and_fall_back_to_their_current_count(
    recipe: str, default: str
) -> None:
    expression = "${BH_GRANT_CPUS:-" + default + "}"

    assert f"-n {expression}" in _expanded_recipe(recipe)
    assert _shell_expand(expression, {}) == default  # unset: exactly today's worker count
    assert _shell_expand(expression, {"BH_GRANT_CPUS": ""}) == default
    assert _shell_expand(expression, {"BH_GRANT_CPUS": "6"}) == "6"
