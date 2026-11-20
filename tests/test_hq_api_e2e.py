"""The 0.40.0 epic proof (bh-jwymz): a frame on ``hq.route: api`` with NO ``hq.sql`` block runs
its whole life against the in-process fake hq-service, and no HQ SQL opener is ever reached.

One frame, in order: ``host init`` (the manifest), the join (incarnation, then open admission:
ACTIVE), ``hq authority rebind``, ``hq authority release-policy`` plan and apply, session renew
straight after admission, the conformance evidence post, the beat verb, then a hive-lease
claim (adopt), renew and release. Nothing needs a network, a database or a secret store.

"No SQL" is asserted three ways. ``BH_HQ_SQL_GUARD=refuse`` makes every guarded opener raise
:class:`SqlPlaneRetired`; ``guard_sql_open`` and the five opener constructors are spied and
must record zero calls; and the raw connect paths are tripwires.

KNOWN 0.40.0 GAP (operator decision 2026-11-20, carried here from bh-65s71): on api a frame
renews only an ACTIVE grant. A CANDIDATE slot has no active grant, so ``GET /eligibility`` reads
``active: null`` and renew is a refusal (``no_current_grant``) that posts nothing; on SQL a
candidate could renew its way to promotion. ``test_a_candidate_slot_does_not_renew_on_api`` pins
the honest behaviour so the gap cannot widen unnoticed. No clock is real: nothing sleeps or
retries.
"""

from __future__ import annotations

import json
import types

import pytest
from tests import test_hq_api_join as join
from tests.harness.fake_hq_service import FakeHqService
from tests.test_hq_api_join import (
    FRAME,
    HOST_ID,
    NOW,
    URL,
    _authority,
    _calls,
    _config,
    _eligibility,
    _Frozen,
    _join,
    _manifest,
    _passthrough,
    _result,
    frame_key,  # noqa: F401 - fixture
)
from typer.testing import CliRunner

from beadhive import (
    config,
    heartbeat_conformance,
    heartbeat_sender,
    host_lease,
    hq_authority_enforce,
    hq_control_plane,
    hq_frame_grant,
    hq_release_catalog,
    hq_service_adapter,
    hq_sql_config,
    hq_sql_guard,
    hq_sql_operator,
    hq_sql_placement,
    hq_sql_transport,
)
from beadhive import fleet_roster as roster
from beadhive import heartbeat_report as report
from beadhive.hq_port import HqUnavailable
from beadhive.hq_service_adapter import adapter_from_config

MONO = 1000.0  # the frozen monotonic clock deadlines are taken from
HOST_CONFIG = (
    f"hq:\n  route: api\n  beadyard_id: {join.YARD}\n  api:\n    url: {URL}\n"
    f"host:\n  frame_id: {FRAME}\n"
)
OPENERS = (
    (hq_control_plane.SqlControlPlane, "SqlControlPlane"),
    (hq_sql_config.SqlFleetConfigRevisionStore, "SqlFleetConfigRevisionStore"),
    (hq_sql_operator.SqlRuntimeOperator, "SqlRuntimeOperator"),
    (hq_release_catalog.ReleaseCatalogStore, "ReleaseCatalogStore"),
    (hq_sql_placement.SqlPlacementDirector, "SqlPlacementDirector"),
)


def _live_eligibility(service: FakeHqService) -> dict:
    """``GET /eligibility/{frame}`` as v0.1.0 serves it: the grant, plus the liveness the
    service holds for the frame's principal (what its renew and evidence posts recorded) and
    the release verdict against the grant's pin."""
    body = _eligibility(service)
    record = service.principals[body["principal"]]
    if record.renewed_at is not None:
        body["liveness"] = {
            "session": {
                "verdict": "fresh",
                "renewed_at": record.renewed_at,
                "age_s": NOW - record.renewed_at,
                "ttl_s": 300,
            },
            "evidence": {
                "verdict": "fresh" if record.evidence else "absent",
                "status": "conformant",
                "measured_at": record.measured_at,
                "age_s": NOW - (record.measured_at or NOW),
                "ttl_s": 900,
                "digest": next(iter(record.evidence), None),
            },
        }
    release = service.authority_state["frames"][FRAME]["active"]["desired"]["release"]
    body["release"] = {**release, "admits": True, "basis": "pin", "row": None}
    return body


@pytest.fixture
def service():
    fake = FakeHqService(clock=lambda: NOW)
    fake.authority_state = {"revision": 1, "expires_at": 4_000_000_000.0, "frames": {}}
    fake.reads["/authority"] = lambda: _authority(fake)
    fake.reads["/config"] = lambda: _config(fake)
    fake.reads[f"/eligibility/{FRAME}"] = lambda: _live_eligibility(fake)
    fake.trusted = True
    fake.operator_record = {
        "revision": 2,
        "state": {
            "frames": {FRAME: {"active": {"desired": {"profile": "reviewed"}}, "candidate": None}}
        },
        "hive_policies": {},
    }
    return fake


@pytest.fixture
def no_sql(monkeypatch):
    """The three tripwires: the guard refuses, the guard and every opener are spied, and the
    raw connect paths fail. ``calls`` is everything that was reached; it must stay empty."""
    calls: list[str] = []
    monkeypatch.setenv("BH_HQ_SQL_GUARD", "refuse")
    hq_sql_guard.reset()

    real_guard = hq_sql_guard.guard_sql_open

    def guard_spy(opener, **kwargs):
        calls.append(f"guard_sql_open({opener})")
        return real_guard(opener, **kwargs)

    monkeypatch.setattr(hq_sql_guard, "guard_sql_open", guard_spy)

    def opener(name):
        def constructed(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"route=api constructed {name}")

        return constructed

    for cls, name in OPENERS:
        monkeypatch.setattr(cls, "__init__", opener(name))
    monkeypatch.setattr(hq_sql_transport, "connect", opener("sql connect"))
    monkeypatch.setattr(hq_sql_transport, "_strict_driver", opener("sql driver"))
    monkeypatch.setattr(hq_sql_transport.FnoxBroker, "__init__", opener("FnoxBroker"))
    for name in ("hq_sql_config", "hq_sql_runtime", "hq_sql_operator", "hq_sql_receiver"):
        module = __import__(f"beadhive.{name}", fromlist=["connect"])
        monkeypatch.setattr(module, "connect", opener(f"{name}.connect"))
    monkeypatch.setattr(report, "_session_opener", opener("session opener"))
    return calls


@pytest.fixture
def api_frame(tmp_path, monkeypatch, service, no_sql, frame_key):  # noqa: F811
    """This host: ``hq.route: api``, NO ``hq.sql``, ``host-1`` with its frame key, a trusted
    fleet, the port wired to the fake, and every clock frozen."""
    home = tmp_path / "bh-home"
    (home / "hq").mkdir(parents=True)
    (home / "state").mkdir()
    (home / "config.yaml").write_text(HOST_CONFIG)
    (home / "host.yaml").write_text(
        f"host_id: {HOST_ID}\nlabel: frame\nsigning_key: {frame_key[0]}\n"
    )
    (home / "state" / "hq-fleet-authority-mode.json").write_text(json.dumps({"mode": "trusted"}))
    monkeypatch.setenv("BH_HOME", str(home))
    for name in (
        "BH_CONFIG",
        "WS_CONFIG",
        "BH_HQ",
        "WS_HQ",
        "BH_HQ_ROUTE",
        "BH_HQ_OPERATOR_SETTINGS",
        "BH_HQ_AUTHORITY_ENFORCE",
        "BH_HQ_SESSION_VIA_PORT",
        "BH_HQ_SESSION_DUAL_WRITE",
        "BH_FRAME_HEARTBEAT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "trusted")
    # The committed fleet manages one hive (``bh``) for the lease claim.
    monkeypatch.setattr(
        join,
        "FLEET_YAML",
        join.FLEET_YAML.replace(
            "managed_repos: []",
            "managed_repos:\n  - {provider: github, org: acme, repo: bh, prefix: bh}",
        ),
    )
    monkeypatch.setattr(hq_authority_enforce, "enforced", lambda: False)

    def port(host_config):
        return adapter_from_config(
            host_config, verifier=_passthrough, transport=service.transport(), clock=lambda: NOW
        )

    monkeypatch.setattr(hq_control_plane, "_api_port", port)
    monkeypatch.setattr(heartbeat_sender, "_select_port", lambda: port(config.load_host()))
    frozen = types.SimpleNamespace(monotonic=lambda: MONO)
    monkeypatch.setattr(hq_service_adapter, "time", frozen)
    monkeypatch.setattr(hq_frame_grant, "time", frozen)
    monkeypatch.setattr(report, "_port_deadline", lambda plane: MONO + 30)
    monkeypatch.setattr(report, "installed_release", lambda: dict(_manifest().release.model_dump()))
    monkeypatch.setattr(report, "datetime", _Frozen)
    monkeypatch.setattr(heartbeat_sender, "status", lambda: {})

    def refresh():
        def measure():
            return [
                {"id": name, "status": "pass"} for name in heartbeat_conformance.CACHED_CHECK_IDS
            ]

        return heartbeat_conformance.refresh(
            measure, path=report.conformance_cache_path(), clock=lambda: NOW
        )

    monkeypatch.setattr(report, "refresh_conformance", refresh)
    refresh()
    report._ACCEPTED.clear()
    yield home
    report._ACCEPTED.clear()


def _run(*args: str):
    from beadhive.cli import app

    return CliRunner().invoke(app, list(args))


def test_a_frame_with_no_hq_sql_runs_its_whole_life_through_hq_service(
    api_frame, service, no_sql, capsys, monkeypatch
):
    # 1. host init: the manifest, published through the port (PUT /manifests).
    init = _run("host", "init", "--role", "executor")
    assert init.exit_code == 0, init.output
    assert _calls(service) == [("PUT", f"/manifests/{HOST_ID}")]
    assert service.manifest_bodies[HOST_ID]["role"] == "executor"

    # 2. the join: the manifest again (now naming the frame), the incarnation, open admission.
    roster.save(config.hq_dir(), _manifest())
    joined = _join()
    assert joined.exit_code == 0, joined.output
    result = _result(joined.output)
    assert (result["registered"], result["admitted"], result["route"]) == (True, True, "api")
    assert [call for call in _calls(service)[1:]] == [
        ("PUT", f"/manifests/{HOST_ID}"),
        ("POST", "/incarnations"),
        ("POST", f"/admissions/{FRAME}"),
    ]
    active = service.authority_state["frames"][FRAME]["active"]
    assert active["state"] == "active" and service.principals[result["principal"]].grant == "active"

    # 3. rebind and release-policy (trusted fleet: the plan needs no signature).
    rebind = _run(
        "hq", "authority", "rebind", "--frame", FRAME, "--confirm",
        "--expected-revision", service.head,
    )  # fmt: skip
    assert rebind.exit_code == 0, rebind.output
    assert json.loads(rebind.stdout)["revision"] == service.head
    base = ["hq", "authority", "release-policy"]
    plan = _run(*base, "plan", "--frame", FRAME, "--spec", "^0.40")
    assert plan.exit_code == 0, plan.output
    digest = json.loads(plan.stdout)["plan_sha256"]
    apply = _run(
        *base, "apply", "--frame", FRAME, "--spec", "^0.40", "--plan-sha256", digest, "--confirm"
    )
    assert apply.exit_code == 0, apply.output
    paths = [path for method, path in _calls(service) if method == "POST"]
    assert paths[2:] == [
        "/authority/rebind/plan",
        "/authority/rebind/apply",
        f"/authority/release-policy/{FRAME}/plan",  # the plan verb
        f"/authority/release-policy/{FRAME}/plan",  # apply re-plans and checks the digest
        f"/authority/release-policy/{FRAME}/plan",
        f"/authority/release-policy/{FRAME}/apply",
    ]

    # 4. renew straight after open admission (ACTIVE, no intermediate step), then the
    # conformance evidence and the beat verb.
    capsys.readouterr()
    assert heartbeat_sender.main(["renew"]) == 0
    assert json.loads(capsys.readouterr().out)["renewed"]
    report._ACCEPTED.clear()
    assert heartbeat_sender.main(["conformance"]) == 0
    assert heartbeat_sender.main(["beat"]) == 0
    assert "beat skipped: hq.route api beats through session renew" in capsys.readouterr().err
    assert [entry for entry in service.seen if entry[0] in ("renew", "evidence")] == [
        ("renew", "accepted"),
        ("renew", "accepted"),
        ("evidence", "accepted"),
    ]
    record = service.principals[result["principal"]]
    assert record.renewed_at == NOW and record.observation is not None and record.evidence

    # 5. a hive-lease claim and release.
    def with_policy() -> dict:
        served = _authority(service)
        served["record"]["hive_policies"] = {"bh": {"config_revision": "cfg-1", "valid_until": 4e9}}
        return served

    service.reads["/authority"] = with_policy
    service.reads["/config"] = lambda: {**_config(service), "authority": with_policy()}
    hq_dir = config.hq_dir()
    adopted = host_lease.adopt("origin", "bh", host_id=HOST_ID, label="frame", cwd=hq_dir)
    assert adopted.lease.epoch == 1 and adopted.previous is None
    released = host_lease.release("origin", "bh", host_id=HOST_ID, cwd=hq_dir)
    assert released.lease.is_tombstone
    leases = [
        body["request"]["operation"]
        for method, path, _q, body in service.requests
        if method == "POST" and path == "/hive-leases/bh"
    ]
    assert leases == ["adopt", "release"]

    # No HQ SQL opener, guard check, connection or secret broker was reached, and the
    # route never fell back to one.
    assert no_sql == []


def test_a_candidate_slot_does_not_renew_on_api(api_frame, service, no_sql, capsys):
    """KNOWN 0.40.0 GAP (bh-65s71, carried by bh-jwymz): api renews ACTIVE grants only. The
    frame holds a CANDIDATE slot and no active grant; renew is refused as ``no_current_grant``
    and posts nothing. Promotion by renewing a candidate is not available on api in 0.40.0."""
    frames = service.authority_state["frames"]
    frames[FRAME] = {"active": None, "candidate": {"state": "candidate"}, "epoch_floor": 0}
    service.reads[f"/eligibility/{FRAME}"] = lambda: {
        **_eligibility_none(service),
        "active": None,
        "principal": None,
        "epoch": None,
        "route": None,
    }
    assert heartbeat_sender.main(["renew"]) == 1
    assert "session renewal refused" in capsys.readouterr().err
    assert [entry for entry in service.seen if entry[0] in ("renew", "evidence")] == []
    assert [p for m, p, _q, _b in service.requests if m == "POST"] == []
    assert no_sql == []


def _eligibility_none(service: FakeHqService) -> dict:
    return {
        "head": service.head,
        "config_head": service.config_head,
        "frame_id": FRAME,
        "liveness": None,
        "release": None,
    }


def test_route_api_never_falls_back_to_sql_for_a_method_with_no_route(api_frame, no_sql):
    adapter = hq_control_plane._api_port(config.load_host())
    with pytest.raises(HqUnavailable, match="no hq-service route for preflight"):
        adapter.preflight(route="x", deadline=MONO + 30)
    with pytest.raises(HqUnavailable, match="describe_incarnation"):
        adapter.describe_incarnation("p", 0, FRAME, target_route=None, deadline=MONO + 30)
    assert no_sql == []


def test_the_guard_refuses_an_sql_opener_on_an_api_host(api_frame, no_sql):
    """Refuse mode is live in this harness: a guarded opener raises before it connects."""
    with pytest.raises(hq_sql_guard.SqlPlaneRetired, match="hq.route: api"):
        hq_sql_guard.guard_sql_open("SqlControlPlane")
    assert no_sql == ["guard_sql_open(SqlControlPlane)"]  # the spy sees it; the e2e sees none
