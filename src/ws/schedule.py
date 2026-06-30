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


@dataclass(frozen=True)
class Group:
    """A set of beads to dispatch to ONE grouped agent. `kind` is 'planner' (declared
    `batch:<group>`) or 'chain' (auto-detected linear chain); `ids` are in dependency order."""

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


def plan_schedule(beads: list[dict], *, max_size: int) -> Schedule:
    """Compute the dispatch plan for a molecule's open beads.

    Honors planner `batch:<group>` labels (≥2 members ⇒ one grouped agent) and auto-detects pure
    linear chains among the rest, guarding each detected chain (single model tier / single review
    gate / size cap). Everything left over is a singleton — the default parallel one-per-worktree.
    """
    by_id = {str(b.get("id")): b for b in beads if b.get("id")}
    order = list(by_id)
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
