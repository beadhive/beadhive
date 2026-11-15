"""The SQL adapter of the HQ lifecycle port (bh-v3dyp): registration, error mapping and the
thin delegation to today's openers. The openers' own behaviour stays covered by their suites."""

from __future__ import annotations

import pytest

from beadhive import hq_port, hq_sql_adapter
from beadhive.hq_port import (
    FrameAnchor,
    HqDeadline,
    HqOutcomeUnknown,
    HqRefused,
    HqUnavailable,
)
from beadhive.hq_service_adapter import select_hq_port


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(hq_port.ROUTE_ENV, raising=False)
    yield
    hq_port.register_sql_adapter(hq_sql_adapter._default_factory)


def test_sql_route_resolves_to_the_registered_adapter(monkeypatch):
    monkeypatch.setattr("beadhive.hq_control_plane.host_sql_settings", lambda: {"enabled": False})
    monkeypatch.setattr(
        "beadhive.hq_control_plane.SqlControlPlane", lambda settings, **kw: ("plane", settings)
    )
    hq_port.register_sql_adapter(hq_sql_adapter._default_factory)
    port = select_hq_port({"hq": {}})
    assert isinstance(port, hq_sql_adapter.SqlHqAdapter)
    assert port._plane == ("plane", {"enabled": False})


def test_choosing_sql_loads_and_registers_the_adapter_on_demand(monkeypatch):
    import sys

    import beadhive
    from beadhive import hq_service_adapter

    monkeypatch.delitem(sys.modules, "beadhive.hq_sql_adapter")  # restored by monkeypatch
    monkeypatch.delattr(beadhive, "hq_sql_adapter")
    hq_port.register_sql_adapter(None)
    hq_service_adapter._load_sql_adapter()
    assert hq_port.sql_adapter_factory() is not None


@pytest.mark.parametrize(
    ("make", "port_error"),
    [
        ("SqlRuntimeDeadline", HqDeadline),
        ("SqlRuntimeUnavailable", HqUnavailable),
        ("SqlConfigUnavailable", HqUnavailable),
        ("SqlRuntimeFloorRefusal", HqRefused),
        ("SqlRuntimeCapabilityMissing", HqRefused),
        ("SqlRuntimeError", HqRefused),
    ],
)
def test_sql_errors_map_onto_the_port_taxonomy(make, port_error):
    from beadhive import hq_sql_config, hq_sql_runtime

    cls = getattr(hq_sql_runtime, make, None) or getattr(hq_sql_config, make)
    error = hq_sql_adapter._classify(cls("boom"))
    assert isinstance(error, port_error)
    assert "boom" in str(error)


def test_unknown_outcomes_are_not_refusals():
    from beadhive.hq_sql_config import PublicationUnknown
    from beadhive.hq_sql_operator import AuthorityPublicationUnknown

    assert isinstance(hq_sql_adapter._classify(AuthorityPublicationUnknown("r1")), HqOutcomeUnknown)
    assert isinstance(hq_sql_adapter._classify(PublicationUnknown("p", "r1")), HqOutcomeUnknown)
    assert hq_sql_adapter._classify(KeyError("x")) is None


def test_mapped_translates_chained_and_leaves_others_alone():
    from beadhive.hq_sql_runtime import SqlRuntimeUnavailable

    class Fake:
        @hq_sql_adapter._mapped
        def unavailable(self):
            raise SqlRuntimeUnavailable("down")

        @hq_sql_adapter._mapped
        def other(self):
            raise KeyError("x")

    with pytest.raises(HqUnavailable) as caught:
        Fake().unavailable()
    assert isinstance(caught.value.__cause__, SqlRuntimeUnavailable)
    with pytest.raises(KeyError):
        Fake().other()


def test_the_adapter_satisfies_every_port_method():
    for protocol in (
        hq_port.HqLiveness,
        hq_port.HqAdmission,
        hq_port.HqReads,
        hq_port.HqOperator,
    ):
        names = [n for n in vars(protocol) if not n.startswith("_")]
        assert names
        for name in names:
            assert callable(getattr(hq_sql_adapter.SqlHqAdapter, name)), name


class _Operator:
    def __init__(self):
        self.calls = []

    def publish(self, state, **kwargs):
        self.calls.append((state, kwargs))
        return "head-2"


def _adapter(operator):
    adapter = object.__new__(hq_sql_adapter.SqlHqAdapter)
    adapter._operator = lambda: operator
    adapter._pending = {}
    adapter._provisioning_password = ""
    return adapter


def test_operator_publication_passes_the_signer_not_a_key():
    operator = _Operator()
    signer = lambda record: "sig"  # noqa: E731
    adapter = _adapter(operator)
    assert adapter.rebind({"revision": 2}, signer=signer, expected_revision="h1", deadline=1.0) == (
        "head-2"
    )
    state, kwargs = operator.calls[0]
    assert kwargs["authority_signer"] is signer
    assert kwargs["expected_revision"] == "h1"
    assert "operator_key" not in kwargs


def test_register_incarnation_needs_the_sql_only_password():
    with pytest.raises(HqRefused) as caught:
        _adapter(_Operator()).register_incarnation(
            ("p", "f", "h", "i", 1, "fp"), expected_revision="h1", frame_address="a", deadline=1.0
        )
    assert caught.value.code == "provisioning_password_required"


def test_read_history_is_not_a_sql_route():
    with pytest.raises(HqUnavailable):
        _adapter(_Operator()).read_history(since="x", deadline=1.0)


def test_read_result_of_a_foreign_submission_is_refused():
    submission = hq_port.Submission("r", "sha256:d", "pending")
    with pytest.raises(HqRefused):
        _adapter(_Operator()).read_result(submission, deadline=1.0)
    done = hq_port.Submission("r", "sha256:d", "accepted")
    assert _adapter(_Operator()).read_result(done, deadline=1.0) is done


def test_frame_anchor_is_a_value_object():
    assert FrameAnchor("p", "f", 1).head is None
