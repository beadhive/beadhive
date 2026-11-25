"""Bead-metadata outcome sink (gas-city-runtime-seam-adr Decision 2, bh-im4dm).

- `outcome.read` classifies a record exactly as the stdout path classifies the same line, for
  done / blocked / handoff / failed fixtures.
- An absent record is None; a stale or mismatched session record is ignored.
- `bh role --outcome-sink bead` writes ONE metadata key as the stamped seat identity, and the
  default stdout path never touches bead metadata.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from beadhive import bd, bd_cli, outcome, seatrun
from beadhive.cli import app
from beadhive.runtime import RoleHandle, RoleOutcome

cli_runner = CliRunner()

SESSION = "provider-0b8f"
BEAD = "bh-fixture"


def _seat_run(status: str, summary: str, session: str = SESSION) -> str:
    return json.dumps(
        {
            "outcome": {"status": status, "summary": summary, "bead_id": BEAD},
            "session_id": session,
            "cost_usd": 0.25,
            "usage": {"input_tokens": 10, "output_tokens": 3},
            "packs": [],
        }
    )


ENVELOPE = json.dumps(
    {
        "session_id": SESSION,
        "cost_usd": 0.1,
        "usage": {"input_tokens": 1},
        "terminal_reason": "aborted_streaming",
    }
)

#: (name, exit code, stdout, expected status) — the stdout path's own verdicts.
FIXTURES = [
    ("done", 0, _seat_run("done", "shipped it"), "done"),
    ("blocked", 0, _seat_run("blocked", "needs a decision"), "blocked"),
    ("handoff", 0, _seat_run("handoff", "over to review"), "handoff"),
    ("failed-envelope", 143, ENVELOPE, "failed"),
    ("failed-empty", 1, "", "failed"),
    ("failed-garbage-exit-0", 0, "not json at all", "failed"),
]


def _stdout_path(exit_code: int, stdout: str) -> RoleOutcome:
    return outcome.role_outcome(seatrun.classify_run(exit_code, stdout, bead=BEAD))


def _metadata_path(exit_code: int, stdout: str) -> RoleOutcome | None:
    document = outcome.record_document(stdout, SESSION, exit_code=exit_code)
    return outcome.from_metadata({outcome.metadata_key(SESSION): document}, SESSION, bead_id=BEAD)


@pytest.mark.parametrize(("name", "exit_code", "stdout", "status"), FIXTURES)
def test_record_classifies_like_the_stdout_path(name, exit_code, stdout, status):
    via_stdout = _stdout_path(exit_code, stdout)
    via_record = _metadata_path(exit_code, stdout)

    assert via_record is not None, name
    assert via_stdout.status == via_record.status == status
    if status != "failed":
        assert via_record == via_stdout


def test_seat_run_record_is_stored_as_printed():
    line = _seat_run("done", "shipped it")
    assert outcome.record_document(line + "\n", SESSION, exit_code=0) == json.loads(line)


def test_record_string_value_is_read_too():
    md = {outcome.metadata_key(SESSION): _seat_run("blocked", "why")}
    assert outcome.from_metadata(md, SESSION) == RoleOutcome(status="blocked", summary="why")


def test_absent_record_is_none_and_observe_reports_running(monkeypatch):
    monkeypatch.setattr(bd, "show", lambda bead, cwd, **_kw: {"id": bead, "metadata": {}})

    assert outcome.read(BEAD, SESSION) is None
    observed = outcome.observe(RoleHandle(bead_id=BEAD, session_id=SESSION))
    assert observed.status == "running"


def test_missing_bead_is_none(monkeypatch):
    monkeypatch.setattr(bd, "show", lambda bead, cwd, **_kw: None)
    assert outcome.read(BEAD, SESSION) is None


def test_stale_session_record_is_ignored(monkeypatch):
    """A record keyed for this session but carrying another session's SeatRun answers nothing."""
    stale = json.loads(_seat_run("done", "old run", session="provider-old"))
    md = {
        outcome.metadata_key(SESSION): stale,
        outcome.metadata_key("provider-old"): stale,
    }
    monkeypatch.setattr(bd, "show", lambda bead, cwd, **_kw: {"metadata": md})

    assert outcome.read(BEAD, SESSION) is None
    assert outcome.read(BEAD, "provider-old") == RoleOutcome(status="done", summary="old run")
    assert outcome.observe(RoleHandle(bead_id=BEAD, session_id=SESSION)).status == "running"


def test_unattributable_record_is_ignored():
    md = {outcome.metadata_key(SESSION): {"outcome": "nonsense"}}
    assert outcome.from_metadata(md, SESSION) is None


def test_bare_exit_zero_record_is_never_success():
    document = outcome.record_document("", SESSION, exit_code=0)
    found = outcome.from_metadata({outcome.metadata_key(SESSION): document}, SESSION)
    assert found is not None and found.status == "failed"


def test_metadata_key_needs_a_session():
    with pytest.raises(ValueError):
        outcome.metadata_key("")


class _Routes:
    def __init__(self, calls, rc=0):
        self.calls = calls
        self.rc = rc

    def issue_set_metadata_document(self, bead, key, document, *, actor="", capture=True):
        self.calls.append({"bead": bead, "key": key, "document": document, "actor": actor})
        return SimpleNamespace(returncode=self.rc, stdout="", stderr="refused")


def test_write_touches_one_key_as_the_actor(monkeypatch, tmp_path):
    calls: list[dict] = []
    monkeypatch.setattr(bd, "show", lambda bead, cwd, **_kw: {"metadata": {"other": "x"}})
    monkeypatch.setattr(bd_cli, "routes", lambda cwd: _Routes(calls))

    result = outcome.write(
        BEAD, SESSION, _seat_run("done", "ok"), exit_code=0, cwd=tmp_path, actor="dev/os1"
    )

    assert result.written
    assert calls == [
        {
            "bead": BEAD,
            "key": f"bh.outcome.{SESSION}",
            "document": json.loads(_seat_run("done", "ok")),
            "actor": "dev/os1",
        }
    ]


def test_write_is_append_only_per_session(monkeypatch, tmp_path):
    calls: list[dict] = []
    md = {outcome.metadata_key(SESSION): json.loads(_seat_run("done", "first"))}
    monkeypatch.setattr(bd, "show", lambda bead, cwd, **_kw: {"metadata": md})
    monkeypatch.setattr(bd_cli, "routes", lambda cwd: _Routes(calls))

    result = outcome.write(
        BEAD, SESSION, _seat_run("blocked", "second"), exit_code=0, cwd=tmp_path, actor="dev/a"
    )

    assert not result.written
    assert calls == []


def test_write_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "show", lambda bead, cwd, **_kw: {"metadata": {}})
    monkeypatch.setattr(bd_cli, "routes", lambda cwd: _Routes([], rc=1))

    with pytest.raises(outcome.OutcomeWriteError):
        outcome.write(BEAD, SESSION, "", exit_code=1, cwd=tmp_path, actor="dev/a")


# ---- bh role --outcome-sink ------------------------------------------------------------------


def _wire_headless(monkeypatch, tmp_path, stdout: str, exit_code: int):
    """Headless unqualified `bh role` whose seat prints ``stdout`` and exits ``exit_code``."""
    from beadhive import cli, identity, localloop, role_execution

    plan = role_execution.RoleLaunchPlan(backend="baml", detail="fixture seat", provider=None)
    entry = {"provider": "github", "org": "acme", "repo": "core"}
    argv_seen: list[list[str]] = []
    script = f"import sys; sys.stdout.write({stdout!r}); sys.exit({exit_code})"

    def fake_seat_argv(*_args, session_id, **_kwargs):
        argv_seen.append(["--session_id", session_id])
        return [sys.executable, "-c", script]

    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(role_execution, "resolve_headless_plan", lambda *_a, **_kw: plan)
    monkeypatch.setattr(cli, "_apply_role_workspace", lambda *_args: None)
    monkeypatch.setattr(cli, "_role_dispatch_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.config, "dispatch_seat_command", lambda *_args: "bh-developer")
    monkeypatch.setattr(cli.config, "dispatch_seat_bundle", lambda *_args: "")
    monkeypatch.setattr(cli.registry, "entry_for_dir", lambda *_args: entry)
    monkeypatch.setattr(cli.registry, "hive_dir", lambda _entry: tmp_path)
    monkeypatch.setattr(identity, "resolve_actor", lambda *_a, **_kw: "dev/seat")
    monkeypatch.setattr(localloop, "seat_argv", fake_seat_argv)
    return argv_seen


@pytest.mark.parametrize(("name", "exit_code", "stdout", "status"), FIXTURES)
def test_role_bead_sink_round_trips_with_stdout_parity(
    monkeypatch, tmp_path, name, exit_code, stdout, status
):
    argv_seen = _wire_headless(monkeypatch, tmp_path, stdout, exit_code)
    calls: list[dict] = []
    monkeypatch.setattr(bd, "show", lambda bead, cwd, **_kw: {"metadata": {}})
    monkeypatch.setattr(bd_cli, "routes", lambda cwd: _Routes(calls))

    result = cli_runner.invoke(
        app,
        [
            "role",
            "developer",
            "--bead",
            BEAD,
            "--outcome-sink",
            "bead",
            "--session-id",
            SESSION,
        ],
    )

    assert result.exit_code == exit_code, result.output
    assert argv_seen == [["--session_id", SESSION]]
    assert stdout in result.stdout  # still forwarded
    assert len(calls) == 1
    call = calls[0]
    assert call["key"] == f"bh.outcome.{SESSION}"
    assert call["actor"] == "dev/seat"
    assert call["bead"] == BEAD
    via_record = outcome.from_metadata({call["key"]: call["document"]}, SESSION, bead_id=BEAD)
    via_stdout = _stdout_path(exit_code, stdout)
    assert via_record is not None
    assert via_record.status == via_stdout.status == status


def test_role_default_sink_never_writes_metadata(monkeypatch, tmp_path):
    from beadhive import cli

    _wire_headless(monkeypatch, tmp_path, _seat_run("done", "ok"), 0)
    runs: list[list[str]] = []
    monkeypatch.setattr(
        cli,
        "run",
        lambda argv, **kwargs: runs.append(argv) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(bd_cli, "routes", lambda cwd: pytest.fail("stdout sink wrote metadata"))
    monkeypatch.setattr(bd, "show", lambda *_a, **_kw: pytest.fail("stdout sink read the bead"))
    monkeypatch.setattr(
        cli, "_run_forwarding_stdout", lambda *_a, **_kw: pytest.fail("stdout sink teed")
    )

    result = cli_runner.invoke(app, ["role", "developer", "--bead", BEAD, "--task", "t"])

    assert result.exit_code == 0, result.output
    assert len(runs) == 1


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["developer", "--outcome-sink", "bead"], "requires a managed bead"),
        (["planner", "--outcome-sink", "bead", "--task", "t"], "needs --bead"),
        (["developer", "--outcome-sink", "bead", "--bead", BEAD, "-d"], "cannot detach"),
        (["developer", "--outcome-sink", "nope", "--bead", BEAD], "must be stdout or bead"),
    ],
)
def test_role_bead_sink_refusals_happen_before_the_workspace(monkeypatch, tmp_path, args, needle):
    from beadhive import cli

    _wire_headless(monkeypatch, tmp_path, "", 0)
    monkeypatch.setattr(
        cli, "_apply_role_workspace", lambda *_a: pytest.fail("refusal claimed a workspace")
    )

    result = cli_runner.invoke(app, ["role", *args])

    assert result.exit_code == 1
    assert needle in result.output


def test_role_bead_sink_refuses_hitch(monkeypatch, tmp_path):
    from beadhive import cli, role_execution

    _wire_headless(monkeypatch, tmp_path, "", 0)
    plan = role_execution.RoleLaunchPlan(backend="hitch", detail="hitch profile", provider=None)
    monkeypatch.setattr(role_execution, "resolve_headless_plan", lambda *_a, **_kw: plan)
    monkeypatch.setattr(
        cli, "_apply_role_workspace", lambda *_a: pytest.fail("refusal claimed a workspace")
    )

    result = cli_runner.invoke(app, ["role", "developer", "--bead", BEAD, "--outcome-sink", "bead"])

    assert result.exit_code == 1
    assert "hitch does not" in result.output


def test_role_bead_sink_write_failure_fails_the_run(monkeypatch, tmp_path):
    _wire_headless(monkeypatch, tmp_path, _seat_run("done", "ok"), 0)
    monkeypatch.setattr(bd, "show", lambda bead, cwd, **_kw: {"metadata": {}})
    monkeypatch.setattr(bd_cli, "routes", lambda cwd: _Routes([], rc=1))

    result = cli_runner.invoke(
        app,
        ["role", "developer", "--bead", BEAD, "--outcome-sink", "bead", "--session-id", SESSION],
    )

    assert result.exit_code == 1
    assert "outcome_sink_write_failed" in result.output


def test_qualified_foreground_hands_the_harvest_to_the_writer(tmp_path):
    """role_process passes the very stdout it classified to the outcome writer."""
    import os

    from beadhive import role_process, run_journal

    journal = run_journal.RunJournal.create(
        run_journal.RunIdentity(
            hive="github/acme/core",
            bead=BEAD,
            driver="baml",
            provider="codex",
            manifest_digest="sha256:" + "d" * 64,
        ),
        base=tmp_path,
        writer=run_journal.WRITER_BAML,
    )
    line = _seat_run("handoff", "to review")
    seen: list[tuple[str, int | None]] = []

    code = role_process.run_foreground(
        [sys.executable, "-c", f"print({line!r})"],
        cwd=tmp_path,
        env=dict(os.environ),
        journal=journal,
        bead=BEAD,
        role="developer",
        seat_process_id="seat-x",
        provider_continuation=SESSION,
        outcome_writer=lambda stdout, rc: seen.append((stdout, rc)) or True,
    )

    assert code == 0
    assert len(seen) == 1
    assert seen[0][0].strip() == line
    found = outcome.from_metadata(
        {outcome.metadata_key(SESSION): outcome.record_document(seen[0][0], SESSION, exit_code=0)},
        SESSION,
    )
    assert found == RoleOutcome(status="handoff", summary="to review")


@pytest.mark.parametrize(("name", "exit_code", "stdout", "status"), FIXTURES)
def test_record_matches_local_runtime_observe(tmp_path, name, exit_code, stdout, status):
    """The metadata path and LocalRuntime.observe (the held-pipe path) agree on every fixture."""
    from beadhive import localloop

    payload = tmp_path / "stdout.txt"
    payload.write_text(stdout, encoding="utf-8")
    seat = tmp_path / "seat.py"
    seat.write_text(
        "import pathlib, sys\n"
        f"sys.stdout.write(pathlib.Path({str(payload)!r}).read_text())\n"
        "sys.stdout.flush()\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    instructions = tmp_path / "i.md"
    instructions.write_text("fixture\n", encoding="utf-8")
    with localloop.LocalRuntime(seat_command=f"{sys.executable} {seat}") as rt:
        handle = rt.schedule(
            BEAD,
            "developer",
            workspace=str(tmp_path),
            instructions=str(instructions),
            session_id=SESSION,
        )
        assert rt._submit(rt._runs[BEAD].wait_exit(30)), "fixture seat never exited"
        via_pipe = rt.observe(handle)

    via_record = _metadata_path(exit_code, stdout)
    assert via_record is not None
    assert via_pipe.status == via_record.status == status, name
    if status != "failed":
        assert (via_pipe.status, via_pipe.summary) == (via_record.status, via_record.summary)


def test_signalled_foreground_writes_a_failed_record(tmp_path):
    """A SIGTERM on the qualified path leaves a record that classifies failed (bh-fz6ee).

    The signal is delivered by invoking the installed handler directly, not by racing a real
    signal against the run."""
    import asyncio
    import os
    import signal

    from beadhive import role_process, run_journal

    journal = run_journal.RunJournal.create(
        run_journal.RunIdentity(
            hive="github/acme/core",
            bead=BEAD,
            driver="baml",
            provider="codex",
            manifest_digest="sha256:" + "d" * 64,
        ),
        base=tmp_path,
        writer=run_journal.WRITER_BAML,
    )
    handlers: dict[int, tuple] = {}

    class _Loop:
        def __init__(self, real):
            self._real = real

        def add_signal_handler(self, sig, cb, *args):
            handlers[sig] = (cb, args)

        def remove_signal_handler(self, sig):
            return True

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_loop = asyncio.get_running_loop
    real_spawn = role_process.localloop.spawn_seat

    async def spawn_then_signal(*args, **kwargs):
        seat = await real_spawn(*args, **kwargs)
        cb, cb_args = handlers[signal.SIGTERM]
        cb(*cb_args)
        return seat

    seen: list[tuple[str, int | None]] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(role_process.asyncio, "get_running_loop", lambda: _Loop(real_loop()))
        mp.setattr(role_process.localloop, "spawn_seat", spawn_then_signal)
        code = role_process.run_foreground(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            env=dict(os.environ),
            journal=journal,
            bead=BEAD,
            role="developer",
            seat_process_id="seat-x",
            provider_continuation=SESSION,
            outcome_writer=lambda stdout, rc: seen.append((stdout, rc)) or True,
        )

    assert code == 128 + signal.SIGTERM
    assert len(seen) == 1
    assert seen[0][1] == code
    doc = outcome.record_document(seen[0][0], SESSION, exit_code=code)
    found = outcome.from_metadata({outcome.metadata_key(SESSION): doc}, SESSION)
    assert found is not None and found.status == "failed"
    assert doc["terminal_reason"] == "terminated by signal 15"
