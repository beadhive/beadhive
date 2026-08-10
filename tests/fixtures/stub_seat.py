#!/usr/bin/env python3
"""Reference stub seat binary — a test double for `bh-<seat>` (bh-c6dk.2).

`claude-code` is the only harness provider with `implemented: true` in `baml-harness`, and
there is no built `dist/` binary in THIS repo (baml-harness compiles its own, out of scope
here — see `docs/design/work-runtime-tiers-adr.md` Amendment 2 §1). This script is how the
beadhive-side scheduler contract (`src/beadhive/seatrun.py`) gets exercised end-to-end without
a real provider: it implements the settled argv/stdout/exit contract closely enough to stand
in for one in tests and in the local demo loop, and nothing more — it does no real agent work.

Usage (matches the settled contract exactly):

    stub_seat.py --workspace <path> --bead <id> --instructions <file|-> \\
                 --session_id <uuid> [--model <tier>] [--resume_session <id>] \\
                 [--input-format stream-json]

What it does, deliberately kept small and legible:

- Validates `--workspace` (exists + is a directory) and requires `--session_id` on every
  invocation, refusing both with a typed JSON error on stderr and exit 2 — never a raw
  traceback — matching the acceptance bar `bh-c6dk.2` sets for the real binary.
- Reads its instructions (`--instructions <file>` or `-` for stdin) and looks for a tiny
  directive mini-language to decide what to report — see `_DIRECTIVES` below. Anything not
  recognized is ignored (so a real natural-language brief is harmless input).
- Echoes `--session_id` verbatim into `SeatRun.session_id`, and `--bead` into
  `RoleOutcome.bead_id` UNLESS `STUB_BEAD_MISMATCH=true` is set, so the round-trip check
  (`bh-c6dk.2` acceptance) has both a matching and a mismatching case to exercise.
- Honors the TARGET exit taxonomy from Amendment 2 §1 (0 done · 10 blocked · 11 handoff) —
  this is the reference implementation demonstrating the taxonomy works, since it is UNBUILT in
  the real upstream binary today (`bh-a7so.1`); `beadhive.seatrun.classify_run` never depends
  on it, but a test can use this stub to confirm exit code and `status` agree once a binary
  does implement it.
- Implements the three-rung CANCEL ladder when `STUB_HANG=true` puts it into a long-running
  loop: (1) a plain-text wrap-up line on stdin (with `--input-format stream-json`) is treated as
  a cooperative cancel — it emits a `handoff` SeatRun carrying `"interrupt_ack": true` and exits
  0; (2) a `{"type":"control_request","request":{"subtype":"interrupt"}}` line is treated as the
  hard rung — it emits a killed-run envelope (no `outcome`) and exits 1; (3) SIGTERM (never
  SIGINT — this script installs no SIGINT handler, on purpose, matching the contract's "never
  SIGINT" rule) is caught and emits the same envelope shape with `terminal_reason: "sigterm"`,
  exiting 143.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time

_DIRECTIVES = """\
Recognized directive lines (anywhere in --instructions text), one per line, KEY=VALUE:
  STUB_STATUS=done|blocked|handoff   what RoleOutcome.status to report (default: done)
  STUB_SUMMARY=<text>                RoleOutcome.summary (default: a canned message)
  STUB_NEXT_ACTION=<text>            RoleOutcome.next_action (default: unset)
  STUB_BEAD_MISMATCH=true            echo a DIFFERENT bead_id than --bead was given
  STUB_HANG=true                     don't return immediately; wait for a CANCEL signal
"""


def _typed_error(kind: str, reason: str, **extra: object) -> None:
    """Emit a typed JSON error on stderr — never a raw traceback — and nothing on stdout."""
    payload = {"error": kind, "reason": reason, **extra}
    print(json.dumps(payload), file=sys.stderr)


def _parse_directives(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in {
            "STUB_STATUS",
            "STUB_SUMMARY",
            "STUB_NEXT_ACTION",
            "STUB_BEAD_MISMATCH",
            "STUB_HANG",
        }:
            out[key] = value.strip()
    return out


def _seat_run_json(
    *,
    status: str,
    summary: str,
    bead_id: str | None,
    next_action: str | None,
    session_id: str,
    extra: dict | None = None,
) -> str:
    payload = {
        "outcome": {
            "status": status,
            "summary": summary,
            "bead_id": bead_id,
            "next_action": next_action,
        },
        "session_id": session_id,
        "cost_usd": 0.01,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 10,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "packs": [],
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload)


def _envelope_json(*, session_id: str, terminal_reason: str) -> str:
    return json.dumps(
        {
            "session_id": session_id,
            "cost_usd": 0.005,
            "usage": {
                "input_tokens": 5,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "terminal_reason": terminal_reason,
        }
    )


_EXIT_BY_STATUS = {"done": 0, "blocked": 10, "handoff": 11}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stub_seat.py", description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--bead", default=None)
    parser.add_argument("--instructions", required=True)
    parser.add_argument("--session_id", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--resume_session", default=None)
    parser.add_argument("--input-format", dest="input_format", default="text")
    args = parser.parse_args(argv)

    import os

    if not os.path.isdir(args.workspace):
        _typed_error(
            "workspace_invalid", f"not a directory: {args.workspace!r}", path=args.workspace
        )
        return 2

    if not args.session_id:
        _typed_error("session_id_required", "--session_id is required on create")
        return 2

    if args.instructions == "-":
        instructions_text = sys.stdin.read()
        stdin_available_for_cancel = False
    else:
        with open(args.instructions, encoding="utf-8") as fh:
            instructions_text = fh.read()
        stdin_available_for_cancel = True

    directives = _parse_directives(instructions_text)
    status = directives.get("STUB_STATUS", "done")
    if status not in _EXIT_BY_STATUS:
        _typed_error("bad_directive", f"unknown STUB_STATUS {status!r}")
        return 2
    summary = directives.get("STUB_SUMMARY", f"stub seat ran with status={status}")
    next_action = directives.get("STUB_NEXT_ACTION") or None
    mismatch = directives.get("STUB_BEAD_MISMATCH", "").lower() == "true"
    hang = directives.get("STUB_HANG", "").lower() == "true"

    bead_id = args.bead
    if bead_id and mismatch:
        bead_id = bead_id + "-DIFFERENT"

    if hang and stdin_available_for_cancel and args.input_format == "stream-json":
        return _run_hang_loop(session_id=args.session_id, bead_id=bead_id)
    if hang:
        # Nothing can cancel it cooperatively or hard — only a signal. Still installs the
        # SIGTERM handler so `kill -TERM` on this process demonstrates rung 3 in isolation.
        return _run_hang_loop(session_id=args.session_id, bead_id=bead_id, stdin_cancel=False)

    print(
        _seat_run_json(
            status=status,
            summary=summary,
            bead_id=bead_id,
            next_action=next_action,
            session_id=args.session_id,
        )
    )
    return _EXIT_BY_STATUS[status]


def _run_hang_loop(*, session_id: str, bead_id: str | None, stdin_cancel: bool = True) -> int:
    """CANCEL ladder, rungs 1-3. Blocks until one of: a stdin line (rung 1 or 2, only when
    *stdin_cancel*) or SIGTERM (rung 3, always)."""
    terminated = {"flag": False}

    def _on_sigterm(_signum: int, _frame: object) -> None:
        terminated["flag"] = True

    signal.signal(signal.SIGTERM, _on_sigterm)
    # Deliberately no SIGINT handler — the contract says NEVER SIGINT, and a default SIGINT
    # exits this process with 0, which would collide with 0 = done. That collision is the
    # entire reason the contract forbids it; the stub doesn't paper over it.

    while True:
        if terminated["flag"]:
            print(_envelope_json(session_id=session_id, terminal_reason="sigterm"), file=sys.stdout)
            return 143

        if stdin_cancel and _stdin_has_line():
            line = sys.stdin.readline()
            outcome = _handle_cancel_line(line, session_id=session_id, bead_id=bead_id)
            if outcome is not None:
                return outcome

        time.sleep(0.05)


def _stdin_has_line() -> bool:
    import select

    ready, _, _ = select.select([sys.stdin], [], [], 0.05)
    return bool(ready)


def _handle_cancel_line(line: str, *, session_id: str, bead_id: str | None) -> int | None:
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict) and payload.get("type") == "control_request":
        subtype = (payload.get("request") or {}).get("subtype")
        if subtype == "interrupt":
            # Hard rung: no ack packaging, just the priced envelope and a dirty-tree exit.
            print(
                _envelope_json(session_id=session_id, terminal_reason="control_request_interrupt")
            )
            return 1
        return None  # not an interrupt request; keep waiting

    # Cooperative rung: any other non-empty line is treated as the wrap-up instruction.
    print(
        _seat_run_json(
            status="handoff",
            summary="cooperative cancel: finished in-flight step and stopped",
            bead_id=bead_id,
            next_action="resume with a fresh turn against the same worktree",
            session_id=session_id,
            extra={"interrupt_ack": True},
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
