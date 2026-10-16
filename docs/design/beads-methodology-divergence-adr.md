# Where bh deliberately diverges from beads' methodology ADR (bh-5lpl9.2)

**Status:** accepted · **Date:** 2026-10-16 · **Decision bead:** `bh-5lpl9.2`
**Epic:** `bh-5lpl9` (hive classification: ownership, remotes, store classes, routing)
**Counterpart:** [beads-multi-repo-adoption-adr.md](beads-multi-repo-adoption-adr.md)
(`bh-5lpl9.1`, what bh adopts)
**Implemented by:** [two-store-scope-adr.md](two-store-scope-adr.md) (`bh-lniwf.1`) and
`bh-lniwf.5` (operational routing to the internal store)

## Why this record exists

Each item below is a beads feature bh considered and **rejected**, with the bh mechanism that
replaces it. Without a record, a reader of the beads documentation can mistake each one for an
oversight and "correct" bh toward it. Source pages and their retrieval dates are listed in the
counterpart record.

## The operative routing rule: subject + visibility

bh routes a bead by **what it is about** and **who may see it** — never by who is writing it.
This table is the operative rule. It is implemented by `src/beadhive/store_target.py`
(`resolve_store`, `operational_store`) and enforced by the guards listed in the last column.

| Subject | Visibility | Destination | Enforced by |
|---|---|---|---|
| one repository | internal | that hive's **internal** store (the default) | `store_target.operational_store` |
| one repository | public | that hive's **public** store — explicit only | `store_target.require_explicit_public` |
| several repositories, or none | internal | **HQ**, the sourceless-beads store | `bh hq bd …` / `bh escalate` |
| explicit target | as chosen | `bh report <hive>` — bh's spelling of `bd --repo` | `registry.resolve_hive` |

Rules the table implies:

- **Internal is implicit; public is never implicit.** A command that names no store gets the
  internal store. A command that wants the public store must say so, and a public request is
  never silently answered from internal (or the reverse) — `store_target.StoreNotConfigured`.
- **Only the internal store is operational.** Claim, assign, submit, review, merge and
  dispatch refuse a public target (`store_target.PublicStoreNotOperational`).
- **One store per hive remains the default.** A hive with no `stores:` block resolves its one
  legacy store as internal, byte-for-byte as before ([two-store-scope-adr.md](two-store-scope-adr.md)
  §1).

## Rejected

### Routing by actor (`routing.mode auto`, maintainer / contributor)

**Beads feature:** `routing.mode auto` picks a destination from the writer's role —
a maintainer writes here, a contributor writes to `~/.beads-planning`.

**Why rejected:** role is a proxy for "can you write here". bh answers that question with two
mechanisms that have no gap left for the role axis to fill:

- the **host lease** (`src/beadhive/host_lease.py`, [multi-host-model-adr.md](multi-host-model-adr.md))
  decides write *authority* — which host may write a store at all;
- **ownership** (`registry` kind today, the stored `ownership` axis after `bh-5lpl9.5`)
  decides *trust* — the furnish policy, the consent gate, the pull-only rail.

**bh replacement:** the subject-and-visibility table above, with `routing.mode: explicit` set
on every hive so bd's role heuristic is never the operative rule
([adoption record](beads-multi-repo-adoption-adr.md) §3). bd's `beads.role` is still set
from the registry kind (`hive_repair.expected_role`) because bd's own doctor warns without it;
under `explicit` routing it no longer selects a destination.

### Multi-phase stores (plan / impl / maint)

**Beads feature:** split one project across three stores by lifecycle phase.

**Why rejected:** bh tracks phase as **bead state** — intake → planned → in progress →
review → merged — through labels, gates and statuses ([LABELS.md](../LABELS.md),
[WORK.md](../WORK.md)). Phase stores would triple the store count, the prefix count
(see `bh-vcfsb`) and the replication surface, and buy nothing the state model does not
already express. A bead moving phase would have to *move stores*, losing the in-place
history that CLAUDE.md treats as an archive.

**bh replacement:** one operational store per hive; phase is state.

### Routing OSS planning away from the fork (`~/.beads-planning`)

**Beads feature:** keep upstream pull requests clean by routing contributor planning into a
separate planning repository outside the fork.

**Why rejected:** bh keeps planning **in** the fork and hides `.beads/` from Git with the
stealth exclude (zero-footprint `furnish: none`, `registry.furnish_of`). Both approaches keep
pull requests clean. Ours keeps beads beside the repository they are about, so context is not
split across two places. Beads' approach survives a fresh clone and a lost worktree, which
ours does only through replication. We accept that cost knowingly; the two-store work adds
the private replicated remote that closes most of it.

**bh replacement:** stealth-excluded `.beads/` in the fork, replicated to a private remote
when one is configured ([two-store-scope-adr.md](two-store-scope-adr.md) §3).

## A gap, not a divergence: beads has no per-bead privacy

Beads cannot mark one bead private and its neighbour public; visibility is a property of the
whole store and its remote. That is not a choice bh makes — it is the constraint that forces
**store partitioning** (two stores per repository) instead of a labelling convention. It is
stated plainly enough to hand upstream as a feature request: *"per-issue visibility, so one
database can replicate a filtered public subset to a public remote while keeping the rest
private"*. Until it exists, the [two-store ADR](two-store-scope-adr.md) is the workaround.
