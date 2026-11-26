"""Minimum ``bd`` release for the CLI claim route (bh-rwjxt, gas-city-runtime-seam ADR Decision 5).

``bd update --claim`` is the lifecycle's claim write. From bd 1.3.0 it is an atomic,
actor-idempotent compare-and-set on the row (``ClaimIssueInTx`` in beads'
``internal/storage/issueops/claim.go``: the ``UPDATE`` is conditional on the pre-image
``row_lock``, so exactly one of two racing claimers sees a row affected and the other is refused
with a non-zero exit). Beadhive relies on that: a non-zero ``--claim`` exit is a clean "lost the
race", and a second driver (Gas City) may claim the same store concurrently.

An older bd cannot be trusted with that guarantee, so the CLI claim route refuses it by name
rather than silently racing.

Verified 2026-11-24 against the beads source: the CAS (``row_lock = ?`` predicate, commit
ad2346e82, 2026-08-09) predates the ``[1.3.0] - 2026-09-15`` changelog entry (release prep
b3ef65c85, 2026-08-30), so 1.3.0 is the floor. No 1.3.x tag is present in the source checkout, so
the 1.3.4 changelog could not be consulted; the unreleased Dolt-commit-after-SQL-commit fix
(#5740) hardens durability of a won claim, not exclusivity, and is not part of the floor.

A version that is not a plain tagged release (a dev, HEAD or rc build, or no bd on PATH) is
allowed: it cannot be judged against a tag, and a missing bd fails the claim itself with its own
error. The parse rule is shared with ``setup.py`` via :mod:`beadhive.bd_version`, so
``bd version 1.1.0 (dev)`` is a dev build (allowed), not a 1.1.0 release (refused).
"""

from __future__ import annotations

from .bd_version import tagged_release

#: First bd release whose ``--claim`` is the atomic compare-and-set.
MIN_ATOMIC_CLAIM_BD = (1, 3, 0)


class BdTooOldError(RuntimeError):
    """The installed bd predates the atomic ``--claim`` this tool relies on."""


def require_atomic_claim(version_line: str | None = None) -> None:
    """Raise :class:`BdTooOldError` when the local bd is a release below the atomic-claim floor.

    ``version_line`` is ``bd --version``'s output; omitted, the process-memoized local probe is
    used."""
    if version_line is None:
        from . import dolt_health

        version_line = dolt_health._local_bd_version_string()
    found = tagged_release(version_line)
    if found is None or found >= MIN_ATOMIC_CLAIM_BD:
        return
    floor = ".".join(str(n) for n in MIN_ATOMIC_CLAIM_BD)
    shown = ".".join(str(n) for n in found)
    raise BdTooOldError(
        f"bd {shown} is too old to claim beads safely: `bd update --claim` is only an atomic "
        f"compare-and-set from bd {floor}. Upgrade bd to {floor} or later."
    )
