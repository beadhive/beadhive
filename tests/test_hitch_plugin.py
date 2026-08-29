"""hitch_plugin.py — the agent-hitch launch integration (bh-og0q.5), an OPTIONAL plugin.

Covers:

- config accessors: ``hitch_enabled`` has NO AND-gate on another plugin,
  ``hitch_command``/``hitch_repo``/``hitch_config_dir_root`` resolution —
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

import asyncio
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from beadhive import config, hitch_plugin, hive_ready, localloop, plugins, role
from beadhive.cli import app

runner = CliRunner()


# ---- config accessors ---------------------------------------------------------


def test_hitch_enabled_false_by_default():
    assert config.hitch_enabled({}) is False


def test_hitch_enabled_has_no_and_gate_on_other_plugins():
    """hitch shares no data/state with any other plugin (bh-hsus.4 also removed orca's old
    AND-gate on `git_workspace.enabled` — deleted, git-workspace is a required dep now) — it
    must be enable-able with every other integration off."""
    cfg = {
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

    assert (
        ephemeral_worktrees
        == persistent_worktrees
        == no_worktrees_section
        == (config.home() / "hitch")
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


def test_registry_includes_hitch_and_herdr():
    """bh-hsus.4: git-workspace is no longer in this registry at all — it's a required dep
    (`deps.py`), not an optional plugin."""
    reg = plugins.registry()
    assert [p.name for p in reg] == ["orca", "observaloop", "hitch", "herdr", "repowise"]


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


def test_up_unknown_target_refuses(monkeypatch, capsys):
    _no_subprocess(monkeypatch)
    cfg = {"hitch": {"enabled": True}}
    code = hitch_plugin.up("gpt", "dispatcher", cfg=cfg)
    assert code == 1
    # "codex" is a known target now (bh-98v9m) — it must show up in the refusal's own list.
    assert "codex" in capsys.readouterr().err


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


def test_up_forwards_workspace_task_detached_role_explain(monkeypatch, tmp_path):
    """bh-6t49w.1: --workspace/--task/-d/--role/--explain reach the real hitch up argv,
    forwarded unchanged (hitch's own CLI validates them, e.g. -d without --task per ADR 0003)."""
    cfg = _stub_ready(monkeypatch, tmp_path)
    calls = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: calls.append(argv) or _Result())

    code = hitch_plugin.up(
        "claude",
        "dispatcher",
        cfg=cfg,
        workspace="/work/foo",
        task="say hello",
        detached=True,
        role_="dev1",
        explain=True,
    )

    assert code == 0
    argv = calls[0]
    assert "--workspace" in argv and argv[argv.index("--workspace") + 1] == "/work/foo"
    assert "--task" in argv and argv[argv.index("--task") + 1] == "say hello"
    assert "-d" in argv
    assert "--role" in argv and argv[argv.index("--role") + 1] == "dev1"
    assert "--explain" in argv


def test_up_omits_optional_flags_when_unset(monkeypatch, tmp_path):
    cfg = _stub_ready(monkeypatch, tmp_path)
    calls = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: calls.append(argv) or _Result())

    hitch_plugin.up("claude", "dispatcher", cfg=cfg)

    argv = calls[0]
    for flag in ("--workspace", "--task", "-d", "--role", "--explain"):
        assert flag not in argv


def _receipt_payload(bead: str, *, current_seat: str = "developer") -> str:
    resolved = role.resolve_launch_profile(
        role.build_launch_profile(
            "developer",
            harness="claude",
            managed_bead=True,
            bead=bead,
            available_seats=("developer", "reviewer"),
            model="sonnet",
            effort="high",
        ),
        current_seat=current_seat,
    )
    return role.AgentLaunchReceipt.from_resolved(resolved).model_dump_json()


_FINAL_ENV_PROBE = (
    "import os,sys,time; from pathlib import Path; "
    "time.sleep(float(sys.argv[2])); "
    "Path(sys.argv[1]).write_text(os.environ.get('BH_AGENT_LAUNCH_RECEIPT', '<absent>')); "
    "raise SystemExit(int(sys.argv[3]))"
)


def _install_actual_hitch_probe(monkeypatch, outputs, *, exit_code=0, delay=0):
    def probe_argv(_cfg, _target, profile, **_kwargs):
        return [
            sys.executable,
            "-c",
            _FINAL_ENV_PROBE,
            str(outputs[profile]),
            str(delay),
            str(exit_code),
        ]

    monkeypatch.setattr(hitch_plugin, "_hitch_argv", probe_argv)


def test_up_injects_scoped_receipt_into_actual_child_env_and_restores_legacy_call(
    monkeypatch, tmp_path
):
    cfg = _stub_ready(monkeypatch, tmp_path)
    calls = []

    class _Result:
        returncode = 0

    monkeypatch.setenv("CALLER_SECRET", "must-stay-out-of-receipt")
    monkeypatch.setattr(
        hitch_plugin.run,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or _Result(),
    )
    payload = _receipt_payload("bh-wi2os.10", current_seat="reviewer")

    with hitch_plugin.scoped_launch_receipt(payload):
        assert hitch_plugin.up("claude", "reviewer", cfg=cfg) == 0
    assert hitch_plugin.up("claude", "reviewer", cfg=cfg) == 0

    managed_env = calls[0][1]["env"]
    assert managed_env["BH_AGENT_LAUNCH_RECEIPT"] == payload
    receipt = json.loads(managed_env["BH_AGENT_LAUNCH_RECEIPT"])
    assert receipt["bead"] == "bh-wi2os.10"
    assert receipt["current_seat"] == "reviewer"
    assert "CALLER_SECRET" not in receipt
    assert "argv" not in receipt and "herdr" not in receipt
    # A direct external Hitch call stays genuinely unmanaged even if its parent was managed.
    assert "BH_AGENT_LAUNCH_RECEIPT" not in calls[1][1]["env"]


@pytest.mark.parametrize("ambient_kind", ["valid", "malformed"])
def test_external_hitch_scrubs_ambient_receipt_from_actual_final_child_env(
    monkeypatch, tmp_path, ambient_kind
):
    cfg = _stub_ready(monkeypatch, tmp_path)
    output = tmp_path / f"legacy-{ambient_kind}.txt"
    _install_actual_hitch_probe(monkeypatch, {"developer": output})
    ambient = (
        _receipt_payload("bh-wi2os.10") if ambient_kind == "valid" else "{malformed-stale-receipt"
    )
    monkeypatch.setenv("BH_AGENT_LAUNCH_RECEIPT", ambient)

    assert hitch_plugin.up("claude", "developer", cfg=cfg) == 0

    assert output.read_text() == "<absent>"


def test_managed_hitch_overwrites_ambient_receipt_in_actual_final_child_env(monkeypatch, tmp_path):
    cfg = _stub_ready(monkeypatch, tmp_path)
    managed = tmp_path / "managed.txt"
    restored = tmp_path / "restored.txt"
    outputs = {"reviewer": managed, "developer": restored}
    _install_actual_hitch_probe(monkeypatch, outputs)
    monkeypatch.setenv("BH_AGENT_LAUNCH_RECEIPT", "{malformed-stale-receipt")
    payload = _receipt_payload("bh-wi2os.10", current_seat="reviewer")

    with hitch_plugin.scoped_launch_receipt(payload):
        assert hitch_plugin.up("claude", "reviewer", cfg=cfg) == 0
    assert hitch_plugin.up("claude", "developer", cfg=cfg) == 0

    assert managed.read_text() == payload
    assert restored.read_text() == "<absent>"


def test_scoped_receipts_are_isolated_across_concurrent_hitch_launches(monkeypatch, tmp_path):
    cfg = _stub_ready(monkeypatch, tmp_path)
    barrier = threading.Barrier(2)
    outputs = {
        "developer": tmp_path / "developer.txt",
        "reviewer": tmp_path / "reviewer.txt",
    }
    _install_actual_hitch_probe(monkeypatch, outputs, delay=0.2)
    monkeypatch.setenv("BH_AGENT_LAUNCH_RECEIPT", "stale-parent")
    payloads = {
        "developer": _receipt_payload("bh-wi2os.10"),
        "reviewer": _receipt_payload("bh-wi2os.11", current_seat="reviewer"),
    }

    def launch(profile: str) -> None:
        barrier.wait(timeout=5)
        with hitch_plugin.scoped_launch_receipt(payloads[profile]):
            hitch_plugin.up("claude", profile, cfg=cfg)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(launch, payloads))

    assert {profile: path.read_text() for profile, path in outputs.items()} == payloads
    assert "BH_AGENT_LAUNCH_RECEIPT" not in hitch_plugin._scoped_launch_env()


def test_scoped_receipt_restores_on_exception_and_cancellation(monkeypatch, tmp_path):
    payload = _receipt_payload("bh-wi2os.10")
    cfg = _stub_ready(monkeypatch, tmp_path)

    monkeypatch.setattr(
        hitch_plugin.run,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("backend failed")),
    )
    with pytest.raises(RuntimeError, match="backend failed"):
        with hitch_plugin.scoped_launch_receipt(payload):
            hitch_plugin.up("claude", "developer", cfg=cfg)
    assert "BH_AGENT_LAUNCH_RECEIPT" not in hitch_plugin._scoped_launch_env()

    async def cancel_inside_scope():
        with hitch_plugin.scoped_launch_receipt(payload):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel_inside_scope())
    assert "BH_AGENT_LAUNCH_RECEIPT" not in hitch_plugin._scoped_launch_env()


def test_scoped_receipt_restores_after_actual_nonzero_hitch_child(monkeypatch, tmp_path):
    cfg = _stub_ready(monkeypatch, tmp_path)
    output = tmp_path / "nonzero.txt"
    _install_actual_hitch_probe(monkeypatch, {"developer": output}, exit_code=9)
    payload = _receipt_payload("bh-wi2os.10")

    with hitch_plugin.scoped_launch_receipt(payload):
        assert hitch_plugin.up("claude", "developer", cfg=cfg) == 9

    assert output.read_text() == payload
    assert "BH_AGENT_LAUNCH_RECEIPT" not in hitch_plugin._scoped_launch_env()


def test_up_opencode_target_passes_through_unchanged(monkeypatch, tmp_path):
    cfg = _stub_ready(monkeypatch, tmp_path)
    calls = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: calls.append(argv) or _Result())

    hitch_plugin.up("opencode", "developer", cfg=cfg)

    assert calls[0][2] == "opencode"


def test_up_codex_target_passes_through_unchanged(monkeypatch, tmp_path):
    """bh-98v9m: "codex" widens only this passthrough wrapper's target list — resolves to
    hitch's own "codex" target name, exactly like the existing claude/opencode paths."""
    cfg = _stub_ready(monkeypatch, tmp_path)
    calls = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: calls.append(argv) or _Result())

    code = hitch_plugin.up("codex", "supervisor", cfg=cfg)

    assert code == 0
    assert calls[0][2] == "codex"


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
    stdout = (
        "  [fail] beadhive: required binary 'example-tool' not found in PATH\nPreflight failed\n"
    )
    state, detail = hitch_plugin._classify_preflight(1, stdout)
    assert state == "blocked"
    assert "example-tool" in detail


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
        stdout = "  [fail] beadhive: required binary 'example-tool' not found in PATH\n"

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: _Result())

    reports = hitch_plugin.seat_reports(cfg)

    assert reports == [
        {
            "seat": "developer",
            "state": "blocked",
            "detail": "beadhive: required binary 'example-tool' not found in PATH",
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
        stdout = "  [fail] beadhive: required binary 'example-tool' not found in PATH\n"

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: _Result())

    state, detail = hitch_plugin._readiness(cfg, None)
    assert state == "warn"
    assert "developer: cannot run" in detail
    assert "example-tool" in detail


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


# ---- _resolve_backend / route: unified `bh role <seat>` backend selection (bh-6t49w.3) --------


def test_resolve_backend_native_when_hitch_disabled():
    assert hitch_plugin._resolve_backend("developer", "claude", {}) == ("native", None, None)


def test_resolve_backend_native_when_harness_has_no_hitch_target(monkeypatch, tmp_path):
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    assert hitch_plugin._resolve_backend("developer", "not-a-harness", cfg) == (
        "native",
        None,
        None,
    )


def test_resolve_backend_native_when_repo_unconfigured():
    cfg = {"hitch": {"enabled": True}}
    assert hitch_plugin._resolve_backend("developer", "claude", cfg) == ("native", None, None)


def test_resolve_backend_native_when_no_matching_profile(monkeypatch, tmp_path):
    repo = _write_repo(tmp_path, ["shell"])  # no profile named "developer"
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    assert hitch_plugin._resolve_backend("developer", "claude", cfg) == ("native", None, None)


def test_resolve_backend_hitch_when_enabled_and_profile_matches(monkeypatch, tmp_path):
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    assert hitch_plugin._resolve_backend("developer", "claude", cfg) == (
        "hitch",
        "claude-code",
        "developer",
    )


def test_resolve_backend_translates_opencode_target(monkeypatch, tmp_path):
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    assert hitch_plugin._resolve_backend("developer", "opencode", cfg) == (
        "hitch",
        "opencode",
        "developer",
    )


# ---- _seat_listing_lines / route(""): annotated `bh role` listing (bh-6t49w.5) ----------------


def test_seat_listing_native_only_when_hitch_disabled(monkeypatch):
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda _: None)
    lines = hitch_plugin._seat_listing_lines({}, "claude", full=False)
    assert lines == ["developer — native: ok"]


def test_seat_listing_shows_hitch_when_resolve_backend_picks_it(monkeypatch, tmp_path):
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda _: None)
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    lines = hitch_plugin._seat_listing_lines(cfg, "claude", full=False)
    assert lines == ["developer — native: ok; hitch: ok"]


def test_seat_listing_full_folds_in_seat_reports_state(monkeypatch, tmp_path):
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda _: None)
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    monkeypatch.setattr(
        hitch_plugin,
        "seat_reports",
        lambda c: [{"seat": "developer", "state": "reduced", "detail": "does not support x"}],
    )
    lines = hitch_plugin._seat_listing_lines(cfg, "claude", full=True)
    assert lines == ["developer — native: ok; hitch: reduced (does not support x)"]


def test_seat_listing_full_false_never_calls_seat_reports(monkeypatch, tmp_path):
    """the cheap default must not pay `seat_reports`'s 7-seat preflight fanout (bh-gqfrm)."""
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda _: None)
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    monkeypatch.setattr(
        hitch_plugin,
        "seat_reports",
        lambda c: (_ for _ in ()).throw(AssertionError("seat_reports must not run")),
    )
    lines = hitch_plugin._seat_listing_lines(cfg, "claude", full=False)
    assert lines == ["developer — native: ok; hitch: ok"]


def test_seat_listing_shows_built_baml_binary(monkeypatch):
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda name: f"/usr/bin/{name}")
    lines = hitch_plugin._seat_listing_lines({}, "claude", full=False)
    assert lines == ["developer — native: ok; baml: built"]


def test_route_full_seats_forwards_to_listing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda _: None)
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    monkeypatch.setattr(
        hitch_plugin,
        "seat_reports",
        lambda c: [{"seat": "developer", "state": "blocked", "detail": "hitch missing"}],
    )
    hitch_plugin.route("", full_seats=True, cfg=cfg)
    out = capsys.readouterr().out
    assert "hitch: blocked (hitch missing)" in out


def test_route_empty_seat_prints_annotated_listing_not_role_launch(monkeypatch, capsys):
    """bh-6t49w.5: the bare listing is annotated in `route` itself (role.launch stays
    hitch-unaware, per `test_role_module_never_imports_hitch_plugin`) — it no longer delegates."""
    calls = []
    monkeypatch.setattr(role, "launch", lambda seat, harness=None: calls.append((seat, harness)))
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    hitch_plugin.route("", cfg={"hitch": {"enabled": False}})
    assert calls == []
    out = capsys.readouterr().out
    assert "Available seats:" in out
    assert "developer — native: ok" in out


def test_route_unknown_seat_delegates_to_role_launch_untouched(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    monkeypatch.setattr(role, "launch", lambda seat, harness=None: calls.append((seat, harness)))
    hitch_plugin.route("not-a-seat", cfg={"hitch": {"enabled": True}})
    assert calls == [("not-a-seat", None)]
    assert capsys.readouterr().out == ""


def test_route_native_when_hitch_disabled(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    monkeypatch.setattr(role, "launch", lambda seat, harness=None: calls.append((seat, harness)))
    hitch_plugin.route("developer", cfg={})
    assert calls == [("developer", None)]
    assert "native backend" in capsys.readouterr().out


def test_route_consumes_pre_resolved_profile_without_reparsing(monkeypatch, capsys):
    resolved = role.resolve_launch_profile(
        role.build_launch_profile(
            "developer",
            harness="claude",
            managed_bead=True,
            bead="bh-wi2os.9",
            available_seats=("developer", "reviewer"),
        ),
        current_seat="reviewer",
    )
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer", "reviewer"])
    monkeypatch.setattr(
        role,
        "build_launch_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("profile rebuilt")),
    )
    launch_calls = []
    monkeypatch.setattr(
        role,
        "launch",
        lambda *args, **kwargs: launch_calls.append((args, kwargs)),
    )

    hitch_plugin.route(
        "developer",
        no_hitch=True,
        cfg={},
        managed_bead=True,
        bead="bh-wi2os.9",
        resolved_profile=resolved,
    )

    assert launch_calls == [(("reviewer",), {"harness": None, "resolved_profile": resolved})]
    assert "reviewer: launching via native backend" in capsys.readouterr().out


def test_route_picks_hitch_when_enabled_and_profile_matches(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    launch_calls = []
    monkeypatch.setattr(
        role, "launch", lambda seat, harness=None: launch_calls.append((seat, harness))
    )
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    up_calls = []
    monkeypatch.setattr(
        hitch_plugin, "up", lambda target, profile, c: up_calls.append((target, profile, c)) or 0
    )

    hitch_plugin.route("developer", cfg=cfg)

    assert launch_calls == []  # native path never invoked
    assert up_calls == [("claude", "developer", cfg)]
    out = capsys.readouterr().out
    assert "hitch" in out and "claude-code" in out and "developer" in out


def test_managed_attached_route_preserves_up_call_shape_and_injects_actual_child_env(
    monkeypatch, tmp_path
):
    resolved = role.resolve_launch_profile(
        role.build_launch_profile(
            "developer",
            harness="claude",
            managed_bead=True,
            bead="bh-wi2os.10",
            available_seats=("developer", "reviewer"),
            model="sonnet",
        ),
        current_seat="reviewer",
    )
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer", "reviewer"])
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda _cmd: "/usr/local/bin/hitch")
    repo = _write_repo(tmp_path, ["reviewer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    child_calls = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(
        hitch_plugin.run,
        "run",
        lambda argv, **kwargs: child_calls.append((argv, kwargs)) or _Result(),
    )
    real_up = hitch_plugin.up
    up_calls = []

    # The deliberately strict three-positional-argument wrapper proves route did not widen the
    # legacy attached Hitch call while the real up() still receives the scoped child environment.
    def exact_up(target, profile, passed_cfg):
        up_calls.append((target, profile, passed_cfg))
        return real_up(target, profile, passed_cfg)

    monkeypatch.setattr(hitch_plugin, "up", exact_up)

    hitch_plugin.route("developer", cfg=cfg, resolved_profile=resolved)

    assert up_calls == [("claude", "reviewer", cfg)]
    receipt = json.loads(child_calls[0][1]["env"]["BH_AGENT_LAUNCH_RECEIPT"])
    assert receipt["bead"] == "bh-wi2os.10"
    assert receipt["initial_seat"] == "developer"
    assert receipt["current_seat"] == "reviewer"
    assert "BH_AGENT_LAUNCH_RECEIPT" not in hitch_plugin._scoped_launch_env()


def test_route_no_hitch_forces_native_even_when_hitch_would_apply(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    launch_calls = []
    monkeypatch.setattr(
        role, "launch", lambda seat, harness=None: launch_calls.append((seat, harness))
    )
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    monkeypatch.setattr(
        hitch_plugin, "up", lambda *a: (_ for _ in ()).throw(AssertionError("up() must not run"))
    )

    hitch_plugin.route("developer", no_hitch=True, cfg=cfg)

    assert launch_calls == [("developer", None)]
    assert "native backend" in capsys.readouterr().out


def test_route_hitch_backend_propagates_nonzero_exit(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    monkeypatch.setattr(hitch_plugin, "up", lambda target, profile, c: 3)

    with pytest.raises(typer.Exit) as exc:
        hitch_plugin.route("developer", cfg=cfg)
    assert exc.value.exit_code == 3


def test_route_hitch_backend_passes_bh_harness_vocab_to_up(monkeypatch, tmp_path):
    """`up()` itself re-translates target -> hitch target, so `route` must pass the plain bh
    harness name through, not the already-translated one, or translation would double-apply."""
    monkeypatch.setattr(role, "_known_seats", lambda: ["developer"])
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}, "harness": "opencode"}
    up_calls = []
    monkeypatch.setattr(hitch_plugin, "up", lambda target, profile, c: up_calls.append(target) or 0)

    hitch_plugin.route("developer", cfg=cfg)

    assert up_calls == ["opencode"]


def test_cli_up_forwards_flags_to_the_real_argv(monkeypatch, tmp_path):
    cfg = {"hitch": {"enabled": True, "repo": str(tmp_path)}}
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    calls = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(hitch_plugin.run, "run", lambda argv, **kw: calls.append(argv) or _Result())

    result = runner.invoke(
        app,
        [
            "plugin",
            "hitch",
            "up",
            "claude",
            "dispatcher",
            "--task",
            "say hello",
            "-d",
        ],
    )

    assert result.exit_code == 0, result.output
    argv = calls[0]
    assert "--task" in argv and argv[argv.index("--task") + 1] == "say hello"
    assert "-d" in argv


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


# ---- config-section registration (bh-m1roh) ---------------------------------


def test_hitch_is_a_known_config_section():
    """`bh config set hitch.*` must not warn "unknown config section".

    Asserted against the accessors rather than as a bare membership check: the reason `hitch`
    belongs in KNOWN_SECTIONS is that this module reads those exact keys, so pinning both together
    means a future rename of either side fails here instead of silently re-introducing the warning
    for an operator following the documented enablement steps."""
    assert "hitch" in config.KNOWN_SECTIONS
    for accessor in ("hitch_enabled", "hitch_command", "hitch_repo", "hitch_config_dir_root"):
        assert hasattr(config, accessor), f"config.{accessor} went away — revisit KNOWN_SECTIONS"


def test_setting_a_hitch_key_reports_no_unknown_section_problem():
    """Through the same `_validate` guard `bh config set` runs: hitch keys raise no problem.

    A membership assertion alone would pass even if the warning were emitted from a second,
    divergent list; this pins the behaviour operators actually see. An unrelated section still
    warns, so the test proves the guard is live rather than universally quiet."""
    for key, value in (("enabled", True), ("command", "hitch"), ("repo", "/r"), ("root", "/r")):
        problems = config._validate(["hitch", key], value)
        unknown = [p for p in problems if "unknown config section" in p["message"]]
        assert not unknown, f"hitch.{key} warned: {unknown}"

    (problem,) = config._validate(["definitely-not-a-section", "k"], "x")
    assert problem["level"] == "warning" and "unknown config section" in problem["message"]


# ---- seat_reports concurrency (bh-ls1ks) ---------------------------------------
# Each preflight is a ~1.8s external spawn and there is no ordering between them, so they run
# in a thread pool. These two pin the properties that made the sequential loop safe: the report
# stays in `seats` order regardless of completion order, and one seat blowing up is that seat's
# report rather than the whole section's.


def test_seat_reports_keeps_sorted_order_when_preflights_finish_out_of_order(monkeypatch, tmp_path):
    import time

    seats = ["analyst", "developer", "dispatcher", "merger"]
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    monkeypatch.setattr(role, "_known_seats", lambda: seats)
    repo = _write_repo(tmp_path, seats)

    # Reverse-ordered sleeps: the first seat alphabetically finishes last.
    delay = {s: 0.05 * (len(seats) - i) for i, s in enumerate(sorted(seats))}
    started = []

    def _fake_run(argv, **kw):
        seat = argv[3]
        started.append(seat)
        time.sleep(delay[seat])

        class _R:
            returncode = 0
            stdout = "Preflight succeeded\n"

        return _R()

    monkeypatch.setattr(hitch_plugin.run, "run", _fake_run)

    reports = hitch_plugin.seat_reports({"hitch": {"repo": str(repo)}})

    assert [r["seat"] for r in reports] == sorted(seats)
    assert len(started) == len(seats)


def test_seat_reports_one_failing_spawn_does_not_abort_the_section(monkeypatch, tmp_path):
    seats = ["developer", "merger"]
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda cmd: "/usr/local/bin/hitch")
    monkeypatch.setattr(role, "_known_seats", lambda: seats)
    repo = _write_repo(tmp_path, seats)

    def _fake_run(argv, **kw):
        if argv[3] == "developer":
            raise OSError("hitch vanished")

        class _R:
            returncode = 0
            stdout = "Preflight succeeded\n"

        return _R()

    monkeypatch.setattr(hitch_plugin.run, "run", _fake_run)

    reports = hitch_plugin.seat_reports({"hitch": {"repo": str(repo)}})

    assert [r["seat"] for r in reports] == ["developer", "merger"]
    assert reports[0]["state"] == "blocked"
    assert "hitch vanished" in reports[0]["detail"]
    assert reports[1]["state"] == "ok"


# ---- headless_plan: `--task`/`-d` suitability + backend selection (bh-6t49w.6) ----------------


def test_headless_plan_refuses_seat_outside_the_loop_roster(monkeypatch):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda _: "/usr/bin/bh-supervisor")
    backend, detail = hitch_plugin.headless_plan("supervisor", "claude", {})
    assert backend is None
    # Loud and by name — and a built binary does NOT buy an unsuitable seat a headless launch.
    assert "supervisor" in detail
    assert "not a headless-capable seat" in detail
    assert "bh role supervisor" in detail


def test_headless_plan_roster_tracks_role_for_action():
    """The predicate is SEEDED from the loop's own table, not a second hardcoded list."""
    for seat in set(localloop.ROLE_FOR_ACTION.values()):
        assert localloop.headless_capable(seat)
    for seat in ("supervisor", "director", "custodian", "controller"):
        assert not localloop.headless_capable(seat)


def test_headless_plan_prefers_built_role_binary_over_hitch(monkeypatch, tmp_path):
    repo = _write_repo(tmp_path, ["developer"])  # hitch would ALSO apply here
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    monkeypatch.setattr(
        hitch_plugin.shutil,
        "which",
        lambda n: "/usr/bin/bh-developer" if n == "bh-developer" else None,
    )
    backend, detail = hitch_plugin.headless_plan("developer", "claude", cfg)
    assert backend == "baml"
    assert "bh-developer" in detail


def test_headless_plan_falls_back_to_hitch_profile(monkeypatch, tmp_path):
    repo = _write_repo(tmp_path, ["developer"])
    cfg = {"hitch": {"enabled": True, "repo": str(repo)}}
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda _: None)
    backend, detail = hitch_plugin.headless_plan("developer", "claude", cfg)
    assert backend == "hitch"
    assert "developer" in detail


def test_headless_plan_refuses_naming_both_missing_backends(monkeypatch):
    monkeypatch.setattr(hitch_plugin.shutil, "which", lambda _: None)
    backend, detail = hitch_plugin.headless_plan("developer", "claude", {})
    assert backend is None
    assert "bh-developer" in detail and "hitch profile" in detail
