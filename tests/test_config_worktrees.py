"""worktrees_root / worktrees_ephemeral resolution — the money path: ephemeral default,
the temp-dir landing, the path override gated to persistent mode, and $BH_WORKTREES winning.
Plus the shipped init-rule defaults (verify flags, bh-7k1p) and the declared-toolchain
registry being knowledge-only — validate_cmd NEVER consults it (bh-d0kb, revised)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from beadhive import config, config_paths, config_schema, toolchain


def test_ephemeral_default_true_when_omitted():
    assert config.worktrees_ephemeral({}) is True
    assert config.worktrees_ephemeral({"worktrees": {}}) is True


def test_ephemeral_root_is_os_temp_and_ignores_path(monkeypatch):
    monkeypatch.delenv("BH_WORKTREES", raising=False)
    monkeypatch.delenv("WS_WORKTREES", raising=False)
    monkeypatch.setattr(config_paths, "statfs_type", lambda _p: _EXT4_MAGIC)  # disk-backed temp dir
    cfg = {"worktrees": {"ephemeral": True, "path": "/should/be/ignored"}}
    root = config.worktrees_root(cfg)
    assert root == Path(tempfile.gettempdir()) / "bh-worktrees"


# ---- RAM-backed (tmpfs/ramfs) root guard (bh-xzsdf) ---------------------------


_EXT4_MAGIC = 0xEF53


def _isolate(monkeypatch, tmp_path):
    for var in ("BH_WORKTREES", "WS_WORKTREES", "BH_WORKTREES_ALLOW_TMPFS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BH_HOME", str(tmp_path / "home"))


def test_default_root_is_disk_backed_when_os_temp_is_tmpfs(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    temp = Path(tempfile.gettempdir()) / "bh-worktrees"
    monkeypatch.setattr(
        config_paths,
        "statfs_type",
        lambda p: config_paths_magic("tmpfs") if Path(p) == temp else _EXT4_MAGIC,
    )
    root = config.worktrees_root({})
    assert root == tmp_path / "home" / "worktrees"
    assert config.worktrees_root_refusal({}) is None  # the disk fallback itself is allowed


def test_in_use_tmpfs_root_keeps_resolving_but_refuses_new_worktrees(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    fake_tmp = tmp_path / "tmp"
    (fake_tmp / "bh-worktrees" / "github").mkdir(parents=True)  # an in-flight legacy worktree
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_tmp))
    monkeypatch.setattr(config_paths, "statfs_type", lambda _p: config_paths_magic("tmpfs"))
    assert config.worktrees_root({}) == fake_tmp / "bh-worktrees"  # still reachable
    message = config.worktrees_root_refusal({})
    assert message is not None and "worktrees.allow_tmpfs" in message
    assert config.worktrees_root_refusal({"worktrees": {"allow_tmpfs": True}}) is None


def test_default_root_is_disk_fallback_honours_worktrees_path(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    monkeypatch.setattr(config_paths, "statfs_type", lambda _p: config_paths_magic("ramfs"))
    cfg = {"worktrees": {"path": str(tmp_path / "disk")}}
    assert config.worktrees_root(cfg) == tmp_path / "disk"


def test_os_temp_root_kept_when_it_is_disk_backed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(config_paths, "statfs_type", lambda _p: _EXT4_MAGIC)
    assert config.worktrees_root({}) == Path(tempfile.gettempdir()) / "bh-worktrees"


def test_allow_tmpfs_opt_in_keeps_the_tmpfs_temp_root(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(config_paths, "statfs_type", lambda _p: config_paths_magic("tmpfs"))
    cfg = {"worktrees": {"allow_tmpfs": True}}
    assert config.worktrees_allow_tmpfs(cfg) is True
    assert config.worktrees_root(cfg) == Path(tempfile.gettempdir()) / "bh-worktrees"
    assert config.worktrees_root_refusal(cfg) is None


def test_allow_tmpfs_env_opt_in(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(config_paths, "statfs_type", lambda _p: config_paths_magic("tmpfs"))
    monkeypatch.setenv("BH_WORKTREES_ALLOW_TMPFS", "1")
    assert config.worktrees_allow_tmpfs({}) is True
    assert config.worktrees_root({}) == Path(tempfile.gettempdir()) / "bh-worktrees"


def test_explicit_tmpfs_root_is_refused_naming_the_opt_in_key(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("BH_WORKTREES", "/explicit/ram")
    monkeypatch.setattr(config_paths, "statfs_type", lambda _p: config_paths_magic("tmpfs"))
    message = config.worktrees_root_refusal({})
    assert message is not None
    assert "worktrees.allow_tmpfs" in message and "BH_WORKTREES_ALLOW_TMPFS" in message
    assert "/explicit/ram" in message
    # opting in lifts the refusal
    assert config.worktrees_root_refusal({"worktrees": {"allow_tmpfs": True}}) is None


def test_persistent_path_on_ramfs_is_refused(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(config_paths, "statfs_type", lambda _p: config_paths_magic("ramfs"))
    cfg = {"worktrees": {"ephemeral": False, "path": "/srv/ram"}}
    assert "worktrees.allow_tmpfs" in (config.worktrees_root_refusal(cfg) or "")


def test_unreadable_statfs_is_not_treated_as_memory_backed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(config_paths, "statfs_type", lambda _p: None)
    assert config.is_memory_backed(tmp_path) is False
    assert config.worktrees_root_refusal({}) is None


def test_statfs_type_reads_a_real_filesystem_and_walks_up_missing_paths(tmp_path):
    real = config.statfs_type(tmp_path)
    assert real is None or isinstance(real, int)
    assert config.statfs_type(tmp_path / "does" / "not" / "exist") == real


def test_do_add_refuses_a_ram_backed_root_before_creating_anything(monkeypatch, tmp_path):
    import pytest
    import typer

    from beadhive import worktree

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "ram"))
    monkeypatch.setattr(config_paths, "statfs_type", lambda _p: config_paths_magic("tmpfs"))
    with pytest.raises(typer.Exit):
        worktree._do_add(
            {}, {"prefix": "x"}, tmp_path, "b", tmp_path / "ram" / "t", new_branch=True
        )
    assert not (tmp_path / "ram").exists()


def test_verify_checkout_follows_the_same_root_rule(monkeypatch, tmp_path):
    from beadhive import worktree_verify

    _isolate(monkeypatch, tmp_path)
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.yaml").write_text("managed_repos: []\n")
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "ram"))
    monkeypatch.setattr(config_paths, "statfs_type", lambda _p: config_paths_magic("tmpfs"))
    path, rc = worktree_verify.impl__prepare_verify_worktree(tmp_path, {"prefix": "x"}, "b", "true")
    assert (path, rc) == (None, 1)
    assert not (tmp_path / "ram").exists()


def config_paths_magic(kind: str) -> int:
    from beadhive import config_paths

    return {"tmpfs": config_paths.TMPFS_MAGIC, "ramfs": config_paths.RAMFS_MAGIC}[kind]


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


# ---- Codex sandbox reachability (bh-rpzaj) -----------------------------------


def test_codex_sandbox_active_reads_the_env_signal(monkeypatch):
    monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED", raising=False)
    assert config.codex_sandbox_active() is False
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    assert config.codex_sandbox_active() is True
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "")  # empty counts as unset
    assert config.codex_sandbox_active() is False


def test_codex_default_sandbox_covers_cwd_and_tmp(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert config.codex_default_sandbox_covers(tmp_path / "worktrees") is True
    assert config.codex_default_sandbox_covers(Path(tempfile.gettempdir()) / "bh-worktrees") is True


def test_codex_default_sandbox_does_not_cover_a_root_outside_cwd_and_tmp(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # a persistent root under $HOME, well outside both cwd and the OS temp dir
    outside = Path("/definitely-not-cwd-or-tmp/beadhive/worktrees")
    assert config.codex_default_sandbox_covers(outside) is False


def test_mise_trust_rule_matches_both_dotted_and_undotted_config(tmp_path):
    """bh-ggfr: the default `mise trust` predicate fires for BOTH mise config spellings.

    mise accepts `.mise.toml` and `mise.toml` and its docs favour the undotted form, so a
    dotted-only predicate silently skipped `mise trust` on modern repos — every `mise exec` recipe
    then failed 'not trusted' inside the verify checkout and surfaced as a submit validation
    failure, i.e. as a broken change rather than an unprovisioned machine.

    Asserted through `Path.glob`, the same call `worktree._init_rules` makes, rather than by
    string-comparing the pattern — the property that matters is which files match, not how the
    glob is spelled. The both-present case is included because `any()` must still fire once."""
    (rule,) = [r for r in config_schema._DEFAULT_WORKTREE_INIT if r.run == "mise trust"]

    for i, names in enumerate([[".mise.toml"], ["mise.toml"], [".mise.toml", "mise.toml"]]):
        d = tmp_path / f"case{i}"  # indexed: `.mise.toml` and `mise.toml` collide on any strip()
        d.mkdir()
        for n in names:
            (d / n).write_text("[tools]\n")
        assert any(d.glob(rule.if_exists)), f"{rule.if_exists!r} did not match {names}"

    empty = tmp_path / "no-mise"
    empty.mkdir()
    (empty / "pyproject.toml").write_text("")
    assert not any(empty.glob(rule.if_exists))  # still no-ops on a repo that does not use mise


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
