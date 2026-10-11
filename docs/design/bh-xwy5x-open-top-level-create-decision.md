# Decision: top-level create under the `open` write policy

> Status: **GO, with three conditions**, 2026-10-11 · tag:decision · Epic: **bh-sfp9o** ·
> Decision bead: **bh-xwy5x** · Answers open question 1 of
> [hq-trust-profiles-and-capabilities-adr.md](hq-trust-profiles-and-capabilities-adr.md#9-open-questions)

## Question

Under `open`, a replica writes its own Dolt data, publishes it to `refs/dolt/frame/<id>` and the
hive's writer merges each frame ref into `main` without `--strategy` (ADR section 4). The
`bh-sieai` spike (section 9) showed that two replicas allocate the same child id `<epic>.N` and
that a `--strategy` merge silently drops one of the two beads, so child create is refused until
[gastownhall/beads#4796](https://github.com/gastownhall/beads/issues/4796). bd allocates
top-level ids as hashes, and the spike did not test them across replicas. Do top-level ids, and
the counters behind them, merge cleanly when two replicas create beads at the same time?

## Method

`tests/test_open_top_level_create_int.py`, against real bd 1.3.0 (embedded) and the real Dolt
CLI on the bh-eybn7 fixture. Three frames share one bare `git+file://` hive remote: `a` founds
the cut-over hive, installs the fence and is its writer; `b` and `c` pull it and run under
`open`. Each replica creates top-level beads through the product engine (`BdEngine.invoke`: the
off-primary decision, the `branch` guard role, bd's own id allocation) and publishes through
`BdEngine.push_state` to its frame ref. The writer fetches both refs and runs `dolt merge` with
no strategy, then reads `dolt_conflicts` and the per-table conflict rows. The only stubs are the
policy itself and the top-level create refusal under test; child create keeps the product's
refusal.

## Evidence

| Test | Replicas write | First merge (`b`) | Second merge (`c`) |
|---|---|---|---|
| `test_any_two_open_replicas_conflict_on_bd_clone_id` | one comment each, no create | clean | **conflict on `metadata.clone_id` only** |
| `test_hash_ids_from_two_open_replicas_merge_cleanly` | 3 top-level creates each, default hash ids | clean | only `metadata.clone_id`; with that row kept as the writer's, the merge completes with all 6 beads, titles intact |
| `test_a_colliding_top_level_id_stops_the_merge_and_drops_nothing` | one create each with the same id (`--id`, standing in for a hash collision) | clean | conflict on `issues` (that id) and `clone_id`; the merge aborts, `main` keeps `b`'s bead, `c`'s is unmerged and still on its frame ref |
| `test_counter_ids_from_two_open_replicas_collide` | one create each, `issue_id_mode=counter` | clean | conflict on `issues` (`fx-1` on both) and `clone_id`; the merge aborts |

Ids and counters:

- **Hash ids did not collide.** The six ids were distinct, `issue_counter` did not move, and no
  `issues`, `events`, `bd_events_*`, `labels` or `dependencies` row conflicted.
- **A hash collision is possible and is loud.** bd picks the shortest id (3 characters on a fresh
  hive, 5 on this repo's hive) that keeps the collision probability *within its own store*
  under its bound, and checks only that store. Two replicas, or a replica and the writer, cannot
  see each other's unmerged ids, so each pair of creates made between two merges collides with
  probability 1/36^L: about 1 in 46 656 at L = 3, 1 in 60 million at L = 5. When it happens, the
  merge stops on the `issues` row; nothing is dropped.
- **Counter ids always collide.** In `issue_id_mode=counter` each replica advances its own copy of
  `issue_counter` to the same value. The counter rows are equal, so they merge "cleanly"; the
  collision lands on the `issues` row instead. This is the child-id failure (#4796) for top-level
  ids.

Finding outside the question (an input for `bh-sfp9o.5`, not for create):

- **Every second frame ref conflicts on `metadata.clone_id`.** bd keeps a per-clone id in its
  versioned `metadata` table and each clone rewrites it, so any two replicas that both publish
  disagree on that row, whatever they wrote. A comment-only pair shows it. The frame-ref merge
  must treat this row as frame-local (the writer keeps its own value); otherwise, under `open`,
  only the first frame ref after each writer sync ever merges without an operator step.

## Decision

**GO:** bd's hash ids merge cleanly across replicas, and a rare collision is a stopped merge,
not a lost bead. Top-level create may open under `open`, on three conditions the replan must
carry:

1. **Hash ids only.** Top-level create stays refused under `open` when the hive's
   `issue_id_mode` is `counter`; counter ids collide on every concurrent create.
2. **`metadata.clone_id` is frame-local in the frame-ref merge** (`bh-sfp9o.5`): the writer keeps
   its own value for that one row, recorded in the typed outcome. This is needed for every
   `open` write, not just create.
3. **An id collision is not resolved by taking a side.** A conflict on an `issues` row whose two
   sides are different beads must not be offered "take main's / the frame's side", which drops
   one bead exactly as `--strategy` does. The resolve verb re-ids the frame's bead, or refuses
   and names both beads.

**Unchanged either way:** child create (`<epic>.N`) stays refused under `open` until
[gastownhall/beads#4796](https://github.com/gastownhall/beads/issues/4796) gives frame-scoped
child ids. No profile or override lifts it.

## Follow-up

This bead lifts nothing. The GO re-enters through `/bh:replan` on `bh-sfp9o`, which lifts
`bh-sxhnq`'s top-level create refusal (`write_policy.open_refusal`) with condition 1 and
sequences it after `bh-sfp9o.5` carries conditions 2 and 3.
