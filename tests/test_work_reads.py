"""`ws work ready|issue|list|show` — the first-class bead reads that replace `ws bd` in the loops.

The forward verbs stream `bd`'s bytes through verbatim, so the coordinator loop's consumed shapes
(`ws bd ready --json`, `ws bd show <id> --json`) stay stable once the bd passthrough is gated off.
The test seam mirrors the rest of the suite: patch the one `ws.work.run` symbol with a fake `bd`,
drive the verbs through Typer's CliRunner, and assert the forwarded argv, the byte-identical
output, and the propagated exit code. `ws work show`'s gates section (bh-i371) is driven through
the same seam with the git producers faked.
"""

from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

from typer.testing import CliRunner

from beadhive import config as config_mod
from beadhive import work
from beadhive import worktree as worktree_mod

_CP = namedtuple("CP", "returncode stdout stderr")


class FakeReadBd:
    """Records every argv `ws work` forwards and returns a canned bd result (ignoring capture,
    exactly like the FakeBd in test_work — so `_forward_read`'s capture-then-write is exercised)."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.calls: list[list[str]] = []
        self._stdout = stdout
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        return _CP(self._returncode, self._stdout, self._stderr)

    @property
    def last(self) -> list[str]:
        return self.calls[-1]


def _run(monkeypatch, fake, argv):
    """Invoke the `ws work` sub-app with a faked bd + a no-op config (hive resolves to cwd)."""
    monkeypatch.setattr(work.bd, "_run", fake)
    monkeypatch.setattr(work.config, "load", lambda: {})
    return CliRunner().invoke(work.app, argv)


# ---- ready ------------------------------------------------------------------


def test_ready_forwards_json_shape_unchanged(monkeypatch):
    payload = '[{"id": "mr-1", "status": "open", "labels": ["model:opus"]}]\n'
    fake = FakeReadBd(stdout=payload)
    res = _run(monkeypatch, fake, ["ready", "--json"])

    assert res.exit_code == 0
    # forwards `bd -C <cwd> ready --json` (the `-C` scopes the DB; the passthrough runs the same
    # `bd ready --json` in-cwd — identical output either way).
    assert fake.last[0] == "bd"
    assert fake.last[-2:] == ["ready", "--json"]
    # output is byte-identical to bd's — the coordinator loop parses the same shape it does today.
    assert res.stdout == payload
    assert json.loads(res.stdout)[0]["id"] == "mr-1"


def test_ready_passes_gated_through(monkeypatch):
    fake = FakeReadBd(stdout="[]\n")
    res = _run(monkeypatch, fake, ["ready", "--gated", "--json"])

    assert res.exit_code == 0
    # extra bd flags (unknown to typer) ride through in order onto `bd ready`.
    assert fake.last[-3:] == ["ready", "--gated", "--json"]


def _opt_into_release(monkeypatch, *, estimator="file-overlap"):
    """Opt the hive into release start-gating for `ws work ready` (bh-k2j8.6)."""
    monkeypatch.setattr(
        work.config, "release_value",
        lambda cfg, entry, key, default=None: "stable-versioning" if key == "strategy" else default,
    )
    monkeypatch.setattr(work.config, "release_conflict_estimator", lambda cfg, entry: estimator)


def test_ready_json_annotates_deferred_when_release_strategy_set(monkeypatch):
    # Opted in: `mr-2` shares src/x.py with `mr-1` ranked ahead of it in the ready queue → deferred.
    beads = [
        {"id": "mr-1", "status": "open", "labels": ["path:src/x.py"]},
        {"id": "mr-2", "status": "open", "labels": ["path:src/x.py"]},
    ]
    fake = FakeReadBd(stdout=json.dumps(beads))
    _opt_into_release(monkeypatch)
    res = _run(monkeypatch, fake, ["ready", "--json"])

    assert res.exit_code == 0
    marks = {b["id"]: b["deferred"] for b in json.loads(res.stdout)}
    assert marks == {"mr-1": False, "mr-2": True}  # head-of-queue startable, overlapper deferred


def test_ready_gated_view_is_not_start_gated(monkeypatch):
    # `--gated` is the merger's scorer-sorted view (sibling bead .7) — start-gating leaves it alone.
    beads = [{"id": "mr-1", "status": "open", "labels": ["path:src/x.py"]}]
    payload = json.dumps(beads)
    fake = FakeReadBd(stdout=payload)
    _opt_into_release(monkeypatch)
    res = _run(monkeypatch, fake, ["ready", "--gated", "--json"])

    assert res.exit_code == 0
    assert res.stdout == payload  # forwarded verbatim — no `deferred` annotation
    assert "deferred" not in json.loads(res.stdout)[0]


# ---- issue (show a single bead) ---------------------------------------------


def test_issue_forwards_show_id_json(monkeypatch):
    payload = '{"id": "mr-7", "labels": ["model:sonnet", "harness:claude"]}\n'
    fake = FakeReadBd(stdout=payload)
    res = _run(monkeypatch, fake, ["issue", "mr-7", "--json"])

    assert res.exit_code == 0
    assert fake.last[-3:] == ["show", "mr-7", "--json"]
    assert res.stdout == payload
    assert json.loads(res.stdout)["labels"] == ["model:sonnet", "harness:claude"]


# ---- list / filter ----------------------------------------------------------


def test_list_filters_by_state(monkeypatch):
    fake = FakeReadBd(stdout="[]\n")
    res = _run(monkeypatch, fake, ["list", "--status", "in_progress", "--json"])

    assert res.exit_code == 0
    assert fake.last[-4:] == ["list", "--status", "in_progress", "--json"]


# ---- exit-code propagation --------------------------------------------------


def test_read_propagates_bd_exit_code(monkeypatch):
    fake = FakeReadBd(returncode=2, stderr="boom\n")
    res = _run(monkeypatch, fake, ["issue", "missing"])

    assert res.exit_code == 2


# ---- show: gates section (bh-i371) -------------------------------------------

# One gate per kind, descriptions mirroring what the verbs stamp: kickoff (plan), review
# (submit), security: (warden), and an unstamped ad-hoc hold. The resolved kickoff gate is
# listed FIRST by bd so the open-first re-ordering is observable.
SHOW_GATES = [
    {"id": "g0", "status": "closed", "description": "blocks mr-9\n\nReason: kickoff mr-epic"},
    {"id": "g1", "status": "open", "description": "blocks mr-9\n\nReason: review cafef00d"},
    {"id": "g2", "status": "open", "description": "blocks mr-9\n\nReason: security:sast pending"},
    {"id": "g3", "status": "open", "description": "blocks mr-9\n\nReason: operator hold"},
]


def _run_show(monkeypatch, gates, argv=("show", "mr-9")):
    """Drive `ws work show` through CliRunner with the git producers faked (no repo needed);
    the same FakeReadBd serves `bd gate list --all` (the only bd read on the show path)."""
    fake = FakeReadBd(stdout=json.dumps(gates))
    monkeypatch.setattr(work.bd, "_run", fake)
    monkeypatch.setattr(work.config, "load", lambda: {})
    monkeypatch.setattr(
        worktree_mod,
        "locate",
        lambda cfg, hive, bead, **kw: (
            {"prefix": "mr"},
            Path("/fake/main"),
            Path("/fake/wt"),
            "wt/bead/issue/mr-9",
        ),
    )
    monkeypatch.setattr(worktree_mod, "integration_base", lambda entry, bead, integration: "main")
    monkeypatch.setattr(worktree_mod, "base_of", lambda entry, branch, integration: "abc1234def")
    monkeypatch.setattr(worktree_mod, "commit_rows", lambda entry, base, branch: [])
    monkeypatch.setattr(config_mod, "integration_branch", lambda cfg, entry: "main")
    monkeypatch.setattr(config_mod, "max_commits", lambda cfg, entry: 10)
    return CliRunner().invoke(work.app, list(argv))


def test_show_gates_section_renders_kind_status_reason_id(monkeypatch):
    """Every gate touching the bead renders: kind (kickoff/review/security/ad-hoc), open ○ vs
    resolved ✓, gate id, and the reason snippet — resolved gates stay visible as history."""
    res = _run_show(monkeypatch, SHOW_GATES)

    assert res.exit_code == 0
    assert "gates: 4 (3 open)" in res.stdout
    assert "○ review gate g1: review cafef00d" in res.stdout
    assert "○ security gate g2: security:sast pending" in res.stdout
    assert "○ ad-hoc gate g3: operator hold" in res.stdout
    assert "✓ kickoff gate g0: kickoff mr-epic" in res.stdout  # resolved, marked ✓


def test_show_gates_open_first_ordering(monkeypatch):
    """Open gates render before resolved ones even when bd lists the resolved gate first."""
    res = _run_show(monkeypatch, SHOW_GATES)

    out = res.stdout
    assert out.index("review gate g1") < out.index("kickoff gate g0")
    assert out.index("ad-hoc gate g3") < out.index("kickoff gate g0")


def test_show_no_gates_no_section(monkeypatch):
    """A bead no gate touches renders no gates section at all (compact, not an empty header)."""
    res = _run_show(monkeypatch, [])

    assert res.exit_code == 0
    assert "gates:" not in res.stdout


def test_show_json_carries_gate_rows_open_first(monkeypatch):
    """`show --json` exposes the same gate list under `gates` — id/kind/status/reason rows."""
    res = _run_show(monkeypatch, SHOW_GATES, argv=("show", "mr-9", "--json"))

    rows = json.loads(res.stdout)["gates"]
    assert [r["id"] for r in rows] == ["g1", "g2", "g3", "g0"]  # open first, bd order kept
    assert rows[3] == {
        "id": "g0",
        "kind": "kickoff",
        "status": "resolved",
        "reason": "kickoff mr-epic",
    }
