"""The outbox write pacer at the composition roots (``bh-eqtxv``).

* the CLI's interrupt handler (design test 9, the CLI half): installed only in the main thread,
  the first ``SIGINT`` sets the stop and puts back ``signal.default_int_handler`` (so a second one
  raises ``KeyboardInterrupt``), and the handler installed before the call is restored after it,
  on a normal end and on an exception. The handler is called directly: no signal is sent;
* the batch JSON's reason-coded ``stopped``, ``stopped_detail`` and ``pacing``, and the non-zero
  exit on ``stopped: error``;
* the two config keys (design test 4): defaults 80 and 500, refused above GitHub's figures or at
  0 with a message naming them; the composition root reads them, builds one pacer per command
  over the bound API host's ledger, and hands it to the adapters it builds as their write gate.
"""

from __future__ import annotations

import json
import signal
import threading

import pytest
import typer
from pydantic import ValidationError

from beadhive import intake_services, work_intake
from beadhive.bootstrap.tracker import TrackerAdapters
from beadhive.modules.config.contracts import (
    OUTBOX_MAX_WRITES_PER_HOUR,
    OUTBOX_MAX_WRITES_PER_MINUTE,
    IntakeOutboxConfig,
)
from beadhive.modules.intake.domain import (
    MAX_WRITES_PER_HOUR,
    MAX_WRITES_PER_MINUTE,
    BatchSendFailure,
    BatchSendOutcome,
    OpKind,
    PacingSummary,
    StopReason,
)
from beadhive.modules.tracker.contracts import Operation

# -- the interrupt handler ---------------------------------------------------------------------


def test_the_first_sigint_sets_the_stop_and_the_second_raises() -> None:
    def custom(_signum, _frame) -> None:
        pass

    before = signal.signal(signal.SIGINT, custom)
    try:
        with work_intake.interrupt_stop() as stop:
            handler = signal.getsignal(signal.SIGINT)
            assert handler is not custom and not stop.is_set()
            handler(signal.SIGINT, None)  # the first SIGINT
            assert stop.reason is StopReason.INTERRUPTED
            assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
            with pytest.raises(KeyboardInterrupt):  # the second
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert signal.getsignal(signal.SIGINT) is custom  # the previous one, not the default

        with pytest.raises(RuntimeError), work_intake.interrupt_stop():
            raise RuntimeError("the batch failed")
        assert signal.getsignal(signal.SIGINT) is custom
    finally:
        signal.signal(signal.SIGINT, before)


def test_no_handler_is_installed_outside_the_main_thread() -> None:
    before = signal.getsignal(signal.SIGINT)
    seen = []

    def run() -> None:
        with work_intake.interrupt_stop() as stop:
            seen.append((signal.getsignal(signal.SIGINT), stop.is_set()))

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(10)
    assert seen == [(before, False)]


# -- the batch output -------------------------------------------------------------------------


def _render(outcome: BatchSendOutcome, capsys) -> tuple[dict, int]:
    call = intake_services.OutboxCall(0, value=outcome)
    code = 0
    try:
        work_intake.render_outbox(call, "send-approved", True)
    except typer.Exit as exited:
        code = exited.exit_code
    return json.loads(capsys.readouterr().out), code


def test_the_batch_json_carries_the_reason_code_its_detail_and_the_pacing(capsys) -> None:
    outcome = BatchSendOutcome(
        failed=(BatchSendFailure("acme-1", "ob1_x", OpKind.COMMENT, "hourly_budget", "frees"),),
        stopped="hourly_budget",
        stopped_detail="frees at 2027-01-15T08:00:00Z",
        pacing=PacingSummary(80, 500, requests=12, waited_seconds=8.25, budget_frees_at=1.5),
    )

    payload, code = _render(outcome, capsys)

    assert code == 1 and payload["ok"] is False
    assert payload["stopped"] == "hourly_budget"
    assert payload["stopped_detail"] == "frees at 2027-01-15T08:00:00Z"
    assert payload["pacing"] == {
        "writes_per_minute": 80,
        "writes_per_hour": 500,
        "requests": 12,
        "waited_seconds": 8.25,
        "budget_frees_at": 1.5,
    }
    assert payload["failed"][0]["reason"] == "hourly_budget"


def test_an_error_stop_exits_non_zero(capsys) -> None:
    outcome = BatchSendOutcome(
        failed=(BatchSendFailure("acme-1", "ob1_x", OpKind.COMMENT, "error", "boom"),),
        stopped="error",
        stopped_detail="boom",
    )
    payload, code = _render(outcome, capsys)
    assert code == 1 and payload["stopped"] == "error"


# -- the keys and the composition root ----------------------------------------------------------


def test_the_pace_keys_default_to_githubs_limits_and_refuse_anything_above() -> None:
    section = IntakeOutboxConfig()
    assert (section.send_writes_per_minute, section.send_writes_per_hour) == (80, 500)
    assert (OUTBOX_MAX_WRITES_PER_MINUTE, OUTBOX_MAX_WRITES_PER_HOUR) == (
        MAX_WRITES_PER_MINUTE,
        MAX_WRITES_PER_HOUR,
    )
    assert IntakeOutboxConfig(send_writes_per_minute=1, send_writes_per_hour=1)
    for key, value, figure in (
        ("send_writes_per_minute", 81, "80"),
        ("send_writes_per_minute", 0, "80"),
        ("send_writes_per_hour", 501, "500"),
        ("send_writes_per_hour", 0, "500"),
    ):
        with pytest.raises(ValidationError, match=f"GitHub documents at most {figure}"):
            IntakeOutboxConfig(**{key: value})


def test_the_root_reads_the_pace_and_refuses_one_outside_the_bounds() -> None:
    def cfg(**outbox):
        return {"intake": {"external": {"outbox": outbox}}}

    assert intake_services.outbox_pace({}) == (80, 500)
    assert intake_services.outbox_pace(cfg(send_writes_per_minute=20)) == (20, 500)
    with pytest.raises(intake_services.IntakeRefused, match="from 1 to 80"):
        intake_services.outbox_pace(cfg(send_writes_per_minute=120))


def test_the_ledger_is_named_for_the_bound_api_host() -> None:
    assert intake_services.github_api_host({}) == "api.github.com"
    ghes = {"plugins": {"tracker-github": {"base_url": "https://GHE.example.com/api/v3"}}}
    assert intake_services.github_api_host(ghes) == "ghe.example.com"


def test_one_pacer_per_command_over_the_runtime_ledger(monkeypatch) -> None:
    from beadhive.testing.intake import FakePacerClock, MemoryWriteLedger

    clock, ledgers = FakePacerClock(), {}

    def ledger(host):
        return ledgers.setdefault(host, MemoryWriteLedger())

    runtime = intake_services.PacingRuntime(clock.monotonic, clock.wall, clock.wait, ledger)
    monkeypatch.setattr(intake_services, "pacing_runtime", lambda: runtime)
    stop = intake_services.SendStop()

    pacer = intake_services.write_pacer({}, stop)
    pacer.admit(1)  # the hook only peeks
    assert ledgers["api.github.com"].taken == 0
    pacer.gate("POST")  # the request records its start

    assert pacer.stop is stop and ledgers["api.github.com"].taken == 1
    assert pacer.summary().writes_per_minute == 80


def _on(monkeypatch):
    from beadhive import run

    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(run, "_derive_gh_auth_token", lambda: pytest.fail("no gh auth token"))
    plugin = {"enabled": True, "read": {"issues": {"enabled": True}}}
    cfg = {"intake": {"external": {"enabled": True}}, "plugins": {"tracker-github": plugin}}
    public = {"prefix": "wpub", "path": "/s/p", "remote": "git-origin", "adapter": "tracker-github"}
    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "widget",
        "prefix": "w",
        "stores": {"internal": {"remote": "r"}, "public": public},
    }
    return cfg, entry


def test_paced_adapters_hand_the_gate_and_observer_to_the_adapter_they_build(monkeypatch) -> None:
    cfg, entry = _on(monkeypatch)
    pacer = intake_services.write_pacer({}, intake_services.SendStop())

    adapters = intake_services.process_adapters(cfg).pace(pacer.gate, pacer.observe)

    assert isinstance(adapters, TrackerAdapters)
    bound = adapters.for_store(entry, requested=frozenset({Operation.ISSUES_READ}))
    transport = bound.port._inner._transport
    assert transport.write_gate == pacer.gate
    transport.observe_wait(type("R", (), {"error": RuntimeError("x")})(), 3.0)  # adapts


# -- N4: no tracker is ever left unpaced ----------------------------------------------------


class _Writer:
    """A tracker with one writer and one reader, recording the order of gate and write."""

    def __init__(self, seen: list[str]) -> None:
        self.seen = seen
        self.locator = "acme/widgets"

    def describe(self):
        return "described"

    def post_comment(self, issue, body, *, marker, grant):
        self.seen.append("write")
        return "receipt"


def test_an_injected_tracker_gets_the_gate_around_each_write() -> None:
    from beadhive.modules.tracker.contracts import CommentWriter, IssueCloser

    seen: list[str] = []
    gated = intake_services.gated_writes(_Writer(seen), lambda method: seen.append("gate"))

    assert gated.post_comment("#1", "b", marker=None, grant=None) == "receipt"
    assert gated.describe() == "described" and gated.locator == "acme/widgets"
    assert seen == ["gate", "write"]  # the gate first, then the one request
    assert isinstance(gated, CommentWriter) and not isinstance(gated, IssueCloser)
    assert intake_services.gated_writes(gated, lambda method: None) is gated  # never twice


@pytest.mark.parametrize("seam", ["tracker", "wiring", "adapters-without-pace"])
def test_paced_never_leaves_an_injected_tracker_unpaced(monkeypatch, seam) -> None:
    seen: list[str] = []
    tracker = _Writer(seen)
    kwargs: dict = {}
    if seam == "tracker":
        kwargs["tracker"] = tracker
    elif seam == "wiring":
        kwargs["wiring"] = {"tracker": tracker}
    else:
        monkeypatch.setattr(intake_services, "process_adapters", lambda cfg: object())
        monkeypatch.setattr(intake_services, "outbox_wiring", lambda *a, **k: {"tracker": tracker})

    paced = intake_services._paced({}, {}, None, kwargs, intake_services.SendStop())

    wired = paced["tracker"] if seam == "tracker" else paced["wiring"]["tracker"]
    pacer = paced["pacer"]
    pacer.admit(1)
    wired.post_comment("#1", "b", marker=None, grant=None)
    assert seen == ["write"] and pacer.summary().requests == 1  # the gate recorded the start


# -- the import-lazy fast path (bh-lwz8a) runs send-approved under the same interrupt stop ------


@pytest.mark.parametrize("tree", ["fast", "full"])
def test_send_approved_runs_under_the_interrupt_stop_on_both_trees(monkeypatch, tree) -> None:
    from pathlib import Path

    from typer.testing import CliRunner

    from beadhive import cli, cli_fastpath

    seen: dict = {}

    def outbox(verb, hive, as_json, run, timings=None):
        seen["handler"] = signal.getsignal(signal.SIGINT)
        seen["call"] = run({}, {}, Path("."))

    def send_approved(cfg, entry, directory, kind, beads, actor, *, stop=None, **kwargs):
        seen["stop"] = stop
        return intake_services.OutboxCall(0, value=BatchSendOutcome())

    monkeypatch.setattr(work_intake, "_outbox", outbox)
    monkeypatch.setattr(work_intake.identity, "resolve_actor", lambda explicit="": "ops/alice")
    monkeypatch.setattr(intake_services, "outbox_send_approved", send_approved)
    before = signal.getsignal(signal.SIGINT)
    app = cli_fastpath.build_app() if tree == "fast" else cli.app
    argv = ["work", "outbox", "send-approved", "--hive", "widget", "--json"]

    result = CliRunner().invoke(app, argv, env={"BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"})

    assert result.exit_code == 0, result.output
    assert isinstance(seen["stop"], intake_services.SendStop)
    assert seen["handler"] not in (before, signal.default_int_handler)  # the first-SIGINT stop
    seen["handler"](signal.SIGINT, None)
    assert seen["stop"].reason is StopReason.INTERRUPTED
    assert signal.getsignal(signal.SIGINT) is before  # restored after the command


def test_the_suites_pacing_seam_is_fake_whenever_the_root_is_imported() -> None:
    """``tests/conftest.py`` patches ``beadhive.outbox_pacing`` for every test, and the root
    reads that seam at call time, so even an ``intake_services`` imported lazily inside a test
    body never sleeps for the pace or writes the real host ledger."""
    from beadhive import outbox_pacing
    from beadhive.testing.intake import FakePacerClock, MemoryWriteLedger

    runtime = intake_services.pacing_runtime()
    assert runtime is outbox_pacing.runtime()  # one fake host per test, read at call time
    assert runtime.wall() == FakePacerClock().now
    assert isinstance(runtime.ledger("api.github.com"), MemoryWriteLedger)
