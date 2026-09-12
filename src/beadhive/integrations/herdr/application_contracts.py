"""Command-specific typed application contracts for the Herdr presentation adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ApplicationNotice:
    """One semantic notice awaiting presentation by an outer adapter."""

    detail: str
    severity: str = "info"
    newline: bool = True


@dataclass(frozen=True, slots=True)
class ApplicationError:
    """Framework-neutral refusal or invalid-input outcome."""

    code: str
    detail: str
    exit_code: int = 0
    parameter: str | None = None
    cause: BaseException | None = None


@dataclass(frozen=True, slots=True)
class HerdrCommandResult:
    """Presentation-neutral semantic outcome produced by one application use case."""

    notices: tuple[ApplicationNotice, ...] = ()
    payload: Mapping[str, object] | None = None
    error: ApplicationError | None = None


@dataclass(frozen=True, slots=True)
class LaunchRequest:
    bead: str | None
    hive: str
    kind: str | None
    session: str | None
    actor: str
    adopt_expired: bool
    direction: str
    focus: bool
    as_json: bool
    profile_json: str | None
    recover_after_pane: str | None
    session_checkout: Path | None


@dataclass(frozen=True, slots=True)
class AddRequest:
    local: Path | None
    managed_ref: str
    consent: bool
    dry_run: bool
    as_json: bool
    operation_id: str


@dataclass(frozen=True, slots=True)
class StatusRequest:
    session: str | None
    as_json: bool
    operation_id: str


@dataclass(frozen=True, slots=True)
class PsRequest:
    session: str | None
    as_json: bool
    operation_id: str


@dataclass(frozen=True, slots=True)
class IntegrateRequest:
    kind: str


@dataclass(frozen=True, slots=True)
class AttachRequest:
    target: str
    session: str | None
    as_json: bool
    operation_id: str


@dataclass(frozen=True, slots=True)
class ReapRequest:
    target: str
    session: str | None
    pane: str
    as_json: bool
    operation_id: str
    generation: int | None
    launch_spec_digest: str


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    hive: str
    bead: str
    kind: str
    session: str | None
    as_json: bool
    operation_id: str


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    target: str
    prompt: str | None
    from_stdin: bool
    prompt_file: str
    session: str | None
    as_json: bool
    operation_id: str


@dataclass(frozen=True, slots=True)
class WatchRequest:
    target: str
    session: str | None
    timeout: float | None
    as_json: bool
    operation_id: str


@dataclass(frozen=True, slots=True)
class LaunchResult(HerdrCommandResult):
    pass


@dataclass(frozen=True, slots=True)
class AddResult(HerdrCommandResult):
    pass


@dataclass(frozen=True, slots=True)
class StatusResult(HerdrCommandResult):
    pass


@dataclass(frozen=True, slots=True)
class PsResult(HerdrCommandResult):
    pass


@dataclass(frozen=True, slots=True)
class IntegrateResult(HerdrCommandResult):
    pass


@dataclass(frozen=True, slots=True)
class AttachResult(HerdrCommandResult):
    pass


@dataclass(frozen=True, slots=True)
class ReapResult(HerdrCommandResult):
    pass


@dataclass(frozen=True, slots=True)
class SpawnResult(HerdrCommandResult):
    pass


@dataclass(frozen=True, slots=True)
class DispatchResult(HerdrCommandResult):
    pass


@dataclass(frozen=True, slots=True)
class WatchResult(HerdrCommandResult):
    pass


class LaunchUseCase(Protocol):
    def execute(self, request: LaunchRequest, /) -> LaunchResult: ...


class AddUseCase(Protocol):
    def execute(self, request: AddRequest, /) -> AddResult: ...


class StatusUseCase(Protocol):
    def execute(self, request: StatusRequest, /) -> StatusResult: ...


class PsUseCase(Protocol):
    def execute(self, request: PsRequest, /) -> PsResult: ...


class IntegrateUseCase(Protocol):
    def execute(self, request: IntegrateRequest, /) -> IntegrateResult: ...


class AttachUseCase(Protocol):
    def execute(self, request: AttachRequest, /) -> AttachResult: ...


class ReapUseCase(Protocol):
    def execute(self, request: ReapRequest, /) -> ReapResult: ...


class SpawnUseCase(Protocol):
    def execute(self, request: SpawnRequest, /) -> SpawnResult: ...


class DispatchUseCase(Protocol):
    def execute(self, request: DispatchRequest, /) -> DispatchResult: ...


class WatchUseCase(Protocol):
    def execute(self, request: WatchRequest, /) -> WatchResult: ...
