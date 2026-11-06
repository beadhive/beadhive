"""The per-invocation effective-config memo (``bh-pc9rf``): ``config.process_load_memo``.

On a SQL-HQ host every ``config.load()`` re-qualifies the committed fleet snapshot (about 1.3 s
each on the factory) and one ``bh work outbox show`` paid that 7 times. Inside the opt-in scope a
one-shot verb resolves it once. Every assertion is a load COUNT (a spy on the SQL derivation) or
an injected fake clock, never a duration; leaving the scope drops the memo, so no state crosses
tests.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from beadhive import cli, cli_entrypoint, config, config_store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_WORKSPACE", raising=False)
    monkeypatch.delenv("BH_CONFIG", raising=False)
    monkeypatch.delenv("WS_HOME", raising=False)
    root = tmp_path / "bh-home"
    monkeypatch.setenv("BH_HOME", str(root))
    root.mkdir()
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("providers: [github]\nmanaged_repos: []\n")
    return root


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(config_store, "_clock", fake)
    return fake


@pytest.fixture
def sql_loads(home, monkeypatch, clock):
    """Select the SQL path (whose snapshot is re-qualified on EVERY load) and count each one."""
    calls: list[int] = []

    def uncached(api):
        return {"providers": ["github"], "managed_repos": []}, True

    def derive(api, host):
        calls.append(1)
        return json.dumps({"providers": ["github"], "managed_repos": [], "n": len(calls)})

    monkeypatch.setattr(config_store, "_load_uncached", uncached)
    monkeypatch.setattr(config_store, "_load_sql", derive)
    return calls


def test_without_the_scope_every_load_requalifies(sql_loads) -> None:
    for _ in range(3):
        config.load()
    assert len(sql_loads) == 3


def test_inside_the_scope_the_invocation_loads_once(sql_loads) -> None:
    with config.process_load_memo():
        first = config.load()
        first["providers"].append("mutated by a caller")
        for _ in range(6):
            again = config.load()
    assert len(sql_loads) == 1
    assert again == {"providers": ["github"], "managed_repos": [], "n": 1}  # a fresh copy each


def test_leaving_the_scope_drops_the_memo(sql_loads) -> None:
    with config.process_load_memo():
        config.load()
    with config.process_load_memo():
        config.load()
    config.load()
    assert len(sql_loads) == 3


def test_nested_scopes_share_one_memo(sql_loads) -> None:
    with config.process_load_memo():
        config.load()
        with config.process_load_memo():
            config.load()
        config.load()  # the outer scope is still active
    assert len(sql_loads) == 1


def test_the_ttl_bounds_a_long_invocation(sql_loads, clock) -> None:
    with config.process_load_memo():
        config.load()
        clock.now += config_store.PROCESS_LOAD_TTL_SECONDS - 1
        config.load()
        assert len(sql_loads) == 1
        clock.now += 1
        config.load()
        assert len(sql_loads) == 2


def test_an_edit_or_an_in_process_write_is_seen(sql_loads, home) -> None:
    with config.process_load_memo():
        config.load()
        config.config_path().write_text("providers: [github]\nmanaged_repos: []\n# edited\n")
        config.load()  # the HOST file signature moved
        assert len(sql_loads) == 2
        config_store.clear_load_cache()  # what every in-process config write calls
        config.load()
        assert len(sql_loads) == 3


def test_a_hive_local_read_is_never_served_to_another_read(sql_loads) -> None:
    with config.process_load_memo():
        with config.hive_local_config_reads():
            config.load()
        config.load()
        assert len(sql_loads) == 2
        config.load()  # the ordinary read is now the one remembered
        assert len(sql_loads) == 2


def test_a_failed_load_is_not_memoized(home, monkeypatch) -> None:
    calls: list[int] = []

    def failing(api):
        calls.append(1)
        raise FileNotFoundError("no config")

    monkeypatch.setattr(config_store, "_load_uncached", failing)
    with config.process_load_memo():
        for _ in range(2):
            with pytest.raises(FileNotFoundError):
                config.load()
    assert len(calls) == 2


# -- who opts in ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "verb"),
    [
        (["work", "outbox", "show"], "work"),
        (["--hive", "acme", "work", "outbox"], "work"),
        (["-a", "bd", "list"], "bd"),
        (["host", "daemon", "serve"], "host"),
        (["--", "work"], ""),
        ([], ""),
    ],
)
def test_the_entrypoint_finds_the_invoked_verb(argv, verb) -> None:
    assert cli_entrypoint._invoked_verb(argv) == verb


class _Recorder:
    def __init__(self, entered: list[str]) -> None:
        self.entered = entered

    def __enter__(self):
        self.entered.append("in")

    def __exit__(self, *exc):
        return None


@pytest.mark.parametrize(
    ("argv", "memoized"),
    [(["work", "outbox", "show"], True), (["host", "daemon"], False), (["mcp"], False)],
)
def test_the_entrypoint_opts_one_shot_verbs_in_before_the_tree_loads(
    monkeypatch, argv, memoized
) -> None:
    entered: list[str] = []
    ran: list[bool] = []
    monkeypatch.setattr(config_store, "process_load_memo", lambda: _Recorder(entered))
    monkeypatch.setattr(cli, "main", lambda: ran.append(bool(entered)))
    monkeypatch.setattr(cli_entrypoint.sys, "argv", ["bh", *argv])

    cli_entrypoint.main()

    assert ran == [memoized]  # entered BEFORE the command tree ran, or not at all


@pytest.mark.parametrize(
    ("argv", "memoized"),
    [(["config", "path"], True), (["host", "--help"], False), (["mcp", "--help"], False)],
)
def test_the_root_callback_opts_one_shot_verbs_in(home, monkeypatch, argv, memoized) -> None:
    entered: list[str] = []
    monkeypatch.setattr(config, "process_load_memo", lambda: _Recorder(entered))
    CliRunner().invoke(cli.app, argv)
    assert bool(entered) is memoized


# -- a direct publish inside one command is seen by its next load --------------------------------


class _Published:
    commit_revision = "rev-2"


@pytest.mark.parametrize("fails", [False, True], ids=["published", "refused"])
def test_a_sql_store_publish_is_seen_by_the_next_load(sql_loads, monkeypatch, fails) -> None:
    from beadhive import hq_authority_fence_notice as notice
    from beadhive import hq_sql_config

    def core(self, documents, **kwargs):
        if fails:
            raise hq_sql_config.SqlConfigError("conflict")
        return _Published()

    monkeypatch.setattr(hq_sql_config.SqlFleetConfigRevisionStore, "_publish_snapshot_core", core)
    monkeypatch.setattr(notice, "probe_authority", lambda store: None)
    monkeypatch.setattr(notice, "announce_before", lambda *a, **k: None)
    monkeypatch.setattr(notice, "announce_after", lambda *a, **k: None)
    store = object.__new__(hq_sql_config.SqlFleetConfigRevisionStore)

    with config.process_load_memo():
        config.load()
        if fails:
            with pytest.raises(hq_sql_config.SqlConfigError):
                store.publish_snapshot((), expected_revision="rev-1")
        else:
            store.publish_snapshot((), expected_revision="rev-1")
        config.load()
    assert len(sql_loads) == 2  # the memo was dropped by the publish (or its attempt)


def test_a_git_store_publish_is_seen_by_the_next_load(sql_loads, monkeypatch) -> None:
    from beadhive import hq_fleet_config

    monkeypatch.setattr(
        hq_fleet_config.GitFleetConfigRevisionStore,
        "_publish_snapshot",
        lambda self, documents, **kwargs: _Published(),
    )
    store = object.__new__(hq_fleet_config.GitFleetConfigRevisionStore)

    with config.process_load_memo():
        config.load()
        assert store.publish_snapshot((), expected_revision="rev-1").commit_revision == "rev-2"
        config.load()
    assert len(sql_loads) == 2
