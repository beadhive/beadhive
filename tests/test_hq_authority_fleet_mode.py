"""Fleet default authority mode and open admission (bh-taa04.3).

Effective mode = explicit frame pin (signed|trusted) > inherit -> the fleet default this host
LEARNED from an operator-authorised config read > signed. A key-less HQ writer cannot raise a
signed fleet to trusted; lowering is always taken. In a trusted fleet a registering frame is
open-admitted (one key-less ACTIVE record, no grant/enrollment/operator key) and cordon still
removes it. A frame pinned signed in a trusted fleet refuses with an actionable message.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from typer.testing import CliRunner

import test_hq_authority_backend as git_hq
import test_hq_authority_trusted as trusted_hq
import test_hq_authority_trusted_publish as trusted_publish
from beadhive import frame_eligibility as policy
from beadhive import hosts, hq_authority_enforce, hq_authority_expiry, hq_operator_settings
from beadhive.cli import app
from beadhive.hq_control_plane import ControlPlaneError, open_admission_guard
from beadhive.hq_hive_policy import config_head_tolerated
from beadhive.hq_sql_runtime import SqlRuntimeError
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot

ae = hq_authority_enforce
MODE_ENV = ae.MODE_ENV
NOW = trusted_hq.NOW
HEAD_CONFIG = trusted_hq.HEAD_CONFIG

# The real SSH-signed Git HQ, guard provisioned by a trusted operator (key-less pushes allowed).
backend, _prepared_hqs = trusted_publish.backend, trusted_publish._prepared_hqs
sql = trusted_hq.sql


@pytest.fixture(autouse=True)
def _baseline(monkeypatch):
    for name in (MODE_ENV, ae.ENFORCE_ENV, hq_operator_settings.ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ae, "_banner_emitted", False)
    monkeypatch.setattr(ae, "_invalid_emitted", set())
    ae.reset_cache()
    yield
    ae.reset_cache()


def _learn(value):
    assert ae.record_fleet_default(value, head="h", via="test") or ae.fleet_default() == value


def _forget():
    path = ae._fleet_state_path()
    if path and os.path.exists(path):
        os.unlink(path)
    ae.reset_cache()


def _fleet(default=None, extra=""):
    hq = f"hq:\n  default_authority_mode: {default}\n" if default else ""
    return FleetConfigDocument("fleet.yaml", f"schema_version: 1\n{hq}managed_repos: []\n{extra}")


def _snapshot(revision, *documents):
    return FleetConfigSnapshot(
        backend_identity="config",
        commit_revision=revision,
        generation="cgen",
        fetched_at=NOW,
        valid_until=NOW + 3600,
        documents=tuple(documents),
    )


# ---- the resolver ----------------------------------------------------------------------------


def test_nothing_set_is_signed_as_in_0_23_1():
    assert ae.fleet_default() == "signed"
    assert ae.resolve(None) == ("signed", "default")
    assert ae.resolve("inherit").mode == "signed"
    assert ae.mode() == "signed" and ae.enforced()


def test_inherit_follows_the_learned_fleet_default():
    _learn("trusted")
    assert ae.resolve(None) == ("trusted", "fleet")
    assert ae.resolve("inherit") == ("trusted", "fleet")
    assert ae.mode() == "trusted" and ae.fleet_trusted()
    assert not ae.content_waived()  # a trusted fleet waives no content
    assert any("fleet default" in line for line in ae.doctor_warnings())
    _learn("signed")
    assert ae.resolve(None) == ("signed", "default")


@pytest.mark.parametrize("fleet", ["signed", "trusted"])
def test_an_explicit_pin_wins_both_ways(monkeypatch, fleet):
    _learn(fleet)
    assert ae.resolve("signed") == ("signed", "host")
    assert ae.resolve("trusted") == ("trusted", "host")
    monkeypatch.setenv(MODE_ENV, "signed")
    assert ae.resolve("trusted") == ("signed", "env")
    monkeypatch.setenv(MODE_ENV, "trusted")
    assert ae.resolve("signed") == ("trusted", "env")
    monkeypatch.setenv(MODE_ENV, "inherit")
    assert ae.resolve("signed").mode == fleet


def test_fleet_default_is_one_stat_per_read(monkeypatch):
    _learn("trusted")
    assert ae.fleet_default() == "trusted"
    reads = []
    original = ae.fleet_state
    monkeypatch.setattr(ae, "fleet_state", lambda: reads.append(1) or original())
    for _ in range(100):
        assert ae.mode() == "trusted"
    assert reads == []  # memoized on the state file's mtime
    path = ae._fleet_state_path()
    stamp = os.stat(path).st_mtime_ns
    with open(path, "w") as handle:
        json.dump({"mode": "signed"}, handle)
    os.utime(path, ns=(stamp + 10**9, stamp + 10**9))
    assert ae.mode() == "signed" and reads == [1]


def test_unreadable_or_invalid_state_is_signed():
    path = ae._fleet_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for body in ("{", json.dumps({"mode": "bogus"}), json.dumps(["trusted"])):
        with open(path, "w") as handle:
            handle.write(body)
        ae.reset_cache()
        assert ae.fleet_default() == "signed"


def test_learning_raises_only_when_authorised_and_always_lowers():
    raised = _snapshot("b" * 32, _fleet("trusted"))
    assert ae.observe_fleet_default(raised, authorised=False) == "signed"
    assert ae.fleet_default() == "signed"  # a key-less HQ write does not raise the fleet
    assert ae.observe_fleet_default(raised, authorised=True) == "trusted"
    assert ae.fleet_state()["config_head"] == "b" * 32
    lowered = _snapshot("c" * 32, _fleet())
    assert ae.observe_fleet_default(lowered, authorised=False) == "signed"
    assert ae.observe_fleet_default(raised, authorised=False) == "signed"


def test_committed_default_parses_only_an_explicit_trusted():
    assert ae.committed_fleet_default([_fleet("trusted")]) == "trusted"
    assert ae.committed_fleet_default([_fleet("signed")]) == "signed"
    assert ae.committed_fleet_default([_fleet()]) == "signed"
    assert ae.committed_fleet_default([FleetConfigDocument("fleet.yaml", ": [")]) == "signed"
    assert ae.committed_fleet_default([]) == "signed"


def test_the_fleet_key_is_fleet_owned_and_validated():
    from beadhive.modules.config import contracts
    from beadhive.modules.config.application import partition

    assert partition.partition_of("hq.default_authority_mode") == partition.FLEET
    assert contracts.HqConfig().default_authority_mode == "signed"
    with pytest.raises(ValueError):
        contracts.HqConfig(default_authority_mode="inherit")


# ---- the relevance set: a changed fleet default fences signed frames --------------------------


def test_a_changed_fleet_default_is_frame_relevant():
    bound = _snapshot("a" * 32, _fleet())
    unrelated = _snapshot("b" * 32, _fleet(extra="# an unrelated comment\n"))
    raised = _snapshot("c" * 32, _fleet("trusted"))
    assert config_head_tolerated(bound, unrelated, {}, valid_until=NOW + 60, now=NOW)
    assert not config_head_tolerated(bound, raised, {}, valid_until=NOW + 60, now=NOW)
    assert not config_head_tolerated(raised, unrelated, {}, valid_until=NOW + 60, now=NOW)


# ---- SQL frames: learning rides on the verified, operator-bound read ----------------------------


def test_sql_keyless_raise_fences_a_signed_frame_and_is_not_learned(sql, monkeypatch):
    crossref = ("config", "cgen", HEAD_CONFIG)
    moved = _snapshot("b" * 32, _fleet("trusted"))  # written by a key-less HQ writer
    monkeypatch.setattr(sql.authority, "load_latest_config_at", lambda *a, **k: moved)
    monkeypatch.setattr(sql.authority, "_tolerated_bound_config", lambda *a, **k: None)
    with pytest.raises(SqlRuntimeError, match="not bound"):
        sql.authority.bound_config_at(sql.cursor, crossref, policies={}, valid_until=NOW + 60)
    assert ae.fleet_default() == "signed" and ae.enforced()


def test_sql_operator_signed_binding_raises_an_inherit_frame(sql, monkeypatch):
    bound = _snapshot(HEAD_CONFIG, _fleet("trusted"))  # the head the signed authority names
    monkeypatch.setattr(sql.authority, "load_latest_config_at", lambda *a, **k: bound)
    crossref = ("config", "cgen", HEAD_CONFIG)
    assert sql.authority.bound_config_at(sql.cursor, crossref)[0] is bound
    assert ae.fleet_default() == "trusted" and ae.mode() == "trusted"
    # Now trusted: a later unbound read that lowers the default is always taken.
    lowered = _snapshot("d" * 32, _fleet())
    monkeypatch.setattr(sql.authority, "load_latest_config_at", lambda *a, **k: lowered)
    sql.authority.bound_config_at(sql.cursor, crossref)
    assert ae.fleet_default() == "signed" and ae.enforced()


def test_sql_trusted_pin_never_learns_trusted_from_an_unbound_head(sql, monkeypatch):
    monkeypatch.setenv(MODE_ENV, "trusted")
    moved = _snapshot("b" * 32, _fleet("trusted"))
    monkeypatch.setattr(sql.authority, "load_latest_config_at", lambda *a, **k: moved)
    sql.authority.bound_config_at(sql.cursor, ("config", "cgen", HEAD_CONFIG))
    assert ae.fleet_default() == "signed"


def test_sql_frame_pinned_signed_in_a_trusted_fleet_refuses_actionably(sql, monkeypatch):
    _learn("trusted")
    assert sql.authority.verified_state_at(sql.cursor, "valid")[0]["revision"] == 2  # inherit
    monkeypatch.setenv(MODE_ENV, "signed")
    with pytest.raises(SqlRuntimeError, match="pins hq.authority_mode: signed.*inherit"):
        sql.authority.verified_state_at(sql.cursor, "valid")
    assert any("pins hq.authority_mode: signed" in line for line in ae.doctor_warnings())


class _HistoryCursor(trusted_hq._Cursor):
    """The trusted-test cursor plus first-parent history (``<head>~<n>``) reads."""

    def execute(self, sql, params=None):
        if "SELECT operator_signature FROM hq_authority AS OF" in sql:
            row = self.rows.get(params[0])
            self.result = [] if row is None else [(row[-1],)]
            return
        if "FROM hq_authority AS OF" in sql and params[0] not in self.rows:
            raise SqlRuntimeError("no such revision")
        super().execute(sql, params)

    def fetchone(self):
        return self.result[0] if self.result else None


def _discovery(sql, monkeypatch, committed):
    rows = dict(sql.cursor.rows)
    rows["late~1"] = rows["unsigned"]  # an earlier key-less publication
    rows["late~2"] = rows["valid"]  # the operator's signed rebind
    rows["late"] = rows["unsigned"]
    cursor = _HistoryCursor(rows)
    seen = []

    def committed_config(_cursor, crossref):
        seen.append(crossref)
        return _snapshot(crossref[2], _fleet(committed))

    monkeypatch.setattr(sql.authority, "_committed_config_at", committed_config)
    return cursor, seen


def test_sql_new_frame_discovers_an_operator_signed_trusted_fleet(sql, monkeypatch):
    cursor, seen = _discovery(sql, monkeypatch, "trusted")
    state, crossref, _ = sql.authority.verified_state_at(cursor, "late")
    assert state["revision"] == 2 and seen == [("config", "cgen", HEAD_CONFIG)]
    assert ae.fleet_default() == "trusted" and ae.fleet_state()["via"] == "sql-discovery"


def test_sql_discovery_of_a_signed_fleet_still_refuses_unsigned(sql, monkeypatch):
    cursor, _seen = _discovery(sql, monkeypatch, None)
    with pytest.raises(SqlRuntimeError, match="UNSIGNED"):
        sql.authority.verified_state_at(cursor, "late")
    assert ae.fleet_default() == "signed"


def test_sql_discovery_needs_an_operator_signature(sql, monkeypatch, tmp_path):
    forged = trusted_hq._key(tmp_path / "forger")
    assert forged
    cursor, seen = _discovery(sql, monkeypatch, "trusted")
    cursor.rows["late~2"] = cursor.rows["bad-signature"]
    with pytest.raises(SqlRuntimeError, match="UNSIGNED"):
        sql.authority.verified_state_at(cursor, "late")
    assert seen == [] and ae.fleet_default() == "signed"


def test_sql_pinned_signed_frame_meeting_unsigned_names_the_conflict(sql, monkeypatch):
    cursor, _seen = _discovery(sql, monkeypatch, "trusted")
    monkeypatch.setenv(MODE_ENV, "signed")
    with pytest.raises(SqlRuntimeError, match="pins hq.authority_mode: signed"):
        sql.authority.verified_state_at(cursor, "late")


# ---- authority check agrees with trusted frames -----------------------------------------------


def test_check_does_not_fail_an_expired_authority_in_trusted_mode(monkeypatch):
    status = {
        "revision": "r",
        "expires_at": NOW - 60,
        "expires_in_s": -60,
        "expires_in": "expired",
        "config_bound": False,
        "expires_never": False,
    }
    monkeypatch.setattr(
        hq_authority_expiry,
        "authority_status",
        lambda plane, now=None: {**status, "mode": hq_authority_expiry._mode()},
    )
    healthy, _, message = hq_authority_expiry.check(object())
    assert not healthy and message.startswith("FAIL")
    _learn("trusted")
    healthy, result, message = hq_authority_expiry.check(object())
    assert healthy and result["mode"] == "trusted" and "not enforced (trusted)" in message


# ---- open admission ---------------------------------------------------------------------------


def test_open_admission_refuses_outside_a_trusted_fleet(monkeypatch):
    with pytest.raises(ControlPlaneError, match="requires a trusted fleet"):
        open_admission_guard({})
    monkeypatch.setenv(MODE_ENV, "trusted")  # a trusted PIN is not a trusted fleet
    with pytest.raises(ControlPlaneError, match="requires a trusted fleet"):
        open_admission_guard({})
    _learn("trusted")
    assert open_admission_guard({"declared": False}) == {"declared": True}


def _cli(*args):
    return CliRunner().invoke(app, ["hq", "authority", *args])


def _base_config(b):
    store = b["plane"].config_store(operator_key=str(b["operator"]))
    return store.publish_snapshot((_fleet(),), expected_revision="")


def _unsigned_config_raise(b, monkeypatch):
    """A key-less HQ write raising the default (possible on this trusted-provisioned server)."""
    monkeypatch.setenv(MODE_ENV, "trusted")
    store = b["plane"].config_store()
    current = store.load_snapshot()
    published = store.publish_snapshot(
        (_fleet("trusted"),), expected_revision=current.commit_revision
    )
    monkeypatch.delenv(MODE_ENV)
    _forget()
    return published


def test_git_mode_command_raise_learn_join_cordon_and_keyless_raise(backend, monkeypatch, tmp_path):
    b = backend
    plane = b["plane"]
    base = _base_config(b)
    assert ae.committed_fleet_default(base) == "signed"

    # Raising needs the operator key once.
    refused = _cli("mode-trusted", "--confirm")
    assert refused.exit_code == 1 and "needs the operator key once" in refused.output
    raised = _cli("mode-trusted", "--operator-key", str(b["operator"]), "--confirm")
    assert raised.exit_code == 0, raised.output
    result = json.loads(raised.output.strip().splitlines()[-1])
    assert result["fleet_default"] == "trusted" and result["published"]
    assert not trusted_publish._unsigned(b, result["config_revision"])
    assert ae.fleet_default() == "trusted"

    # A frame that never saw the command learns it from the operator-signed carrier.
    _forget()
    snapshot = plane.config_store().load_snapshot()
    assert ae.committed_fleet_default(snapshot) == "trusted"
    assert ae.fleet_default() == "trusted" and ae.mode() == "trusted"
    shown = _cli("mode")
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output.strip().splitlines()[-1])["mode"] == "trusted"

    # ... and so does one that meets an unsigned record first (discovery).
    trusted_hq._unsigned_revision(b)
    _forget()
    assert plane._read()[0] and ae.fleet_default() == "trusted"

    # Open admission: a brand-new frame joins with no grant, enrollment or operator key.
    other = git_hq.key(tmp_path / "frame-two-runtime")
    public = tmp_path / "frame-two-runtime.pub"
    manifest = hosts.HostManifest(
        host_id="host-two",
        frame_id="frame-two",
        instance_ref="vm-two",
        release=b["desired"]["release"],
        capabilities=b["desired"]["caps"],
        os="linux",
        arch="x86_64",
        label="two",
        role="executor",
        identity={"kind": "none"},
    )
    hosts.save(b["op_repo"], manifest)
    joined = _cli("join", "--frame", "host-two", "--public-key", str(public), "--confirm")
    assert joined.exit_code == 0, joined.output
    result = json.loads(joined.output.strip().splitlines()[-1])
    assert result["admitted"] and result["changed"] and result["frame_id"] == "frame-two"
    assert trusted_publish._unsigned(b, result["revision"])
    _sha, state, _ = plane._read()
    record = state["frames"]["frame-two"]["active"]
    assert record["state"] == "active" and record["desired"]["declared"] is True
    assert record["authority"]["holder_identity"] == "host-two"
    assert record["authority"]["instance_ref"] == "vm-two"
    assert record["public_key"] == other.strip()
    assert record["authority"]["audience"] == "fleet-one"  # copied from the fleet
    again = _cli("join", "--frame", "host-two", "--public-key", str(public), "--confirm")
    assert json.loads(again.output.strip().splitlines()[-1])["changed"] is False

    desired = plane.fetch_config("frame-two", holder_identity="host-two")
    facts = _facts(manifest, desired, record)
    assert policy.eligible(manifest, {}, facts).allowed

    # Cordon (key-less, trusted) removes it.
    plan = plane.lifecycle("cordon", "frame-two")
    plane.lifecycle(
        "cordon",
        "frame-two",
        "apply",
        expected=plan["revision"],
        expected_host_id="host-two",
        expected_release=plan["release"],
        operator_key="",
        confirm=True,
    )
    desired = plane.fetch_config("frame-two", holder_identity="host-two")
    decision = policy.eligible(manifest, {}, _facts(manifest, desired, record))
    assert not decision.allowed and "not_cordoned" in decision.reason.split(", ")

    # Lowering is always allowed; the operator falls back to signed.
    lowered = _cli("mode-signed", "--operator-key", str(b["operator"]), "--confirm")
    assert lowered.exit_code == 0, lowered.output
    assert ae.fleet_default() == "signed" and ae.enforced()
    assert ae.committed_fleet_default(plane.config_store().load_snapshot()) == "signed"

    # A key-less HQ write raising the default again: a signed frame neither learns nor accepts
    # it; discovery finds the operator's newest signed carrier (signed) instead.
    keyless = _unsigned_config_raise(b, monkeypatch)
    assert trusted_publish._unsigned(b, keyless.commit_revision)
    with pytest.raises(ValueError, match="UNSIGNED"):
        plane.config_store().load_snapshot()
    assert ae.fleet_default() == "signed" and ae.enforced()


def _facts(manifest, desired, record):
    from datetime import UTC, datetime

    from beadhive.host_heartbeat_core import HeartbeatLease, VerifiedObservation

    authority = record["authority"]
    lease = HeartbeatLease(
        frame_id=manifest.frame_id,
        holderIdentity=manifest.host_id,
        instance_ref=manifest.instance_ref,
        key_id=authority["key_fingerprint"],
        epoch=authority["epoch"],
        audience=authority["audience"],
        config_revision=authority["config_revision"],
        seq=1,
        renewTime=datetime.now(UTC).isoformat(),
        release=desired["release"],
        state_seen="active",
        conformance={
            "profile": desired["profile"],
            "status": "conformant",
            "checks": [{"id": "required", "status": "pass"}],
        },
        report_digest="sha256:" + "2" * 64,
    )
    observation = VerifiedObservation("fresh", True, True, 1.0, lease)
    return policy.EligibilityFacts(observation, desired, True, True, time.time())


def test_git_signed_fleet_refuses_open_admission(backend, tmp_path):
    b = backend
    with pytest.raises(ControlPlaneError, match="requires a trusted fleet"):
        b["plane"].admit_open(b["authority"], b["public"], b["desired"], expected="")
    result = _cli("join", "--frame", "host-two", "--public-key", str(b["operator_public"]))
    assert result.exit_code == 1 and "requires --confirm" in result.output


def test_mode_value_rides_in_the_action_and_is_validated():
    assert _cli("mode", "trusted").exit_code == 2  # the value rides in the action
    result = _cli("mode-inherit", "--confirm")
    assert result.exit_code == 1 and "signed|trusted" in result.output


def test_registration_open_admits_only_in_a_trusted_fleet(monkeypatch, capsys):
    from beadhive import (
        host,
        hq,
        hq_authority_fleet_mode,
        hq_open_admission,
        hq_operator_settings,
    )

    calls = []
    monkeypatch.setattr(host, "host_id", lambda: "host-two")
    monkeypatch.setattr(hq_operator_settings, "select_plane", lambda *a: "plane")
    monkeypatch.setattr(hq_authority_fleet_mode, "_local_public_key", lambda: "ssh-ed25519 K")
    monkeypatch.setattr(
        hq_open_admission, "join", lambda plane, host_id, key, **_: calls.append((plane, host_id))
    )
    hq._open_admission("host-two")
    assert calls == []  # signed fleet: registration waits for a grant as before
    _learn("trusted")
    hq._open_admission("other-host")
    assert calls == []  # only the registering frame admits itself
    hq._open_admission("host-two")
    assert calls == [("plane", "host-two")]

    def refuse(*_a, **_k):
        raise ControlPlaneError("separate authority writer capability unavailable")

    monkeypatch.setattr(hq_open_admission, "join", refuse)
    hq._open_admission("host-two")  # never fails registration
    assert "open admission skipped" in capsys.readouterr().err
