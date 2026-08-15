"""`just push`'s pending-bump refusal (bh-8c2yo) — the justfile wiring itself, driven for real.

`_refuse-if-bump-pending` is bash glued into the justfile, not Python, so its fail-open/refuse
contract needs a test that actually runs `just` rather than one that only reads the recipe body
as text. `bh release pending`'s own read (`_marker_for_tree`) is proven against a real hive in
tests/test_release_flow.py; this file proves the JUSTFILE calls it correctly and reacts to its
two answers — pending / not pending — the way `just push` needs to: refuse with a pointer to
`just release-push`, or stay silent and let an ordinary push through unchanged.

Every case stubs `bh` (via `BH_EXEC`, the same escape hatch the recipe's own capability probe
documents) rather than talking to a real hive — no git, no marker file, no network.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = ROOT / "justfile"

needs_just = pytest.mark.skipif(shutil.which("just") is None, reason="needs just")


def _stub_bh(tmp_path: Path, *, help_names_pending: bool, pending_exit: int) -> Path:
    """A `bh` stand-in answering only the two calls `_refuse-if-bump-pending` makes: the
    capability probe (`release --help`) and the read itself (`release pending`)."""
    help_line = "await recover order attest preflight" + (" pending" if help_names_pending else "")
    script = tmp_path / "bh"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "release" ] && [ "$2" = "--help" ]; then\n'
        f'  echo "{help_line}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "release" ] && [ "$2" = "pending" ]; then\n'
        f"  exit {pending_exit}\n"
        "fi\n"
        'echo "unexpected bh call: $*" >&2\n'
        "exit 2\n"
    )
    script.chmod(0o755)
    return script


def _run(bh: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "BH_EXEC": str(bh)}
    return subprocess.run(
        ["just", "-f", str(JUSTFILE), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@needs_just
def test_a_pending_marker_refuses_and_names_release_push(tmp_path):
    bh = _stub_bh(tmp_path, help_names_pending=True, pending_exit=0)

    res = _run(bh, "_refuse-if-bump-pending")

    assert res.returncode != 0, res.stderr
    assert "release-push" in res.stderr
    assert "bh release recover" in res.stderr


@needs_just
def test_no_marker_pending_lets_the_push_proceed_silently(tmp_path):
    """The ordinary-push shape: `bh release pending` exits 1 (not pending), and the recipe must
    behave exactly as it does today — succeed, and say nothing."""
    bh = _stub_bh(tmp_path, help_names_pending=True, pending_exit=1)

    res = _run(bh, "_refuse-if-bump-pending")

    assert res.returncode == 0, res.stderr
    assert res.stderr.strip() == ""
    assert res.stdout.strip() == ""


@needs_just
def test_an_older_bh_with_no_pending_verb_warns_and_does_not_block(tmp_path):
    """FAIL OPEN on the capability probe too: an `${BH_EXEC:-bh}` that predates this bead has no
    `release pending` at all, and the recipe must never turn that absence into a blocked push —
    only a loud warning that the check did not happen (same shape as `_await-bump-gate`'s own
    probe, bh-ku9n9.7)."""
    bh = _stub_bh(tmp_path, help_names_pending=False, pending_exit=0)

    res = _run(bh, "_refuse-if-bump-pending")

    assert res.returncode == 0, res.stderr
    assert "release pending" in res.stderr
    assert "NOT CHECKED" in res.stderr


@needs_just
def test_push_calls_the_refusal_before_touching_the_remote():
    """The recipe body, not just its behavior in isolation: `push` must consult the refusal
    BEFORE `scripts/push-main.sh` ever runs, or a pending release could still slip a push out
    ahead of the check."""
    body = subprocess.run(
        ["just", "--show", "push"], cwd=str(ROOT), capture_output=True, text=True, check=True
    ).stdout
    refuse_at = body.index("_refuse-if-bump-pending")
    script_at = body.index("push-main.sh")
    assert refuse_at < script_at


@needs_just
def test_release_push_is_untouched_and_still_waits_rather_than_refuses():
    """Not in scope, and must stay that way: `just release-push` keeps `_await-bump-gate`, the
    WAIT-for-verdict recipe — it is the atomic path this check exists to route operators onto,
    so it must not itself start refusing a pending gate it is meant to ride to green."""
    body = subprocess.run(
        ["just", "--show", "release-push"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "_await-bump-gate" in body
    assert "_refuse-if-bump-pending" not in body


def test_pending_gate_is_the_only_thing_push_gained():
    """A cheap text-level guard against `push`'s body drifting to call something other than the
    one new recipe — read directly rather than via `just --show`, so it also catches a typo that
    would make `just --show` itself fail loudly (which the recipe-driven tests above would not
    distinguish from "the check correctly refused")."""
    text = JUSTFILE.read_text()
    m = re.search(r"^push remote=.*?\n((?:.*\n)*?)\n", text, re.M)
    assert m, "no `push` recipe in the justfile"
    body = m.group(1)
    assert "_refuse-if-bump-pending" in body
    assert "_await-bump-gate" not in body
