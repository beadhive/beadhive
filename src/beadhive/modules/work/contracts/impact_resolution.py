"""Impact resolvers: ``native-full`` and the fail-closed wrapper every backend runs inside.

Attested Green ADR, Amendment 1. :class:`NativeFullResolver` is the default and reproduces the
all-or-nothing gate: any change invalidates every key. :class:`FailClosedResolver` wraps one
:class:`ImpactBackend` and is the *only* way a backend's answer becomes a receipt — it computes
the changed paths itself, degrades to ``native-full`` on error / timeout / version mismatch
(rule 3), and hands the raw answer to :func:`apply_fail_closed` for rules 1, 2 and 4.

:func:`select_resolver` is a pure factory for the bootstrap composition root: it takes the
backend name from config plus the explicitly provided backends, never a global registry.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence

from ..domain.impact import (
    NATIVE_FULL,
    AttestKey,
    BackendImpact,
    ImpactReceipt,
    ImpactRequest,
    apply_fail_closed,
    native_full_receipt,
)
from .impact import ImpactBackend, ImpactResolver, TreeDiffPort

#: Default wall-clock budget for one backend answer (``work.attest.impact.timeout_seconds``).
DEFAULT_TIMEOUT_SECONDS = 300.0

Clock = Callable[[], float]


def _elapsed_ms(start: float, clock: Clock) -> int:
    return max(0, int(round((clock() - start) * 1000)))


class NativeFullResolver:
    """Every key invalidated on every change. A non-empty ``fallback_reason`` marks each receipt
    as a degraded answer (e.g. the configured backend is not available on this host)."""

    def __init__(
        self, tree_diff: TreeDiffPort, *, fallback_reason: str = "", clock: Clock = time.monotonic
    ) -> None:
        self._tree_diff = tree_diff
        self._fallback_reason = fallback_reason
        self._clock = clock

    def resolve(
        self, repo: str, base_rev: str, head_rev: str, keys: Sequence[AttestKey]
    ) -> ImpactReceipt:
        start = self._clock()
        base_tree = self._tree_diff.tree_of(repo, base_rev)
        head_tree = self._tree_diff.tree_of(repo, head_rev)
        changed = self._tree_diff.changed_paths(repo, base_tree, head_tree)
        return native_full_receipt(
            base_tree=base_tree,
            head_tree=head_tree,
            changed=changed,
            keys=tuple(keys),
            fallback_reason=self._fallback_reason,
            elapsed_ms=_elapsed_ms(start, self._clock),
        )


class FailClosedResolver:
    """Run one backend inside the amendment's fail-closed rules.

    Tree resolution and the diff are core's (a git failure there propagates: there is no pair
    of trees to write a receipt about). Everything the backend does is contained — any exception,
    an answer slower than ``timeout_seconds``, or a version other than ``backend.version``
    degrades to the ``native-full`` receipt with ``fallback_reason`` set, never a partial answer.
    """

    def __init__(
        self,
        backend: ImpactBackend,
        tree_diff: TreeDiffPort,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Clock = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._backend = backend
        self._tree_diff = tree_diff
        self._timeout = float(timeout_seconds)
        self._clock = clock

    @property
    def backend(self) -> ImpactBackend:
        return self._backend

    def resolve(
        self, repo: str, base_rev: str, head_rev: str, keys: Sequence[AttestKey]
    ) -> ImpactReceipt:
        start = self._clock()
        base_tree = self._tree_diff.tree_of(repo, base_rev)
        head_tree = self._tree_diff.tree_of(repo, head_rev)
        changed = tuple(self._tree_diff.changed_paths(repo, base_tree, head_tree))
        request = ImpactRequest(
            repo=repo,
            base_tree=base_tree,
            head_tree=head_tree,
            changed=changed,
            keys=tuple(keys),
            timeout_seconds=self._timeout,
            base_rev=base_rev,
            head_rev=head_rev,
        )

        def fallback(reason: str) -> ImpactReceipt:
            return native_full_receipt(
                base_tree=base_tree,
                head_tree=head_tree,
                changed=changed,
                keys=request.keys,
                fallback_reason=f"{self._backend.name}: {reason}",
                elapsed_ms=_elapsed_ms(start, self._clock),
            )

        if not changed:  # identical trees: nothing to ask, nothing a backend could invalidate
            return apply_fail_closed(
                backend=self._backend.name,
                backend_version=self._backend.version,
                request=request,
                raw=BackendImpact(backend_version=self._backend.version),
                elapsed_ms=_elapsed_ms(start, self._clock),
            )
        try:
            raw = self._backend.analyze(request)
        except Exception as exc:  # noqa: BLE001 - rule 3: any backend failure degrades to full
            return fallback(f"error: {type(exc).__name__}: {exc}")
        spent = self._clock() - start
        if spent > self._timeout:
            return fallback(f"timeout: answered after {spent:.1f}s (budget {self._timeout:g}s)")
        return apply_fail_closed(
            backend=self._backend.name,
            backend_version=self._backend.version,
            request=request,
            raw=raw,
            elapsed_ms=_elapsed_ms(start, self._clock),
        )


def select_resolver(
    backend: str,
    *,
    tree_diff: TreeDiffPort,
    backends: Mapping[str, ImpactBackend] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    clock: Clock = time.monotonic,
) -> ImpactResolver:
    """The resolver for a configured backend name, from explicitly provided backends.

    ``native-full`` (the default) needs no backend. A name with no provided backend — or a
    provided object that does not implement :class:`ImpactBackend` — does not fail the caller:
    it yields ``native-full`` receipts carrying a ``fallback_reason`` that names the problem
    (rule 3), so a misconfigured hive gets today's gate, never a partial answer."""
    if backend == NATIVE_FULL:
        return NativeFullResolver(tree_diff, clock=clock)
    provided = (backends or {}).get(backend)
    if provided is None:
        return NativeFullResolver(
            tree_diff, fallback_reason=f"{backend}: backend not available", clock=clock
        )
    if not isinstance(provided, ImpactBackend) or provided.name != backend:
        return NativeFullResolver(
            tree_diff,
            fallback_reason=f"{backend}: provided backend does not implement ImpactBackend",
            clock=clock,
        )
    return FailClosedResolver(provided, tree_diff, timeout_seconds=timeout_seconds, clock=clock)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FailClosedResolver",
    "NativeFullResolver",
    "select_resolver",
]
