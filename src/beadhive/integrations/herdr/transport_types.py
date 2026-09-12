"""Shared transport result types kept dependency-free to avoid import cycles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

T = TypeVar("T")


class FailureCode(StrEnum):
    UNAVAILABLE = "unavailable"
    REFUSED = "refused"
    CONFLICT = "conflict"
    STALE = "stale"
    MALFORMED = "malformed"
    RETRYABLE = "retryable"


@dataclass(frozen=True, slots=True)
class HerdrFailure:
    code: FailureCode
    message: str
    detail: str = ""
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class HerdrResult(Generic[T]):
    value: T | None = None
    failure: HerdrFailure | None = None

    @classmethod
    def ok(cls, value: T) -> HerdrResult[T]:
        return cls(value=value)

    @classmethod
    def fail(
        cls,
        code: FailureCode,
        message: str,
        *,
        detail: str = "",
        retryable: bool | None = None,
    ) -> HerdrResult[T]:
        return cls(
            failure=HerdrFailure(
                code,
                message,
                detail,
                code == FailureCode.RETRYABLE if retryable is None else retryable,
            )
        )

    @property
    def is_ok(self) -> bool:
        return self.failure is None

    def unwrap(self) -> T:
        if self.failure is not None:
            raise RuntimeError(self.failure.message)
        return self.value  # type: ignore[return-value]
