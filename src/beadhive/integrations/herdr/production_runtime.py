"""Production Herdr runtime for the provider-neutral agent lifecycle port.

The runtime owns provider effects only.  Beadhive workspace/work authority is prepared by the
application layer before this object is constructed.  Exact action callables are deliberately
injected from the compatibility composition root so the frozen command monkeypatch seams remain
usable while all effects still cross :class:`HerdrAgentSessionPort`.
"""

from __future__ import annotations

from collections.abc import Callable

from beadhive.modules.agents.domain import (
    LaunchReceiptV1,
    RecoverLaunchRequest,
    TeardownLaunchRequest,
)

from .agent_adapter import (
    HerdrAgentRuntime,
    HerdrCommitCommand,
    HerdrCommitEvidence,
    HerdrObservationEvidence,
    HerdrRecoveryEvidence,
    HerdrRuntimeResolver,
    HerdrTeardownEvidence,
)
from .transport_types import HerdrResult

CommitEffect = Callable[[HerdrCommitCommand], HerdrResult[HerdrCommitEvidence]]
ObserveEffect = Callable[[LaunchReceiptV1], HerdrResult[HerdrObservationEvidence]]
RecoverEffect = Callable[[RecoverLaunchRequest], HerdrResult[HerdrRecoveryEvidence]]
TeardownEffect = Callable[[TeardownLaunchRequest], HerdrResult[HerdrTeardownEvidence]]


class HerdrProductionRuntime(HerdrAgentRuntime):
    """One exact-session provider runtime with no Beadhive authority methods."""

    def __init__(
        self,
        *,
        commit: CommitEffect,
        observe: ObserveEffect,
        recover: RecoverEffect,
        teardown: TeardownEffect,
    ) -> None:
        self._commit = commit
        self._observe = observe
        self._recover = recover
        self._teardown = teardown

    def commit(self, command: HerdrCommitCommand) -> HerdrResult[HerdrCommitEvidence]:
        return self._commit(command)

    def observe(self, receipt: LaunchReceiptV1) -> HerdrResult[HerdrObservationEvidence]:
        return self._observe(receipt)

    def recover(self, request: RecoverLaunchRequest) -> HerdrResult[HerdrRecoveryEvidence]:
        return self._recover(request)

    def teardown(self, request: TeardownLaunchRequest) -> HerdrResult[HerdrTeardownEvidence]:
        return self._teardown(request)


class HerdrProductionResolver(HerdrRuntimeResolver):
    """Rehydrate a runtime solely from durable portable receipt evidence."""

    def __init__(self, factory: Callable[[LaunchReceiptV1], HerdrAgentRuntime]) -> None:
        self._factory = factory

    def resolve(self, receipt: LaunchReceiptV1) -> HerdrAgentRuntime:
        return self._factory(receipt)
