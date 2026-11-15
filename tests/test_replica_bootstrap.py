"""Replica bootstrap on a busy executor (bh-pjhwe).

Hermetic: every bd call meets a fake, the shared-server root is the per-test sandbox
(``_sandbox_shared_server``), and nothing here starts, stops or restarts a real bd/dolt server.
"""

from __future__ import annotations

import json

import pytest

from beadhive import frame_eligibility as policy
from beadhive import guard, hive, onboard, registry, replica_bootstrap, store_locator

_GATE_REFUSAL = (
    "Error: bd bootstrap refuses to run over live bd activity on this workspace: "
    'workspace gate busy ("bd serve pid 4242")\n'
)


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_server_database(name: str) -> None:
    (store_locator.server_store_dir() / name / ".dolt").mkdir(parents=True)


# ---------------------------------------------------------------------------
# replica_bootstrap.run_through_gate — the bounded gate window
# ---------------------------------------------------------------------------


def _attempts(*results):
    seen: list[dict] = []
    queue = list(results)

    def attempt(env):
        seen.append(env)
        return queue.pop(0)

    return seen, attempt


def test_gate_refused_for_a_new_database_reruns_once_without_the_shared_gate(monkeypatch):
    monkeypatch.delenv("BEADS_DOLT_SERVER_HOST", raising=False)
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", "43308")
    seen, attempt = _attempts(_Result(1, stderr=_GATE_REFUSAL), _Result(0))
    notes: list[str] = []

    result = replica_bootstrap.run_through_gate(
        attempt, {"BEADS_DOLT_SHARED_SERVER": "1", "KEEP": "x"}, "acme_widget", note=notes.append
    )

    assert result.returncode == 0
    assert len(seen) == 2
    assert seen[0]["BEADS_DOLT_SHARED_SERVER"] == "1"
    gate_free = seen[1]
    assert "BEADS_DOLT_SHARED_SERVER" not in gate_free
    assert gate_free["BD_DOLT_SHARED_SERVER"] == "false"
    assert gate_free["BEADS_DOLT_SERVER_MODE"] == "1"
    assert gate_free["BEADS_DOLT_SERVER_HOST"] == "127.0.0.1"
    assert gate_free["BEADS_DOLT_SERVER_PORT"] == "43308"
    assert gate_free["KEEP"] == "x"
    assert len(notes) == 1 and "no bd server is stopped" in notes[0]


def test_success_inside_the_gate_window_never_reruns():
    seen, attempt = _attempts(_Result(0))
    assert replica_bootstrap.run_through_gate(attempt, {}, "acme_widget").returncode == 0
    assert len(seen) == 1


@pytest.mark.parametrize(
    "result",
    [_Result(1, stderr="Error: clone from remote: authentication failed\n"), _Result(2)],
)
def test_a_failure_other_than_the_gate_is_returned_as_is(result):
    seen, attempt = _attempts(result)
    assert replica_bootstrap.run_through_gate(attempt, {}, "acme_widget") is result
    assert len(seen) == 1


def test_gate_refusal_stands_when_the_database_already_exists():
    """An existing database may have live users: the gate is then doing its job."""
    _make_server_database("acme_widget")
    refused = _Result(1, stderr=_GATE_REFUSAL)
    seen, attempt = _attempts(refused)
    assert replica_bootstrap.run_through_gate(attempt, {}, "acme_widget") is refused
    assert len(seen) == 1


def test_gate_refusal_stands_without_a_database_name():
    refused = _Result(1, stderr=_GATE_REFUSAL)
    seen, attempt = _attempts(refused)
    assert replica_bootstrap.run_through_gate(attempt, {}, None) is refused
    assert len(seen) == 1


def test_gate_free_env_keeps_an_endpoint_the_caller_set():
    env = replica_bootstrap.gate_free_env(
        {"BEADS_DOLT_SERVER_HOST": "10.0.0.5", "BEADS_DOLT_SERVER_PORT": "3999"}
    )
    assert env["BEADS_DOLT_SERVER_HOST"] == "10.0.0.5"
    assert env["BEADS_DOLT_SERVER_PORT"] == "3999"


def test_gate_free_env_turns_off_a_tracked_shared_server_config_key():
    """bh-jktzj: a furnished hive's tracked ``dolt.shared-server: true`` is read by bd through
    viper, whose ``BD_``-prefixed env binding outranks config.yaml — so the gate-free attempt
    forces it ``false``, even over a caller that set it ``true``."""
    env = replica_bootstrap.gate_free_env({"BD_DOLT_SHARED_SERVER": "true"})
    assert env["BD_DOLT_SHARED_SERVER"] == "false"
    assert replica_bootstrap.gate_free_env({})["BD_DOLT_SHARED_SERVER"] == "false"


def test_gate_free_env_drops_a_data_dir_override(tmp_path):
    """bh-jktzj: bd resolves BEADS_DOLT_DATA_DIR before the hive's own .beads/dolt, so one aimed
    at the shared dir would put the gate right back on it."""
    shared = str(store_locator.shared_server_dir() / "dolt")
    env = replica_bootstrap.gate_free_env({"BEADS_DOLT_DATA_DIR": shared, "KEEP": "x"})
    assert "BEADS_DOLT_DATA_DIR" not in env
    assert env["KEEP"] == "x"


def test_gate_free_env_leaves_the_caller_env_untouched():
    caller = {"BEADS_DOLT_SHARED_SERVER": "1", "BEADS_DOLT_DATA_DIR": "/x"}
    replica_bootstrap.gate_free_env(caller)
    assert caller == {"BEADS_DOLT_SHARED_SERVER": "1", "BEADS_DOLT_DATA_DIR": "/x"}


# ---------------------------------------------------------------------------
# onboard: origin bootstrap names the database from the prefix and survives a busy gate
# ---------------------------------------------------------------------------


def _ctx(tmp_path, *, furnish: bool, prefix: str = "acme-widget") -> onboard.Ctx:
    ctx = onboard.Ctx(
        hive="github/acme/widget",
        target=str(tmp_path),
        provider="github",
        org="acme",
        repo="widget",
        cwd=str(tmp_path),
        prefix=prefix,
        furnish=furnish,
    )
    ctx._derived = True
    return ctx


def _patch_neighbors(monkeypatch):
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: True)
    monkeypatch.setattr(onboard, "_configure_auto_export", lambda ctx: None)
    monkeypatch.setattr(onboard, "_guard_beads_remote", lambda ctx: None)
    monkeypatch.setattr(onboard, "_ensure_server_mode_persisted", lambda ctx: None)
    monkeypatch.setattr(onboard, "_exclude_clone_footprint", lambda ctx: None)
    monkeypatch.setattr(hive, "_ensure_bd_backup_gitignored", lambda base: False)


def _fake_bd(tmp_path, *, bootstrap_results):
    """Fake ``bd.run``: ``bootstrap`` pops the next scripted result and records the metadata it
    would have read; every other bd call succeeds."""
    calls: list[tuple[list[str], dict]] = []
    seen_metadata: list[dict] = []
    queue = list(bootstrap_results)

    def fake_run(args, _cwd, **kw):
        calls.append((list(args), dict(kw.get("env") or {})))
        if args[:1] == ["bootstrap"]:
            path = tmp_path / ".beads" / "metadata.json"
            seen_metadata.append(json.loads(path.read_text()) if path.is_file() else {})
            return queue.pop(0)
        return _Result(0)

    return calls, seen_metadata, fake_run


def test_zero_footprint_replica_lands_in_a_database_named_from_the_prefix(tmp_path, monkeypatch):
    _patch_neighbors(monkeypatch)
    calls, seen_metadata, fake_run = _fake_bd(tmp_path, bootstrap_results=[_Result(0)])
    monkeypatch.setattr(onboard.bd_mod, "run", fake_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=False))

    # bd read the stamped identity — never its generic default — before it cloned
    assert seen_metadata == [{"dolt_database": "acme_widget", "dolt_mode": "server"}]
    recorded = json.loads((tmp_path / ".beads" / "metadata.json").read_text())
    assert recorded[store_locator.SERVER_DATABASE_KEY] == "acme_widget"
    assert not any(args[:2] == ["dolt", "stop"] for args, _ in calls)


def test_busy_executor_bootstraps_gate_free_without_stopping_any_server(
    tmp_path, monkeypatch, capsys
):
    _patch_neighbors(monkeypatch)
    calls, _, fake_run = _fake_bd(
        tmp_path, bootstrap_results=[_Result(1, stderr=_GATE_REFUSAL), _Result(0, stdout="ok\n")]
    )
    monkeypatch.setattr(onboard.bd_mod, "run", fake_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=False))

    bootstraps = [env for args, env in calls if args[:1] == ["bootstrap"]]
    assert len(bootstraps) == 2
    assert bootstraps[0]["BEADS_DOLT_SHARED_SERVER"] == "1"
    assert "BEADS_DOLT_SHARED_SERVER" not in bootstraps[1]
    # the only server lifecycle call allowed is starting one when nothing answers
    lifecycle = [args for args, _ in calls if args[:1] == ["dolt"]]
    assert all(args == ["dolt", "start", "--global"] for args in lifecycle)
    out = capsys.readouterr()
    assert "refuses to run over live bd activity" in out.err  # bd's own output, verbatim
    assert "ok\n" in out.out
    assert (tmp_path / ".beads" / "metadata.json").is_file()


def test_a_failed_replica_bootstrap_cleans_up_and_exits(tmp_path, monkeypatch, capsys):
    import typer

    _patch_neighbors(monkeypatch)
    _make_server_database("acme_widget")  # present: the gate refusal stands
    _, _, fake_run = _fake_bd(tmp_path, bootstrap_results=[_Result(1, stderr=_GATE_REFUSAL)])
    monkeypatch.setattr(onboard.bd_mod, "run", fake_run)
    cleaned: list = []
    monkeypatch.setattr(hive, "cleanup_failed_bd_init", cleaned.append)

    with pytest.raises(typer.Exit):
        onboard._act_bd_init(_ctx(tmp_path, furnish=False))

    assert cleaned == [tmp_path]
    assert "no bd server was stopped" in capsys.readouterr().err


def test_furnished_hive_bootstraps_existing_remote_data_instead_of_minting(tmp_path, monkeypatch):
    _patch_neighbors(monkeypatch)
    calls, _, fake_run = _fake_bd(tmp_path, bootstrap_results=[_Result(0)])
    monkeypatch.setattr(onboard.bd_mod, "run", fake_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=True))

    verbs = [args[0] for args, _ in calls]
    assert "bootstrap" in verbs
    assert "init" not in verbs


# ---------------------------------------------------------------------------
# store_locator.replica_missing
# ---------------------------------------------------------------------------


def _write_metadata(root, data):
    (root / ".beads").mkdir(parents=True, exist_ok=True)
    (root / ".beads" / "metadata.json").write_text(json.dumps(data))


def test_no_beads_at_all_is_missing_until_the_prefix_database_exists(tmp_path):
    assert store_locator.replica_missing(tmp_path, "acme-widget")
    _make_server_database("acme_widget")
    assert not store_locator.replica_missing(tmp_path, "acme-widget")


def test_server_mode_resolves_the_recorded_database(tmp_path):
    _write_metadata(tmp_path, {"dolt_mode": "server", "dolt_server_database": "other_db"})
    assert store_locator.replica_missing(tmp_path, "acme-widget")
    _make_server_database("other_db")
    assert not store_locator.replica_missing(tmp_path, "acme-widget")


def test_a_local_store_on_disk_is_never_missing(tmp_path):
    (tmp_path / ".beads" / "embeddeddolt").mkdir(parents=True)
    assert not store_locator.replica_missing(tmp_path, "acme-widget")


def test_a_remote_server_is_never_provably_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("BEADS_DOLT_SERVER_HOST", "db.example.internal")
    assert not store_locator.replica_missing(tmp_path, "acme-widget")


# ---------------------------------------------------------------------------
# guard: an ineligible frame may create the replica that makes it eligible
# ---------------------------------------------------------------------------

_ENTRY = {"provider": "github", "org": "acme", "repo": "widget", "prefix": "acme-widget"}


def _ineligible(monkeypatch, entry=_ENTRY):
    monkeypatch.setattr(guard, "primary_state", lambda **kw: None)
    monkeypatch.setattr(guard, "writer_state", lambda **kw: None)
    monkeypatch.setattr(registry, "entry_for_dir", lambda cfg, cwd: dict(entry))

    def refuse(**kwargs):
        raise policy.EligibilityError("frame ineligible: hives_ready")

    monkeypatch.setattr(policy, "require_intake", refuse)
    return {"hq": {}, "managed_repos": [dict(entry)]}


def test_ineligible_frame_may_bootstrap_its_missing_replica(tmp_path, monkeypatch):
    cfg = _ineligible(monkeypatch)
    assert guard.bd_write_refusal(["bootstrap", "--non-interactive"], tmp_path, cfg=cfg) == ""
    # every other write is still fenced
    assert "frame ineligible" in guard.bd_write_refusal(
        ["update", "x-1", "--claim"], tmp_path, cfg=cfg
    )


def test_bootstrap_over_an_existing_replica_is_still_fenced(tmp_path, monkeypatch):
    cfg = _ineligible(monkeypatch)
    _make_server_database("acme_widget")
    assert "frame ineligible" in guard.bd_write_refusal(["bootstrap"], tmp_path, cfg=cfg)


def test_bootstrap_waiver_never_applies_to_an_hq_declared_remote(tmp_path, monkeypatch):
    cfg = _ineligible(monkeypatch, entry={**_ENTRY, "dolt_remote": {"name": "internal"}})
    assert "frame ineligible" in guard.bd_write_refusal(["bootstrap"], tmp_path, cfg=cfg)


def test_bootstrap_waiver_needs_a_registered_hive(tmp_path, monkeypatch):
    cfg = _ineligible(monkeypatch)
    cfg["managed_repos"] = []
    assert "frame ineligible" in guard.bd_write_refusal(["bootstrap"], tmp_path, cfg=cfg)
