"""`ws work ready|issue|list` — the first-class bead reads that replace `ws bd` in the loops.

These verbs forward straight to `bd` and stream its bytes through verbatim, so the coordinator
loop's consumed shapes (`ws bd ready --json`, `ws bd show <id> --json`) stay stable once the bd
passthrough is gated off. The test seam mirrors the rest of the suite: patch the one `ws.work.run`
symbol with a fake `bd`, drive the verbs through Typer's CliRunner, and assert the forwarded argv,
the byte-identical output, and the propagated exit code.
"""

from __future__ import annotations

import json
from collections import namedtuple

from typer.testing import CliRunner

from beadhive import work

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
