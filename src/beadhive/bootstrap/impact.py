"""Composition root for impact resolution (Attested Green ADR, Amendment 1).

Turns a resolved hive `work.attest` config into domain :class:`AttestKey` values, and binds the
configured backend into an :class:`ImpactResolver` for explicit injection.
Backends are passed in by the caller that composes them (e.g. a Pants backend, bh-1j3ei.7);
nothing here — or downstream — looks one up in a global registry. With no config the result is
``native-full``: every key invalidated on every change, exactly today's gate.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..adapters.impact_git import GitTreeDiff
from ..modules.config.contracts import AttestConfig
from ..modules.work.application.impact import select_resolver
from ..modules.work.contracts.impact import ImpactBackend, ImpactResolver, TreeDiffPort
from ..modules.work.domain.impact import AttestKey


def attest_keys(attest: AttestConfig) -> tuple[AttestKey, ...]:
    """The configured key catalog as domain values, in declared order."""
    return tuple(
        AttestKey(
            name=k.name,
            cmd=k.cmd,
            policy=k.policy,
            selectors=dict(k.selectors),
            enabled=k.enabled,
            disabled_reason=k.disabled_reason or "",
            disabled_until=k.disabled_until,
        )
        for k in attest.keys
    )


def impact_resolver(
    attest: AttestConfig | None = None,
    *,
    backends: Mapping[str, ImpactBackend] | None = None,
    tree_diff: TreeDiffPort | None = None,
) -> ImpactResolver:
    """Bind a resolver from an already-resolved config, defaulting to ``native-full``.

    Config loading and layering stay outside this composition root so the bootstrap boundary
    does not depend back on the legacy configuration facade.
    """
    attest = attest if attest is not None else AttestConfig()
    return select_resolver(
        attest.impact.backend,
        tree_diff=tree_diff if tree_diff is not None else GitTreeDiff(),
        backends=backends,
        timeout_seconds=attest.impact.timeout_seconds,
    )


__all__ = ["attest_keys", "impact_resolver"]
