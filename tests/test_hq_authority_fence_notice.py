from __future__ import annotations

from types import SimpleNamespace

from beadhive import hq_sql_runtime
from beadhive.hq_sql_config import SqlFleetConfigRevisionStore


class _Authority:
    def __init__(self, result):
        self.result = result

    def __call__(self, *args, **kwargs):
        return self

    def load_state(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _state(frames):
    return {"frames": {f"f{i}": {"active": {"x": 1}, "candidate": None} for i in range(frames)}}


def _store(monkeypatch, result, *, runtime=True):
    monkeypatch.setattr(hq_sql_runtime, "SqlRuntimeAuthority", _Authority(result))
    store = SqlFleetConfigRevisionStore({"runtime": {"x": 1}} if runtime else {}, broker=object())
    monkeypatch.setattr(
        store,
        "_publish_snapshot_core",
        lambda documents, **kw: SimpleNamespace(commit_revision="NEWHEAD"),
    )
    return store


def test_bound_authority_announces_count_and_renew_command(monkeypatch, capsys):
    store = _store(monkeypatch, ("AUTHREV", _state(3), ("b", "g", "OLDHEAD"), {}))
    out = store.publish_snapshot((), expected_revision="OLDHEAD")
    assert out.commit_revision == "NEWHEAD"
    err = capsys.readouterr().err
    assert "unbind HQ authority for 3 active frame(s)" in err
    assert "3 active frame(s) are fenced" in err
    assert "OLDHEAD" in err and "NEWHEAD" in err
    assert "bh hq authority renew --expected-revision AUTHREV" in err
    assert "--operator-key" in err and "--confirm" in err and "OFF-FRAME" in err


def test_silent_without_runtime_authority(monkeypatch, capsys):
    store = _store(monkeypatch, RuntimeError("never"), runtime=False)
    store.publish_snapshot((), expected_revision="OLDHEAD")
    assert capsys.readouterr().err == ""


def test_silent_when_authority_not_bound_to_this_head(monkeypatch, capsys):
    store = _store(monkeypatch, ("AUTHREV", _state(2), ("b", "g", "NEWHEAD"), {}))
    store.publish_snapshot((), expected_revision="OLDHEAD")
    assert capsys.readouterr().err == ""


def test_authority_read_error_degrades_and_never_fails_publish(monkeypatch, capsys):
    store = _store(monkeypatch, ValueError("boom"))
    out = store.publish_snapshot((), expected_revision="OLDHEAD")
    assert out.commit_revision == "NEWHEAD"
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "could not be read" in err[0]
