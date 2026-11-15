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


def test_register_incarnation_calls_login_secret_once_inside_provisioning():
    calls = []

    class Op:
        def provision_release_route(self, route, **kwargs):
            calls.append(("provision", kwargs["password"]))
            return "head-3"

    def secret():
        calls.append("secret")
        return "pw"

    adapter = _adapter(Op())
    assert (
        adapter.register_incarnation(
            ("p", "f", "h", "i", 1, "fp"),
            expected_revision="h1",
            frame_address="a",
            login_secret=secret,
            deadline=1.0,
        )
        == "head-3"
    )
    assert calls == ["secret", ("provision", "pw")]


def test_register_incarnation_needs_the_sql_only_password():
    with pytest.raises(HqRefused) as caught:
        _adapter(_Operator()).register_incarnation(
            ("p", "f", "h", "i", 1, "fp"), expected_revision="h1", frame_address="a", deadline=1.0
        )
    assert caught.value.code == "provisioning_password_required"


def test_read_history_is_not_a_public_port_method():
    from beadhive.hq_service_adapter import HqServiceAdapter

    assert "read_history" not in vars(hq_port.HqReads)
    assert not hasattr(hq_sql_adapter.SqlHqAdapter, "read_history")
    assert not hasattr(HqServiceAdapter, "read_history")
    assert callable(HqServiceAdapter._read_history)


def test_read_result_of_a_foreign_submission_is_refused():
    submission = hq_port.Submission("r", "sha256:d", "pending")
    with pytest.raises(HqRefused):
        _adapter(_Operator()).read_result(submission, deadline=1.0)
    done = hq_port.Submission("r", "sha256:d", "accepted")
    assert _adapter(_Operator()).read_result(done, deadline=1.0) is done


def test_frame_anchor_is_a_value_object():
    assert FrameAnchor("p", "f", 1).head is None


def test_withdraw_manifest_missing_is_a_stable_refusal():
    from types import SimpleNamespace

    store = SimpleNamespace(
        load_snapshot=lambda deadline: SimpleNamespace(commit_revision="r1", documents=[]),
        publish_snapshot=lambda *a, **k: pytest.fail("must not publish"),
    )
    adapter = _adapter(None)
    adapter._store = lambda: store
    with pytest.raises(HqRefused) as raised:
        adapter.withdraw_manifest("h1", expected_revision="r1", deadline=1.0)
    assert raised.value.code == "manifest_not_found"


# ValueError families defined in hq_sql_* that the adapter cannot surface: none today. Add a
# class here only with a reason it is unreachable from the adapter's openers.
_UNREACHABLE: frozenset[str] = frozenset()


def test_every_hq_sql_value_error_is_mapped_or_listed_unreachable():
    import importlib
    import inspect
    import pkgutil

    import beadhive

    classes = {}
    for info in pkgutil.iter_modules(beadhive.__path__):
        if not info.name.startswith("hq_sql_"):
            continue
        module = importlib.import_module(f"beadhive.{info.name}")
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == module.__name__ and issubclass(cls, ValueError):
                classes[f"{module.__name__}.{name}"] = cls
    assert classes, "enumeration found nothing"
    from beadhive.hq_authority_enforce import AuthorityEnforcementError

    classes["hq_authority_enforce.AuthorityEnforcementError"] = AuthorityEnforcementError
    unmapped = []
    for qualified, cls in classes.items():
        if qualified in _UNREACHABLE:
            continue
        error = cls.__new__(cls)
        Exception.__init__(error, "boom")
        if hq_sql_adapter._classify(error) is None:
            unmapped.append(qualified)
    assert not unmapped, f"unmapped by SqlHqAdapter._classify: {unmapped}"


@pytest.mark.parametrize(
    ("path", "name", "kind"),
    [
        ("hq_sql_receiver", "ReceiverUnknown", HqOutcomeUnknown),
        ("hq_sql_placement", "PlacementUnknown", HqOutcomeUnknown),
        ("hq_sql_receiver", "ReceiverError", HqRefused),
        ("hq_sql_placement", "PlacementError", HqRefused),
        ("hq_sql_placement", "PlacementLost", HqRefused),
        ("hq_sql_capabilities", "CapabilityProbeError", HqRefused),
        ("hq_sql_runtime_schema", "RuntimeSchemaError", HqRefused),
        ("hq_sql_signatures", "SqlSignatureError", HqRefused),
        ("hq_authority_enforce", "AuthorityEnforcementError", HqRefused),
    ],
)
def test_unknown_classes_map_to_outcome_unknown_and_errors_to_refusals(path, name, kind):
    import importlib

    cls = getattr(importlib.import_module(f"beadhive.{path}"), name)
    error = cls.__new__(cls)
    Exception.__init__(error, "boom")
    assert type(hq_sql_adapter._classify(error)) is kind


class _Runtime:
    def __init__(self):
        self.reads = []

    def publish_inbox(self, kind, envelope, *, binding, expected_head, request_id, deadline):
        return request_id or "req-1", "sha256:" + "ab" * 32

    def read_public_result(self, request_id, **kwargs):
        self.reads.append((request_id, kwargs))
        return "accepted", "rev-9"


def _runtime_adapter(runtime):
    adapter = _adapter(None)
    adapter._runtime = lambda: runtime
    return adapter


def test_read_result_works_from_a_fresh_adapter_instance():
    runtime = _Runtime()
    anchor = FrameAnchor("p", "f", 3, holder="h", instance="i", carrier="inbox", head="h0")
    submitted = _runtime_adapter(runtime).submit_heartbeat(
        anchor, {"audience": "aud"}, request_id="req-1", deadline=1.0
    )
    assert (submitted.audience, submitted.expected_head) == ("aud", "h0")

    fresh = _runtime_adapter(runtime)  # no state shared with the submitting instance
    result = fresh.read_result(submitted, deadline=1.0)
    assert (result.outcome, result.revision) == ("accepted", "rev-9")
    request_id, kwargs = runtime.reads[0]
    assert request_id == "req-1"
    assert kwargs["audience"] == "aud"
    assert kwargs["expected_revision"] == "h0"
    assert kwargs["request_sha256"] == "ab" * 32
    principal = kwargs["principal"]
    assert (principal.principal, principal.frame_id, principal.epoch) == ("p", "f", 3)


class _Store:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def publish_snapshot(self, documents, **kwargs):
        self.calls.append(("publish", kwargs))
        if self.error:
            raise self.error
        return "published"

    def recover_publication(self, publication_id, **kwargs):
        self.calls.append(("recover", kwargs))
        if self.error:
            raise self.error
        return "witness"


def test_config_writes_pass_the_deadline_to_the_store():
    store = _Store()
    adapter = _adapter(None)
    adapter._store = lambda: store
    assert adapter.publish_config((), expected_revision="r", deadline=7.5) == "published"
    assert adapter.recover_publication("id", expected_revision="r", deadline=7.5) == "witness"
    assert [kw["deadline"] for _, kw in store.calls] == [7.5, 7.5]


def test_a_spent_deadline_maps_to_hq_deadline_and_an_unknown_write_to_outcome_unknown():
    from beadhive.hq_sql_config import PublicationUnknown, SqlConfigDeadline

    adapter = _adapter(None)
    adapter._store = lambda: _Store(SqlConfigDeadline("spent"))
    with pytest.raises(HqDeadline):
        adapter.publish_config((), expected_revision="r", deadline=1.0)
    adapter._store = lambda: _Store(PublicationUnknown("id", "r"))
    with pytest.raises(HqOutcomeUnknown):
        adapter.publish_config((), expected_revision="r", deadline=1.0)


def _config_store(monkeypatch, now):
    from beadhive import hq_sql_config

    store = object.__new__(hq_sql_config.SqlFleetConfigRevisionStore)
    store._history = object()
    monkeypatch.setattr(hq_sql_config, "_validate", lambda documents: None)
    monkeypatch.setattr(hq_sql_config.time, "monotonic", lambda: now[0])
    return hq_sql_config, store


def test_store_publish_with_a_spent_deadline_sends_nothing(monkeypatch):
    now = [100.0]
    module, store = _config_store(monkeypatch, now)
    sent = []
    store._publication_preflight = lambda *a, **k: sent.append("preflight") or 1
    monkeypatch.setattr(module, "publish_documents", lambda *a, **k: sent.append("write"))
    with pytest.raises(module.SqlConfigDeadline):
        store._publish_snapshot_core(("d",), expected_revision="r", deadline=100.0)
    assert sent == []


def test_store_publish_spent_after_preflight_sends_nothing(monkeypatch):
    now = [100.0]
    module, store = _config_store(monkeypatch, now)
    sent = []

    def preflight(*a, **k):
        now[0] = 200.0  # the budget runs out during the read-only checks
        return 1

    store._publication_preflight = preflight
    monkeypatch.setattr(module, "publish_documents", lambda *a, **k: sent.append("write"))
    with pytest.raises(module.SqlConfigDeadline):
        store._publish_snapshot_core(("d",), expected_revision="r", deadline=150.0)
    assert sent == []


def test_store_publish_spent_once_the_write_was_sent_is_unknown(monkeypatch):
    now = [100.0]
    module, store = _config_store(monkeypatch, now)
    store._publication_preflight = lambda *a, **k: 1

    def write(*a, **k):
        now[0] = 200.0  # the write went out, then the budget ran out before the readback
        return "committed"

    def load(revision=None, deadline=None):
        if deadline is not None and now[0] >= deadline:
            raise module.SqlConfigError("deadline")
        raise AssertionError("readback must honor the spent deadline")

    store.load_snapshot = load
    monkeypatch.setattr(module, "publish_documents", write)
    with pytest.raises(module.PublicationUnknown):
        store._publish_snapshot_core(("d",), expected_revision="r", deadline=150.0)


def test_store_recover_with_a_spent_deadline_reads_nothing(monkeypatch):
    now = [100.0]
    module, store = _config_store(monkeypatch, now)
    monkeypatch.setattr(module, "confirmed_publication", lambda *a, **k: pytest.fail("read"))
    with pytest.raises(module.SqlConfigDeadline):
        store.recover_publication(
            "12345678-1234-4234-8234-123456789abc", expected_revision="r", deadline=50.0
        )


def test_store_writes_without_a_deadline_are_unchanged(monkeypatch):
    now = [100.0]
    module, store = _config_store(monkeypatch, now)
    store._publication_preflight = lambda *a, **k: 1
    monkeypatch.setattr(module, "publish_documents", lambda *a, **k: "committed")
    store.load_snapshot = lambda revision=None, deadline=None: type(
        "S", (), {"documents": ("d",)}
    )()
    assert store._publish_snapshot_core(("d",), expected_revision="r").documents == ("d",)
