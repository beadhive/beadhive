"""deps.py — the characterization proof that ONE table reproduces the seven registries.

Every assertion here compares a DERIVATION over `deps.DEPS` against **the literal value as of
bh-hsus.2**, recorded inline, *and* against the live registry it replaces. Both directions on
purpose: pinning only the live module would go tautological the moment that module becomes a
derivation (bh-hsus.3), and pinning only the recorded literal would not notice a registry
drifting away underneath. Together they fail whichever side moves.

`harness_auth` lives on `wt/bead/epic/bh-q160` and main has never seen it, so the credential
derivation is pinned to the recorded literal and additionally compared to the live registry
**only when it is importable** — the guard arms itself the moment that epic merges.
"""

from __future__ import annotations

import inspect
import shutil
import typing

import pytest

from beadhive import config, config_schema, deps, harness, hitch_plugin, role
from beadhive import setup as setup_mod

# ---- the literals as of bh-hsus.2, recorded so a drift on EITHER side fails ------

PROBE_TABLE_AS_OF_HSUS = [
    ("git-workspace", "git-workspace", ["git", "workspace", "--version"]),
    ("gh", "gh", ["gh", "--version"]),
    ("bd", "bd", ["bd", "--version"]),
    ("dolt", "dolt", ["dolt", "version"]),
]

RUNTIME_PROBES_AS_OF_HSUS = {
    "colima": ("colima", "colima", ["colima", "--version"]),
    "docker": ("docker", "docker", ["docker", "--version"]),
    "podman": ("podman", "podman", ["podman", "--version"]),
}

HARNESSES_AS_OF_HSUS = ["claude", "codex"]
KNOWN_HARNESSES_AS_OF_HSUS = ("claude", "opencode")
CREDENTIAL_PROBES_AS_OF_HSUS = ["gh", "claude", "codex"]


def _as_probe_row(dep: deps.Dep) -> tuple[str, str, list[str]]:
    return (dep.name, dep.binary, list(dep.version_cmd))


# ---- derivation 1: setup.PROBE_TABLE = [d for d in DEPS if d.required == "always"] --


def test_probe_table_is_the_always_required_rows():
    derived = [_as_probe_row(d) for d in deps.DEPS if d.required == "always"]
    assert derived == PROBE_TABLE_AS_OF_HSUS
    assert [tuple(row) for row in setup_mod.PROBE_TABLE] == [
        tuple(row) for row in PROBE_TABLE_AS_OF_HSUS
    ]


# ---- derivation 2: setup.RUNTIME_PROBES = group "store-runtime" -------------------


def test_runtime_probes_are_the_store_runtime_group():
    derived = {d.name: _as_probe_row(d) for d in deps.group_members("store-runtime")}
    assert derived == RUNTIME_PROBES_AS_OF_HSUS
    assert setup_mod.RUNTIME_PROBES == RUNTIME_PROBES_AS_OF_HSUS


def test_store_runtime_selector_is_dolt_backend():
    assert deps.GROUPS["store-runtime"].selector == "dolt.backend"


# ---- derivation 3: harness.HARNESSES = rows bh can run an install for -------------


def test_harnesses_are_the_installable_rows():
    derived = [d.name for d in deps.installable()]
    assert derived == HARNESSES_AS_OF_HSUS
    assert sorted(harness.HARNESSES) == sorted(HARNESSES_AS_OF_HSUS)


def test_installable_rows_carry_harness_records_field_for_field():
    """Not just membership: the licence stance must survive the move, or bh-hsus.5 would be
    re-deciding it rather than relocating it."""
    for name in HARNESSES_AS_OF_HSUS:
        spec = harness.HARNESSES[name]
        dep = deps.by_name(name)
        assert dep.binary == spec.binary
        assert dep.install is not None
        assert dep.install.package == spec.package
        assert dep.install.license == spec.license
        assert dep.install.version_env == spec.version_env
        assert dep.install.proprietary == spec.proprietary


def test_install_cmd_prefix_matches_what_harness_install_actually_runs():
    """`harness.install` builds `npm install -g --no-fund --no-audit <pkg>@<ver>`; the row
    carries that prefix so `bh dep install` composes the same argv rather than a new one."""
    source = inspect.getsource(harness.install)
    assert '["npm", "install", "-g", "--no-fund", "--no-audit", f"{spec.package}@{wanted}"]' in (
        source
    )
    for dep in deps.installable():
        assert dep.install is not None
        assert dep.install.cmd == ("npm", "install", "-g", "--no-fund", "--no-audit")


# ---- derivation 4: role.KNOWN_HARNESSES = [d for d in DEPS if d.runs_seats] -------


def test_known_harnesses_are_the_seat_runners():
    derived = tuple(d.name for d in deps.DEPS if d.runs_seats)
    assert derived == KNOWN_HARNESSES_AS_OF_HSUS
    assert role.KNOWN_HARNESSES == KNOWN_HARNESSES_AS_OF_HSUS
    assert config.KNOWN_HARNESSES == KNOWN_HARNESSES_AS_OF_HSUS


def test_installs_it_and_runs_a_seat_are_genuinely_different_sets():
    """The whole reason "harness" stopped being one axis: the two sets INTERSECT at {claude}
    and neither contains the other."""
    installs = {d.name for d in deps.installable()}
    runs = {d.name for d in deps.seat_runners()}
    assert installs & runs == {"claude"}
    assert installs - runs == {"codex"}
    assert runs - installs == {"opencode"}


# ---- derivation 5: credential probes = [d for d in DEPS if d.auth] ---------------


def test_credential_probes_are_the_rows_with_auth():
    derived = [d.name for d in deps.authenticated_deps()]
    assert derived == CREDENTIAL_PROBES_AS_OF_HSUS


def test_credential_probes_match_harness_auth_when_it_is_on_this_branch():
    """Self-arming: skips on main (no `harness_auth`), guards for real once bh-q160 merges."""
    harness_auth = pytest.importorskip("beadhive.harness_auth")
    assert sorted(harness_auth.PROBES) == sorted(CREDENTIAL_PROBES_AS_OF_HSUS)
    for dep in deps.authenticated_deps():
        assert dep.name in harness_auth.PROBES


# ---- `required` has exactly two values, and they cover every row -----------------


def test_required_has_exactly_two_forms_with_nothing_left_over():
    for dep in deps.DEPS:
        assert dep.required == "always" or dep.required.startswith("group:"), dep.name
        if dep.required != "always":
            assert dep.group in deps.GROUPS, dep.name
    assert {d.name for d in deps.DEPS} == {d.name for d in deps.always_required()} | {
        d.name for group in deps.GROUPS for d in deps.group_members(group)
    }


def test_every_group_member_is_selectable_or_deliberately_not():
    """Group membership partitions the table: no row belongs to two groups, none to zero."""
    grouped = [d for d in deps.DEPS if d.required != "always"]
    seen = [d.name for group in deps.GROUPS for d in deps.group_members(group)]
    assert sorted(seen) == sorted(d.name for d in grouped)
    assert len(seen) == len(set(seen))


# ---- is_required(): two branches, and jsonl falls out with no special case -------


@pytest.mark.parametrize("name", ["git-workspace", "gh", "bd", "dolt"])
def test_always_rows_are_required_under_any_config(name):
    assert deps.is_required(deps.by_name(name), {}) is True
    assert deps.is_required(deps.by_name(name), {"dolt": {"backend": "none"}}) is True


@pytest.mark.parametrize("backend", ["colima", "docker", "podman"])
def test_backend_selects_exactly_one_runtime(backend):
    cfg = {"dolt": {"backend": backend}}
    selected = [d.name for d in deps.group_members("store-runtime") if deps.is_required(d, cfg)]
    assert selected == [backend]


@pytest.mark.parametrize("backend", ["none", "jsonl"])
def test_backend_that_names_no_runtime_requires_none_of_them(backend):
    """`jsonl`/`none` select nothing and nothing is required — no special case in the code,
    which is the signal the group shape is right."""
    cfg = {"dolt": {"backend": backend}}
    assert [d for d in deps.group_members("store-runtime") if deps.is_required(d, cfg)] == []


def test_agent_group_selects_the_configured_harness(monkeypatch):
    monkeypatch.delenv("BH_HARNESS", raising=False)
    cfg = {"harness": "opencode"}
    selected = [d.name for d in deps.group_members("agent") if deps.is_required(d, cfg)]
    assert selected == ["opencode"]


def test_agent_group_defaults_to_claude(monkeypatch):
    monkeypatch.delenv("BH_HARNESS", raising=False)
    selected = [d.name for d in deps.group_members("agent") if deps.is_required(d, {})]
    assert selected == ["claude"]


def test_codex_is_a_declared_member_config_can_never_select(monkeypatch):
    """bh-hsus.2 Q1: codex has no `--agent`-equivalent flag, so it must NOT become a legal
    value of the agent selector. It stays a declared member that selects to nothing — the same
    shape as `dolt.backend: jsonl`, and required by nobody."""
    monkeypatch.delenv("BH_HARNESS", raising=False)
    legal = typing.get_args(config_schema.BeadhiveConfig.model_fields["harness"].annotation)
    assert "codex" not in legal
    for cfg in ({}, {"harness": "claude"}, {"harness": "opencode"}):
        assert deps.is_required(deps.by_name("codex"), cfg) is False


# ---- residue: registries the table does NOT subsume, named rather than hidden ----


def test_residue_config_schema_literal_mirrors_the_seat_runners():
    """RESIDUE 1: a pydantic `Literal` cannot be built from a runtime list without losing
    static typing, so `config_schema` stays hand-written. This guard is the reconciliation the
    table cannot perform: it fails the moment the enum and the seat-runners disagree."""
    runners = tuple(d.name for d in deps.seat_runners())
    assert typing.get_args(config_schema.BeadhiveConfig.model_fields["harness"].annotation) == (
        runners
    )
    # The per-hive override is `Literal[...] | None`, so unwrap the optional first.
    optional = typing.get_args(config_schema.ManagedRepoEntry.model_fields["harness"].annotation)
    literal = next(a for a in optional if a is not type(None))
    assert typing.get_args(literal) == runners


def test_residue_hitch_targets_keys_mirror_the_seat_runners():
    """RESIDUE 2: `_HITCH_TARGETS` translates bh's harness names into hitch's own vocabulary
    ("claude" -> "claude-code"). The VALUES are hitch's and cannot be derived; the KEYS are
    bh's and must not drift."""
    assert tuple(hitch_plugin._HITCH_TARGETS) == tuple(d.name for d in deps.seat_runners())


def test_residue_plugin_readiness_probes_are_not_dep_probes():
    """RESIDUE 3: `hitch_plugin` and `orca` call `shutil.which()` themselves. That is NOT a
    second dep-detection mechanism to fold in — hitch's binary name is a CONFIG value
    (`hitch.command`), so it cannot be a static table row at all. It is a per-plugin readiness
    probe, which is precisely the required/optional type boundary doing its job."""
    assert callable(config.hitch_command)
    assert config.hitch_command({"hitch": {"command": "somethingelse"}}) == "somethingelse"
    assert shutil.which is not None  # the mechanism plugins use, deliberately left alone
    assert {d.name for d in deps.DEPS}.isdisjoint({"hitch", "orca", "observaloop"})


# ---- the two detection stages stay separate --------------------------------------


def test_present_delegates_to_probe_one(monkeypatch):
    seen: list[tuple] = []

    def fake(name, which_binary, version_cmd):
        seen.append((name, which_binary, tuple(version_cmd)))
        return {"found": True, "version": "1.0"}

    monkeypatch.setattr(setup_mod, "probe_one", fake)
    assert deps.present(deps.by_name("bd")) is True
    assert seen == [("bd", "bd", ("bd", "--version"))]


def test_satisfied_is_stage_one_and_stage_two(monkeypatch):
    monkeypatch.setattr(setup_mod, "probe_one", lambda *a: {"found": True, "version": "1.0"})
    assert deps.satisfied(deps.by_name("bd")) is True  # no auth gate at all
    assert deps.satisfied(deps.by_name("gh")) is False  # auth gate, no stage-2 answer
    assert deps.satisfied(deps.by_name("gh"), authenticated=True) is True

    monkeypatch.setattr(setup_mod, "probe_one", lambda *a: {"found": False, "version": None})
    assert deps.satisfied(deps.by_name("bd")) is False
    assert deps.satisfied(deps.by_name("gh"), authenticated=True) is False
