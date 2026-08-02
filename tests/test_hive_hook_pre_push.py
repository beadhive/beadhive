"""`bh hive hook pre-push` (bh-smcj) — the fence as a callable verb, not a generated script.

`prepush.hook_script` builds the same ref filter as a SHELL STRING and installs it, so any
second dispatcher (lefthook, a plain `.git/hooks` file) had to transcribe the
`refs/dolt/data` check into its own copy — free to drift, and silently wrong in both
directions when it does (fence every ordinary push, or never fence at all). See
docs/design/hooks-as-functionality-adr.md.

This verb owns the whole hook contract so a dispatcher never has to know which refs matter:

  * git's pre-push stdin protocol ("<local_ref> <sha> <remote_ref> <sha>" per line);
  * the `refs/dolt/data` filter — anything else exits 0 without touching the fence;
  * exit semantics (0 allows, non-zero refuses, `detail` on stderr).

Fails OPEN for "nothing to fence" and CLOSED only on a real not-primary verdict, and warns
loudly when it gets no stdin at all — the shape a dispatcher that forgot to forward stdin
(lefthook's `use_stdin: true`) would otherwise take silently.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from beadhive import prepush
from beadhive.cli import app

runner = CliRunner()

DATA_PUSH = "refs/dolt/data abc123 refs/dolt/data def456\n"
CODE_PUSH = "refs/heads/main abc123 refs/heads/main def456\n"


@pytest.fixture
def fence(monkeypatch):
    """Record every `check_fence` call and script its verdict, so these tests exercise the
    HOOK CONTRACT (filtering, stdin, exit codes) without standing up a lease/HQ world —
    `check_fence`'s own primary/not-primary logic is covered by the host-lease suites."""
    calls: list = []
    verdict = {"ok": True, "detail": ""}

    class _FakePrepush:
        @staticmethod
        def check_fence(hive_dir, **_kw):
            calls.append(hive_dir)
            return verdict["ok"], verdict["detail"]

    monkeypatch.setattr(prepush, "check_fence", _FakePrepush.check_fence)
    return {"calls": calls, "verdict": verdict}


def test_ordinary_code_push_exits_zero_without_consulting_the_fence(fence):
    """The filter's whole point: a normal `git push` costs no fence evaluation at all."""
    result = runner.invoke(app, ["hive", "hook", "pre-push"], input=CODE_PUSH)

    assert result.exit_code == 0, result.output
    assert fence["calls"] == []  # never reached check_fence


def test_dolt_data_push_reaches_the_fence_and_is_allowed_when_primary(fence):
    result = runner.invoke(app, ["hive", "hook", "pre-push"], input=DATA_PUSH)

    assert result.exit_code == 0, result.output
    assert len(fence["calls"]) == 1


def test_dolt_data_push_is_refused_when_not_primary(fence):
    fence["verdict"].update(ok=False, detail="✗ this host is not primary for bh")

    result = runner.invoke(app, ["hive", "hook", "pre-push"], input=DATA_PUSH)

    assert result.exit_code == 1
    assert "not primary" in result.output


def test_a_mixed_push_touching_dolt_data_still_reaches_the_fence(fence):
    """git sends one line per ref; the fence must trigger on ANY matching line, not just the
    first — a `git push --all`-shaped invocation must not slip past."""
    result = runner.invoke(app, ["hive", "hook", "pre-push"], input=CODE_PUSH + DATA_PUSH)

    assert result.exit_code == 0, result.output
    assert len(fence["calls"]) == 1


def test_empty_stdin_warns_rather_than_allowing_silently(fence):
    """The failure mode the ADR exists to prevent: a dispatcher that forgets to forward stdin
    would make the fence a no-op. Allowing is right (nothing to fence) but silence is not."""
    result = runner.invoke(app, ["hive", "hook", "pre-push"], input="")

    assert result.exit_code == 0, result.output
    assert "no ref list on stdin" in result.output
    assert fence["calls"] == []


def test_install_is_opt_in_and_refuses_outside_a_hive(fence, tmp_path, monkeypatch):
    """`bh hive hook install` exists so the transport repo — which no dispatcher can reach —
    can still be fenced, but it must be an explicit act. Outside a managed hive with no
    HIVE_ID there is nothing to install for, and it says so rather than guessing."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["hive", "hook", "install"])

    assert result.exit_code == 1
    assert "no managed hive" in result.output


def test_a_ref_merely_containing_the_data_ref_name_does_not_trigger_the_fence(fence):
    """Match the FIRST field exactly, never a substring: a branch named
    `refs/heads/refs/dolt/data-notes` is not a dolt-data push."""
    result = runner.invoke(
        app,
        ["hive", "hook", "pre-push"],
        input="refs/heads/refs/dolt/data-notes abc refs/heads/x def\n",
    )

    assert result.exit_code == 0, result.output
    assert fence["calls"] == []
