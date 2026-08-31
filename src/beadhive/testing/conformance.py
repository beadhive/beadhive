"""Reusable semantic conformance runners for Beadhive extension boundaries.

Conformance cases are ordinary callables rather than pytest fixtures.  Consumers can use them
from pytest, unittest, or another runner, and each case receives a fresh subject so one semantic
scenario cannot leak state into the next.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

T = TypeVar("T")
_CASE_ID = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


class ConformanceFailure(AssertionError):
    """One named semantic case rejected a subject."""

    def __init__(self, *, suite: str, case_id: str, subject: str, detail: str) -> None:
        self.suite = suite
        self.case_id = case_id
        self.subject = subject
        self.detail = detail
        super().__init__(
            f"{suite} conformance failed for {subject!r} [semantic-case={case_id}]: {detail}"
        )


@dataclass(frozen=True)
class ConformanceCase(Generic[T]):
    """A stable semantic scenario applied to one freshly constructed subject."""

    case_id: str
    check: Callable[[T], None]
    description: str = ""

    def __post_init__(self) -> None:
        if not _CASE_ID.fullmatch(self.case_id):
            raise ValueError(
                "conformance case_id must be a stable lowercase semantic identifier; "
                f"got {self.case_id!r}"
            )
        if not callable(self.check):
            raise TypeError(f"conformance case {self.case_id!r} check must be callable")


class PortCase(ConformanceCase[T]):
    """A semantic case shared by implementations of one outbound port."""


class LifecycleCase(ConformanceCase[T]):
    """A semantic case shared by participants in one lifecycle family."""


@dataclass(frozen=True)
class PluginSubject:
    """The declaration and live capability providers tested as one plugin boundary."""

    manifest: Mapping[str, Any]
    capabilities: Mapping[str, object]


class PluginCase(ConformanceCase[PluginSubject]):
    """A plugin-specific semantic case beyond the common declaration checks."""


class _RuntimeProtocol(Protocol):
    @classmethod
    def __instancecheck__(cls, instance: object) -> bool: ...


def _subject_name(subject: object, fallback: str) -> str:
    value = getattr(subject, "name", None)
    return value if isinstance(value, str) and value else fallback


def _failure(*, suite: str, case_id: str, subject: str, exc: BaseException) -> ConformanceFailure:
    detail = str(exc).strip() or type(exc).__name__
    return ConformanceFailure(suite=suite, case_id=case_id, subject=subject, detail=detail)


def _require_cases(suite: str, cases: Sequence[ConformanceCase[Any]]) -> None:
    if not cases:
        raise ValueError(f"{suite} conformance requires at least one semantic case")


def _run_cases(
    *,
    suite: str,
    subject_factory: Callable[[], T],
    cases: Sequence[ConformanceCase[T]],
    subject: str,
) -> None:
    _require_cases(suite, cases)
    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise ValueError(f"duplicate {suite} semantic case: {case.case_id}")
        seen.add(case.case_id)
        try:
            candidate = subject_factory()
        except ConformanceFailure:
            raise
        except Exception as exc:
            raise _failure(
                suite=suite,
                case_id=f"{suite}.subject-construction",
                subject=subject,
                exc=exc,
            ) from exc
        candidate_name = _subject_name(candidate, subject)
        try:
            case.check(candidate)
        except ConformanceFailure:
            raise
        except Exception as exc:
            raise _failure(
                suite=suite,
                case_id=case.case_id,
                subject=candidate_name,
                exc=exc,
            ) from exc


def assert_port_conforms(
    subject_factory: Callable[[], T],
    cases: Sequence[PortCase[T]],
    *,
    port: _RuntimeProtocol | None = None,
    subject: str = "outbound-port implementation",
) -> None:
    """Assert structural membership, when supplied, and every semantic port case.

    ``port`` must be runtime-checkable (normally a ``@runtime_checkable Protocol``).  Semantic
    cases remain mandatory evidence: an ``isinstance`` result alone cannot prove substitutability.
    """

    _require_cases("outbound-port", cases)
    if port is not None:
        try:
            candidate = subject_factory()
        except Exception as exc:
            raise _failure(
                suite="outbound-port",
                case_id="port.subject-construction",
                subject=subject,
                exc=exc,
            ) from exc
        candidate_name = _subject_name(candidate, subject)
        try:
            conforms = isinstance(candidate, port)
        except TypeError as exc:
            raise _failure(
                suite="outbound-port",
                case_id="port.runtime-checkable",
                subject=candidate_name,
                exc=exc,
            ) from exc
        if not conforms:
            raise ConformanceFailure(
                suite="outbound-port",
                case_id="port.runtime-shape",
                subject=candidate_name,
                detail=f"does not implement {getattr(port, '__name__', port)!s}",
            )
    _run_cases(
        suite="outbound-port",
        subject_factory=subject_factory,
        cases=cases,
        subject=subject,
    )


def assert_lifecycle_conforms(
    participant_factory: Callable[[], T],
    cases: Sequence[LifecycleCase[T]],
    *,
    subject: str = "lifecycle participant",
) -> None:
    """Assert the ordered, failure, retry, or compensation semantics named by ``cases``."""

    _run_cases(
        suite="lifecycle",
        subject_factory=participant_factory,
        cases=cases,
        subject=subject,
    )


def _manifest_capabilities(manifest: Mapping[str, Any], field: str) -> tuple[str, ...]:
    raw = manifest.get(field)
    if not isinstance(raw, (list, tuple)):
        raise TypeError(f"manifest field {field!r} must be a list of capability ids")
    if any(not isinstance(value, str) or not value for value in raw):
        raise ValueError(f"manifest field {field!r} must contain non-empty string ids")
    declared = tuple(raw)
    if len(declared) != len(set(declared)):
        raise ValueError(f"manifest field {field!r} contains duplicate capability ids")
    return declared


def _fresh_plugin_factory(
    subject_factory: Callable[[], PluginSubject], subject: str
) -> Callable[[], PluginSubject]:
    seen_subjects: list[PluginSubject] = []
    seen_manifests: list[Mapping[str, Any]] = []
    seen_capability_maps: list[Mapping[str, object]] = []
    seen_providers: list[object] = []

    def create() -> PluginSubject:
        candidate = subject_factory()
        if not isinstance(candidate, PluginSubject):
            raise ConformanceFailure(
                suite="plugin",
                case_id="plugin.subject-type",
                subject=subject,
                detail=f"factory returned {type(candidate).__name__}, expected PluginSubject",
            )
        reused_subject = any(candidate is previous for previous in seen_subjects)
        reused_manifest = any(candidate.manifest is previous for previous in seen_manifests)
        reused_capability_map = any(
            candidate.capabilities is previous for previous in seen_capability_maps
        )
        reused_capabilities = sorted(
            capability
            for capability, provider in candidate.capabilities.items()
            if provider is not None and any(provider is previous for previous in seen_providers)
        )
        if reused_subject or reused_manifest or reused_capability_map or reused_capabilities:
            detail = "factory reused its PluginSubject" if reused_subject else ""
            if reused_manifest:
                detail = f"{detail}; " if detail else ""
                detail += "factory reused its manifest mapping"
            if reused_capability_map:
                detail = f"{detail}; " if detail else ""
                detail += "factory reused its capability mapping"
            if reused_capabilities:
                detail = f"{detail}; " if detail else ""
                detail += f"factory reused capability providers: {reused_capabilities}"
            raise ConformanceFailure(
                suite="plugin",
                case_id="plugin.subject-freshness",
                subject=subject,
                detail=detail,
            )
        seen_subjects.append(candidate)
        seen_manifests.append(candidate.manifest)
        seen_capability_maps.append(candidate.capabilities)
        seen_providers.extend(
            provider for provider in candidate.capabilities.values() if provider is not None
        )
        return candidate

    return create


def assert_plugin_conforms(
    subject_factory: Callable[[], PluginSubject],
    *,
    manifest_validator: Callable[[Mapping[str, Any]], None],
    cases: Sequence[PluginCase] = (),
    capability_field: str = "capabilities",
    subject: str | None = None,
) -> None:
    """Assert a plugin declaration, its provided capabilities, and extension-specific cases.

    The manifest validator is supplied by the owning plugin contract (for example a JSON Schema
    validator).  The common kit then proves that declarations are unambiguous and exactly match
    the live capability providers instead of accepting drift between metadata and behavior.
    """

    plugin_name = subject or "plugin"

    def manifest_is_valid(candidate: PluginSubject) -> None:
        manifest_validator(candidate.manifest)

    def capabilities_are_declared(candidate: PluginSubject) -> None:
        _manifest_capabilities(candidate.manifest, capability_field)

    def capabilities_match_manifest(candidate: PluginSubject) -> None:
        declared = _manifest_capabilities(candidate.manifest, capability_field)
        provided = tuple(candidate.capabilities)
        if set(declared) == set(provided):
            return
        missing = sorted(set(declared) - set(provided))
        undeclared = sorted(set(provided) - set(declared))
        raise AssertionError(f"missing providers={missing}; undeclared providers={undeclared}")

    def capabilities_are_provided(candidate: PluginSubject) -> None:
        empty = sorted(
            capability
            for capability, provider in candidate.capabilities.items()
            if provider is None
        )
        if empty:
            raise AssertionError(f"capability ids with no provider: {empty}")

    common_cases = (
        PluginCase("plugin.manifest-valid", manifest_is_valid),
        PluginCase("plugin.capabilities-declared", capabilities_are_declared),
        PluginCase("plugin.capabilities-match-manifest", capabilities_match_manifest),
        PluginCase("plugin.capabilities-provided", capabilities_are_provided),
    )
    _run_cases(
        suite="plugin",
        subject_factory=_fresh_plugin_factory(subject_factory, plugin_name),
        cases=(*common_cases, *cases),
        subject=plugin_name,
    )


def canonical_json_bytes(value: object) -> bytes:
    """Return the checked-in canonical JSON representation used by schema conformance."""

    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


@dataclass(frozen=True)
class SchemaArtifact:
    """One deterministic generated JSON artifact and its checked-in release bytes."""

    artifact_id: str
    version: int | str
    generate: Callable[[], object | str | bytes]
    checked_in: str | bytes | Path

    def __post_init__(self) -> None:
        if type(self.artifact_id) is not str:
            raise TypeError("schema artifact_id must be a string")
        if not self.artifact_id:
            raise ValueError("schema artifact_id cannot be empty")
        if type(self.version) not in (int, str):
            raise TypeError("schema version must be an integer or string")
        if self.version == "":
            raise ValueError("schema version cannot be empty")


def _artifact_bytes(value: object | str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode()
    return canonical_json_bytes(value)


def _checked_in_bytes(value: str | bytes | Path) -> bytes:
    if isinstance(value, Path):
        return value.read_bytes()
    return value if isinstance(value, bytes) else value.encode()


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant {value!r}")


def assert_schema_artifact_conforms(artifact: SchemaArtifact) -> None:
    """Assert repeatability, canonical JSON, identity/version, and checked-in drift."""

    subject = artifact.artifact_id
    try:
        first = _artifact_bytes(artifact.generate())
        second = _artifact_bytes(artifact.generate())
    except Exception as exc:
        raise _failure(
            suite="schema-artifact",
            case_id="schema.generation",
            subject=subject,
            exc=exc,
        ) from exc
    if first != second:
        raise ConformanceFailure(
            suite="schema-artifact",
            case_id="schema.deterministic-generation",
            subject=subject,
            detail="two generations produced different bytes",
        )
    try:
        document = json.loads(first, parse_constant=_reject_nonstandard_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _failure(
            suite="schema-artifact",
            case_id="schema.valid-json",
            subject=subject,
            exc=exc,
        ) from exc
    if first != canonical_json_bytes(document):
        raise ConformanceFailure(
            suite="schema-artifact",
            case_id="schema.canonical-json",
            subject=subject,
            detail="generated bytes are not sorted, compact UTF-8 JSON with one trailing newline",
        )
    if not isinstance(document, dict):
        raise ConformanceFailure(
            suite="schema-artifact",
            case_id="schema.document-object",
            subject=subject,
            detail="released schema artifact must be a JSON object",
        )
    document_id = document.get("$id", document.get("id"))
    if type(document_id) is not str or document_id != artifact.artifact_id:
        raise ConformanceFailure(
            suite="schema-artifact",
            case_id="schema.stable-identity",
            subject=subject,
            detail=f"document identity is {document_id!r}",
        )
    document_version = document.get("version", document.get("schemaVersion"))
    if type(document_version) is not type(artifact.version) or document_version != artifact.version:
        raise ConformanceFailure(
            suite="schema-artifact",
            case_id="schema.explicit-version",
            subject=subject,
            detail=f"document version is {document_version!r}; expected {artifact.version!r}",
        )
    try:
        checked_in = _checked_in_bytes(artifact.checked_in)
    except OSError as exc:
        raise _failure(
            suite="schema-artifact",
            case_id="schema.checked-in-readable",
            subject=subject,
            exc=exc,
        ) from exc
    if first != checked_in:
        raise ConformanceFailure(
            suite="schema-artifact",
            case_id="schema.checked-in-drift",
            subject=subject,
            detail="generated bytes differ from the checked-in artifact",
        )
