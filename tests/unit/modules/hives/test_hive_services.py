"""Pure hive application tests with no filesystem, Dolt, plugin, or transport setup."""

from __future__ import annotations

from beadhive.modules.hives import (
    DiscoverHivesResult,
    HiveIdentity,
    HiveIdentityPage,
    HiveLifecycleService,
    HiveListRequest,
    HiveListResult,
    HiveStatusRequest,
    HiveStatusResult,
    OnboardHiveRequest,
    OnboardHiveResult,
    ReadinessCheck,
    ReadinessRequest,
    ReadinessResult,
    RegisterHiveRequest,
    RegisterHiveResult,
    RetireHiveRequest,
    RetireHiveResult,
    RetireScope,
)


class MemoryRegistry:
    def __init__(self) -> None:
        self.registered: list[RegisterHiveRequest] = []

    def register(self, request):
        self.registered.append(request)
        return RegisterHiveResult(request.identity, request.prefix or "acme-api", request.kind)

    def discover(self):
        return DiscoverHivesResult(
            ("github/acme/new",),
            ("github/acme/api",),
        )

    def list(self, request):
        discovery = self.discover()
        return HiveListResult(
            discovery,
            HiveIdentityPage(
                "sha256:test",
                1,
                "fresh",
                1,
                "complete",
                None,
                (),
                0,
                request.limit,
                False,
                None,
            ),
        )

    def status(self, request):
        return HiveStatusResult((request.hive_id,), (), (), ())


class MemoryWorkspace:
    def target_for(self, identity):
        return f"/workspace/{identity.canonical_id}"


class MemoryProbe:
    def readiness(self, request):
        return ReadinessResult(
            True,
            "acme-api",
            (ReadinessCheck("workspace", True, "ok", request.cwd or "."),),
        )


class MemoryLifecycle:
    def __init__(self) -> None:
        self.onboard_calls = []
        self.retire_calls = []

    def onboard(self, request, *, target):
        self.onboard_calls.append((request, target))
        return OnboardHiveResult(
            request.identity,
            target,
            cloned=True,
            registered=True,
            prefix="acme-api",
            synced=True,
        )

    def retire(self, request):
        self.retire_calls.append(request)
        return RetireHiveResult(
            request.hive_id,
            request.scope,
            "/workspace/github/acme/api",
            request.dry_run,
            unregistered=request.scope is RetireScope.FLEET and not request.dry_run,
        )


def _service():
    registry = MemoryRegistry()
    lifecycle = MemoryLifecycle()
    return (
        HiveLifecycleService(registry, MemoryWorkspace(), MemoryProbe(), lifecycle),
        registry,
        lifecycle,
    )


def test_identity_is_canonical_and_service_registers_exact_identity() -> None:
    identity = HiveIdentity.parse("github/acme/api")
    service, registry, _ = _service()

    result = service.register(RegisterHiveRequest(identity, kind="org-native"))

    assert identity.canonical_id == "github/acme/api"
    assert result.identity == identity
    assert registry.registered == [RegisterHiveRequest(identity, kind="org-native")]


def test_query_use_cases_share_the_registry_port() -> None:
    service, _, _ = _service()

    assert service.discover().as_payload() == {
        "candidates": ["github/acme/new"],
        "registered": ["github/acme/api"],
    }
    listed = service.list(HiveListRequest(available=True))
    assert listed.discovery.candidates == ("github/acme/new",)
    assert listed.page is not None and listed.page.source_revision == "sha256:test"
    assert service.status(HiveStatusRequest("github/acme/api")).candidates == ("github/acme/api",)


def test_onboard_resolves_workspace_before_lifecycle_effects() -> None:
    service, _, lifecycle = _service()
    request = OnboardHiveRequest(
        HiveIdentity.parse("github/acme/api"),
        clone_url="https://example.test/acme/api.git",
        plugins=("orca",),
    )

    result = service.onboard(request)

    assert result.target == "/workspace/github/acme/api"
    assert lifecycle.onboard_calls == [(request, result.target)]


def test_readiness_and_retire_are_typed_port_use_cases() -> None:
    service, _, lifecycle = _service()

    ready = service.readiness(ReadinessRequest(verbose=True, cwd="/workspace/github/acme/api"))
    retired = service.retire(
        RetireHiveRequest("github/acme/api", scope=RetireScope.HOST, dry_run=True)
    )

    assert ready.ready
    assert ready.checks[0].detail == "/workspace/github/acme/api"
    assert retired.scope is RetireScope.HOST
    assert lifecycle.retire_calls[0].dry_run is True


def test_service_rejects_port_identity_drift_before_handoff() -> None:
    service, registry, lifecycle = _service()
    identity = HiveIdentity.parse("github/acme/api")

    registry.register = lambda _request: RegisterHiveResult(
        HiveIdentity.parse("github/acme/other"), "acme-other", "org-native"
    )
    try:
        service.register(RegisterHiveRequest(identity))
    except ValueError as exc:
        assert "changed the requested hive identity" in str(exc)
    else:
        raise AssertionError("registry identity drift was accepted")

    lifecycle.onboard = lambda request, *, target: OnboardHiveResult(
        HiveIdentity.parse("github/acme/other"),
        target,
        False,
        True,
        "acme-other",
        True,
    )
    try:
        service.onboard(OnboardHiveRequest(identity))
    except ValueError as exc:
        assert "onboard result conflicts" in str(exc)
    else:
        raise AssertionError("lifecycle identity drift was accepted")
