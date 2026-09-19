"""Build-system-agnostic impact resolution: attest keys, receipts, and the fail-closed rules.

Attested Green ADR, Amendment 1 (epic bh-1j3ei). An **attest key** is an opaque command with a
``required`` / ``optional`` policy and a per-backend **selector**. An impact resolver maps
``(base tree, head tree)`` to the keys whose inputs changed and writes an :class:`ImpactReceipt`
recording how it knows.

This module is the pure core. It knows nothing about any build system: a selector is an opaque
``{backend: value}`` pair that only the named backend reads, and a backend's raw answer
(:class:`BackendImpact`) only ever reaches a receipt through :func:`apply_fail_closed`, which
enforces the amendment's rules on it so no backend can forget them:

1. **Unowned paths invalidate everything** — a changed path (deleted ones included) the backend
   attributes to no owning unit invalidates every key.
2. **Global inputs invalidate everything** — a changed path matching one of the backend's global
   input patterns invalidates every key. Patterns only ever add invalidation.
3. **Resolver failure falls back to full** — an error, timeout, or backend-version mismatch yields
   the ``native-full`` answer (every key invalidated) with ``fallback_reason`` set.
4. **Unproven keys depend on everything** — a key with no selector for the active backend, with
   no selected units, or whose units the backend has not proven, is invalidated by any change.
5. **The git-metadata class never carries** — such a key has no selector, so rule 4 covers it.

Every rule can only invalidate. Nothing here can mark a key unaffected that a rule fired on.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

#: The default backend: every key invalidated on every change (today's all-or-nothing gate).
NATIVE_FULL = "native-full"
#: ``native-full`` has no external tool, so its version is this contract's own.
NATIVE_FULL_VERSION = "1"
#: Serialized receipt schema identity; part of the digest, so a format change changes digests.
RECEIPT_SCHEMA = "beadhive-impact-receipt/v1"

KeyPolicy = Literal["required", "optional"]
KEY_POLICIES: tuple[str, ...] = ("required", "optional")


class ImpactReason:
    """Stable per-key evidence reasons recorded on a receipt."""

    UNCHANGED = "unchanged"
    NATIVE_FULL = "native-full"
    FALLBACK = "fallback"
    UNOWNED_PATH = "unowned-path"
    GLOBAL_INPUT = "global-input"
    NO_SELECTOR = "no-selector"
    UNPROVEN = "unproven"
    AFFECTED = "affected"
    UNAFFECTED = "unaffected"


class ReceiptIntegrityError(ValueError):
    """A serialized receipt whose recorded digest does not match its content."""


def _frozen_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(sorted((value or {}).items())))


@dataclass(frozen=True)
class AttestKey:
    """One named attest key. ``cmd`` is run verbatim and never parsed; ``selectors`` maps a
    backend name to that backend's opaque selector value, which core never interprets."""

    name: str
    cmd: str
    policy: KeyPolicy = "required"
    selectors: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("attest key name must be non-empty")
        if not self.cmd:
            raise ValueError(f"attest key {self.name!r}: cmd must be non-empty")
        if self.policy not in KEY_POLICIES:
            raise ValueError(f"attest key {self.name!r}: policy must be one of {KEY_POLICIES}")
        object.__setattr__(self, "selectors", _frozen_mapping(self.selectors))

    def selector(self, backend: str) -> str | None:
        """This key's selector for ``backend``, or ``None`` (rule 4: then it is unproven)."""
        return self.selectors.get(backend)


@dataclass(frozen=True, order=True)
class ChangedPath:
    """One path that differs between the base and head trees. ``status`` is git's single-letter
    name-status (``A`` / ``M`` / ``D`` / ``T``); a deleted path must be attributed through the
    base tree (rule 1)."""

    path: str
    status: str = "M"

    @property
    def deleted(self) -> bool:
        return self.status == "D"


@dataclass(frozen=True)
class ImpactRequest:
    """What a backend is asked. ``changed`` is computed by core from git, never by the backend,
    so a backend cannot drop a path from the question it answers."""

    repo: str
    base_tree: str
    head_tree: str
    changed: tuple[ChangedPath, ...]
    keys: tuple[AttestKey, ...]
    timeout_seconds: float


@dataclass(frozen=True)
class BackendImpact:
    """A backend's raw, *unchecked* answer to the amendment's three questions.

    - ``owners``: changed path -> owning unit ids. A path absent or mapped to nothing is
      *unowned* (rule 1) — "no target matched" never means safe.
    - ``affected_units``: units transitively depending on the changed paths' owners. Owners are
      always treated as affected too, whether or not the backend lists them here.
    - ``key_units``: key name -> the units that key's selector selects.
    - ``proven_keys``: keys whose selected units are proven under enforced declared inputs.
    - ``global_inputs``: the backend's global build-input path patterns (rule 2).
    - ``backend_version``: the version that actually answered; must match the backend's own.
    """

    backend_version: str
    owners: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    affected_units: frozenset[str] = frozenset()
    key_units: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    proven_keys: frozenset[str] = frozenset()
    global_inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class KeyEvidence:
    """Why one key landed where it did, and the backend unit ids that say so."""

    reason: str
    units: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "units": list(self.units)}


@dataclass(frozen=True)
class ImpactReceipt:
    """The durable record of one resolution: what changed, which keys it invalidated, and how
    the resolver knows. ``digest`` is a stable content hash over everything except
    ``elapsed_ms`` (wall time differs run to run; the answer must not)."""

    backend: str
    backend_version: str
    base_tree: str
    head_tree: str
    changed_paths: tuple[str, ...]
    unowned_paths: tuple[str, ...]
    global_inputs_hit: tuple[str, ...]
    invalidated_keys: tuple[str, ...]
    unaffected_keys: tuple[str, ...]
    evidence: Mapping[str, KeyEvidence]
    fallback_reason: str = ""
    elapsed_ms: int = 0
    schema: str = RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "changed_paths",
            "unowned_paths",
            "global_inputs_hit",
            "invalidated_keys",
            "unaffected_keys",
        ):
            object.__setattr__(self, name, tuple(sorted(set(getattr(self, name)))))
        object.__setattr__(self, "evidence", _frozen_mapping(self.evidence))
        overlap = set(self.invalidated_keys) & set(self.unaffected_keys)
        if overlap:
            raise ValueError(f"keys both invalidated and unaffected: {sorted(overlap)}")

    @property
    def is_fallback(self) -> bool:
        return bool(self.fallback_reason)

    def is_unaffected(self, key: str) -> bool:
        """True only when this receipt can vouch that ``key``'s inputs did not change."""
        return not self.fallback_reason and key in self.unaffected_keys

    def _content(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "backend": self.backend,
            "backend_version": self.backend_version,
            "base_tree": self.base_tree,
            "head_tree": self.head_tree,
            "changed_paths": list(self.changed_paths),
            "unowned_paths": list(self.unowned_paths),
            "global_inputs_hit": list(self.global_inputs_hit),
            "invalidated_keys": list(self.invalidated_keys),
            "unaffected_keys": list(self.unaffected_keys),
            "evidence": {k: v.to_dict() for k, v in self.evidence.items()},
            "fallback_reason": self.fallback_reason,
        }

    @property
    def digest(self) -> str:
        """``sha256:<hex>`` over the canonical JSON of the receipt's content."""
        canonical = json.dumps(
            self._content(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self._content(), "elapsed_ms": self.elapsed_ms, "digest": self.digest}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ImpactReceipt:
        """Rebuild a receipt, refusing one whose recorded digest does not match its content."""
        if data.get("schema") != RECEIPT_SCHEMA:
            raise ValueError(f"unsupported impact receipt schema {data.get('schema')!r}")
        receipt = cls(
            backend=str(data["backend"]),
            backend_version=str(data["backend_version"]),
            base_tree=str(data["base_tree"]),
            head_tree=str(data["head_tree"]),
            changed_paths=tuple(data.get("changed_paths", ())),
            unowned_paths=tuple(data.get("unowned_paths", ())),
            global_inputs_hit=tuple(data.get("global_inputs_hit", ())),
            invalidated_keys=tuple(data.get("invalidated_keys", ())),
            unaffected_keys=tuple(data.get("unaffected_keys", ())),
            evidence={
                str(k): KeyEvidence(str(v["reason"]), tuple(v.get("units", ())))
                for k, v in (data.get("evidence") or {}).items()
            },
            fallback_reason=str(data.get("fallback_reason", "")),
            elapsed_ms=int(data.get("elapsed_ms", 0)),
        )
        recorded = data.get("digest")
        if recorded is not None and recorded != receipt.digest:
            raise ReceiptIntegrityError(
                f"impact receipt digest mismatch: recorded {recorded}, content {receipt.digest}"
            )
        return receipt

    @classmethod
    def from_json(cls, text: str) -> ImpactReceipt:
        return cls.from_dict(json.loads(text))


def _key_names(keys: Iterable[AttestKey]) -> tuple[str, ...]:
    names = tuple(key.name for key in keys)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate attest key names: {duplicates}")
    return names


def matches_global_input(path: str, patterns: Iterable[str]) -> bool:
    """True when ``path`` matches a global-input pattern. A slash-free pattern also matches any
    path's basename (``BUILD`` hits every BUILD file) — over-matching only adds invalidation,
    which is the safe direction for rule 2."""
    base = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if "/" not in pattern and fnmatch.fnmatchcase(base, pattern):
            return True
    return False


def invalidate_all(
    *,
    backend: str,
    backend_version: str,
    base_tree: str,
    head_tree: str,
    changed: Iterable[ChangedPath],
    keys: Iterable[AttestKey],
    reason: str,
    fallback_reason: str = "",
    unowned_paths: Iterable[str] = (),
    global_inputs_hit: Iterable[str] = (),
    elapsed_ms: int = 0,
) -> ImpactReceipt:
    """A receipt that invalidates every key — ``native-full``, a fallback, or rule 1/2.

    With no changed paths nothing can be invalidated: the trees are identical, so every key is
    trivially unaffected (and an exact tree match needs no receipt anyway)."""
    changed = tuple(changed)
    names = _key_names(keys)
    if not changed:
        return ImpactReceipt(
            backend=backend,
            backend_version=backend_version,
            base_tree=base_tree,
            head_tree=head_tree,
            changed_paths=(),
            unowned_paths=(),
            global_inputs_hit=(),
            invalidated_keys=(),
            unaffected_keys=names,
            evidence={name: KeyEvidence(ImpactReason.UNCHANGED) for name in names},
            fallback_reason=fallback_reason,
            elapsed_ms=elapsed_ms,
        )
    return ImpactReceipt(
        backend=backend,
        backend_version=backend_version,
        base_tree=base_tree,
        head_tree=head_tree,
        changed_paths=tuple(c.path for c in changed),
        unowned_paths=tuple(unowned_paths),
        global_inputs_hit=tuple(global_inputs_hit),
        invalidated_keys=names,
        unaffected_keys=(),
        evidence={name: KeyEvidence(reason) for name in names},
        fallback_reason=fallback_reason,
        elapsed_ms=elapsed_ms,
    )


def native_full_receipt(
    *,
    base_tree: str,
    head_tree: str,
    changed: Iterable[ChangedPath],
    keys: Iterable[AttestKey],
    fallback_reason: str = "",
    elapsed_ms: int = 0,
) -> ImpactReceipt:
    """The ``native-full`` answer: every key invalidated by any change. With a
    ``fallback_reason`` it is rule 3's degraded answer for a backend that could not answer."""
    return invalidate_all(
        backend=NATIVE_FULL,
        backend_version=NATIVE_FULL_VERSION,
        base_tree=base_tree,
        head_tree=head_tree,
        changed=changed,
        keys=keys,
        reason=ImpactReason.FALLBACK if fallback_reason else ImpactReason.NATIVE_FULL,
        fallback_reason=fallback_reason,
        elapsed_ms=elapsed_ms,
    )


def apply_fail_closed(
    *,
    backend: str,
    backend_version: str,
    request: ImpactRequest,
    raw: BackendImpact,
    elapsed_ms: int = 0,
) -> ImpactReceipt:
    """Turn a backend's raw answer into a receipt, enforcing rules 1, 2 and 4 (rule 3 — error,
    timeout, version mismatch — is the caller's, which never gets a ``raw`` to pass here).

    ``backend_version`` is the version the resolver expected; ``raw.backend_version`` differing
    from it is a rule 3 fallback, checked here too so no caller can skip it."""
    common = {
        "base_tree": request.base_tree,
        "head_tree": request.head_tree,
        "changed": request.changed,
        "keys": request.keys,
        "elapsed_ms": elapsed_ms,
    }
    if raw.backend_version != backend_version:
        return native_full_receipt(
            fallback_reason=(
                f"backend-version-mismatch: {backend} expected {backend_version!r}, "
                f"answered {raw.backend_version!r}"
            ),
            **common,
        )
    _key_names(request.keys)  # refuse duplicate names before any rule runs
    if not request.changed:
        return invalidate_all(
            backend=backend,
            backend_version=backend_version,
            reason=ImpactReason.UNCHANGED,
            **common,
        )

    changed_paths = tuple(c.path for c in request.changed)
    unowned = tuple(p for p in changed_paths if not tuple(raw.owners.get(p) or ()))
    if unowned:  # rule 1
        return invalidate_all(
            backend=backend,
            backend_version=backend_version,
            reason=ImpactReason.UNOWNED_PATH,
            unowned_paths=unowned,
            **common,
        )
    global_hit = tuple(p for p in changed_paths if matches_global_input(p, raw.global_inputs))
    if global_hit:  # rule 2
        return invalidate_all(
            backend=backend,
            backend_version=backend_version,
            reason=ImpactReason.GLOBAL_INPUT,
            global_inputs_hit=global_hit,
            **common,
        )

    affected = set(raw.affected_units)
    for path in changed_paths:
        affected.update(raw.owners.get(path) or ())

    invalidated: list[str] = []
    unaffected: list[str] = []
    evidence: dict[str, KeyEvidence] = {}
    for key in request.keys:
        units = tuple(sorted(set(raw.key_units.get(key.name) or ())))
        if key.selector(backend) is None:  # rule 4 (and rule 5: git-metadata keys)
            evidence[key.name] = KeyEvidence(ImpactReason.NO_SELECTOR)
        elif not units or key.name not in raw.proven_keys:  # rule 4
            evidence[key.name] = KeyEvidence(ImpactReason.UNPROVEN, units)
        else:
            hit = tuple(u for u in units if u in affected)
            if hit:
                evidence[key.name] = KeyEvidence(ImpactReason.AFFECTED, hit)
            else:
                evidence[key.name] = KeyEvidence(ImpactReason.UNAFFECTED, units)
                unaffected.append(key.name)
                continue
        invalidated.append(key.name)

    return ImpactReceipt(
        backend=backend,
        backend_version=backend_version,
        base_tree=request.base_tree,
        head_tree=request.head_tree,
        changed_paths=changed_paths,
        unowned_paths=(),
        global_inputs_hit=(),
        invalidated_keys=tuple(invalidated),
        unaffected_keys=tuple(unaffected),
        evidence=evidence,
        elapsed_ms=elapsed_ms,
    )


__all__ = [
    "KEY_POLICIES",
    "NATIVE_FULL",
    "NATIVE_FULL_VERSION",
    "RECEIPT_SCHEMA",
    "AttestKey",
    "BackendImpact",
    "ChangedPath",
    "ImpactReason",
    "ImpactReceipt",
    "ImpactRequest",
    "KeyEvidence",
    "KeyPolicy",
    "ReceiptIntegrityError",
    "apply_fail_closed",
    "invalidate_all",
    "matches_global_input",
    "native_full_receipt",
]
