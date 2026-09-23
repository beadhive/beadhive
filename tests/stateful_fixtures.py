"""Explicit stateful fixtures for the existing integration-shaped test closure.

This compatibility plugin is loaded by the side-effect-free root conftest only when the module
is owned by the test sandbox, and every fixture in it is lazy.  Native validation always owns
the module.  A qualified pure-unit sandbox may exclude it, removing the stateful fixture edge
entirely while keeping plugin registration static and early.
``pytest_collection_modifyitems`` is the compatibility inventory that requests the aggregate
scope for existing test locations.  New pure unit tests live under ``tests/unit`` and must name
any stateful scope they need instead of inheriting one accidentally.
"""

from __future__ import annotations

import getpass
import hashlib
import importlib.abc
import importlib.metadata
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from harness.world import World


# Checked compatibility inventory.  Existing flat tests still exercise composition and adapters,
# so they explicitly receive the aggregate scope here while pure module tests graduate into the
# ``tests/unit`` subtree without it.  Narrow a consumer by moving it into that subtree and naming
# only the concern fixtures it actually needs.
STATEFUL_CONSUMER_ROOTS = ("tests/",)
PURE_CONSUMER_ROOTS = ("tests/unit/",)

# Reviewed bh-7ks1c.7 inventory.  Keeping the classification next to collection makes adding a
# new real-server module without deciding its freshness contract a testable error.
DOLT_SERVER_FRESHNESS = {
    "tests/test_bd_repo_sync_additive.py": ("fresh", "single case; no startup to amortize"),
    "tests/test_coordination_int.py": ("fresh", "owned-mode lifecycle and reclaim"),
    "tests/test_dolt_health_real_server_int.py": ("fresh", "endpoint lifecycle probe"),
    "tests/test_frame_bridge_live.py": ("fresh", "whole real-process composition"),
    "tests/test_host_fence_int.py": ("fresh", "embedded/shared transport boundary"),
    "tests/test_hq.py": ("fresh", "single real-store case; no startup to amortize"),
    "tests/test_hq_backup_server_mode_int.py": ("fresh", "destroy/restore two owned servers"),
    "tests/test_hub_bulk_int.py": ("mixed", "per-test contracts in HUB_BULK_FRESHNESS"),
    "tests/test_hub_rebuild.py": ("fresh", "destructive aggregate rebuild and prune"),
    "tests/test_onboard_server_mode_int.py": ("fresh", "startup and busy-port lifecycle"),
    "tests/test_storage_migrate_int.py": ("fresh", "embedded-to-server migration lifecycle"),
}

HUB_BULK_FRESHNESS = {
    "test_bulk_copy_matches_a_real_bd_produced_aggregate": (
        "reusable",
        "isolated namespaced source and aggregate databases",
    ),
    "test_hub_sync_row_counts_are_non_decreasing_per_prefix_across_a_sync": (
        "fresh",
        "mutates the reserved hub database through destructive sync/rebuild behavior",
    ),
    "test_co_located_database_and_server_databases_against_the_real_server": (
        "reusable",
        "read-only discovery over isolated namespaced databases",
    ),
}


def _interleave_dolt_items(items, slots):
    """Keep ready work behind every bounded wave of real-server tests."""
    if slots <= 0:
        return list(items)
    marked = [item for item in items if item.get_closest_marker("dolt_server") is not None]
    ordinary = [item for item in items if item.get_closest_marker("dolt_server") is None]
    if not marked or not ordinary:
        return list(items)
    scheduled = []
    while marked or ordinary:
        scheduled.extend(marked[:slots])
        del marked[:slots]
        scheduled.extend(ordinary[:slots])
        del ordinary[:slots]
    return scheduled


def pytest_collection_modifyitems(config, items):
    """Attach the inventoried compatibility scope, leaving pure-unit collection untouched."""
    for item in items:
        relative = item.path.relative_to(config.rootpath).as_posix()
        if relative.startswith(PURE_CONSUMER_ROOTS):
            item.fixturenames.insert(0, "pure_test_scope")
            continue
        if relative.startswith(STATEFUL_CONSUMER_ROOTS):
            # Compatibility defaults must establish their empty baseline before an explicit
            # ``world`` or per-test fixture overrides it, matching pytest's former autouse order.
            item.fixturenames.insert(0, "legacy_stateful_test_scope")
    slots = int(os.environ.get("BH_DOLT_SLOTS", "4"))
    items[:] = _interleave_dolt_items(items, slots)


@pytest.fixture
def pure_test_scope(monkeypatch, request):
    """Fail pure tests at the missing dependency with the explicit scope they must request."""
    requested_scopes = set(request.fixturenames)
    import_scopes = {
        "beadhive.config": "config_test_scope",
        "beadhive.dolt": "dolt_test_scope",
        "beadhive.plugins": "plugin_test_scope",
        "beadhive.otel": "telemetry_test_scope",
        "beadhive.run": "runtime_test_scope",
    }
    guarded_imports = {
        prefix: scope for prefix, scope in import_scopes.items() if scope not in requested_scopes
    }

    class DiagnoseOuterLayer(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            for prefix, scope in guarded_imports.items():
                if fullname.startswith(prefix):
                    raise AssertionError(
                        f"pure test imported {fullname}; request the explicit {scope} fixture"
                    )
            return None

    def refuse(action: str, scope: str):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"pure test attempted {action}; request {scope}")

        return fail

    finder = DiagnoseOuterLayer()
    sys.meta_path.insert(0, finder)
    if "config_test_scope" not in requested_scopes:
        monkeypatch.setattr(
            Path, "home", refuse("operator home/config access", "config_test_scope")
        )
    if "plugin_test_scope" not in requested_scopes:
        monkeypatch.setattr(
            importlib.metadata, "entry_points", refuse("plugin discovery", "plugin_test_scope")
        )
    if "dolt_test_scope" not in requested_scopes:
        monkeypatch.setattr(
            socket, "create_connection", refuse("Dolt/network access", "dolt_test_scope")
        )
    if "runtime_test_scope" not in requested_scopes:
        monkeypatch.setattr(subprocess, "Popen", refuse("process spawn", "runtime_test_scope"))
        monkeypatch.setattr(subprocess, "run", refuse("process spawn", "runtime_test_scope"))
        monkeypatch.setattr(os, "fork", refuse("process fork", "runtime_test_scope"))
        monkeypatch.setattr(
            threading.Thread, "start", refuse("runtime thread start", "runtime_test_scope")
        )
    yield
    if finder in sys.meta_path:
        sys.meta_path.remove(finder)


@pytest.fixture
def _reject_local_runtime_thread_leaks():
    """Name the owning test when a LocalRuntime forgets to close its private loop."""
    before = {thread for thread in threading.enumerate() if thread.is_alive()}
    yield
    leaked = [
        thread
        for thread in threading.enumerate()
        if thread.is_alive() and thread not in before and thread.name == "bh-local-runtime"
    ]
    assert not leaked, "LocalRuntime leaked private event-loop thread(s); call close()"


def _pytest_tmp_root(config):
    """The `pytest-of-<user>` root holding EVERY session's numbered tmp dir, ours included.

    `config._tmp_path_factory` only exists once the tmpdir plugin has configured, which is AFTER
    this conftest at session start (measured: `AttributeError` in `pytest_configure`, present by
    `pytest_unconfigure`), so the conventional location is reconstructed when it is absent. The
    root, not our own `pytest-N` dir: the point is to see the sessions that came BEFORE."""
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        return factory.getbasetemp().parent
    if getattr(config.option, "basetemp", None):
        return Path(config.option.basetemp).parent
    return Path(tempfile.gettempdir()) / f"pytest-of-{getpass.getuser()}"


def _sweep(config, when: str) -> None:
    """Reap orphaned dolt sql-servers under the pytest tmp root, and SAY what was reaped.

    Controller-only (`workerinput` is set on an xdist worker): the sweep is a session-level
    backstop, so running it 24 times under `-n auto` would be noise, and `pytest_configure` fires
    on the controller before any worker starts. Never fatal — a backstop that can fail a run it
    exists to protect is a worse trade than the leak."""
    from harness.world import sweep_orphaned_dolt_servers

    if hasattr(config, "workerinput"):
        return
    try:
        killed = sweep_orphaned_dolt_servers(_pytest_tmp_root(config))
    except Exception as exc:  # noqa: BLE001 — see the docstring: never fatal
        print(f"dolt sweep ({when}) skipped: {type(exc).__name__}: {exc}")
        return
    if killed:
        print(f"dolt sweep ({when}): reaped {len(killed)} orphaned sql-server(s)")
        for pid, cfg in killed:
            print(f"  pid {pid} -> {cfg} (config path no longer exists)")


def pytest_configure(config):
    """Run the prior-session Dolt backstop once on the xdist controller before workers start."""
    _sweep(config, "session start")
    if not hasattr(config, "workerinput") and "BH_DOLT_SLOT_EVENTS" not in os.environ:
        path = Path(tempfile.gettempdir()) / f"bh-dolt-slot-events-{os.getpid()}.jsonl"
        os.environ["BH_DOLT_SLOT_EVENTS"] = str(path)
        config._bh_dolt_slot_events_owned = path
    if not hasattr(config, "workerinput"):
        from harness.world import free_port

        reusable = Path(tempfile.gettempdir()) / f"bh-reusable-dolt-{os.getpid()}"
        os.environ["BH_REUSABLE_DOLT_DIR"] = str(reusable)
        os.environ["BH_REUSABLE_DOLT_PORT"] = str(free_port())
        config._bh_reusable_dolt_owned = reusable


def pytest_unconfigure(config):
    """Run the final Dolt backstop once on the xdist controller after workers finish."""
    _sweep(config, "session end")
    owned = getattr(config, "_bh_dolt_slot_events_owned", None)
    if owned is not None:
        owned.unlink(missing_ok=True)
        os.environ.pop("BH_DOLT_SLOT_EVENTS", None)
    reusable = getattr(config, "_bh_reusable_dolt_owned", None)
    if reusable is not None:
        _cleanup_reusable_dolt_server(reusable)
        os.environ.pop("BH_REUSABLE_DOLT_DIR", None)
        os.environ.pop("BH_REUSABLE_DOLT_PORT", None)


@pytest.fixture
def _bound_concurrent_dolt_servers(request):
    """A test marked `dolt_server` holds one of `MAX_CONCURRENT_DOLT_SERVER_TESTS` run-wide slots
    for its whole duration (bh-wa3ch).

    THE MARKER IS THE DECLARATION. `-n auto` is 24 workers for 54 integration tests here, and
    before this nothing bounded how many real sql-servers they stood up at once — measured at 16
    concurrent in an unbounded fenced run, with no lock, no xdist group and no ceiling. A test
    that starts a real server says so with `pytest.mark.dolt_server` (usually file-level, in
    `pytestmark`), and this autouse fixture is where that declaration turns into a bound. Every
    other test is untouched and pays nothing — `request.node.get_closest_marker` is a dict lookup.

    Deliberately NOT `--dist loadgroup` + `xdist_group`: that bounds only a run invoked with the
    flag, so a bare `pytest -n auto` or a single-file run would be unbounded again. The slot is a
    `flock`, so it binds every invocation and the kernel frees it if a worker dies."""
    from harness.world import MAX_CONCURRENT_DOLT_SERVER_TESTS, dolt_server_slot

    if request.node.get_closest_marker("dolt_server") is None:
        yield
        return
    with dolt_server_slot(MAX_CONCURRENT_DOLT_SERVER_TESTS, request.node.nodeid):
        yield


@pytest.fixture
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
    # `bd` loads its own global config from HOME (and XDG_CONFIG_HOME), independently of
    # Beadhive's BH_HOME. Keep a developer's global Beads configuration from changing fixture
    # behavior, particularly embedded-vs-shared-server initialization.
    # The outer bubblewrap fence already replaces HOME with its own tmpfs mount. Preserve that
    # mount (and the UV cache it explicitly rebinds) when fenced; direct pytest runs need the
    # per-test HOME because they have no process-level isolation.
    if os.environ.get("BH_HERMETIC_FENCE") != "1":
        monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
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


@pytest.fixture
def _sandbox_claude_home(tmp_path_factory, monkeypatch):
    """Every test gets an isolated ``$BH_CLAUDE_HOME`` (bh-nvv66) — the sibling hole to
    :func:`_sandbox_bh_home`, and the one that made a test's verdict depend on the DEVELOPER.

    ``hive._is_plugin_installed`` and ``hive._known_marketplace_path`` read Claude Code's own
    registries under ``~/.claude/plugins/``. Until this fixture, both went through ``Path.home()``
    with no override, so ``bh hive ready``'s skills and agents checks answered "is the bh plugin
    installed FOR THE PERSON RUNNING THE SUITE" — and
    ``test_zero_footprint_hive_is_ready_without_repo_files`` was green only because the machine it
    was written on happened to have it. Measured: that test fails inside the bubblewrap fence
    (tmpfs HOME), and would fail identically in CI, a container, or on a fresh host.

    Seeded EMPTY, so a test observes a machine with NO plugins installed by default. That is the
    honest default — the ambient case is the unusual one — and it means a test that needs an
    install says so by writing the registry itself (see ``tests/test_hive_ready.py``), which is
    also the only way that code path gets exercised deterministically at all."""
    monkeypatch.setenv("BH_CLAUDE_HOME", str(tmp_path_factory.mktemp("claude-home")))


@pytest.fixture
def _sandbox_codex_home(tmp_path_factory, monkeypatch):
    """Every test gets an isolated ``$BH_CODEX_HOME`` (bh-n0m7n) — the Codex sibling to
    :func:`_sandbox_claude_home`. `hive._install_global_codex_sandbox_grant` and
    `hive.global_codex_grant_is_current` read/write Codex's ambient ``~/.codex/config.toml``
    (`config.codex_home()`); without this, a test exercising the global grant would read or
    write the OPERATOR's real Codex config — including its `[projects."<path>"]` trust
    records — same real-state risk bh-nvv66 closed off for Claude."""
    monkeypatch.setenv("BH_CODEX_HOME", str(tmp_path_factory.mktemp("codex-home")))


@pytest.fixture
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
    home-relative lookup honest. It has no identity, so a test observes a bare host by default;
    its sole seed points ``core.excludesFile`` at an empty sibling file. Git otherwise falls back
    to the operator's ``$XDG_CONFIG_HOME/git/ignore`` independently of ``GIT_CONFIG_GLOBAL``,
    making hermetic fixture setup depend on personal ignore state (bh-idn2c). A test that wants
    existing identity or excludes writes them into this isolated file itself."""
    root = tmp_path_factory.mktemp("git-global")
    cfg = root / "gitconfig"
    excludes = root / "excludes"
    excludes.write_text("")
    cfg.write_text(f"[core]\n\texcludesFile = {excludes}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))


@pytest.fixture
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


@pytest.fixture
def _sandbox_worktree_root_override(monkeypatch):
    """Do not let the operator's worktree transport override enter a test.

    A gate launched from a non-default persistent root legitimately carries ``BH_WORKTREES`` so
    ``bh work check`` can locate its target.  That parent-only locator used to flow into pytest,
    outrank every test's legacy ``WS_WORKTREES`` sandbox, and route hundreds of fake lifecycle
    operations back to the operator's real root.  Individual tests remain free to set either
    spelling after this autouse baseline.
    """
    monkeypatch.delenv("BH_WORKTREES", raising=False)
    monkeypatch.delenv("WS_WORKTREES", raising=False)


@pytest.fixture
def _sandbox_validation_host(tmp_path_factory, monkeypatch):
    """Give every test its own simulated validation host and admission semaphore.

    A clean-checkout gate legitimately holds a permit from the operator host while it runs this
    suite. Tests that exercise the real CLI must not contend with that outer permit: their host is
    a fixture, just like their BH_HOME, workspace, Git config, and shared Dolt server. Child
    processes inherit this per-test root, so real contention and owner-death tests still exercise
    production flock behavior inside their sandbox. Tests needing a particular root or capacity
    may explicitly override either variable after this baseline.
    """
    root = tmp_path_factory.mktemp("validation-host")
    monkeypatch.setenv("BH_VALIDATION_SLOT_ROOT", str(root))
    monkeypatch.delenv("BH_VALIDATION_SLOTS", raising=False)


@pytest.fixture
def _fresh_bd_version_memo():
    """`dolt_health._local_bd_version_string` is memoized for the process (bh-i6e5g: `bh doctor`
    spawned `bd --version` 12 times per run, 1.30 s of pure repetition). A process-lifetime memo
    is a shared mutable in a test *process*, so it is cleared per test — same argument as
    `_unsealed_ledger` below."""
    from beadhive import dolt_health

    memo = dolt_health._local_bd_version_string
    memo.cache_clear()
    yield
    # Clear the object this fixture observed even when the test replaces the module attribute.
    # The explicit monkeypatch fixture now outlives this concern scope by dependency, unlike the
    # former independent autouse ordering.
    memo.cache_clear()


@pytest.fixture
def _clear_git_fact_caches():
    """Every test starts with the process-lifetime git-fact caches EMPTY (bh-z31lc).

    `identity.workspace_identity` and `gitauth._get_regexp` are `lru_cache`d because they fork
    git to read a fact that cannot change while one CLI verb runs. A test PROCESS is not one
    verb: it runs hundreds, each with its own monkeypatched `run` and its own tmp_path. Without
    this, a value cached under one test's fake git is served to the next test — which is not a
    hypothetical, it is how this fixture got written (two `test_gitauth` tests passed alone and
    failed in file order).

    Cleared here rather than in each test: the hazard belongs to the cache, not to whichever
    test happens to trip over it next.
    """
    from beadhive import gitauth, identity

    workspace_identity = identity.workspace_identity
    get_regexp = gitauth._get_regexp
    workspace_identity.cache_clear()
    get_regexp.cache_clear()
    yield
    workspace_identity.cache_clear()
    get_regexp.cache_clear()


@pytest.fixture
def _unsealed_ledger(monkeypatch):
    """Every test starts with the verdict ledger UNSEALED (bh-ku9n9.8).

    `validation_ledger.seal_subset_run` latches a process-global for the life of the process on
    purpose — a converged result must never become an attestation, and an un-clearable flag is
    what makes that structural rather than a convention. In a test *process* that is a shared
    mutable: one test that converges would otherwise silently stop every later test on the same
    xdist worker from recording a verdict, and which tests those are depends on the shard. So the
    latch is reset per test here, in the one place a reset is legitimate, rather than by giving
    production code a clear-the-seal function that exists only for tests."""
    from beadhive import validation_ledger

    monkeypatch.setattr(validation_ledger, "_SEALED", False)


@pytest.fixture
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
    from harness.world import free_port, reap_dolt_server

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


class ReusableDoltServer:
    """One run-owned server plus collision-proof per-test database names."""

    def __init__(self, port: int, suffix: str):
        self.port = port
        self.suffix = suffix
        self.databases: set[str] = set()

    def database(self, prefix: str) -> str:
        name = f"{prefix}{self.suffix}"
        self.databases.add(name)
        return name


def _cleanup_reusable_dolt_server(server_dir, reap=None):
    """Reap the run-owned process and discard all controller-owned startup state."""
    if reap is None:
        from harness.world import reap_dolt_server

        reap = reap_dolt_server
    reap(server_dir)
    shutil.rmtree(server_dir.with_name(f"{server_dir.name}-bootstrap"), ignore_errors=True)
    server_dir.with_name(f"{server_dir.name}.startup.lock").unlink(missing_ok=True)


def _recover_and_start_reusable_dolt_server(server_dir, run_cmd=None, reap=None):
    """Recover a killed worker's partial state before starting its replacement server."""
    if run_cmd is None:
        from beadhive.run import run

        run_cmd = run
    if reap is None:
        from harness.world import reap_dolt_server

        reap = reap_dolt_server
    bootstrap = server_dir.with_name(f"{server_dir.name}-bootstrap")
    reap(server_dir)
    shutil.rmtree(bootstrap, ignore_errors=True)
    bootstrap.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            "bd",
            "init",
            "--prefix",
            "bhboot",
            "--shared-server",
            "--skip-agents",
            "--skip-hooks",
            "--non-interactive",
        ],
        cwd=str(bootstrap),
        check=True,
        capture=True,
        timeout=60,
    )


def _ensure_reusable_dolt_server(server_dir, port, start, connect=socket.create_connection):
    """Serialize the first start across xdist workers and wait until it accepts connections."""
    lock_path = server_dir.with_name(f"{server_dir.name}.startup.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as startup_lock:
        import fcntl

        fcntl.flock(startup_lock.fileno(), fcntl.LOCK_EX)
        try:
            with connect(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            start()
        deadline = time.monotonic() + 30
        while True:
            try:
                with connect(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"reusable Dolt server did not accept port {port}") from None
                time.sleep(0.1)


@pytest.fixture
def reusable_dolt_server(request, monkeypatch):
    """Share startup across compatible tests while isolating and deleting every database."""
    from beadhive.run import run

    server_dir = Path(os.environ["BH_REUSABLE_DOLT_DIR"])
    port = int(os.environ["BH_REUSABLE_DOLT_PORT"])
    suffix = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:4]
    server = ReusableDoltServer(port, suffix)
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(server_dir))
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(port))

    def start():
        # A worker can die after writing a pidfile or half-initializing the bootstrap store.
        # The next lock holder owns recovery before attempting the sole replacement start.
        _recover_and_start_reusable_dolt_server(server_dir)

    _ensure_reusable_dolt_server(server_dir, port, start)
    yield server
    for database in sorted(server.databases):
        run(
            [
                "dolt",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "sql",
                "-q",
                f"DROP DATABASE IF EXISTS `{database}`",
            ],
            check=False,
            capture=True,
            timeout=10,
        )


@pytest.fixture
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
    from beadhive import otel

    for key in list(os.environ):
        if key.startswith("OTEL_") or key in ("WS_OBSERVALOOP_PROFILE", "BH_OBSERVALOOP_PROFILE"):
            monkeypatch.delenv(key, raising=False)
    otel.shutdown()  # reset any _initialized state leaked from a prior test
    # Bypass the setup gate for all tests unless they explicitly clear this env var.
    # test_setup.py tests that exercise the gate use monkeypatch.delenv to remove it.
    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.delenv("WS_SKIP_SETUP_CHECK", raising=False)


@pytest.fixture
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
def config_test_scope(_sandbox_bh_home):
    """Request isolated Beadhive configuration and setup-check defaults."""


@pytest.fixture
def identity_test_scope(
    _sandbox_global_git_config,
    _sandbox_workspace_root,
    _clear_git_fact_caches,
):
    """Request isolated Git identity, workspace discovery, and identity caches."""


@pytest.fixture
def dolt_test_scope(
    _bound_concurrent_dolt_servers,
    _fresh_bd_version_memo,
    _sandbox_shared_server,
):
    """Request isolated and bounded Dolt access with deterministic cleanup."""


@pytest.fixture
def validation_test_scope(_sandbox_validation_host, _unsealed_ledger):
    """Request an isolated validation host and fresh attestation latch."""


@pytest.fixture
def telemetry_test_scope(_telemetry_neutral_env):
    """Request a neutral telemetry environment and process-global state."""


@pytest.fixture
def runtime_test_scope(
    config_test_scope,
    _reject_local_runtime_thread_leaks,
    _sandbox_worktree_root_override,
    _logging_pipeline_keeps_caplogs_handler,
):
    """Request runtime leak checks, transport overrides, and logging capture isolation."""


@pytest.fixture
def plugin_test_scope(_sandbox_claude_home, _sandbox_codex_home):
    """Request empty, isolated Claude and Codex plugin registries."""


@pytest.fixture
def legacy_stateful_test_scope(
    config_test_scope,
    identity_test_scope,
    dolt_test_scope,
    validation_test_scope,
    telemetry_test_scope,
    runtime_test_scope,
    plugin_test_scope,
):
    """Compatibility scope for inventoried tests not yet graduated to pure unit scope."""


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    """A World, with its shared-server target reaped afterwards.

    The reap is normally a no-op statfile check twice over: a World inherits
    `_sandbox_shared_server`'s dir when that autouse fixture supplied one, and that fixture
    reaps it too. It matters for the case where a World minted its own (no autouse fixture in
    play) — a test that started a real dolt server would otherwise leave it holding a port and
    a tmpdir pytest has already deleted, the exact leak bh-cbou measured at 16 stray servers.
    """
    from harness.world import World, reap_dolt_server

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
