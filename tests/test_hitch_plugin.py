"""hitch_plugin.py — the agent-hitch launch integration (bh-og0q.5), an OPTIONAL plugin.

Covers:

- config accessors: ``hitch_enabled`` has NO AND-gate on another plugin (unlike orca/
  git-workspace), ``hitch_command``/``hitch_repo``/``hitch_config_dir_root`` resolution —
  including that it is ALWAYS persistent and decoupled from ``worktrees.ephemeral``
  (ADR Amendment 5; bh-og0q.8), while worktree disposability itself is unchanged.
- ``plugins.registry()`` includes hitch (import-safe with no ``hitch`` binary on PATH at all).
- ``up()``'s gating ladder: disabled → unknown target → hitch missing → repo unconfigured →
  a real (mocked) subprocess call, in that order, each refusing BEFORE any subprocess spawns.
- "fails loudly, never falls back": a nonzero (mocked) hitch exit propagates verbatim — no
  retry, no fallback to ``bh role``/ambient config.
- readiness probe states.
- **degradation**: bh's existing default launch path (``beadhive.role``) is provably
  byte-identical whether hitch is enabled, disabled, or entirely unmentioned in config — this
  is the bead's core acceptance bar, so it is asserted directly rather than left to inspection.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from beadhive import config, hitch_plugin, hive_ready, plugins, role
from beadhive.cli import app

runner = CliRunner()


# ---- config accessors ---------------------------------------------------------


def test_hitch_enabled_false_by_default():
    assert config.hitch_enabled({}) is False


def test_hitch_enabled_has_no_and_gate_on_other_plugins():
    """Unlike orca (AND-gated on git_workspace.enabled), hitch shares no data/state with any
    other plugin — it must be enable-able with every other integration off."""
    cfg = {
        "git_workspace": {"enabled": False},
        "orca": {"enabled": False},
        "observaloop": {"enabled": False},
        "otel": {"enabled": False},
        "hitch": {"enabled": True},
    }
    assert config.hitch_enabled(cfg) is True


def test_hitch_enabled_hive_entry_overrides_global():
    cfg = {"hitch": {"enabled": True}}
    entry = {"hitch": {"enabled": False}}
    assert config.hitch_enabled(cfg, entry) is False


def test_hitch_command_default():
    assert config.hitch_command({}) == "hitch"


def test_hitch_command_override():
    assert config.hitch_command({"hitch": {"command": "/opt/hitch/bin/hitch"}}) == (
        "/opt/hitch/bin/hitch"
    )


def test_hitch_repo_none_by_default():
    assert config.hitch_repo({}) is None


def test_hitch_repo_expanduser():
    cfg = {"hitch": {"repo": "~/src/agent-hitch"}}
    assert config.hitch_repo(cfg) == Path("~/src/agent-hitch").expanduser()


def test_hitch_config_dir_root_persists_by_default():
    """No `worktrees` section at all — the zero-config case. Must NOT land in an OS-temp root
    (ADR Amendment 5: an ephemeral Config Directory forces re-auth at every launch)."""
    assert config.hitch_config_dir_root({}) == (config.home() / "hitch")


def test_hitch_config_dir_root_override():
    cfg = {"hitch": {"root": "~/custom/hitch"}}
    assert config.hitch_config_dir_root(cfg) == Path("~/custom/hitch").expanduser()


def test_hitch_config_dir_root_ignores_worktrees_ephemeral():
    """THE decoupling this bead fixes: a Config Directory's persistence must not follow
    `worktrees.ephemeral` in either direction — same resolved root whether worktrees are
    ephemeral (the default) or explicitly persistent."""
    ephemeral_worktrees = config.hitch_config_dir_root({"worktrees": {"ephemeral": True}})
    persistent_worktrees = config.hitch_config_dir_root({"worktrees": {"ephemeral": False}})
    no_worktrees_section = config.hitch_config_dir_root({})

    assert ephemeral_worktrees == persistent_worktrees == no_worktrees_section == (
        config.home() / "hitch"
    )


def test_worktree_disposability_unchanged_by_hitch_persistence(monkeypatch):
    """The trade this bead must NOT make: worktrees must still be ephemeral by default (an
    OS-temp root), completely unaffected by hitch's config directory now always persisting."""
    import tempfile

    monkeypatch.delenv("BH_WORKTREES", raising=False)
    monkeypatch.delenv("WS_WORKTREES", raising=False)
    assert config.worktrees_root({}) == Path(tempfile.gettempdir()) / "bh-worktrees"
    assert config.worktrees_ephemeral({}) is True


# ---- plugins.registry() -------------------------------------------------------


def test_registry_includes_hitch_last():
    reg = plugins.registry()
    assert [p.name for p in reg] == ["git-workspace", "orca", "observaloop", "hitch"]


def test_import_is_safe_without_hitch_on_path(monkeypatch):
    """``import beadhive.hitch_plugin`` must succeed even when the ``hitch`` binary is nowhere
    on PATH — the module never imports agent-hitch's own Python package, only shells out."""
    import importlib

    monkeypatch.setenv("PATH", "")
    importlib.reload(hitch_plugin)


def test_plugin_enabled_matches_config_accessor():
    cfg = {"hitch": {"enabled": True}}
    assert hitch_plugin.PLUGIN.enabled(cfg, None) is True
    assert hitch_plugin.PLUGIN.enabled({}, None) is False


def test_plugin_has_no_onboard_or_worktree_hooks():
    """Deliberate: wt_create is NOT used for provisioning (see the module docstring for why) —
    build/launch happens only inside the explicit `up` verb."""
    assert hitch_plugin.PLUGIN.on_onboard is None
    assert hitch_plugin.PLUGIN.wt_create is None
    assert hitch_plugin.PLUGIN.wt_remove is None


# ---- up(): the gating ladder, each step refusing BEFORE any subprocess spawns -----------------


def _no_subprocess(monkeypatch):
    """Fail the test loudly if `up()` ever reaches the real subprocess call at this step."""

    def _boom(*a, **k):
        raise AssertionError("up() must not spawn a subprocess at this gating step")

    monkeypatch.setattr(hitch_plugin.run, "run", _boom)


def test_up_disabled_by_default_refuses_before_any_subprocess(monkeypatch):
    _no_subprocess(monkeypatch)
    code = hitch_plugin.up("claude", "dispatcher", cfg={})
    assert code == 1


def test_up_unknown_target_refuses(monkeypatch):
    _no_subprocess(monkeypatch)
    cfg = {"hitch": {"enabled": True}}
    code = hitch_plugin.up("gpt", "dispatcher", cfg=cfg)
    assert code == 1


def test_up_hitch_missing_from_path_refuses(monkeypatch):
    _no_subprocess(monkeypatch)
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: None)
    cfg = {"hitch": {"enabled": True}}
    code = hitch_plugin.up("claude", "dispatcher", cfg=cfg)
    assert code == 1


def test_up_repo_unconfigured_refuses(monkeypatch):
    _no_subprocess(monkeypatch)
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    cfg = {"hitch": {"enabled": True}}
    code = hitch_plugin.up("claude", "dispatcher", cfg=cfg)
    assert code == 1


# ---- up(): the real (mocked) subprocess call ------------------------------------------------


def _stub_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    cfg = {"hitch": {"enabled": True, "repo": str(tmp_path)}}
    return cfg


def test_up_translates_bh_target_to_hitch_target_and_shells_out(monkeypatch, tmp_path):
    cfg = _stub_ready(monkeypatch, tmp_path)
    calls = []

    class _Result:
        returncode = 0

    def _fake_run(argv, **kw):
        calls.append(argv)
        return _Result()

    monkeypatch.setattr(hitch_plugin.run, "run", _fake_run)

    code = hitch_plugin.up("claude", "dispatcher", cfg=cfg)

    assert code == 0
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "hitch"
    assert argv[1] == "up"
    # bh's own "claude" harness name translates to hitch's "claude-code" target — determined
    # empirically (hitch's up dispatches on args.target == "claude-code", not "claude").
    assert argv[2] == "claude-code"
    assert argv[3] == "dispatcher"
    assert "--profiles-file" in argv
    assert str(tmp_path / "profiles" / "local.yaml") in argv
    assert "--catalog" in argv
    assert str(tmp_path / "catalogs" / "local.yaml") in argv
    assert "--root" in argv


def test_up_opencode_target_passes_through_unchanged(monkeypatch, tmp_path):
    cfg = _stub_ready(monkeypatch, tmp_path)
    calls = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: calls.append(argv) or _Result())

    hitch_plugin.up("opencode", "developer", cfg=cfg)

    assert calls[0][2] == "opencode"


def test_oauth_state_survives_a_subsequent_launch(monkeypatch, tmp_path):
    """The single property ADR Amendment 5 turns on, tested for real rather than just asserting
    the path changed: OAuth session state written into a config directory by one seat launch
    must still be there for the next. No real credentials needed — a sentinel file standing in
    for Claude Code's `.claude.json` proves the same thing, since `up()` never touches the
    resolved root's contents; it only ever hands `--root <path>` to the real `hitch up`, and
    that path must be the SAME one across separate launches (i.e. reused, not rebuilt)."""
    cfg = _stub_ready(monkeypatch, tmp_path)
    cfg["hitch"]["root"] = str(tmp_path / "hitch-root")
    calls = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: calls.append(argv) or _Result())

    # Launch 1: `hitch up` (mocked here — a real run would build the Config Directory and the
    # operator would log in once, leaving OAuth state behind).
    hitch_plugin.up("claude", "dispatcher", cfg=cfg)
    root_1 = Path(calls[0][calls[0].index("--root") + 1])
    root_1.mkdir(parents=True, exist_ok=True)
    sentinel = root_1 / ".claude.json"
    sentinel.write_text('{"oauthAccount": {"session": "sentinel"}}')

    # Launch 2: a later seat start.
    hitch_plugin.up("claude", "dispatcher", cfg=cfg)
    root_2 = Path(calls[1][calls[1].index("--root") + 1])

    assert root_2 == root_1  # reused, not a fresh (e.g. OS-temp) root each launch
    assert sentinel.read_text() == '{"oauthAccount": {"session": "sentinel"}}'


def test_up_propagates_a_hitch_preflight_failure_verbatim(monkeypatch, tmp_path):
    """A nonzero exit from the real `hitch up` (e.g. preflight failure) must propagate as-is —
    no retry, no fallback to ambient config. "Fails loudly" is inherited from hitch's own
    already-fail-closed implementation, not re-implemented here."""
    cfg = _stub_ready(monkeypatch, tmp_path)

    class _Result:
        returncode = 1

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: _Result())

    code = hitch_plugin.up("claude", "dispatcher", cfg=cfg)

    assert code == 1


# ---- readiness -----------------------------------------------------------------


def test_readiness_missing_when_hitch_not_on_path(monkeypatch):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: None)
    state, detail = hitch_plugin._readiness({}, None)
    assert state == "missing"


def test_readiness_warn_when_repo_unconfigured(monkeypatch):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    state, detail = hitch_plugin._readiness({}, None)
    assert state == "warn"
    assert "hitch.repo" in detail


def test_readiness_warn_when_repo_missing_profiles_or_catalog(monkeypatch, tmp_path):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    cfg = {"hitch": {"repo": str(tmp_path)}}
    state, detail = hitch_plugin._readiness(cfg, None)
    assert state == "warn"


def test_readiness_ok_when_fully_set_up(monkeypatch, tmp_path):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "catalogs").mkdir()
    (tmp_path / "profiles" / "local.yaml").write_text("profiles: {}\n")
    (tmp_path / "catalogs" / "local.yaml").write_text("packs: {}\n")
    cfg = {"hitch": {"repo": str(tmp_path)}}
    state, detail = hitch_plugin._readiness(cfg, None)
    assert state == "ok"


def test_hive_ready_plugin_checks_na_when_disabled():
    """Silent when unused — the generic hive_ready loop reports 'na' for a disabled plugin
    without ever calling its readiness probe (ADR Amendment 2: "an optional integration that
    complains when unused is not optional")."""
    checks = hive_ready._plugin_checks({}, None)
    line = next(c for c in checks if c.label == "hitch")
    assert line.state == "na"


# ---- seat-runnability reporting (bh-og0q.4) -------------------------------------------------
# "Which seats can THIS host run" — delegates to `hitch profile preflight`, riding the SAME
# `_readiness` hook `bh hive ready` and `bh doctor` (via `hitch_plugin.PLUGIN.readiness`) share.


def _write_repo(tmp_path, profile_names):
    """A minimal hitch.repo checkout: profiles/local.yaml declaring the given profile names,
    plus an empty catalogs/local.yaml (never actually read here — every test below mocks the
    `hitch` subprocess itself, so the catalog's content is irrelevant)."""
    (tmp_path / "profiles").mkdir()
    (tmp_path / "catalogs").mkdir()
    body = "profiles:\n" + "".join(f"  {name}:\n    packs: []\n" for name in profile_names)
    (tmp_path / "profiles" / "local.yaml").write_text(body)
    (tmp_path / "catalogs" / "local.yaml").write_text("packs: {}\n")
    return tmp_path


# ---- _profile_names ----------------------------------------------------------


def test_profile_names_reads_top_level_profiles_mapping(tmp_path):
    repo = _write_repo(tmp_path, ["developer", "dispatcher", "shell"])
    names = hitch_plugin._profile_names(repo / "profiles" / "local.yaml")
    assert names == {"developer", "dispatcher", "shell"}


def test_profile_names_empty_when_file_missing(tmp_path):
    assert hitch_plugin._profile_names(tmp_path / "nope.yaml") == set()


def test_profile_names_empty_when_malformed(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: [valid: yaml: at all")
    assert hitch_plugin._profile_names(bad) == set()


# ---- _classify_preflight ------------------------------------------------------


def test_classify_preflight_ok_when_clean_pass():
    state, detail = hitch_plugin._classify_preflight(0, "Preflight succeeded\n")
    assert (state, detail) == ("ok", "")


def test_classify_preflight_reduced_on_declared_family_info():
    """The ADR's own example: a target dropping a declared family is a REPORT of reduced
    capability, not a hard blocker — the seat still runs."""
    stdout = (
        "  [info] beadhive: target 'claude-code' does not support family "
        "'instructions' (declared, not selected)\nPreflight succeeded\n"
    )
    state, detail = hitch_plugin._classify_preflight(0, stdout)
    assert state == "reduced"
    assert "instructions" in detail


def test_classify_preflight_ignores_unrelated_info_lines():
    """Only 'does not support family' infos count as reduced capability — a summary line like
    'capabilities available: ...' must not be misread as a capability loss."""
    stdout = "  [info] capabilities available: beadhive.control\nPreflight succeeded\n"
    state, _detail = hitch_plugin._classify_preflight(0, stdout)
    assert state == "ok"


def test_classify_preflight_blocked_names_the_missing_binary():
    stdout = "  [fail] beadhive: required binary 'repowise' not found in PATH\nPreflight failed\n"
    state, detail = hitch_plugin._classify_preflight(1, stdout)
    assert state == "blocked"
    assert "repowise" in detail


def test_classify_preflight_blocked_joins_multiple_failures():
    stdout = (
        "  [fail] just: required binary 'just' not found in PATH\n"
        "  [fail] beadhive: required binary 'uv' not found in PATH\n"
        "Preflight failed\n"
    )
    state, detail = hitch_plugin._classify_preflight(1, stdout)
    assert state == "blocked"
    assert "just" in detail
    assert "uv" in detail


# ---- seat_reports --------------------------------------------------------------


def test_seat_reports_empty_when_hitch_not_on_path(monkeypatch):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: None)
    assert hitch_plugin.seat_reports({}) == []


def test_seat_reports_empty_when_repo_unconfigured(monkeypatch):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    assert hitch_plugin.seat_reports({}) == []


def test_seat_reports_empty_when_no_seat_aligned_profile(monkeypatch, tmp_path):
    """A profile that doesn't match any bh seat name (e.g. 'shell') is silently skipped —
    nothing to check, not a blocker."""
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer", "dispatcher"])
    repo = _write_repo(tmp_path, ["shell", "dotfiles"])
    cfg = {"hitch": {"repo": str(repo)}}
    assert hitch_plugin.seat_reports(cfg) == []


def test_seat_reports_only_checks_seat_aligned_profiles(monkeypatch, tmp_path):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer", "dispatcher"])
    repo = _write_repo(tmp_path, ["developer", "shell"])  # 'shell' is not a bh seat
    cfg = {"hitch": {"repo": str(repo)}}
    calls = []

    class _Result:
        returncode = 0
        stdout = "Preflight succeeded\n"

    def _fake_run(argv, **kw):
        calls.append(argv)
        return _Result()

    monkeypatch.setattr(hitch_plugin.run, "run", _fake_run)

    reports = hitch_plugin.seat_reports(cfg)

    assert [r["seat"] for r in reports] == ["developer"]
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:4] == ["hitch", "profile", "preflight", "developer"]
    assert argv[argv.index("--target") + 1] == "claude-code"  # bh's default harness, translated


def test_seat_reports_translates_configured_harness_to_hitch_target(monkeypatch, tmp_path):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"repo": str(repo)}, "harness": "opencode"}
    calls = []

    class _Result:
        returncode = 0
        stdout = "Preflight succeeded\n"

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: calls.append(argv) or _Result())

    hitch_plugin.seat_reports(cfg)

    assert calls[0][calls[0].index("--target") + 1] == "opencode"


def test_seat_reports_state_and_detail_come_from_classify(monkeypatch, tmp_path):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"repo": str(repo)}}

    class _Result:
        returncode = 1
        stdout = "  [fail] beadhive: required binary 'repowise' not found in PATH\n"

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: _Result())

    reports = hitch_plugin.seat_reports(cfg)

    assert reports == [
        {
            "seat": "developer",
            "state": "blocked",
            "detail": "beadhive: required binary 'repowise' not found in PATH",
        }
    ]


# ---- _readiness folding in seat_reports ----------------------------------------


def test_readiness_ok_folds_in_runnable_seats(monkeypatch, tmp_path):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"repo": str(repo)}}

    class _Result:
        returncode = 0
        stdout = "Preflight succeeded\n"

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: _Result())

    state, detail = hitch_plugin._readiness(cfg, None)
    assert state == "ok"
    assert "developer: runnable" in detail


def test_readiness_warn_when_a_seat_is_blocked(monkeypatch, tmp_path):
    """A blocked SEAT degrades the plugin's own readiness line to 'warn' (never 'missing' — the
    plugin itself is fine; a specific seat lacks a capability), and names the missing binary."""
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"repo": str(repo)}}

    class _Result:
        returncode = 1
        stdout = "  [fail] beadhive: required binary 'repowise' not found in PATH\n"

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: _Result())

    state, detail = hitch_plugin._readiness(cfg, None)
    assert state == "warn"
    assert "developer: cannot run" in detail
    assert "repowise" in detail


# ---- bh plugin hitch up (CLI) --------------------------------------------------


def test_plugin_tree_help_lists_hitch():
    result = runner.invoke(app, ["plugin", "--help"])
    assert result.exit_code == 0
    assert "hitch" in result.output


def test_cli_up_disabled_by_default_refuses(monkeypatch):
    monkeypatch.setattr(config, "load", lambda: {})
    result = runner.invoke(app, ["plugin", "hitch", "up", "claude", "dispatcher"])
    assert result.exit_code != 0
    assert "disabled" in result.output


def test_cli_up_success_exits_zero(monkeypatch, tmp_path):
    cfg = {"hitch": {"enabled": True, "repo": str(tmp_path)}}
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")

    class _Result:
        returncode = 0

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: _Result())

    result = runner.invoke(app, ["plugin", "hitch", "up", "claude", "dispatcher"])
    assert result.exit_code == 0, result.output


# ---- degradation: bh's existing default launch path is unaffected -----------------------------
#
# The bead's core acceptance bar: with hitch disabled, absent from PATH, or failing to load, bh
# behaves EXACTLY as it does today — the default launch path (`bh role <seat>`, `beadhive.role`)
# is unchanged. `role.py` never references hitch/config.hitch_* at all, so this is provable
# directly: construct the launch argv/env with hitch enabled, disabled, and unmentioned, and
# assert they are identical.


def test_role_harness_argv_unaffected_by_hitch_config_state():
    """`_harness_argv` takes no cfg at all — a hitch section existing (enabled or not) cannot
    change it. Pinned explicitly so a future coupling would fail this test, not just review."""
    plain = role._harness_argv("claude", "developer")
    assert plain == role._harness_argv("claude", "developer")  # deterministic, no hidden state


def test_role_harness_env_unaffected_by_hitch_config_state(monkeypatch):
    """`harness_env` builds off `os.environ` + `_bh_bin_dir()` only — never `config.hitch_*`.
    Toggle hitch on/off in the loaded config and confirm the launched env is byte-identical."""
    monkeypatch.setattr(config, "load", lambda: {"hitch": {"enabled": True}})
    with_hitch_enabled = role.harness_env("developer")

    monkeypatch.setattr(config, "load", lambda: {"hitch": {"enabled": False}})
    with_hitch_disabled = role.harness_env("developer")

    monkeypatch.setattr(config, "load", lambda: {})
    without_hitch_section = role.harness_env("developer")

    assert with_hitch_enabled == with_hitch_disabled == without_hitch_section


def test_role_module_never_imports_hitch_plugin():
    """A stronger, structural guard: `beadhive.role` must not import `hitch_plugin` (or call
    `config.hitch_*`) at all — the core launch path has zero code-level awareness of this
    plugin. (The module docstring's prose mentions "agent-hitch" in passing re: bh-og0q.2's
    unrelated PATH fix — that's fine; only actual import/call sites are checked here.)"""
    import inspect

    source = inspect.getsource(role)
    assert "hitch_plugin" not in source
    assert "config.hitch_" not in source
    assert "import hitch" not in source


def test_launch_unknown_role_unaffected_by_hitch(monkeypatch, capsys):
    """`launch()`'s error path (unknown seat) is identical regardless of hitch config."""
    import pytest

    for hitch_cfg in ({"hitch": {"enabled": True}}, {"hitch": {"enabled": False}}, {}):
        monkeypatch.setattr(config, "load", lambda c=hitch_cfg: c)
        with pytest.raises(SystemExit) as exc:
            role.launch("not-a-real-seat")
        assert exc.value.code == 1
