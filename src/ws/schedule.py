"""Coordinator scheduling — the cost model that decides when to batch beads vs run singletons.

The default AGF unit is one bead → one worktree → one developer → one merge (parallel devs,
serial merge). That wastes effort or invites conflict in the cases the epic identified: a linear
chain with no mid-point testable unit, DAG-parallel beads contending on one file, or expensive
validation cheaper to run once. So the coordinator may dispatch a *work group* — several beads
handled by ONE agent in ONE `wt/batch/<group>` worktree, validated and merged once (8v8.2 carries
the verbs; 8v8.1 carries the `batch:<group>` data model + plan-time cohesion/size/model checks).

This module is the pure, CLI-free decision core (mirrors `molecule.py`): given a molecule's beads
(each a `bd` JSON dict carrying `id`, `labels`, `dependencies`), it returns a `Schedule` —
the groups to run as one agent and the singletons to fan out for parallel wall-time.

Two grouping triggers, both honored:

  1. **Planner batches** — a shared `batch:<group>` label the planner declared (it knows the
     same-file / expensive-validate cases). Already validated at plan time (8v8.1), so honored
     as-is when it has ≥2 members.
  2. **Auto-detected linear chains** — a run of beads connected by *private* `blocks` edges (no
     fan-in / fan-out): a chain can't be parallelized anyway, so batching is strictly cheaper
     (one worktree/validate/merge instead of N sequential ones) with no wall-time lost.

Auto-detected chains aren't plan-validated, so the scheduler re-applies the same guards before
batching them: a single model tier, a single review gate, and the size cap. (Cohesion is implicit
— a private-edge chain is contiguous in the DAG by construction.) A candidate that trips a guard
is NOT batched; its members fall back to singletons. `ws work schedule` wraps this for the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass

_BATCH = "batch:"
_MODEL = "model:"
_GATE = "gate:"
_SIZE = "size:"

# The planner's t-shirt size labels, mapped to an ordinal cost weight (rank order xs<s<m<l<xl).
# `auto` mode sums these across an epic's beads as its cost signal instead of a bare bead count.
_SIZE_WEIGHT = {"xs": 1, "s": 2, "m": 3, "l": 4, "xl": 5}

# An unlabeled or unrecognized size is assumed medium — a neutral, non-zero cost so an unestimated
# bead still consumes budget rather than collapsing for free.
_DEFAULT_SIZE_WEIGHT = _SIZE_WEIGHT["m"]

# `model:` tiers ordered least→most capable. A collapsed Task covers N beads whose `model:` labels
# may differ; the one dispatched session must be capable enough for the HARDEST bead, so
# `max_model_tier` dispatches at the max tier across the batch (haiku < sonnet < opus).
_MODEL_TIER_ORDER = ("haiku", "sonnet", "opus")

# When no batched bead carries a `model:` label there's no signal to widen from, so the dispatch
# falls back to the most-capable tier — never under-provision a collapsed session.
_DEFAULT_MODEL_TIER = _MODEL_TIER_ORDER[-1]


@dataclass(frozen=True)
class Group:
    """A set of beads to dispatch to ONE grouped agent. `kind` is 'planner' (declared
    `batch:<group>`), 'chain' (auto-detected linear chain), or 'collapsed' (operator-forced
    single group that bypasses the guards); `ids` are in dependency order."""

    kind: str
    ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Schedule:
    """The dispatch plan: grouped agents + singletons (default one-per-worktree, parallel)."""

    groups: list[Group]
    singletons: list[str]


def _label_value(bead: dict, prefix: str) -> str:
    """Value of the first `<prefix><value>` label on a bead ('' if none)."""
    for lbl in (bead or {}).get("labels", []) or []:
        s = str(lbl)
        if s.startswith(prefix):
            return s[len(prefix) :]
    return ""


def batch_group(bead: dict) -> str:
    """The `<group>` of a bead's `batch:<group>` label ('' ⇒ unbatched)."""
    return _label_value(bead, _BATCH)


def model_tier(bead: dict) -> str:
    """The bead's `model:<tier>` label ('' ⇒ inherits / unset)."""
    return _label_value(bead, _MODEL)


def review_gate(bead: dict) -> str:
    """An explicit per-bead `gate:<type>` override ('' ⇒ inherits the rig's uniform gate)."""
    return _label_value(bead, _GATE)


def size_weight(bead: dict) -> int:
    """Ordinal cost weight of a bead's `size:<xs..xl>` label. An unlabeled or unrecognized size
    counts as `m` (`_DEFAULT_SIZE_WEIGHT`) — a bead with no estimate still consumes budget."""
    return _SIZE_WEIGHT.get(_label_value(bead, _SIZE), _DEFAULT_SIZE_WEIGHT)


def _model_rank(tier: str) -> int:
    """Ordinal capability rank of a `model:` tier per `_MODEL_TIER_ORDER`; an unrecognized tier
    ranks below every known tier so a recognized label always wins the max."""
    return _MODEL_TIER_ORDER.index(tier) if tier in _MODEL_TIER_ORDER else -1


def max_model_tier(beads: list[dict], *, default: str = _DEFAULT_MODEL_TIER) -> str:
    """The most-capable `model:<tier>` among `beads` — the tier a collapsed session must run at to
    handle its HARDEST bead. Reads each bead via `model_tier`, skips the unlabeled ones (no signal),
    and ranks the rest by `_MODEL_TIER_ORDER` (haiku < sonnet < opus). Returns `default` (opus)
    when no bead is labeled. Advisory: decides the dispatch tier only, never claims or merges."""
    labeled = [tier for tier in (model_tier(b) for b in beads) if tier]
    if not labeled:
        return default
    return max(labeled, key=_model_rank)


def _blockers(bead: dict, within: set[str]) -> set[str]:
    """In-molecule ids that block `bead` (its `blocks`-type deps; parent-child epic edges and
    deps pointing outside the molecule are ignored — only intra-molecule scheduling edges count)."""
    out: set[str] = set()
    for dep in (bead or {}).get("dependencies", []) or []:
        if str(dep.get("type")) != "blocks":
            continue
        blocker = str(dep.get("depends_on_id") or "")
        if blocker in within:
            out.add(blocker)
    return out


def _linear_chains(order: list[str], succ: dict[str, set], pred: dict[str, set]) -> list[list[str]]:
    """Maximal runs of *private* blocker→dependent edges. An edge p→n is private iff p has exactly
    one dependent and n exactly one blocker (no fan-out at p, no fan-in at n). A node starts a
    chain when it has no incoming private edge; we then walk forward while the edge stays private.
    Each node lands in at most one chain. Only chains of ≥2 nodes are returned (in dep order)."""
    chains: list[list[str]] = []
    for node in order:
        has_private_in = len(pred[node]) == 1 and len(succ[next(iter(pred[node]))]) == 1
        if has_private_in:
            continue  # mid-chain — reached by walking from its start
        chain = [node]
        cur = node
        while len(succ[cur]) == 1:
            nxt = next(iter(succ[cur]))
            if len(pred[nxt]) != 1:
                break
            chain.append(nxt)
            cur = nxt
        if len(chain) >= 2:
            chains.append(chain)
    return chains


def _guard_group(ids: list[str], by_id: dict[str, dict], max_size: int) -> tuple[bool, str]:
    """Re-apply the scheduler's batch guards to an auto-detected chain (planner batches are already
    validated at plan time). Returns (ok, reason-if-not-ok): a single model tier, a single review
    gate, and within the size cap."""
    if len(ids) > max_size:
        return False, f"exceeds batch size cap ({len(ids)} > {max_size})"
    models = {model_tier(by_id[i]) for i in ids if model_tier(by_id[i])}
    if len(models) > 1:
        return False, f"mixed model tiers {sorted(models)}"
    gates = {review_gate(by_id[i]) for i in ids if review_gate(by_id[i])}
    if len(gates) > 1:
        return False, f"mixed review gates {sorted(gates)}"
    return True, ""


def _chunk(ids: list[str], size: int) -> list[list[str]]:
    """Split `ids` into consecutive chunks of ≤ `size` (size ≤ 0 or no overflow ⇒ one chunk)."""
    if size <= 0 or len(ids) <= size:
        return [ids]
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def plan_schedule(
    beads: list[dict],
    *,
    max_size: int,
    force_single_group: bool = False,
    max_beads_per_session: int | None = None,
) -> Schedule:
    """Compute the dispatch plan for a molecule's open beads.

    Honors planner `batch:<group>` labels (≥2 members ⇒ one grouped agent) and auto-detects pure
    linear chains among the rest, guarding each detected chain (single model tier / single review
    gate / size cap). Everything left over is a singleton — the default parallel one-per-worktree.

    Operator override: `force_single_group` collapses every open bead into one `collapsed` group,
    bypassing the cohesion/size/model/gate guards (`_guard_group`) — the operator is vouching for
    cohesion instead of the algorithm. It's split only when the molecule exceeds
    `max_beads_per_session` (each chunk its own `collapsed` group). Advisory like the default
    path: it decides grouping only and never claims or merges anything.
    """
    by_id = {str(b.get("id")): b for b in beads if b.get("id")}
    order = list(by_id)

    if force_single_group:
        if not order:
            return Schedule([], [])
        cap = max_beads_per_session if max_beads_per_session is not None else 0
        groups = [
            Group("collapsed", tuple(chunk), f"operator-forced collapsed group of {len(chunk)}")
            for chunk in _chunk(order, cap)
        ]
        return Schedule(groups, [])

    idset = set(order)
    groups: list[Group] = []
    consumed: set[str] = set()

    # 1) honor planner-declared batches (plan-time validated in 8v8.1; ≥2 members to be a group).
    declared: dict[str, list[str]] = {}
    for i in order:
        group = batch_group(by_id[i])
        if group:
            declared.setdefault(group, []).append(i)
    for group, members in declared.items():
        if len(members) >= 2:
            groups.append(Group("planner", tuple(members), f"planner batch '{group}'"))
            consumed.update(members)

    # 2) auto-detect linear chains among the unbatched remainder (DAG over intra-molecule edges).
    remaining = [i for i in order if i not in consumed]
    rem = set(remaining)
    succ: dict[str, set] = {i: set() for i in remaining}
    pred: dict[str, set] = {i: set() for i in remaining}
    for i in remaining:
        for blocker in _blockers(by_id[i], idset):
            if blocker in rem:
                succ[blocker].add(i)
                pred[i].add(blocker)
    for chain in _linear_chains(remaining, succ, pred):
        ok, _why = _guard_group(chain, by_id, max_size)
        if ok:
            reason = f"linear chain of {len(chain)} (no fan-in/out)"
            groups.append(Group("chain", tuple(chain), reason))
            consumed.update(chain)

    # 3) the rest fan out as singletons (parallel wall-time, default one-per-worktree).
    singletons = [i for i in order if i not in consumed]
    return Schedule(groups, singletons)


def auto_should_collapse(beads: list[dict], *, budget: int) -> bool:
    """`auto` mode's per-epic collapse-vs-fanout decision, weighted by the planner's `size:` labels.

    Sums each candidate bead's `size:<xs..xl>` ordinal weight (`_SIZE_WEIGHT`) and collapses the
    epic into one grouped session only when that cost stays within `budget`. Falls back to fanout
    when the sum exceeds budget, or when the beads carry mixed `model:` tiers or mixed `gate:` types
    — the latter two reuse `_guard_group`'s disqualifiers (the batch size-*count* cap is neutralized
    by passing `len(ids)`, since here the cost gate is the ordinal budget, not a bead count).

    Advisory like `plan_schedule`: it decides grouping only and never claims or merges anything.
    """
    by_id = {str(b.get("id")): b for b in beads if b.get("id")}
    ids = list(by_id)
    if not ids:
        return False
    ok, _why = _guard_group(ids, by_id, len(ids))
    if not ok:
        return False
    total = sum(size_weight(by_id[i]) for i in ids)
    return total <= budget
