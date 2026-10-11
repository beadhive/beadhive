"""hive.sync: batch auto-commit, the daemon's auto-push / auto-pull, durable verbs (bh-midl0).

Trust-profiles ADR section 5. Time is a fake clock everywhere: the loop's timer, its backoff and
the ``--await-sync`` wait all advance it explicitly; nothing here waits on real time.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from pydantic import ValidationError

from beadhive import (
    engine,
    hive_sync_loop,
    hive_sync_settings,
    hive_sync_state,
    work_close,
    work_merge,
    write_policy,
)
from beadhive.hive_sync_loop import FRAME, MAIN, NONE, Route, SyncHive, SyncLoop
from beadhive.hive_sync_settings import SyncSettings
from beadhive.hive_sync_state import Status
from beadhive.modules.config.contracts import (
    BeadhiveConfig,
    HiveSyncConfig,
    HiveSyncOverride,
)


class Clock:
    """A clock that moves only when told to."""

    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["bd"], 0, stdout, "")


def _dead_remote() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        ["bd", "dolt", "push"], 1, "", "fatal: unable to access remote: connection refused\n"
    )


class Remote:
    """A push/pull collaborator: records every call, answers alive or dead."""

    def __init__(self, *, alive: bool = True):
        self.alive = alive
        self.pushes: list[tuple[str, str]] = []
        self.pulls: list[str] = []

    def push(self, hive: SyncHive, route: Route):
        self.pushes.append((hive.hive, route.target))
        return _ok() if self.alive else _dead_remote()

    def pull(self, hive: SyncHive):
        self.pulls.append(hive.hive)
        return _ok() if self.alive else _dead_remote()


def _settings(**overrides) -> SyncSettings:
    base = hive_sync_settings.resolve({"auto_push": {"enabled": True}})
    return SyncSettings(**{**base.__dict__, **overrides})


_WRITER = Route(MAIN, MAIN)


def _loop(
    tmp_path: Path,
    *,
    clock: Clock,
    remote: Remote,
    settings: SyncSettings,
    route: Route = _WRITER,
    pending: int = 0,
    merges=None,
):
    counts = {"n": pending}
    hive = SyncHive("fx", tmp_path / "hive", settings)
    loop = SyncLoop(
        hives=lambda: [hive],
        route=lambda _hive: route,
        pending=lambda _hive, _route: counts["n"],
        push=remote.push,
        pull=remote.pull,
        merge_frames=merges or (lambda _hive: []),
        clock=clock,
        state_path=tmp_path / "state" / "hive-sync.json",
    )
    return loop, counts


def _record(loop: SyncLoop) -> hive_sync_state.HiveSyncRecord:
    return hive_sync_state.recorded("fx", path=loop.state_path)


# =============================================================================================
# Configuration: every key at fleet default and per-hive override, with the ADR defaults
# =============================================================================================


def test_the_fleet_defaults_are_the_adr_defaults():
    sync = BeadhiveConfig.model_validate({}).hive.sync
    assert sync.auto_commit == "on"
    assert sync.auto_push.enabled is False
    assert sync.auto_push.interval == "5m"
    assert sync.auto_push.max_pending == 50
    assert sync.auto_push.retry_backoff == "30s..10m"
    assert sync.auto_pull.enabled is False
    assert sync.auto_pull.interval == "15m"
    assert sync.durable_verbs == ["submit", "finish"]
    effective = hive_sync_settings.resolve({})
    assert effective == SyncSettings()
    assert (effective.push_interval_s, effective.pull_interval_s) == (300.0, 900.0)
    assert (effective.backoff_first_s, effective.backoff_cap_s) == (30.0, 600.0)
    assert not effective.active


def test_the_default_interval_is_marked_provisional_on_the_cadence_spike():
    assert "bh-infra-b7at" in hive_sync_settings.resolve({}).as_dict()["cadence_evidence"]
    push = HiveSyncConfig.model_fields["auto_push"].annotation
    description = push.model_fields["interval"].description
    assert "PROVISIONAL" in description and "bh-infra-b7at" in description


def test_every_key_validates_at_fleet_default_and_per_hive_override():
    raw = {
        "auto_commit": "batch",
        "auto_push": {
            "enabled": True,
            "interval": "PT1M",
            "max_pending": 7,
            "retry_backoff": "5s..2m",
        },
        "auto_pull": {"enabled": True, "interval": "90s"},
        "durable_verbs": ["submit", "finish", "close"],
    }
    cfg = BeadhiveConfig.model_validate(
        {"hive": {"sync": raw}, "managed_repos": [{"prefix": "fx", "hive": {"sync": raw}}]}
    )
    assert cfg.hive.sync.auto_push.max_pending == 7
    assert cfg.managed_repos[0].hive.sync.auto_pull.interval == "90s"


def test_yaml_on_is_auto_commit_on():
    assert HiveSyncConfig.model_validate({"auto_commit": True}).auto_commit == "on"


@pytest.mark.parametrize(
    "bad",
    [
        {"auto_commit": "off"},
        {"auto_push": {"interval": "0s"}},
        {"auto_push": {"interval": "soon"}},
        {"auto_push": {"max_pending": 0}},
        {"auto_push": {"retry_backoff": "10m..30s"}},
        {"auto_push": {"retry_backoff": "30s"}},
        {"auto_pull": {"interval": "-5m"}},
        {"durable_verbs": ["finish"]},
        {"durable_verbs": ["submit", "finish", "teleport"]},
        {"auto_push": {"unknown": 1}},
    ],
)
def test_bad_values_are_refused_at_both_layers(bad):
    with pytest.raises(ValidationError):
        HiveSyncConfig.model_validate(bad)
    with pytest.raises(ValidationError):
        HiveSyncOverride.model_validate(bad)


def test_a_hive_override_replaces_the_fleet_default_key_by_key():
    cfg = {
        "hive": {"sync": {"auto_push": {"enabled": True, "interval": "10m"}}},
        "managed_repos": [
            {"prefix": "a"},
            {"prefix": "b", "hive": {"sync": {"auto_push": {"interval": "1m"}}}},
            {"prefix": "c", "hive": {"sync": {"auto_push": {"enabled": False}}}},
        ],
    }
    a, b, c = (hive_sync_settings.for_entry(e, cfg) for e in cfg["managed_repos"])
    assert (a.push_enabled, a.push_interval_s) == (True, 600.0)
    assert (b.push_enabled, b.push_interval_s) == (True, 60.0)
    assert (c.push_enabled, c.push_interval_s) == (False, 600.0)


def test_hive_sync_is_a_fleet_key():
    from beadhive.modules.config.application.partition import FLEET_PREFIXES

    assert "hive.sync" in FLEET_PREFIXES


# ---- auto_commit maps onto bd's dolt.auto-commit ---------------------------------------------


def test_auto_commit_batch_maps_onto_bds_auto_commit_env(monkeypatch, tmp_path):
    from beadhive import registry

    monkeypatch.delenv(hive_sync_settings.BD_AUTO_COMMIT_ENV, raising=False)
    entry = {"prefix": "fx", "hive": {"sync": {"auto_commit": "batch"}}}
    cfg = {"managed_repos": [entry]}
    monkeypatch.setattr(registry, "entry_for_dir", lambda _cfg, _dir: entry)
    env = hive_sync_settings.bd_env(tmp_path, None, cfg=cfg)
    assert env[hive_sync_settings.BD_AUTO_COMMIT_ENV] == "batch"
    # `on` (bd's own default) leaves the environment untouched.
    plain = {"managed_repos": [{"prefix": "x"}]}
    assert hive_sync_settings.bd_env(tmp_path, None, cfg=plain) is None
    # An explicit choice by the caller wins.
    explicit = {hive_sync_settings.BD_AUTO_COMMIT_ENV: "on"}
    assert hive_sync_settings.bd_env(tmp_path, explicit, cfg=cfg) is explicit


def test_engine_writes_carry_batch_auto_commit_and_reads_skip_it(monkeypatch, tmp_path):
    looked: list[str] = []

    def bd_env(cwd, env):
        looked.append(str(cwd))
        return {hive_sync_settings.BD_AUTO_COMMIT_ENV: "batch"}

    seen: list = []

    def run(cmd, **kw):
        seen.append(kw.get("env"))
        return _ok()

    monkeypatch.setattr(hive_sync_settings, "bd_env", bd_env)
    monkeypatch.setattr(engine.bd_mod, "_run", run)
    monkeypatch.setattr(engine, "_forwarded", lambda _cwd, env: (env, None, False))
    monkeypatch.setattr(engine, "_replica_ident_refusal", lambda _cwd, _args: None)
    bd = engine.BdEngine()
    bd.invoke(["close", "fx-1"], cwd=tmp_path, capture=True)
    bd.invoke(["show", "fx-1"], cwd=tmp_path, capture=True)
    assert seen == [{hive_sync_settings.BD_AUTO_COMMIT_ENV: "batch"}, None]
    assert looked == [str(tmp_path)]


# =============================================================================================
# Auto-push: timer, max_pending, backoff against a dead remote
# =============================================================================================


def test_auto_push_pushes_on_the_timer_while_anything_is_pending(tmp_path):
    clock, remote = Clock(), Remote()
    loop, counts = _loop(tmp_path, clock=clock, remote=remote, settings=_settings())
    loop.tick()  # nothing pending: nothing pushed
    assert remote.pushes == []
    counts["n"] = 3
    clock.advance(60)
    loop.tick()  # pending counted, the timer has been due since start
    assert remote.pushes == [("fx", MAIN)]
    counts["n"] = 2
    clock.advance(120)
    loop.tick()  # inside the 5m interval: no push
    assert len(remote.pushes) == 1
    clock.advance(181)
    loop.tick()
    assert len(remote.pushes) == 2
    record = _record(loop)
    assert record.push.status == Status.OK and record.push.reason == "timer"
    assert record.pending == 0 and record.last_push_ok_at == clock()


def test_auto_push_pushes_early_after_max_pending_commits(tmp_path):
    clock, remote = Clock(), Remote()
    loop, counts = _loop(
        tmp_path, clock=clock, remote=remote, settings=_settings(max_pending=5), pending=1
    )
    loop.tick()  # the first push at start
    assert len(remote.pushes) == 1
    counts["n"] = 5
    clock.advance(61)  # well inside the 5m timer, past the count cadence
    loop.tick()
    assert len(remote.pushes) == 2
    assert _record(loop).push.reason == "max_pending"


def test_a_dead_remote_backs_off_per_retry_backoff_and_never_raises(tmp_path):
    clock, remote = Clock(), Remote(alive=False)
    settings = _settings(backoff_first_s=30.0, backoff_cap_s=100.0)
    loop, _counts = _loop(tmp_path, clock=clock, remote=remote, settings=settings, pending=4)
    delays = []
    for _ in range(5):
        outcomes = loop.tick()
        assert [o.status for o in outcomes] == [Status.FAILED]
        failed = outcomes[0]
        assert "connection refused" in failed.error
        delays.append(failed.next_retry_at - clock())
        clock.advance(failed.next_retry_at - clock() - 1)
        assert loop.tick() == []  # still backing off
        clock.advance(1)
    assert delays == [30.0, 60.0, 100.0, 100.0, 100.0]
    assert len(remote.pushes) == 5
    record = _record(loop)
    assert record.failures == 5 and "connection refused" in record.last_error
    # The remote comes back: the next retry lands and the backoff resets.
    remote.alive = True
    ok = loop.tick()
    assert [o.status for o in ok] == [Status.OK] and ok[0].reason == "retry"
    assert _record(loop).next_retry_at is None and _record(loop).last_error == ""


def test_a_failed_background_push_never_blocks_a_verb(tmp_path, monkeypatch):
    """A close without --await-sync returns at once while the loop is failing against a dead
    remote: it neither pushes nor waits."""
    clock, remote = Clock(), Remote(alive=False)
    loop, _counts = _loop(tmp_path, clock=clock, remote=remote, settings=_settings(), pending=2)
    loop.tick()
    world = _close_world(monkeypatch, tmp_path, sync={"auto_push": {"enabled": True}})

    def no_wait(_seconds):
        raise AssertionError("a verb without --await-sync must not wait")

    work_close.close(
        "fx-1", push=world.push, wait=no_wait, clock=clock, now=clock, state_path=loop.state_path
    )
    assert world.pushes == [] and world.closed == ["fx-1"]
    assert _record(loop).flush_requested_at is None


# =============================================================================================
# Only the writer pushes main; an open replica pushes only its frame ref
# =============================================================================================


def test_a_non_writer_never_pushes_main(tmp_path):
    clock, remote = Clock(), Remote()
    route = Route(NONE, reason="not the writer (frame-a is; this frame is frame-b)")
    loop, _counts = _loop(
        tmp_path, clock=clock, remote=remote, settings=_settings(), route=route, pending=9
    )
    for _ in range(3):
        loop.tick()
        clock.advance(400)
    assert remote.pushes == []
    push = _record(loop).push
    assert push.status == Status.SKIPPED and "not the writer" in push.error


def test_an_open_replica_pushes_only_its_frame_ref(tmp_path):
    clock, remote = Clock(), Remote()
    route = Route(FRAME, "refs/dolt/frame/frame-b", open=True, frame="frame-b")
    loop, _counts = _loop(
        tmp_path, clock=clock, remote=remote, settings=_settings(), route=route, pending=2
    )
    loop.tick()
    clock.advance(400)
    loop.tick()
    assert remote.pushes == [("fx", "refs/dolt/frame/frame-b")] * 2


@pytest.mark.parametrize(
    ("off", "policy", "expected"),
    [
        (False, "fenced", (MAIN, MAIN)),
        (False, "open", (MAIN, MAIN)),
        (True, "open", (FRAME, "refs/dolt/frame/frame-b")),
        (True, "fenced", (NONE, "")),
        (True, "forward", (NONE, "")),
    ],
)
def test_live_route_decides_as_the_guard_does(monkeypatch, tmp_path, off, policy, expected):
    where = write_policy.OffPrimary("fx", "frame-a", "frame-b", True) if off else None
    effective = write_policy.EffectivePolicy(policy, "open", policy, hive="fx")
    monkeypatch.setattr(write_policy, "off_primary", lambda **_kw: where)
    monkeypatch.setattr(write_policy, "for_dir", lambda _dir, cfg=None: effective)
    route = hive_sync_loop.live_route(SyncHive("fx", tmp_path, _settings()))
    assert (route.kind, route.target) == expected
    if route.kind == NONE:
        assert "only the writer pushes main" in route.reason


def test_an_unreadable_fence_never_pushes(tmp_path):
    clock, remote = Clock(), Remote()
    hive = SyncHive("fx", tmp_path, _settings())

    def unreadable(_hive):
        raise RuntimeError("bh_writer unreadable")

    state = tmp_path / "s.json"
    loop = SyncLoop(
        hives=lambda: [hive],
        route=unreadable,
        pending=lambda *_: 3,
        push=remote.push,
        pull=remote.pull,
        merge_frames=lambda _h: [],
        clock=clock,
        state_path=state,
    )
    loop.tick()
    assert remote.pushes == []
    assert "cannot decide who writes" in hive_sync_state.recorded("fx", path=state).push.error


# =============================================================================================
# Auto-pull; the writer under open runs the frame-ref merge slice
# =============================================================================================


def test_a_replica_pulls_on_the_timer(tmp_path):
    clock, remote = Clock(), Remote()
    settings = _settings(push_enabled=False, pull_enabled=True)
    loop, _counts = _loop(
        tmp_path, clock=clock, remote=remote, settings=settings, route=Route(NONE, reason="x")
    )
    loop.tick()
    clock.advance(600)
    loop.tick()
    clock.advance(301)
    loop.tick()
    assert remote.pulls == ["fx", "fx"]
    assert _record(loop).pull.status == Status.OK


def test_the_writer_under_open_merges_frame_refs_and_a_held_ref_blocks_nothing(tmp_path):
    from beadhive import frame_merge

    clock, remote = Clock(), Remote()
    held = frame_merge.FrameMergeOutcome(
        "fx", "frame-b", frame_merge.Status.CONFLICT, clock(), error="conflict on issues"
    )
    merged = frame_merge.FrameMergeOutcome("fx", "frame-c", frame_merge.Status.MERGED, clock())
    calls: list[str] = []

    def merges(hive):
        calls.append(hive.hive)
        return [held, merged]

    loop, _counts = _loop(
        tmp_path,
        clock=clock,
        remote=remote,
        settings=_settings(pull_enabled=True),
        route=Route(MAIN, MAIN, open=True),
        pending=2,
        merges=merges,
    )
    outcomes = loop.tick()
    assert calls == ["fx"] and remote.pulls == []  # the writer merges; it does not dolt-pull
    assert [(o.op, o.status) for o in outcomes] == [("push", Status.OK), ("pull", Status.OK)]
    pull = _record(loop).pull
    assert [m["frame"] for m in pull.frame_merges] == ["frame-b", "frame-c"]
    assert pull.frame_merges[0]["status"] == frame_merge.Status.CONFLICT


def test_the_writer_off_open_pulls_nothing(tmp_path):
    clock, remote = Clock(), Remote()
    called: list = []
    settings = _settings(push_enabled=False, pull_enabled=True)
    loop, _counts = _loop(
        tmp_path,
        clock=clock,
        remote=remote,
        settings=settings,
        merges=lambda hive: called.append(hive) or [],
    )
    loop.tick()
    assert remote.pulls == [] and called == []
    assert _record(loop).pull.status == Status.SKIPPED


def test_live_merge_frames_is_the_frame_merge_after_pull_seam(monkeypatch, tmp_path):
    from beadhive import frame_merge

    seen = []
    monkeypatch.setattr(
        frame_merge, "after_pull", lambda hive_dir, cfg=None: seen.append(hive_dir) or []
    )
    monkeypatch.setattr(hive_sync_loop, "_settings_port", lambda: SimpleNamespace(load=lambda: {}))
    assert hive_sync_loop.live_merge_frames(SyncHive("fx", tmp_path, _settings())) == []
    assert seen == [tmp_path]


# =============================================================================================
# Durable verbs: submit and finish push synchronously; close --await-sync waits for its flush
# =============================================================================================


def test_submit_and_finish_are_always_durable():
    for durable_verbs in ([], ["close"], ["submit", "finish"]):
        settings = hive_sync_settings.resolve({"durable_verbs": durable_verbs})
        assert hive_sync_settings.durable("submit", settings)
        assert hive_sync_settings.durable("finish", settings)
    assert not hive_sync_settings.durable("close", hive_sync_settings.resolve({}))
    durable_close = hive_sync_settings.resolve({"durable_verbs": ["submit", "finish", "close"]})
    assert hive_sync_settings.durable("close", durable_close)


def test_finish_pushes_state_synchronously():
    pushed = []
    api = SimpleNamespace(_push_state=lambda cfg, main, actor, message: pushed.append(message))
    work_merge._durable_push(api, {}, Path("/hive"), "finish fx-epic")
    assert pushed == ["finish fx-epic"]


def test_submit_still_pushes_state_synchronously():
    import inspect

    from beadhive import work_submission

    source = inspect.getsource(work_submission)
    assert 'api._push_state(cfg, main, actor, f"submit {bead} @ {sha}")' in source


def _close_world(monkeypatch, tmp_path, *, sync=None):
    entry: dict = {"prefix": "fx"}
    if sync is not None:
        entry["hive"] = {"sync": sync}
    cfg = {"managed_repos": [entry]}
    world = SimpleNamespace(closed=[], pushes=[])

    class Routes:
        def issue_close(self, bead, **_kw):
            world.closed.append(bead)
            return _ok()

    monkeypatch.setattr(work_close, "config", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(
        work_close,
        "registry",
        SimpleNamespace(
            hive_dir_for=lambda _cfg, _hive: tmp_path, entry_for_dir=lambda _c, _d: entry
        ),
    )
    monkeypatch.setattr(work_close, "bd_cli", SimpleNamespace(routes=lambda _dir: Routes()))
    monkeypatch.setattr(work_close.identity, "resolve_actor", lambda _as="": "dev/test")

    def push(_cfg, _dir, _actor, message):
        world.pushes.append(message)
        return True, ""

    world.push = push
    return world


def test_close_await_sync_waits_for_the_flush_that_includes_its_commits(monkeypatch, tmp_path):
    clock, remote = Clock(), Remote()
    loop, counts = _loop(tmp_path, clock=clock, remote=remote, settings=_settings())
    loop.tick()  # the daemon is idle: nothing pending
    counts["n"] = 1
    clock.advance(400)
    loop.tick()  # an earlier write is pushed on the timer
    early = _record(loop).push
    assert len(remote.pushes) == 1
    world = _close_world(monkeypatch, tmp_path, sync={"auto_push": {"enabled": True}})
    clock.advance(1)
    waits: list[float] = []

    def daemon_ticks(seconds):
        # The handshake: while the verb waits, the daemon's loop runs one pass.
        waits.append(seconds)
        clock.advance(seconds)
        counts["n"] = 1  # the close's commit is pending
        loop.tick()

    work_close.close(
        "fx-1",
        await_sync=True,
        push=world.push,
        wait=daemon_ticks,
        clock=clock,
        now=clock,
        state_path=loop.state_path,
    )
    assert world.closed == ["fx-1"] and world.pushes == []  # the daemon pushed, not the verb
    assert waits == [1.0] and len(remote.pushes) == 2
    flushed = _record(loop).push
    # The push that answered started after the close asked; the earlier one did not count.
    assert flushed.reason == "flush" and flushed.attempted_at > early.attempted_at
    assert flushed.attempted_at >= _record(loop).flush_requested_at


def test_a_push_started_before_the_request_does_not_answer_it(tmp_path):
    clock, remote = Clock(), Remote()
    loop, _counts = _loop(tmp_path, clock=clock, remote=remote, settings=_settings(), pending=1)
    loop.tick()
    clock.advance(1)
    requested = hive_sync_state.request_flush("fx", now=clock, path=loop.state_path)
    assert hive_sync_state.flushed("fx", requested, path=loop.state_path) is None
    assert _record(loop).flush_pending


def test_close_await_sync_times_out_naming_the_last_error(monkeypatch, tmp_path, capsys):
    clock, remote = Clock(), Remote(alive=False)
    loop, _counts = _loop(tmp_path, clock=clock, remote=remote, settings=_settings(), pending=1)
    world = _close_world(monkeypatch, tmp_path, sync={"auto_push": {"enabled": True}})

    def daemon_ticks(seconds):
        clock.advance(seconds)
        loop.tick()

    with pytest.raises(typer.Exit) as exc:
        work_close.close(
            "fx-1",
            await_sync=True,
            await_timeout=5,
            push=world.push,
            wait=daemon_ticks,
            clock=clock,
            now=clock,
            state_path=loop.state_path,
        )
    assert exc.value.exit_code == 1
    assert "connection refused" in capsys.readouterr().err
    assert world.closed == ["fx-1"]  # the close itself stands


def test_close_await_sync_without_auto_push_pushes_now(monkeypatch, tmp_path):
    world = _close_world(monkeypatch, tmp_path)

    def no_wait(_seconds):
        raise AssertionError("nothing to wait for without auto-push")

    work_close.close(
        "fx-1", await_sync=True, push=world.push, wait=no_wait, state_path=tmp_path / "s"
    )
    assert world.pushes == ["close fx-1"]


def test_close_as_a_durable_verb_pushes_synchronously(monkeypatch, tmp_path):
    world = _close_world(
        monkeypatch,
        tmp_path,
        sync={"auto_push": {"enabled": True}, "durable_verbs": ["submit", "finish", "close"]},
    )
    work_close.close("fx-1", push=world.push, state_path=tmp_path / "s")
    assert world.pushes == ["close fx-1"]


# =============================================================================================
# Typed outcomes in doctor
# =============================================================================================


def test_doctor_shows_pending_last_push_last_error_and_next_retry(tmp_path):
    clock, remote = Clock(), Remote()
    loop, counts = _loop(tmp_path, clock=clock, remote=remote, settings=_settings(), pending=2)
    loop.tick()  # lands
    remote.alive = False
    counts["n"] = 4
    clock.advance(400)
    loop.tick()  # fails against the dead remote
    cfg = {
        "hive": {"sync": {"auto_push": {"enabled": True}}},
        "managed_repos": [{"prefix": "fx"}, {"prefix": "hq", "kind": "hq"}, {"prefix": "idle"}],
    }
    data = write_policy.sync_doctor_data(cfg, path=loop.state_path)
    assert [item["hive"] for item in data["hives"]] == ["fx", "idle"]
    item = data["hives"][0]
    assert item["pending"] == 4
    assert item["last_push_ok_at"] == clock() - 400
    assert "connection refused" in item["last_error"]
    assert item["next_retry_at"] == clock() + 30
    assert item["last_push"]["status"] == Status.FAILED
    assert data["errors"] == 1
    lines = hive_sync_state.doctor_lines(data, now=clock())
    assert lines[0] == "  ⚠ fx: hive.sync 4 pending, last push 400s ago, next retry in 30s"
    assert "connection refused" in lines[1]
    assert lines[2] == "  ✓ idle: hive.sync ? pending, last push never"


def test_outcomes_round_trip_the_record(tmp_path):
    outcome = hive_sync_state.SyncOutcome(
        "fx",
        "pull",
        Status.OK,
        1.0,
        2.0,
        target=MAIN,
        reason="timer",
        frame_merges=({"frame": "b", "status": "conflict", "error": "x"},),
    )
    hive_sync_state.record(outcome, path=tmp_path / "s.json")
    assert hive_sync_state.recorded("fx", path=tmp_path / "s.json").pull == outcome


def test_close_await_sync_on_a_non_writer_says_why_nothing_flushes(monkeypatch, tmp_path, capsys):
    clock, remote = Clock(), Remote()
    route = Route(NONE, reason="not the writer (frame-a is)")
    loop, _counts = _loop(
        tmp_path, clock=clock, remote=remote, settings=_settings(), route=route, pending=1
    )
    world = _close_world(monkeypatch, tmp_path, sync={"auto_push": {"enabled": True}})

    def daemon_ticks(seconds):
        clock.advance(seconds)
        loop.tick()

    with pytest.raises(typer.Exit):
        work_close.close(
            "fx-1",
            await_sync=True,
            await_timeout=3,
            push=world.push,
            wait=daemon_ticks,
            clock=clock,
            now=clock,
            state_path=loop.state_path,
        )
    assert "not the writer (frame-a is)" in capsys.readouterr().err
    assert remote.pushes == []
