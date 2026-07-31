"""`bh config init` provisions the internal workspace root (bh-cgcg.2).

Internal (the default): create `<bh home>/ws` and seed a minimal `workspace.toml`.
External (existing populated `~/workspace`, or explicit config): write nothing under the
internal root — that tree is the user's, not bh's.

Every test isolates the legacy `~/workspace` stand-in the same way test_workspace_root.py's
`_isolated` fixture does (bh-cgcg.1), so resolution never depends on — or risks matching
against — whatever the real machine running the suite happens to have under its actual home
directory. `BH_HOME` is already isolated per-test by the autouse `_sandbox_bh_home` fixture
(tests/conftest.py).
"""

from __future__ import annotations

import tomllib

import pytest
from typer.testing import CliRunner

from beadhive import config, gitworkspace, identity
from beadhive.cli import app

runner = CliRunner()


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    legacy = tmp_path / "home-workspace"
    monkeypatch.setattr(identity, "_legacy_root", lambda: legacy)
    return legacy


# ---- internal mode (default): create + seed -----------------------------------


def test_config_init_creates_internal_workspace_and_seeds_toml(_isolated):
    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0, result.output
    root = config.home() / "ws"
    assert identity.workspace_root() == str(root.resolve())
    assert root.is_dir()
    toml = root / "workspace.toml"
    assert toml.is_file()
    assert tomllib.loads(toml.read_text()).get("provider", []) == []
    assert f"wrote {root}" in result.output


def test_config_init_rerun_is_a_genuine_no_op(_isolated):
    """Idempotency is a hard requirement: re-running against an existing managed root must
    not duplicate provider entries, clobber, or truncate the toml."""
    runner.invoke(app, ["config", "init"])
    toml = config.home() / "ws" / "workspace.toml"
    before = toml.read_text()

    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0, result.output
    assert toml.read_text() == before  # byte-identical — never rewritten
    assert f"skip {config.home() / 'ws'} (already seeded)" in result.output


def test_config_init_force_still_never_touches_seeded_workspace_toml(_isolated):
    """--force overwrites the templated config files but must never clobber an already-seeded
    workspace.toml — mirrors host.yaml's own never-overwritten guarantee."""
    runner.invoke(app, ["config", "init"])
    toml = config.home() / "ws" / "workspace.toml"
    hand_edited = '[[provider]]\nprovider = "github"\nname = "acme"\npath = "github"\n'
    toml.write_text(hand_edited)

    result = runner.invoke(app, ["config", "init", "--force"])

    assert result.exit_code == 0, result.output
    assert toml.read_text() == hand_edited


def test_config_init_internal_root_matches_ws_sibling_of_worktrees_tree(_isolated):
    """The internal root is `<bh home>/ws` — a sibling of the worktrees shadow tree
    `<bh home>/wt`, per the epic's path-name decision (NOT `<bh home>/workspace`)."""
    runner.invoke(app, ["config", "init"])

    assert (config.home() / "ws").is_dir()
    assert not (config.home() / "workspace").exists()


# ---- external mode: write nothing under the internal root ---------------------


def test_config_init_external_mode_writes_nothing_under_internal_root(_isolated):
    """The legacy-populated guard (bh-cgcg.1): an existing, populated `~/workspace` resolves
    external with no config needed — the operator's own factory-orca host is a live instance
    of exactly this case. `bh config init` must not create anything under the internal root
    in that case."""
    (_isolated / "github" / "acme" / "api" / ".git").mkdir(parents=True)

    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0, result.output
    assert identity.workspace_mode() == "external"
    assert not (config.home() / "ws").exists()
    assert "external workspace" in result.output


def test_config_init_external_mode_via_explicit_config_writes_nothing(_isolated):
    """An explicit `git_workspace.mode: external` (today's opt-in, preserved intact) also
    writes nothing under the internal root, even on an otherwise-fresh machine."""
    config.home().mkdir(parents=True, exist_ok=True)
    cfg_path = config.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        "providers: [github]\nmanaged_repos: []\ngit_workspace:\n  mode: external\n"
    )

    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0, result.output
    assert identity.workspace_mode() == "external"
    assert not (config.home() / "ws").exists()


def test_config_init_external_mode_reports_existing_workspace_toml_path(_isolated):
    """External mode records the existing root + workspace*.toml path — informational only,
    no write."""
    (_isolated / "github" / "acme" / "api" / ".git").mkdir(parents=True)
    _isolated.mkdir(parents=True, exist_ok=True)
    (_isolated / "workspace.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "acme"\npath = "github"\n'
    )
    before = (_isolated / "workspace.toml").read_text()

    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0, result.output
    assert str(_isolated / "workspace.toml") in result.output
    assert (_isolated / "workspace.toml").read_text() == before  # untouched


# ---- gitworkspace.ensure_seeded is the single writer reused by config init -----


def test_config_init_reuses_gitworkspace_ensure_seeded(_isolated, monkeypatch):
    """DRY guard: `config init` must call the shared `ensure_seeded`, not re-implement its
    own create/seed logic — the single-writer contract idempotency depends on."""
    calls = []
    real = gitworkspace.ensure_seeded

    def spy(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(gitworkspace, "ensure_seeded", spy)

    runner.invoke(app, ["config", "init"])

    assert len(calls) == 1
