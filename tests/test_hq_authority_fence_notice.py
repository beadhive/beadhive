from __future__ import annotations

from types import SimpleNamespace

import pytest

from beadhive import hq_sql_runtime
from beadhive.hq_sql_config import SqlFleetConfigRevisionStore
from harness import config_tolerance as tolerance


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
    assert "bh hq authority rebind --expected-revision AUTHREV" in err
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


# bh-3h6al: the notice uses the same tolerance check as the frames.
NOW = 1_000_000.0
EXPIRES = NOW + 86400


def _binding_store(monkeypatch, latest_fleet=tolerance.FLEET, *, latest=tolerance.H0):
    policies = tolerance.signed_policies(expires_at=EXPIRES, now=NOW)
    state = {**_state(3), "expires_at": EXPIRES}
    monkeypatch.setattr(
        hq_sql_runtime,
        "SqlRuntimeAuthority",
        _Authority(("AUTHREV", state, tolerance.CROSSREF, policies)),
    )
    current = tolerance.snapshot(latest, latest_fleet, now=NOW)
    store = tolerance.binding_store(monkeypatch, current, now=NOW)
    store.settings["runtime"] = {"x": 1}
    monkeypatch.setattr(
        store,
        "_publish_snapshot_core",
        lambda documents, **kw: SimpleNamespace(commit_revision="c" * 32),
    )
    return store


@pytest.mark.parametrize("latest", [tolerance.H0, tolerance.H1], ids=["exact", "tolerated"])
@pytest.mark.parametrize("fleet", [tolerance.FLEET, tolerance.BYPASS_FLEET])
def test_frame_irrelevant_publish_announces_nothing(monkeypatch, capsys, latest, fleet):
    store = _binding_store(monkeypatch, latest=latest)
    out = store.publish_snapshot(tolerance.documents(fleet), expected_revision=latest)
    assert out.commit_revision == "c" * 32
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "published",
    [
        tolerance.documents(tolerance.POLICY_FLEET),
        tolerance.documents(host=tolerance.HOST + "label: x\n"),
    ],
    ids=["frame_policy", "host-manifest"],
)
@pytest.mark.parametrize("latest", [tolerance.H0, tolerance.H1], ids=["exact", "tolerated"])
def test_frame_relevant_publish_still_announces(monkeypatch, capsys, published, latest):
    store = _binding_store(monkeypatch, tolerance.BYPASS_FLEET, latest=latest)
    store.publish_snapshot(published, expected_revision=latest)
    err = capsys.readouterr().err
    assert "unbind HQ authority for 3 active frame(s)" in err
    assert "3 active frame(s) are fenced" in err
    assert "bh hq authority rebind --expected-revision AUTHREV" in err


def test_binding_read_failure_keeps_exact_head_notices(monkeypatch, capsys):
    store = _binding_store(monkeypatch)
    monkeypatch.setattr(
        store, "authority_binding", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    store.publish_snapshot(
        tolerance.documents(tolerance.BYPASS_FLEET), expected_revision=tolerance.H0
    )
    err = capsys.readouterr().err
    assert "unbind HQ authority" in err and "are fenced" in err
