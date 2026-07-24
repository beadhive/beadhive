"""Conflict estimation — the advisory *start-verdict* read: "how likely is this bead to conflict
with the work already queued ahead of it?"

Where `release_order.py` decides the *merge order* of a set of beads, this module answers a
narrower, earlier question consulted by the dispatcher's start-verdict path: given a bead about to
start and the beads already queued ahead of it, how likely is a merge conflict, and why? Like
`release_order.py` and `schedule.py` this is the pure, CLI-free decision core — it estimates only
and never claims, merges, or mutates anything. The verdict is strictly **advisory**.

The `ConflictEstimator` protocol is deliberately narrow — one method:

    estimate(bead, queue_ahead) -> ConflictEstimate(likelihood: float 0..1, reason: str)

Estimators are resolved by name through a small registry (`_ESTIMATORS`), selected by the
`release.conflict_estimator` config key (default `file-overlap`). Only the bundled `file-overlap`
floor ships today.

`file-overlap` is the most basic floor: it reads each bead's *expected paths* from its `path:<p>`
labels (mirroring how `release_order.py` reads `release:`/`wave:` labels — the estimator stays pure
over `bd list --json` bead dicts) and reports a HIGH likelihood when a bead's expected paths
overlap any bead ahead of it, LOW when they are disjoint. It is intentionally crude: overlapping
files is a conflict *floor*, not a real structural diff.

**Plugin seam (registry is open; discovery is NOT).** `register_estimator` lets a future external
estimator — a structural (real diff/AST overlap), business-rule, or deployment-aware plugin — be
swapped in under its own name and selected via `release.conflict_estimator`. A plugin *loader /
discovery* mechanism (entry-points, path scanning, dynamic import) is explicitly **out of scope**
for this bead: the registry is left open for a future entry, but nothing auto-populates it — a
plugin must call `register_estimator` itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple, Protocol, runtime_checkable

_PATH = "path:"

# The default named estimator the registry resolves — mirrors config's
# `release.conflict_estimator` default (`config.release_conflict_estimator`). Only this floor
# ships today.
DEFAULT_ESTIMATOR = "file-overlap"

# Likelihood floor/ceiling for the file-overlap estimator. It is a binary floor — overlap or not —
# so it reports the extremes rather than a calibrated probability; a richer estimator would return
# intermediate values.
HIGH_LIKELIHOOD = 0.9
LOW_LIKELIHOOD = 0.0


class ConflictEstimate(NamedTuple):
    """An estimator's verdict: `likelihood` in 0..1 that the bead conflicts with the queue ahead,
    and a human `reason` explaining it. A plain tuple, so callers can `likelihood, reason = ...`."""

    likelihood: float
    reason: str


@runtime_checkable
class ConflictEstimator(Protocol):
    """The narrow start-verdict protocol: estimate one bead's conflict likelihood against the beads
    already queued ahead of it. Pure and advisory — an estimator inspects, never mutates. External
    structural / business / deployment estimators satisfy this same one-method shape."""

    def estimate(self, bead: dict, queue_ahead: Sequence[dict]) -> ConflictEstimate: ...


def _bead_id(bead: dict) -> str:
    return str((bead or {}).get("id") or "")


def expected_paths(bead: dict) -> frozenset[str]:
    """The files a bead is expected to touch, read from its `path:<p>` labels ('' set if none).

    Same label-driven reader family as `release_order._label_value`, so estimators consume
    `bd list --json` bead dicts directly. Collects *all* `path:` labels (a bead usually touches
    several files), unlike the single-value `release:`/`wave:` readers."""
    return frozenset(
        s[len(_PATH) :]
        for lbl in (bead or {}).get("labels", []) or []
        if (s := str(lbl)).startswith(_PATH)
    )


class FileOverlapEstimator:
    """The bundled floor (`file-overlap`): overlapping expected paths ⇒ likely conflict.

    Reports HIGH likelihood when this bead's `path:` labels intersect any bead queued ahead, LOW
    when disjoint (or when the bead declares no paths — nothing to overlap on). The `reason` names
    the beads ahead it collides with and the shared files. No structural insight — just file-set
    intersection, the crudest useful conflict signal."""

    def estimate(self, bead: dict, queue_ahead: Sequence[dict]) -> ConflictEstimate:
        mine = expected_paths(bead)
        if not mine:
            return ConflictEstimate(
                LOW_LIKELIHOOD, "no expected paths declared; file-overlap cannot detect a conflict"
            )
        collisions = []
        for ahead in queue_ahead or []:
            shared = mine & expected_paths(ahead)
            if shared:
                collisions.append(f"{_bead_id(ahead)} ({', '.join(sorted(shared))})")
        if not collisions:
            return ConflictEstimate(
                LOW_LIKELIHOOD, "expected paths are disjoint from every bead queued ahead"
            )
        return ConflictEstimate(
            HIGH_LIKELIHOOD, "expected paths overlap beads ahead: " + "; ".join(collisions)
        )


# The estimator registry: named implementations, resolved by `release.conflict_estimator`. Only the
# `file-overlap` floor ships today; the registry is intentionally left OPEN for a future external
# entry (see the module docstring's plugin-seam note) — but no loader/discovery populates it, so a
# plugin must `register_estimator` itself.
_ESTIMATORS: dict[str, ConflictEstimator] = {
    DEFAULT_ESTIMATOR: FileOverlapEstimator(),
}


def register_estimator(name: str, estimator: ConflictEstimator) -> None:
    """Register `estimator` under `name` so `release.conflict_estimator: <name>` resolves it.

    The plugin seam: an external structural / business / deployment estimator calls this to make
    itself selectable by name. This is NOT plugin discovery — nothing auto-calls it; wiring a
    plugin in is a deliberate, explicit act (out-of-scope loader left for later)."""
    _ESTIMATORS[name] = estimator


def available_estimators() -> list[str]:
    """The names the registry can resolve, sorted — surfaced in the unknown-estimator error."""
    return sorted(_ESTIMATORS)


def get_estimator(name: str = DEFAULT_ESTIMATOR) -> ConflictEstimator:
    """Resolve the estimator registered under `name` (default/config `release.conflict_estimator`).

    Raises `ValueError` — listing the available names — when `name` is not registered (mirrors
    `release_order.order_beads`'s unknown-strategy behavior)."""
    try:
        return _ESTIMATORS[name]
    except KeyError:
        raise ValueError(
            f"unknown conflict estimator {name!r}; "
            f"available: {', '.join(available_estimators())}"
        ) from None


def _self_check() -> None:
    """`python -m beadhive.conflict_estimator` self-check: the file-overlap floor reports HIGH on
    overlapping expected paths and LOW on disjoint ones, and the registry resolves by name."""

    def _bead(bead_id: str, *paths: str) -> dict:
        return {"id": bead_id, "labels": [f"{_PATH}{p}" for p in paths]}

    est = get_estimator()  # file-overlap
    ahead = [_bead("a", "src/x.py"), _bead("b", "src/y.py")]

    overlap = est.estimate(_bead("me", "src/x.py", "src/z.py"), ahead)
    assert overlap.likelihood == HIGH_LIKELIHOOD, overlap
    assert "a (src/x.py)" in overlap.reason, overlap.reason

    disjoint = est.estimate(_bead("me", "src/q.py"), ahead)
    assert disjoint.likelihood == LOW_LIKELIHOOD, disjoint

    unknown_raised = False
    try:
        get_estimator("structural")
    except ValueError as exc:
        unknown_raised = "file-overlap" in str(exc)
    assert unknown_raised, "unknown estimator must raise ValueError listing available estimators"

    print("conflict_estimator self-check OK:", overlap.reason)


if __name__ == "__main__":
    _self_check()
