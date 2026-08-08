"""Shared pytest fixtures + markers for the AGF harness."""

from __future__ import annotations

import os

import pytest

from beadhive import otel
from harness.world import World, free_port, reap_dolt_server


@pytest.fixture(autouse=True)
def _sandbox_bh_home(tmp_path_factory, monkeypatch):
    """Every test gets an isolated `BH_HOME` so `config.home()` (and its one-time
    `~/.ws` -> `~/.beadhive` migration,) can NEVER resolve to — or mutate —
    the operator's real home directory. A test that merely imports `beadhive.config`, or
    invokes the CLI via `CliRunner`, must not be able to touch real state on the machine
    running the suite. Runs before every other fixture (defined first in this module).

    Uses `tmp_path_factory` (its own tmp root) rather than the test's own `tmp_path` — several
    tests scan/assert on the exact contents of their `tmp_path` (e.g. a directory-listing test),
    and a `bh-home` subdir nested inside it would show up as unexpected stray content.

    Also seeds a minimal `config.yaml` at the default path: a handful of tests call into a
    verb that loads config without setting up their own isolation (previously harmless only
    because it silently fell through to the *real* ~/.ws/config.yaml on a dev machine that
    happens to have one — exactly the kind of hidden real-state dependency this fixture exists
    to close off). Tests that need specific config content still set their own `BH_CONFIG` /
    `config.config_path` override, which simply wins over this default."""
    home = tmp_path_factory.mktemp("bh-home")
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.delenv("WS_HOME", raising=False)
    # Same reasoning for the in-image component manifest: running the suite INSIDE a Beadhive
    # image must not change what `bh setup check` does under test. Point it at a path that does
    # not exist, so live probing stays the default everywhere; the manifest tests set their own.
    monkeypatch.setenv("BH_IMAGE_MANIFEST", str(home / "absent-image-manifest.json"))
    (home / "config.yaml").write_text(
        "schema_version: 1\n"
        "providers: [github]\n"
        "managed_repos: []\n"
        "exclude:\n"
        "  orgs: []\n"
        "  repos: []\n"
        "otel:\n"
        "  enabled: false\n"
        "  protocol: grpc\n"
    )


@pytest.fixture(autouse=True)
def _sandbox_global_git_config(tmp_path_factory, monkeypatch):
    """Every test gets an isolated ``$GIT_CONFIG_GLOBAL`` (bh-ijd4) — the third sibling to
    :func:`_sandbox_bh_home` and :func:`_sandbox_workspace_root`, and the one whose absence
    would be the most damaging.

    ``git_identity`` writes the host's GLOBAL git config (``git config --global user.name`` and
    friends). Without this fixture, any test that reaches ``host_provision.provision`` or
    ``bh host identity`` would run those writes against the *operator's own* ``~/.gitconfig`` —
    silently editing the identity every commit on the machine is authored and signed with.

    ``git_identity`` is gap-fill-only, so on a fully configured machine the damage is nil and
    the suite passes either way. That is precisely why this must be a fixture and not a habit:
    the failure only appears on a machine with a PARTIAL git identity, which is exactly the
    provisioned-host case the feature exists for.

    ``GIT_CONFIG_GLOBAL`` (not ``HOME``) is the lever, because it is surgical: it redirects
    only what ``--global`` reads and writes, leaving ``~/.ssh`` probing and every other
    home-relative lookup honest. Seeded EMPTY so a test observes a bare host by default —
    a test that wants an existing identity writes it into this file itself."""
    cfg = tmp_path_factory.mktemp("git-global") / "gitconfig"
    cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))


@pytest.fixture(autouse=True)
def _sandbox_workspace_root(tmp_path_factory, monkeypatch):
    """Every test gets an isolated ``$GIT_WORKSPACE`` (bh-myp0) — the sibling hole to
    :func:`_sandbox_bh_home`, and the more expensive one.

    ``identity.workspace_root()`` reads ``$GIT_WORKSPACE`` (default ``~/workspace``) and does
    NOT consult config. So ``metadata.refresh``, which recomputes "the full on-disk fleet" from
    that root, walked the operator's REAL workspace on every test that reached it — even with
    ``managed_repos: []``. Profiling one ``runner.invoke(app, ["doctor"])`` found 84 ×
    ``safety.scan`` taking 26s against 125 real repos on the dev machine that surfaced this.

    Three things wrong with that, only one of which is speed:

    * the suite did real (read-only) git work inside repos that have nothing to do with it —
      exactly what ``_sandbox_bh_home`` exists to prevent, one env var over;
    * runtime scaled with the *developer's* repo count, so CI with an empty workspace ran fast
      and the problem stayed invisible;
    * results were not reproducible between machines.

    Tests needing a populated workspace still build one and point at it — via their own
    ``monkeypatch.setenv``/``registry.workspace_root`` override, which runs after this autouse
    default and simply wins, the same way ``BH_CONFIG`` overrides the seeded config above."""
    root = tmp_path_factory.mktemp("git-workspace")
    monkeypatch.setenv("GIT_WORKSPACE", str(root))


@pytest.fixture(autouse=True)
def _sandbox_shared_server(tmp_path_factory, monkeypatch):
    """Every test gets an isolated dolt shared-server target — the sibling hole to
    `_sandbox_bh_home`/`_sandbox_workspace_root` above, opened by bh-areg.7: a freshly-minted
    hive now defaults to `bd init --shared-server` / `bd bootstrap` with
    `BEADS_DOLT_SHARED_SERVER=1`, and bd resolves `BEADS_SHARED_SERVER_DIR`/
    `BEADS_DOLT_SERVER_PORT` from the ambient environment when unset — defaulting to
    `~/.beads/shared-server/` at the fixed port 3308, the OPERATOR'S REAL fleet server.
    Without this, any test that runs a real `bd init --shared-server`/`bd bootstrap` (e.g.
    `test_onboard_dag.py`, `test_hive_*.py` — hermetic in every OTHER respect, but never
    isolated from bd's own shared-server resolution before this bead added it) would connect
    to that real server and leave scratch databases on it — measured, not hypothetical: this
    is exactly what happened during this bead's own review cycle.

    A fresh ephemeral port per test also means concurrent `-n auto` workers, and a test run
    alongside a real fleet server already listening on 3308, never collide. Tests that need
    their OWN specific shared-server instance (the real-bd `integration` suite) still set
    their own `isolated_shared_server`-style fixture, which simply overrides this default the
    same way `BH_CONFIG` overrides `_sandbox_bh_home`'s seeded config."""
    shared = tmp_path_factory.mktemp("bh-shared-server")
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(shared))
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(free_port()))
    yield
    # Isolating the TARGET is only half of it (bh-cbou): a test that actually starts a server
    # here leaves it running, holding this port and this tmpdir after pytest deletes the dir
    # underneath it. Reaped for EVERY test, not just the real-bd ones, because this fixture is
    # autouse and so is the exposure — most tests never start one and the reap is then a no-op
    # statfile check. A finalizer, not a happy-path call, so a failing or interrupted test
    # cleans up too.
    reap_dolt_server(shared)


@pytest.fixture(autouse=True)
def _telemetry_neutral_env(monkeypatch):
    """Scrub telemetry config from the process env for every test so results never depend on — nor
    are skewed by — the operator's otel setup. Without this, a parent ``ws`` running the suite as
    its clean-checkout validation leaks ``OTEL_EXPORTER_OTLP_ENDPOINT`` (the worktree overlay /
    self-heal endpoint) into the child, and any test reading the otel endpoint (e.g. doctor's
    observability section) would see the ambient value instead of its expected default/config one.
    Suite-wide hermeticity replaces per-test ``delenv`` scrubbing; tests that need a telemetry var
    set it explicitly via ``monkeypatch`` (which runs after this autouse fixture).

    Also reset otel's process-global ``_initialized`` state: a test that calls ``otel.init()``
    without tearing down would otherwise leak ``_initialized=True`` into later tests, making the
    otel-off no-op tests (which assume the default off state) fail only in the full suite."""
    for key in list(os.environ):
        if key.startswith("OTEL_") or key in ("WS_OBSERVALOOP_PROFILE", "BH_OBSERVALOOP_PROFILE"):
            monkeypatch.delenv(key, raising=False)
    otel.shutdown()  # reset any _initialized state leaked from a prior test
    # Bypass the setup gate for all tests unless they explicitly clear this env var.
    # test_setup.py tests that exercise the gate use monkeypatch.delenv to remove it.
    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.delenv("WS_SKIP_SETUP_CHECK", raising=False)


@pytest.fixture(autouse=True)
def _logging_pipeline_keeps_caplogs_handler():
    """Stop ``log.configure()``'s one-time root-handler wipe from eating pytest's ``caplog``.

    ``log.configure()`` ends with ``root.handlers.clear()`` before installing bh's own handler,
    and runs **once per process** behind the ``log._configured`` guard. pytest's ``caplog``
    works by putting a ``LogCaptureHandler`` on that same root logger. So whichever test happens
    to be the first in its worker process to emit a bh diagnostic has its capture handler
    removed mid-test and sees ``caplog.records == []`` — while the record itself is plainly
    visible in that test's captured stderr.

    The failure is invisible until suite *composition* changes, because "which test is first"
    is decided by pytest-randomly's seed and xdist's distribution, not by anything in the test.
    It surfaced when bh-7daa6.6 added a test module and shifted the split; before that the two
    ``test_config.py`` schema-version tests happened to always land behind something that had
    already logged.

    Same class of process-global leak as ``_telemetry_neutral_env``'s ``otel.shutdown()`` above
    — and ``log.configure()``'s own docstring records a previous round of it (bh-lbcf, the
    pinned-stderr bug that "made a full-suite run fail a test that passed alone, because alone
    it *was* the first"). Fixed the same way: neutralize it per test rather than let suite
    composition decide.

    Done here rather than in ``pytest_configure`` deliberately: ``configure()`` reads bh config,
    and at ``pytest_configure`` time the ``BH_HOME`` sandbox above is not yet in place, so
    warming up there would read the operator's real ``config.yaml`` — precisely the real-state
    dependency this module exists to close off.
    """
    import logging

    from beadhive import log

    root = logging.getLogger()
    before = list(root.handlers)
    log.get_logger("conftest-warmup")  # triggers the one-time configure(), if it has not run
    for handler in before:
        # Re-seat whatever the wipe took (pytest's, on the first test through here in a worker).
        # bh's own handler stays exactly where configure() put it — this restores, never reorders.
        if handler not in root.handlers:
            root.addHandler(handler)


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    """A World, with its shared-server target reaped afterwards.

    The reap is normally a no-op statfile check twice over: a World inherits
    `_sandbox_shared_server`'s dir when that autouse fixture supplied one, and that fixture
    reaps it too. It matters for the case where a World minted its own (no autouse fixture in
    play) — a test that started a real dolt server would otherwise leave it holding a port and
    a tmpdir pytest has already deleted, the exact leak bh-cbou measured at 16 stray servers.
    """
    w = World(tmp_path, monkeypatch)
    yield w
    reap_dolt_server(w.shared_server)


@pytest.fixture
def fake_plugin(tmp_path, monkeypatch):
    """BH_PLUGIN_DIR → a minimal plugin tree (skills/ + agents/). The bh plugin is no longer
    vendored in this repo (beadhive/claude-plugin is canonical), so tests that need a real
    skills/agents source supply their own."""
    root = tmp_path / "fake-plugin"
    (root / "skills" / "demo-skill").mkdir(parents=True)
    (root / "skills" / "demo-skill" / "SKILL.md").write_text("skill\n")
    (root / "agents").mkdir()
    (root / "agents" / "developer.md").write_text("agent\n")
    monkeypatch.setenv("BH_PLUGIN_DIR", str(root))
    return root
