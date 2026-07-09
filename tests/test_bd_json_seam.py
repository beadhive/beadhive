"""Unit tests for the public bd.json seam —.

Two paths only (per acceptance criteria):
  * JSON return path  — non-zero exit → None; valid JSON stdout → parsed dict/list.
  * None path         — non-zero exit → None; invalid JSON stdout → None.

The seam patches ``ws.bd.run`` so no real ``bd`` binary is needed.
"""

from __future__ import annotations

import json
from collections import namedtuple

from beadhive import bd as bd_mod

_CP = namedtuple("CP", "returncode stdout stderr")


def test_bd_json_returns_parsed_dict_on_success(monkeypatch):
    """Happy path: bd exits 0 with valid JSON → bd.json returns the parsed object."""
    payload = {"id": "mr-1", "status": "open"}

    monkeypatch.setattr(bd_mod, "run", lambda cmd, **_kw: _CP(0, json.dumps(payload), ""))

    result = bd_mod.json(["show", "mr-1"], "/some/rig")

    assert result == payload


def test_bd_json_returns_parsed_list_on_success(monkeypatch):
    """bd.json handles a list-shaped response (bd list returns an array)."""
    payload = [{"id": "mr-1"}, {"id": "mr-2"}]

    monkeypatch.setattr(bd_mod, "run", lambda cmd, **_kw: _CP(0, json.dumps(payload), ""))

    result = bd_mod.json(["list"], "/some/rig")

    assert result == payload


def test_bd_json_appends_json_flag(monkeypatch):
    """bd.json appends --json to the command itself; callers must NOT pass it."""
    recorded = []

    def fake_run(cmd, **_kw):
        recorded.append(list(cmd))
        return _CP(0, "null", "")

    bd_mod.json(["show", "mr-1"], "/rig")  # no monkeypatch yet — just verify flag is appended

    monkeypatch.setattr(bd_mod, "run", fake_run)
    bd_mod.json(["show", "mr-1"], "/rig")

    assert recorded[0][-1] == "--json"
    assert "show" in recorded[0]
    assert "mr-1" in recorded[0]


def test_bd_json_returns_none_on_nonzero_exit(monkeypatch):
    """None path (non-zero exit): bd.json returns None, never raises."""
    monkeypatch.setattr(bd_mod, "run", lambda cmd, **_kw: _CP(1, "", "Error: not found"))

    result = bd_mod.json(["show", "missing"], "/rig")

    assert result is None


def test_bd_json_returns_none_on_invalid_json(monkeypatch):
    """None path (bad JSON): bd exits 0 but stdout is not JSON → bd.json returns None."""
    monkeypatch.setattr(bd_mod, "run", lambda cmd, **_kw: _CP(0, "not valid json }{", ""))

    result = bd_mod.json(["show", "mr-1"], "/rig")

    assert result is None


def test_bd_json_returns_none_on_empty_stdout(monkeypatch):
    """bd exits 0 with empty stdout → None (json.loads('null') returns None, not an error)."""
    monkeypatch.setattr(bd_mod, "run", lambda cmd, **_kw: _CP(0, "", ""))

    result = bd_mod.json(["show", "mr-1"], "/rig")

    # json.loads("null") == None in Python, so the contract is preserved
    assert result is None
