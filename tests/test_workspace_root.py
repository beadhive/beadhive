"""identity.workspace_root() — the ONE root-resolution choke point (bh-cgcg.1).

3-tier precedence: `$GIT_WORKSPACE` > config `git_workspace.mode`/`.root` > internal default
(``<bh home>/ws`` — a sibling of the worktrees shadow tree at ``<bh home>/wt``), with a legacy
guard on the internal-default tier so flipping the default never silently relocates an
existing populated ``~/workspace``. Every test wires its own env/config in isolation via the
``_isolated`` fixture (no `GIT_WORKSPACE`, an isolated `$BH_HOME`, and `identity._legacy_root`
monkeypatched to a tmp stand-in) — nothing here may resolve to, or depend on, the real
``~/workspace`` / ``~/.beadhive`` on the machine running the suite.

``root`` is EXTERNAL-ONLY (bh-cgcg.6): it wins under `mode: external` or unset (unchanged),
and is REJECTED — `config.ConfigError`, at `workspace_root()` — under `mode: internal`, where
the internal root is fixed (`<bh home>/ws`, not configurable via `root`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from beadhive import cli, config, config_store, identity, metadata, run


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    """The "fresh install" baseline: no `$GIT_WORKSPACE`, a bh home under `tmp_path`, no
    config.yaml yet, and the legacy `~/workspace` stand-in monkeypatched to an as-yet
    nonexistent tmp dir. Returns the `home` and `legacy` paths for assertions."""
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    home = tmp_path / "bh-home"
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.delenv("BH_CONFIG", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)
    legacy = tmp_path / "home-workspace"  # stand-in for ~/workspace
    monkeypatch.setattr(identity, "_legacy_root", lambda: legacy)
    return {"home": home, "legacy": legacy}


def _write_config(mode=None, root=None, managed_repos=None):
    """Scaffold a minimal host config.yaml (so `config.load()` succeeds) and optionally set
    `git_workspace.mode`/`.root` and/or `managed_repos`."""
    cfg_path = config.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("providers: [github]\nmanaged_repos: []\n")
    cfg = config.load()
    gw = {}
    if mode is not None:
        gw["mode"] = mode
    if root is not None:
        gw["root"] = root
    if gw:
        cfg["git_workspace"] = gw
    if managed_repos is not None:
        cfg["managed_repos"] = managed_repos
    if mode == "internal" and root is not None:
        # Deliberately inject a corrupt legacy file. The public save boundary
        # now rejects this candidate before persistence; these tests exercise
        # the independent read and environment-precedence contracts.
        cfg_path.write_text(json.dumps(cfg))
    else:
        config.save(cfg)


# ---- 1. $GIT_WORKSPACE always wins, over both config and any legacy state ----


def test_env_wins_over_config_and_default(_isolated, tmp_path, monkeypatch):
    _write_config(mode="internal")
    (_isolated["legacy"] / "github" / "acme" / "api" / ".git").mkdir(parents=True)
    env_root = tmp_path / "env-workspace"
    monkeypatch.setenv("GIT_WORKSPACE", str(env_root))

    assert identity.workspace_root() == str(env_root.resolve())


# ---- 2. fresh install: no config, no existing workspace -> internal default --


def test_fresh_install_resolves_to_bh_home_ws(_isolated):
    assert not _isolated["legacy"].exists()  # nothing pre-existing to guard against

    assert identity.workspace_root() == str((_isolated["home"] / "ws").resolve())


def test_internal_default_derives_from_bh_home_not_tilde(tmp_path, monkeypatch):
    """bh-cgcg.1 acceptance: the internal default follows the resolved bh home ($BH_HOME),
    never a hardcoded ``~`` / ``/home/<user>`` assumption — verified with `$BH_HOME` at a
    non-``~`` location, mirroring the operator's own host (``HOME=/opt/orca``)."""
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    non_tilde_home = tmp_path / "opt" / "orca" / ".beadhive"
    monkeypatch.setenv("BH_HOME", str(non_tilde_home))
    monkeypatch.delenv("BH_CONFIG", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)
    legacy = tmp_path / "opt" / "orca" / "workspace"  # ~/workspace under this same non-~ HOME
    monkeypatch.setattr(identity, "_legacy_root", lambda: legacy)
    assert not str(non_tilde_home).startswith(str(Path.home()))

    assert identity.workspace_root() == str((non_tilde_home / "ws").resolve())


# ---- 3. explicit config: mode + optional root override -----------------------


def test_config_mode_internal_explicit(_isolated):
    """An explicit `internal` wins even over a populated legacy workspace — a deliberate
    operator opt-in, not the guarded default path."""
    _write_config(mode="internal")
    (_isolated["legacy"] / "github" / "acme" / "api" / ".git").mkdir(parents=True)

    assert identity.workspace_root() == str((_isolated["home"] / "ws").resolve())


def test_config_mode_external_explicit_reads_legacy_default(_isolated):
    """`mode: external` with no `root` override resolves to the same default location
    git-workspace itself has always used — reading the user's workspace*.toml exactly as
    today."""
    _write_config(mode="external")

    assert identity.workspace_root() == str(_isolated["legacy"].resolve())


def test_config_root_override_wins_under_external_mode(_isolated, tmp_path):
    # `_isolated` isn't referenced by name — it's needed for its side effect (an isolated
    # $BH_HOME + no $GIT_WORKSPACE) so `_write_config` resolves config_path() under tmp_path.
    override = tmp_path / "custom-root"
    _write_config(mode="external", root=str(override))

    assert identity.workspace_root() == str(override.resolve())


def test_config_root_override_wins_with_mode_unset(_isolated, tmp_path):
    """`root` with no `mode` at all still implies external (unchanged) — the third row of
    the mode/root matrix."""
    override = tmp_path / "custom-root"
    _write_config(root=str(override))

    assert identity.workspace_root() == str(override.resolve())


# ---- 3b. root is EXTERNAL-ONLY: mode: internal + root is REJECTED (bh-cgcg.6) -


def test_config_mode_internal_with_root_is_rejected(_isolated, tmp_path):
    """The internal root is not configurable — it is always derived (`<bh home>/ws`) and
    lives in code, not `config.yaml`. `mode: internal` + an explicit `root` must be REJECTED
    at load, not silently honored (the old, buggy behavior) and not silently ignored either.
    The message must point at `$BH_HOME` as the way to relocate an internal workspace."""
    override = tmp_path / "custom-root"
    _write_config(mode="internal", root=str(override))

    with pytest.raises(config.ConfigError, match="BH_HOME"):
        identity.workspace_root()


def test_config_mode_internal_with_root_equal_to_internal_default_is_still_rejected(
    _isolated,
):
    """Rejection is unconditional — even setting `root` to the exact value the internal
    default would already derive is refused, not silently allowed as a no-op: `root` is not a
    config knob under `mode: internal` at all."""
    internal_root = _isolated["home"] / "ws"
    _write_config(mode="internal", root=str(internal_root))

    with pytest.raises(config.ConfigError, match="BH_HOME"):
        identity.workspace_root()


def test_env_wins_even_over_invalid_mode_internal_plus_root_config(
    _isolated, tmp_path, monkeypatch
):
    """`$GIT_WORKSPACE` is resolved before config is even consulted, so it wins — and the
    rejection above never fires — even when the config underneath holds the invalid
    `mode: internal` + `root` combination: the documented escape hatch is not narrowed by
    this bead, in every mode, including internal."""
    override = tmp_path / "custom-root"
    _write_config(mode="internal", root=str(override))
    env_root = tmp_path / "env-workspace"
    monkeypatch.setenv("GIT_WORKSPACE", str(env_root))

    assert identity.workspace_root() == str(env_root.resolve())


# ---- 4. the legacy guard: populated vs. merely-existing-and-empty ------------


def test_existing_populated_workspace_resolves_external_no_root_swap(_isolated):
    """A real clone under the legacy root (no explicit config) resolves EXTERNAL — the
    breaking-change guard bh-cgcg exists for. factory-orca's 16 real clones are the
    acceptance test for this exact branch."""
    (_isolated["legacy"] / "github" / "acme" / "api" / ".git").mkdir(parents=True)

    assert identity.workspace_root() == str(_isolated["legacy"].resolve())


def test_existing_but_empty_workspace_dir_does_not_pin_external(_isolated):
    """An empty leftover `~/workspace` (directory exists, no clones, no registered hives)
    must NOT pin a fresh install to external mode forever — it still resolves the internal
    default."""
    _isolated["legacy"].mkdir(parents=True)

    assert identity.workspace_root() == str((_isolated["home"] / "ws").resolve())


def test_registered_hive_resolving_under_legacy_root_counts_as_populated(_isolated):
    """ "Existing and populated" also covers a hive registered in `managed_repos` whose
    derived clone path (`root/provider/org/repo`, `config.managed_repo_path`) actually exists
    under the legacy root — registered hives keep resolving external."""
    entry = {"provider": "gitlab", "org": "widgetco", "repo": "core", "prefix": "wc-core"}
    _write_config(managed_repos=[entry])
    (_isolated["legacy"] / "gitlab" / "widgetco" / "core" / ".git").mkdir(parents=True)

    assert identity.workspace_root() == str(_isolated["legacy"].resolve())


def test_registered_hive_not_scoped_to_legacy_root_does_not_count_as_populated(_isolated):
    """`managed_repos` alone is NOT enough — a registered hive only populates the legacy root
    when its derived clone actually lives there. A bare (empty) legacy dir plus a registration
    whose clone is nowhere on disk must NOT flip a fresh install to external: checking
    non-empty `managed_repos` without scoping to `root` was the bug (an internal-mode install
    that onboards its first hive would otherwise flip to ~/workspace on its very next call)."""
    entry = {"provider": "github", "org": "acme", "repo": "api", "prefix": "ac-api"}
    _write_config(managed_repos=[entry])
    _isolated["legacy"].mkdir(parents=True)  # exists, but the registered hive isn't under it
    assert not (_isolated["legacy"] / "github" / "acme" / "api").exists()

    assert identity.workspace_root() == str((_isolated["home"] / "ws").resolve())


def test_hive_registered_under_internal_root_keeps_resolving_internal(_isolated):
    """The acceptance case this bug produced: a fresh internal-mode install clones its first
    hive under the resolved internal root, `<bh home>/ws/provider/org/repo`, and registers it
    in `managed_repos`. The NEXT `workspace_root()` call (mode unset again, guard path) must
    still resolve internal — the registration must not be mistaken for legacy-root content
    just because `managed_repos` is non-empty."""
    entry = {"provider": "github", "org": "acme", "repo": "api", "prefix": "ac-api"}
    _write_config(managed_repos=[entry])
    internal_root = _isolated["home"] / "ws"
    (internal_root / "github" / "acme" / "api" / ".git").mkdir(parents=True)
    assert not _isolated["legacy"].exists()  # nothing under the legacy root at all

    assert identity.workspace_root() == str(internal_root.resolve())


def test_no_cache_invalidation_when_populated_workspace_resolves_external(_isolated):
    """metadata.py coarse-invalidates its cache on a `workspace_root` mismatch (root moved).
    The guard keeping an existing populated workspace external must make `workspace_root()`
    stable across calls (nothing actually moved), so a warm cache for a registered hive stays
    warm rather than being dropped."""
    resolved_legacy = str(_isolated["legacy"].resolve())
    (_isolated["legacy"] / "github" / "acme" / "api" / ".git").mkdir(parents=True)

    cache_path = config.cache_dir() / "metadata.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "last_updated": None,
                "workspace_root": resolved_legacy,
                "repos": {
                    "github/acme/api": {
                        "git_head": "abc123",
                        "git_mtime": 1000.0,
                        "measured_at": "2026-01-01T00:00:00Z",
                        "category": "READY",
                        "has_origin": True,
                        "stash_count": 0,
                        "disk_bytes": 42,
                        "commit_count": 7,
                        "age_days": 1.5,
                        "last_commit": "2026-01-01",
                        "branches": [],
                        "worktrees": [],
                        "dolt_ref": {},
                    }
                },
            }
        )
    )

    cache = metadata.load()

    assert cache.workspace_root == resolved_legacy
    assert "github/acme/api" in cache.repos  # preserved, not coarse-invalidated


def test_config_scope_resolves_from_the_threaded_cfg_without_reloading(_isolated, monkeypatch):
    """bh-931we: a validation lane threads its already-loaded cfg; every `workspace_root` in the
    scope (one per git spawn's GIT_WORKSPACE fill) reuses it instead of calling `config.load()`
    — and the scope ends with the operation, so the next one resolves from fresh config."""
    pinned = tmp = _isolated["home"].parent / "pinned"
    _write_config(root=str(tmp / "fresh"))
    loads = []
    real_load = config.load
    monkeypatch.setattr(config, "load", lambda: (loads.append(1), real_load())[1])

    with identity.config_scope({"git_workspace": {"root": str(pinned)}}):
        assert [identity.workspace_root() for _ in range(5)] == [str(pinned.resolve())] * 5
        assert loads == []
        monkeypatch.setenv("GIT_WORKSPACE", str(tmp / "env"))
        assert identity.workspace_root() == str((tmp / "env").resolve())  # env still wins
        monkeypatch.delenv("GIT_WORKSPACE")
    assert identity.workspace_root() == str((tmp / "fresh").resolve())
    assert loads == [1]
    with identity.config_scope(None):  # no cfg in hand: unchanged fresh resolution
        identity.workspace_root()
    assert loads == [1, 1]


# ---- one config resolution per bh process for the child-env fill (bh-olnhf) ----------
#
# One effective-config resolution per bh process for the child-env ``GIT_WORKSPACE`` fill
# (bh-olnhf).
#
# ``run.child_env`` -> ``identity.workspace_root`` used to call ``config.load()`` for every child
# bh spawned; on a SQL-HQ host that is a committed-snapshot requalification plus a ~66-document
# YAML parse per launch. These tests pin, by COUNTING ``config.load`` calls through a spy (never by
# timing):
#
#   * N launches inside the process memo resolve the config at most once;
#   * ``GIT_WORKSPACE`` in the child env is byte-identical with and without the memo, for the
#     env-set, config-set and default cases (env > config > internal default, blank env = unset);
#   * a process that outlives one verb does not serve a stale value forever: a changed config
#     source, an in-process save and the TTL each force a re-resolution, a failed load is never
#     memoized, and the serving verbs (``host``, ``mcp``) never opt in.


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Fresh-install baseline: isolated bh home, no ``GIT_WORKSPACE``, legacy root pointed at a
    nonexistent tmp dir, and the memo clock under test control."""
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    monkeypatch.delenv("BH_CONFIG", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)
    root = tmp_path / "bh-home"
    monkeypatch.setenv("BH_HOME", str(root))
    monkeypatch.setattr(identity, "_legacy_root", lambda: tmp_path / "home-workspace")
    root.mkdir()
    return root


def _write_memo_config(git_workspace_root: str | None = None) -> None:
    body = "providers: [github]\nmanaged_repos: []\n"
    if git_workspace_root is not None:
        body += f"git_workspace:\n  root: {git_workspace_root}\n"
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


@pytest.fixture
def loads(monkeypatch):
    """Spy on the store behind ``config.load`` — every ``config.load()`` is exactly one call here,
    and the count is the assertion, never a duration."""
    calls: list[int] = []
    real = config_store.load

    def spy(api):
        calls.append(1)
        return real(api)

    monkeypatch.setattr(config_store, "load", spy)
    return calls


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(identity, "_clock", fake)
    return fake


def test_n_child_launches_resolve_the_config_once(home, loads, tmp_path):
    _write_memo_config(str(tmp_path / "ws-root"))
    with identity.process_config_memo():
        envs = [run.child_env() for _ in range(8)]
    assert len(loads) == 1
    assert {e["GIT_WORKSPACE"] for e in envs} == {str((tmp_path / "ws-root").resolve())}


def test_without_the_memo_every_launch_loads_as_before(home, loads, tmp_path):
    """The default is unchanged: opt-in only, so long-lived processes keep the uncached path."""
    _write_memo_config(str(tmp_path / "ws-root"))
    for _ in range(3):
        run.child_env()
    assert len(loads) == 3


def test_scope_end_drops_the_memo(home, loads, tmp_path):
    _write_memo_config(str(tmp_path / "a"))
    with identity.process_config_memo():
        run.child_env()
    with identity.process_config_memo():
        run.child_env()
    assert len(loads) == 2
    run.child_env()
    assert len(loads) == 3


def test_nested_scopes_share_one_memo(home, loads, tmp_path):
    _write_memo_config(str(tmp_path / "a"))
    with identity.process_config_memo():
        run.child_env()
        with identity.process_config_memo():
            run.child_env()
        run.child_env()  # outer scope still active
    assert len(loads) == 1


@pytest.mark.parametrize("memo", [False, True], ids=["uncached", "memoized"])
def test_git_workspace_parity_env_config_default(home, tmp_path, monkeypatch, memo):
    """env > config > internal default, blank env = unset — identical with and without the memo."""
    from contextlib import ExitStack

    def child_value() -> str:
        with ExitStack() as stack:
            if memo:
                stack.enter_context(identity.process_config_memo())
            return run.child_env()["GIT_WORKSPACE"]

    # default: no config at all -> bh-owned internal root
    assert child_value() == str((home / "ws").resolve())

    # config-set
    _write_memo_config(str(tmp_path / "from-config"))
    assert child_value() == str((tmp_path / "from-config").resolve())

    # env-set wins over config, and is read live
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "from-env"))
    assert child_value() == str((tmp_path / "from-env").resolve())

    # blank env is unset, not the empty path
    monkeypatch.setenv("GIT_WORKSPACE", "   ")
    assert child_value() == str((tmp_path / "from-config").resolve())


def test_env_change_inside_the_memo_is_seen_without_a_reload(home, loads, tmp_path, monkeypatch):
    """Only the config is memoized; ``$GIT_WORKSPACE`` is evaluated live on every call."""
    _write_memo_config(str(tmp_path / "from-config"))
    with identity.process_config_memo():
        assert run.child_env()["GIT_WORKSPACE"] == str((tmp_path / "from-config").resolve())
        monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "from-env"))
        assert run.child_env()["GIT_WORKSPACE"] == str((tmp_path / "from-env").resolve())
        monkeypatch.delenv("GIT_WORKSPACE")
        assert run.child_env()["GIT_WORKSPACE"] == str((tmp_path / "from-config").resolve())
    assert len(loads) == 1


def test_scoped_config_still_outranks_the_memo(home, loads, tmp_path):
    _write_memo_config(str(tmp_path / "from-config"))
    with identity.process_config_memo():
        with identity.config_scope({"git_workspace": {"root": str(tmp_path / "scoped")}}):
            assert identity.workspace_root() == str((tmp_path / "scoped").resolve())
        assert loads == []


def test_a_changed_config_source_is_seen(home, loads, tmp_path):
    """The key carries the host + fleet file signatures: an edit by ANOTHER process is picked up."""
    _write_memo_config(str(tmp_path / "one"))
    with identity.process_config_memo():
        assert identity.workspace_root() == str((tmp_path / "one").resolve())
        _write_memo_config(str(tmp_path / "two-longer-path"))  # new size/mtime, same inode or not
        assert identity.workspace_root() == str((tmp_path / "two-longer-path").resolve())
    assert len(loads) == 2


def test_an_in_process_config_save_is_seen(home, loads, tmp_path):
    _write_memo_config(str(tmp_path / "one"))
    with identity.process_config_memo():
        assert identity.workspace_root() == str((tmp_path / "one").resolve())
        cfg = config.load()
        cfg["git_workspace"] = {"root": str(tmp_path / "saved")}
        config.save(cfg)
        assert identity.workspace_root() == str((tmp_path / "saved").resolve())


def test_the_ttl_bounds_staleness_of_unkeyed_sources(home, loads, tmp_path, clock):
    """A committed-SQL fleet snapshot has no cheap identity, so time bounds it."""
    _write_memo_config(str(tmp_path / "one"))
    with identity.process_config_memo():
        identity.workspace_root()
        clock.now += identity.PROCESS_CONFIG_TTL_SECONDS - 1
        identity.workspace_root()
        assert len(loads) == 1
        clock.now += 2
        identity.workspace_root()
        assert len(loads) == 2


def test_a_failed_load_is_not_memoized(home, loads, tmp_path):
    """No config yet -> ``FileNotFoundError`` -> default root; once the file exists it is seen."""
    with identity.process_config_memo():
        assert identity.workspace_root() == str((home / "ws").resolve())
        _write_memo_config(str(tmp_path / "now-configured"))
        assert identity.workspace_root() == str((tmp_path / "now-configured").resolve())


@pytest.mark.parametrize(
    ("argv", "memoized"),
    [(["config", "path"], True), (["host", "--help"], False), (["mcp", "--help"], False)],
)
def test_serving_verbs_never_opt_in(home, monkeypatch, argv, memoized):
    from typer.testing import CliRunner

    entered: list[str] = []

    class _Recorder:
        def __enter__(self):
            entered.append("in")

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(identity, "process_config_memo", lambda: _Recorder())
    CliRunner().invoke(cli.app, argv)
    assert bool(entered) is memoized
    assert {"host", "mcp"} <= cli._LONG_LIVED_VERBS
