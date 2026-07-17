"""worktrees_root / worktrees_ephemeral resolution — the money path: ephemeral default,
the temp-dir landing, the path override gated to persistent mode, and $BH_WORKTREES winning.
Plus the shipped init-rule defaults (verify flags, bh-7k1p) and the declared-toolchain
registry being knowledge-only — validate_cmd NEVER consults it (bh-d0kb, revised)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from beadhive import config, toolchain


def test_ephemeral_default_true_when_omitted():
    assert config.worktrees_ephemeral({}) is True
    assert config.worktrees_ephemeral({"worktrees": {}}) is True


def test_ephemeral_root_is_os_temp_and_ignores_path(monkeypatch):
    monkeypatch.delenv("BH_WORKTREES", raising=False)
    monkeypatch.delenv("WS_WORKTREES", raising=False)
    cfg = {"worktrees": {"ephemeral": True, "path": "/should/be/ignored"}}
    root = config.worktrees_root(cfg)
    assert root == Path(tempfile.gettempdir()) / "bh-worktrees"


def test_persistent_uses_path_then_default(monkeypatch):
    monkeypatch.delenv("BH_WORKTREES", raising=False)
    monkeypatch.delenv("WS_WORKTREES", raising=False)
    assert config.worktrees_root({"worktrees": {"ephemeral": False, "path": "/srv/wt"}}) == Path(
        "/srv/wt"
    )
    monkeypatch.setenv("BH_HOME", "/tmp/wshome")
    assert config.worktrees_root({"worktrees": {"ephemeral": False}}) == Path(
        "/tmp/wshome/worktrees"
    )


def test_bh_worktrees_env_overrides_both_modes(monkeypatch):
    monkeypatch.setenv("BH_WORKTREES", "/explicit/override")
    for ephemeral in (True, False):
        assert config.worktrees_root({"worktrees": {"ephemeral": ephemeral}}) == Path(
            "/explicit/override"
        )


def test_config_example_init_defaults_flag_verify():
    """The shipped template parses with the verify flag, and the defaults draw the line the
    verify-environment contract demands (bh-7k1p): dependency sync ('uv sync') and trust stamps
    ('mise trust') are verify: true (they run per clean-checkout validation); heavy seat
    provisioning (the probe-guarded 'just setup' rule, bh-17n4) stays unflagged."""
    data = config._yaml.load(config.template("config.example.yaml").read_text())
    rules = {r["run"]: dict(r) for r in data["worktrees"]["init"]}
    assert rules["mise trust"].get("verify") is True
    assert rules["uv sync"].get("verify") is True
    (just_rule,) = [r for run, r in rules.items() if "just setup" in run]
    assert "verify" not in just_rule


# ---- declared toolchains: knowledge-only (bh-d0kb, revised) ------------------


def test_toolchain_registry_builtins_carry_discovery_and_suggestions():
    """Shipped templates: an entrypoints_cmd (the discovery command `show` runs) plus
    propose-only suggested_* fields. The suggestions keep the bh-7k1p verify line
    (dependency sync flagged, seat provisioning not) and the bh-17n4 probe guards —
    knowledge an agent proposes to the operator, never acted on by bh."""
    reg = toolchain.registry({})
    assert set(reg) >= {"just", "uv", "npm", "make"}
    assert reg["just"]["entrypoints_cmd"] == "just --list"
    assert reg["npm"]["entrypoints_cmd"] == "npm run"
    assert "tomllib" in reg["uv"]["entrypoints_cmd"]  # pyproject [project.scripts] reader
    assert "make -pRrq" in reg["make"]["entrypoints_cmd"]  # best-effort target dump
    (uv_rule,) = reg["uv"]["suggested_init"]
    assert uv_rule == {"if_exists": "pyproject.toml", "run": "uv sync", "verify": True}
    (npm_rule,) = reg["npm"]["suggested_init"]
    assert npm_rule["run"] == "npm ci" and npm_rule["verify"] is True
    for name, probe in (("just", "just --show setup"), ("make", "make -n setup")):
        (rule,) = reg[name]["suggested_init"]
        assert "verify" not in rule
        assert probe in rule["run"]  # probe before running
        assert "not configured in this repo" in rule["run"]  # quiet info fallback
    assert reg["just"]["suggested_validate_cmd"] == "just check"


def test_toolchain_registry_config_override_replaces_per_name():
    cfg = {
        "worktrees": {
            "toolchains": {
                "just": {"entrypoints_cmd": "just --list --list-heading ''"},
                "gradle": {"suggested_validate_cmd": "./gradlew check"},
            }
        }
    }
    reg = toolchain.registry(cfg)
    # replace, not merge — the override owns its whole template
    assert reg["just"] == {"entrypoints_cmd": "just --list --list-heading ''"}
    assert reg["gradle"]["suggested_validate_cmd"] == "./gradlew check"  # additions allowed
    assert reg["uv"]["suggested_validate_cmd"] == "uv run pytest"  # untouched built-ins remain


def test_declared_resolves_per_hive_over_global_and_normalizes_to_list():
    assert toolchain.declared({"worktrees": {"toolchain": "npm"}}, {}) == ["npm"]
    assert toolchain.declared({"worktrees": {"toolchain": "npm"}}, {"toolchain": "make"}) == [
        "make"
    ]
    assert toolchain.declared({"worktrees": {"toolchain": ["uv", "just"]}}, {}) == ["uv", "just"]
    assert toolchain.declared({}, {}) == []


def test_validate_cmd_ignores_declared_toolchain():
    """Knowledge-only: a declaration NEVER supplies the validate default — the template's
    suggested_validate_cmd is a proposal for the operator, not a fallback layer."""
    assert config.validate_cmd({"worktrees": {"toolchain": "npm"}}, {}) == "just check"
    assert config.validate_cmd({"worktrees": {"toolchain": ["uv", "npm"]}}, {}) == "just check"
    assert config.validate_cmd({}, {"toolchain": "make"}) == "just check"


def test_validate_cmd_explicit_config_only():
    cfg = {"worktrees": {"toolchain": "npm"}, "work": {"validate_cmd": "just check-all"}}
    assert config.validate_cmd(cfg, {}) == "just check-all"
    cfg = {"worktrees": {"toolchain": "npm"}, "work": {"validate": {"submit": "just fast"}}}
    assert config.validate_cmd(cfg, {}, "submit") == "just fast"
    assert config.validate_cmd({}, None) == "just check"  # unset ⇒ the hard default
