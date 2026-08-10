"""ws.log foundation — both render faces (JSON vs rich/ANSI), format override, otel no-op.

The runnable acceptance check for bead: force each face via config/args
and assert the JSON parses or the ANSI path fires, and that get_logger() works with no
OpenTelemetry installed (the span processor no-ops cleanly).
"""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest
import structlog

from beadhive import config, log

# A bare CSI byte — ConsoleRenderer (rich face) emits these; JSONRenderer never does.
_ANSI = "\x1b["


class _FakeTTY(io.StringIO):
    """A StringIO that claims to be an interactive terminal, to exercise auto TTY-detect."""

    def isatty(self) -> bool:  # noqa: D401 - trivial
        return True


@pytest.fixture(autouse=True)
def _reset_logging():
    """Isolate each test: forget any prior structlog config + the module guard + root
    handlers, so configure()/get_logger() start from a clean slate."""
    log._configured = False
    structlog.reset_defaults()
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()
    yield
    structlog.reset_defaults()
    log._configured = False
    root.handlers.clear()
    root.handlers.extend(saved)


def _emit(fmt=None, level=None, stream=None, *, msg="hello", **fields):
    """Configure with the given face/level/stream, emit one event, return captured text."""
    stream = stream if stream is not None else io.StringIO()
    log.configure(fmt=fmt, level=level, stream=stream)
    log.get_logger("test").info(msg, **fields)
    return stream.getvalue()


# ---- render faces -----------------------------------------------------------


def test_json_face_emits_parseable_json():
    out = _emit(fmt="json", msg="event-a", widget="x")
    line = out.strip().splitlines()[-1]
    record = json.loads(line)  # must parse — that's the whole point of the JSON face
    assert record["event"] == "event-a"
    assert record["widget"] == "x"
    assert record["level"] == "info"


def test_rich_face_emits_ansi():
    out = _emit(fmt="rich", msg="event-b")
    assert _ANSI in out  # ConsoleRenderer colorizes → CSI escapes present
    assert "event-b" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip().splitlines()[-1])  # not JSON


# ---- auto TTY-detect --------------------------------------------------------


def test_auto_non_tty_is_json():
    out = _emit(fmt="auto", stream=io.StringIO(), msg="auto-json")
    json.loads(out.strip().splitlines()[-1])  # non-TTY ⇒ JSON face


def test_auto_tty_is_rich():
    out = _emit(fmt="auto", stream=_FakeTTY(), msg="auto-rich")
    assert _ANSI in out  # interactive ⇒ rich face


# ---- explicit format overrides auto-detect ----------------------------------


def test_format_overrides_tty_detect_to_json():
    # A real TTY would auto-pick rich; json must win when configured explicitly.
    out = _emit(fmt="json", stream=_FakeTTY(), msg="forced-json")
    json.loads(out.strip().splitlines()[-1])
    assert _ANSI not in out


def test_format_overrides_to_rich_on_non_tty():
    # A non-TTY would auto-pick JSON; rich must win when configured explicitly.
    out = _emit(fmt="rich", stream=io.StringIO(), msg="forced-rich")
    assert _ANSI in out


# ---- level resolution -------------------------------------------------------


def test_level_filters_below_threshold():
    stream = io.StringIO()
    log.configure(fmt="json", level="warning", stream=stream)
    logger = log.get_logger("lvl")
    logger.info("dropped")
    logger.warning("kept")
    lines = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
    events = [json.loads(ln)["event"] for ln in lines]
    assert "dropped" not in events
    assert "kept" in events


def test_resolve_level_accepts_names_numbers_and_falls_back():
    assert log._resolve_level("debug") == logging.DEBUG
    assert log._resolve_level("WARNING") == logging.WARNING
    assert log._resolve_level(logging.ERROR) == logging.ERROR
    assert log._resolve_level(None) == logging.INFO
    assert log._resolve_level("bogus") == logging.INFO  # never raise on bad config


# ---- stdlib bridge ----------------------------------------------------------


def test_stdlib_records_flow_through_pipeline():
    stream = io.StringIO()
    log.configure(fmt="json", stream=stream)
    logging.getLogger("third.party").warning("foreign-record")
    record = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert record["event"] == "foreign-record"
    assert record["level"] == "warning"


# ---- otel hook no-ops without the SDK ---------------------------------------


def test_otel_processor_noops_when_module_absent(monkeypatch):
    # Force `from opentelemetry import trace` to raise ImportError (otel not installed) by
    # poisoning sys.modules — the processor must swallow it and return the event unchanged.
    import sys

    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    event = {"event": "x", "level": "info"}
    out = log.add_open_telemetry_spans(None, "info", dict(event))
    assert out == event  # no trace_id/span_id when otel is absent


def test_otel_processor_noops_without_active_span():
    # otel-api may be importable transitively, but with no recording span the context is
    # invalid → still a no-op (no correlation ids leak into the event).
    event = {"event": "x", "level": "info"}
    out = log.add_open_telemetry_spans(None, "info", dict(event))
    assert "trace_id" not in out and "span_id" not in out


def test_otel_processor_injects_ids_for_recording_span(monkeypatch):
    trace = pytest.importorskip("opentelemetry.trace")

    class _Ctx:
        is_valid = True
        trace_id = 0x0123456789ABCDEF0123456789ABCDEF
        span_id = 0x0123456789ABCDEF

    class _Span:
        def get_span_context(self):
            return _Ctx()

    monkeypatch.setattr(trace, "get_current_span", lambda: _Span())
    out = log.add_open_telemetry_spans(None, "info", {"event": "x"})
    assert out["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert out["span_id"] == "0123456789abcdef"


def test_get_logger_works_with_no_otel_installed():
    # The acceptance line: get_logger() emits cleanly with no SDK active — the span
    # processor no-ops, so no trace_id appears in the rendered event.
    out = _emit(fmt="json", msg="no-otel")
    record = json.loads(out.strip().splitlines()[-1])
    assert record["event"] == "no-otel"
    assert "trace_id" not in record


# ---- defaults to stderr / lazy configure ------------------------------------


def test_get_logger_lazily_configures():
    assert log._configured is False
    log.get_logger("lazy")  # first use wires the pipeline
    assert log._configured is True


def test_default_stderr_is_resolved_live_not_pinned_at_configure(monkeypatch):
    """The pipeline configures ONCE per process (`_configured`), so pinning the `sys.stderr`
    OBJECT it saw sends every later diagnostic to a stream nobody reads. pytest's capsys
    installs a fresh CaptureIO per test, so whichever test configured first captured the
    pipeline and every later one saw an empty stderr — which is how a full-suite run failed a
    test that passed alone, because alone it *was* the first (bh-lbcf)."""
    at_configure = io.StringIO()
    monkeypatch.setattr(sys, "stderr", at_configure)
    log.configure(fmt="json")  # no stream= → destination is whatever sys.stderr is
    log.get_logger("t").warning("during-configure")
    assert "during-configure" in at_configure.getvalue()

    later = io.StringIO()
    monkeypatch.setattr(sys, "stderr", later)  # the swap capsys performs between tests
    log.get_logger("t").warning("after-the-swap")

    assert "after-the-swap" in later.getvalue()  # follows the live stream
    assert "after-the-swap" not in at_configure.getvalue()  # and not to the dead one


def test_explicit_stream_stays_pinned(monkeypatch):
    """Only the stderr DEFAULT is live-resolved. An explicit `stream=` names a destination and
    keeps it — a caller that picked an object meant that object."""
    chosen = io.StringIO()
    log.configure(fmt="json", stream=chosen)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    log.get_logger("t").warning("pinned")

    assert "pinned" in chosen.getvalue()


# ---- flush-per-record off a TTY (bh-29r28) ----------------------------------


def test_add_file_sink_tees_json_alongside_the_configured_stream(tmp_path):
    """`add_file_sink` is an ADDITION, not a replacement — the record still reaches the
    originally-configured stream too, and the file gets its own valid JSON line."""
    primary = io.StringIO()
    log.configure(fmt="rich", stream=primary)  # rich face, to prove the file stays JSON anyway
    events = tmp_path / "events.jsonl"
    log.add_file_sink(str(events))

    log.get_logger("t").info("tick", pass_=1)

    assert _ANSI in primary.getvalue()  # unchanged: still the configured (rich) face
    line = events.read_text().strip().splitlines()[-1]
    payload = json.loads(line)  # the file is always JSON, independent of log.format
    assert payload["event"] == "tick"


def test_add_file_sink_flushes_before_close(tmp_path):
    """Each record lands on disk immediately — read the file without closing the handler."""
    log.configure(fmt="json", stream=io.StringIO())
    events = tmp_path / "events.jsonl"
    log.add_file_sink(str(events))

    log.get_logger("t").info("first")
    assert "first" in events.read_text()  # visible without flushing/closing anything ourselves

    log.get_logger("t").info("second")
    lines = events.read_text().strip().splitlines()
    assert len(lines) == 2


def test_stderr_record_is_readable_through_a_pipe_before_the_process_exits():
    """THE regression guard for bh-29r28: run a child process with stdout/stderr as PIPES (never
    a TTY — exactly systemd's / `bh work loop | tee`'s shape) and assert a log record is
    observable WHILE THE PROCESS IS STILL RUNNING, not just after it exits.

    Tests that read output only post-exit cannot catch block-buffering, because buffered output
    flushes at exit regardless — which is exactly the failure mode this bead fixes. This test
    reads incrementally, with a timeout, from a live pipe.
    """
    import fcntl
    import os
    import subprocess
    import time
    from pathlib import Path

    repo_src = str(Path(log.__file__).resolve().parents[1])
    code = (
        "import sys, time\n"
        f"sys.path.insert(0, {repo_src!r})\n"
        "from beadhive import log\n"
        "lg = log.get_logger('regression')\n"
        "lg.info('first_record')\n"
        "time.sleep(30)\n"  # only reached if the test doesn't kill it first
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        fd = proc.stderr.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        deadline = time.monotonic() + 10
        seen = b""
        while time.monotonic() < deadline:
            assert proc.poll() is None, (
                "process exited before the record was read — the test would then only be "
                "proving post-exit flush, not the live-pipe property"
            )
            try:
                chunk = proc.stderr.read()
            except (BlockingIOError, TypeError):
                chunk = b""
            if chunk:
                seen += chunk
            if b"first_record" in seen:
                break
            time.sleep(0.05)
        else:
            pytest.fail("record never appeared on the pipe within the timeout")

        assert proc.poll() is None, "process must still be alive when the record was observed"
    finally:
        proc.kill()
        proc.wait(timeout=5)


# ---- config accessors -------------------------------------------------------


def test_config_log_defaults():
    assert config.log_format({}) == "auto"
    assert config.log_level({}) == "info"


def test_config_log_overrides():
    cfg = {"log": {"format": "json", "level": "debug"}}
    assert config.log_format(cfg) == "json"
    assert config.log_level(cfg) == "debug"
