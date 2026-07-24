"""Release-order planning — the advisory scorer that decides the *merge order* of a set of beads.

The default AGF merge policy is first-come-first-served: the merger lands approved beads in the
order they clear review. That's fine until a molecule mixes change *kinds* — a hotfix for the
released version, additive features that should ship as one clean minor bump, and a breaking change
that must not leak into a patch release. Landing those in review-clearing order produces a messy,
hard-to-version history. This module computes a *stable-versioning* order instead.

Like `schedule.py`, this is the pure, CLI-free decision core: given a molecule's beads (each a `bd`
JSON dict carrying `id` and `labels`) it returns a `ReleaseOrder` — a full merge order plus the
tiers that produced it. It is strictly **advisory**: it decides order only and never claims,
merges, or mutates anything. The dispatcher's start-verdict and the merger's merge-order consult
it; neither is obeyed blindly.

It reads the two labels the sibling beads added:

  * `release:<breaking|feature|fix>` — the change's semantic-version impact (bh-k2j8.2, a
    code-owned CLOSED dimension: `registry.RELEASE_VALUES`).
  * `wave:<name>` — an OPEN batching label grouping additive features into release waves,
    distinct from the worktree-collapse `batch:<group>`.

The stable-versioning ordering, in tiers:

  1. **fixes-for-latest** — `release:fix` beads, capped at `fix_churn_budget` (config
     `release.fix_churn_budget`). These flush ahead of everything so the released version keeps
     getting patched; past the cap, "further fixes yield to additive work".
  2. **additive features grouped by wave** — `release:feature` beads, one group per wave in wave
     order (first-appearance), forming what would be a single minor bump.
  3. **deferred fixes** — any `release:fix` beyond the churn budget: they yielded to the additive
     cohort but a plain fix still precedes a breaking change.
  4. **breaking changes** — `release:breaking` beads last, deferred behind their additive cohort
     so they never leak into a patch/minor window early.

Strategy is selected by name through a small registry (`_STRATEGIES`); only `stable-versioning`
ships today. An unknown name raises `ValueError` listing the available strategies.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .conflict_estimator import DEFAULT_ESTIMATOR, ConflictEstimate, get_estimator
from .registry import RELEASE_VALUES  # code-owned {breaking, feature, fix} — keep the vocab uniform

_RELEASE = "release:"
_WAVE = "wave:"

# The default named strategy the scorer registry resolves — mirrors config's
# `release.strategy` default (`config.release_strategy`). Only this one ships today.
DEFAULT_STRATEGY = "stable-versioning"

# Default patch-window churn cap — mirrors config's `release.fix_churn_budget`
# (`config.release_fix_churn_budget`). Kept in sync but duplicated so the pure core carries no
# config dependency (callers pass the layered value in).
DEFAULT_FIX_CHURN_BUDGET = 3


@dataclass(frozen=True)
class Wave:
    """One additive-feature release wave: the `<name>` of its `wave:<name>` label and the bead
    `ids` in it, in input order. Unwaved features collect under the empty-string wave."""

    name: str
    ids: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseOrder:
    """The advisory merge plan for a set of beads under the stable-versioning strategy.

    `order` is the flattened merge order (the thing a merger consults); the tier fields expose how
    it was built so callers (and tests) can assert the churn cap and wave grouping directly:
      * `fixes` — `release:fix` beads flushed first, capped at the churn budget (tier 1);
      * `waves` — additive `release:feature` beads grouped one `Wave` per wave, in order (tier 2);
      * `deferred_fixes` — `release:fix` beyond the churn budget, after the features (tier 3);
      * `breaking` — `release:breaking` beads, last (tier 4).

    Beads with no `release:` label are unclassified and dropped from the order (advisory ordering
    only covers version-impacting changes); `unlabeled` records them for the caller."""

    order: tuple[str, ...]
    fixes: tuple[str, ...] = ()
    waves: tuple[Wave, ...] = ()
    deferred_fixes: tuple[str, ...] = ()
    breaking: tuple[str, ...] = ()
    unlabeled: tuple[str, ...] = field(default=())


def _label_value(bead: dict, prefix: str) -> str:
    """Value of the first `<prefix><value>` label on a bead ('' if none) — same reader as
    `schedule._label_value`, so both consume `bd list --json` bead dicts identically."""
    for lbl in (bead or {}).get("labels", []) or []:
        s = str(lbl)
        if s.startswith(prefix):
            return s[len(prefix) :]
    return ""


def release_impact(bead: dict) -> str:
    """The bead's `release:<breaking|feature|fix>` impact ('' ⇒ unclassified). Only a value in
    `RELEASE_VALUES` counts — an off-vocabulary label is treated as unset (the validator rejects
    it upstream; the scorer just doesn't order on it)."""
    value = _label_value(bead, _RELEASE)
    return value if value in RELEASE_VALUES else ""


def wave_name(bead: dict) -> str:
    """The bead's `wave:<name>` batching label ('' ⇒ unwaved)."""
    return _label_value(bead, _WAVE)


def _bead_id(bead: dict) -> str:
    return str((bead or {}).get("id") or "")


def _group_waves(features: list[dict]) -> tuple[Wave, ...]:
    """Group additive features one `Wave` per `wave:<name>`, in first-appearance wave order, ids
    in input order within each wave. Unwaved features collect under the empty-string wave, ordered
    by first appearance like any other."""
    order: list[str] = []
    members: dict[str, list[str]] = {}
    for bead in features:
        name = wave_name(bead)
        if name not in members:
            members[name] = []
            order.append(name)
        members[name].append(_bead_id(bead))
    return tuple(Wave(name, tuple(members[name])) for name in order)


def _stable_versioning(beads: list[dict], *, fix_churn_budget: int) -> ReleaseOrder:
    """The stable-versioning scorer (see the module docstring for the tier rationale).

    Partitions beads by `release:` impact preserving input order, caps the leading fix block at
    `fix_churn_budget`, groups additive features by wave, and defers the fix overflow behind the
    additive cohort with breaking changes last. Pure — no side effects, no mutation of inputs."""
    fixes: list[str] = []
    features: list[dict] = []
    breaking: list[str] = []
    unlabeled: list[str] = []
    for bead in beads:
        impact = release_impact(bead)
        if impact == "fix":
            fixes.append(_bead_id(bead))
        elif impact == "feature":
            features.append(bead)
        elif impact == "breaking":
            breaking.append(_bead_id(bead))
        else:
            unlabeled.append(_bead_id(bead))

    cap = fix_churn_budget if fix_churn_budget > 0 else 0
    flushed = tuple(fixes[:cap])
    deferred = tuple(fixes[cap:])
    waves = _group_waves(features)
    wave_ids = tuple(i for wave in waves for i in wave.ids)
    breaking_ids = tuple(breaking)

    order = flushed + wave_ids + deferred + breaking_ids
    return ReleaseOrder(
        order=order,
        fixes=flushed,
        waves=waves,
        deferred_fixes=deferred,
        breaking=breaking_ids,
        unlabeled=tuple(unlabeled),
    )


# The strategy registry: named scorer implementations, resolved by `release.strategy`. Only
# `stable-versioning` ships today; add a key here to introduce another ordering policy.
_STRATEGIES: dict[str, Callable[..., ReleaseOrder]] = {
    DEFAULT_STRATEGY: _stable_versioning,
}


def available_strategies() -> list[str]:
    """The names the registry can resolve, sorted — surfaced in the unknown-strategy error."""
    return sorted(_STRATEGIES)


def order_beads(
    beads: list[dict],
    *,
    strategy: str = DEFAULT_STRATEGY,
    fix_churn_budget: int = DEFAULT_FIX_CHURN_BUDGET,
) -> ReleaseOrder:
    """Compute the advisory merge order for `beads` under the named `strategy`.

    Selects the scorer from the registry by `strategy` (default/config `release.strategy`) and runs
    it with the patch-window `fix_churn_budget` (config `release.fix_churn_budget`). Advisory:
    returns an order, never claims or merges. Raises `ValueError` — listing the available
    strategies — when `strategy` is not registered."""
    try:
        scorer = _STRATEGIES[strategy]
    except KeyError:
        raise ValueError(
            f"unknown release strategy {strategy!r}; "
            f"available: {', '.join(available_strategies())}"
        ) from None
    return scorer(beads, fix_churn_budget=fix_churn_budget)


def merge_sequence(
    beads: list[dict],
    *,
    strategy: str = DEFAULT_STRATEGY,
    fix_churn_budget: int = DEFAULT_FIX_CHURN_BUDGET,
) -> tuple[str, ...]:
    """The full advisory merge order over *every* bead in `beads` (ids only).

    `order_beads` drops beads with no `release:` label from its `order` (advisory ordering only
    covers version-impacting changes); the merge queue must still list them. So this appends the
    unclassified ids after the strategy-ordered ones, each block keeping input order — the shape a
    merger consults to sort `work ready --gated` without losing any ready bead."""
    result = order_beads(beads, strategy=strategy, fix_churn_budget=fix_churn_budget)
    return result.order + result.unlabeled


def start_verdict(
    bead: dict,
    queue_ahead: Sequence[dict],
    *,
    estimator: str = DEFAULT_ESTIMATOR,
) -> ConflictEstimate:
    """The advisory start-verdict: "how likely is `bead` to conflict with the queue ahead?"

    The seam wiring the `ConflictEstimator` registry into the scorer's start-verdict path: resolve
    the named estimator (default/config `release.conflict_estimator`) and estimate `bead`'s conflict
    likelihood against `queue_ahead` (the beads already ordered ahead of it). Advisory — returns a
    likelihood + reason, never claims or merges. A dispatcher consults it before starting a bead:

        v = release_order.start_verdict(bead, ahead,
                                        estimator=config.release_conflict_estimator(cfg, entry))

    Raises `ValueError` — listing the available estimators — when `estimator` is not registered."""
    return get_estimator(estimator).estimate(bead, queue_ahead)


def _self_check() -> None:
    """`python -m beadhive.release_order` self-check: order a hand-built bead set and assert the
    tiers (fixes-for-latest capped, features grouped by wave, breaking last) came out right."""

    def _bead(bead_id: str, *, release: str | None = None, wave: str | None = None) -> dict:
        labels = []
        if release:
            labels.append(f"{_RELEASE}{release}")
        if wave:
            labels.append(f"{_WAVE}{wave}")
        return {"id": bead_id, "labels": labels}

    beads = [
        _bead("brk", release="breaking"),
        _bead("fix1", release="fix"),
        _bead("feat-b", release="feature", wave="two"),
        _bead("fix2", release="fix"),
        _bead("feat-a", release="feature", wave="one"),
        _bead("fix3", release="fix"),
        _bead("feat-a2", release="feature", wave="one"),
        _bead("fix4", release="fix"),  # 4th fix — over the budget of 2
    ]

    result = order_beads(beads, fix_churn_budget=2)

    assert result.fixes == ("fix1", "fix2"), result.fixes
    assert result.deferred_fixes == ("fix3", "fix4"), result.deferred_fixes
    assert result.breaking == ("brk",), result.breaking
    assert [(w.name, w.ids) for w in result.waves] == [
        ("two", ("feat-b",)),
        ("one", ("feat-a", "feat-a2")),
    ], result.waves
    assert result.order == (
        "fix1", "fix2", "feat-b", "feat-a", "feat-a2", "fix3", "fix4", "brk",
    ), result.order

    unknown_raised = False
    try:
        order_beads(beads, strategy="rolling")
    except ValueError as exc:
        unknown_raised = "stable-versioning" in str(exc)
    assert unknown_raised, "unknown strategy must raise ValueError listing available strategies"

    print("release_order self-check OK:", " -> ".join(result.order))


if __name__ == "__main__":
    _self_check()
