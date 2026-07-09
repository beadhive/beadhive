# Naming & strategy ADR — Beadhive / `bh`

> Status: **decided.** This is the decision record for the rename epic
>. It captures the chosen identity, the alternatives that were
> considered and rejected (including the Beadery/`bdws` → Beadhive/`bh` pivot), and the
> rationale — folding in the collision-check findings from
> [`limn-name-collision-check.md`](limn-name-collision-check.md).
> Execution (package rename, asset migration, docs/skills sweep, first release) is tracked by
> the epic's other children (`.2`, `.4`, `.5`, `.6`, `.7`, `.9`); this doc only records *what*
> was decided and *why*.

## Decision

This repo (`ws`, the Workspace CLI) rebrands to its open-source identity:

| Axis | Chosen |
|---|---|
| Umbrella brand / GitHub org | **Beadhive** (`github.com/beadhive`) |
| This repo | **`beadhive/workspace`** — the workspace component under the Beadhive org |
| Canonical published package | **`beadhive`** (PyPI, crates.io) — defensive holds: `bead-hive`, `beadhivecli` |
| Binary / console-script name | **`bh`** — exposed *inside* the `beadhive` package, not published as its own package (see [Accepted tradeoff](#accepted-tradeoff-bh-is-not-a-reservable-standalone-package-name) below) |
| Process layering | **AGF** stays the abstract, tracker-independent Agentic Git Flow process; **Beadflow** is AGF implemented on beads (this tool, unchanged behavior — a naming/framing layer, not a rewrite) |
| Competitor strategy | Do **not** compete head-on with jallum/`beadwork` (`bw`) on bead storage. Instead, **absorb `bd`/`br`/`bw` as pluggable JSONL-interchange backends** (tracked as its own future epic — see [`bead-backend-abstraction.md`](bead-backend-abstraction.md) — out of scope for this rename) |
| Domains (when ready) | `beadhive.ai` + `beadhive.io` (+ `.org`); `beadwork.space` is decorative-only, not a primary property |

These were locked at the epic level (description) and are restated here as
the durable decision record with rationale, per the epic's kickoff-approved acceptance
criteria.

## Alternatives considered and rejected

### Beadworks / Beadworx

**Rejected.** Phonetically routes users straight to `jallum/beadwork` — the exact competitor
this brand needs to be distinct from, not confusable with. Compounding the collision:
`github.com/beadworks` (the org) is **already held** by that competitor, so even the "just add
an s" variant isn't ours to take. (The jewelry "Beadworks"® trademark is a different goods
class and was confirmed **not** a real blocker — the actual obstacle is the competitor's
namespace, not trademark risk.)

### `beadwork` + `bw` binary

**Rejected outright.** This isn't a near-miss — it *is* `jallum/beadwork`'s literal repo name,
binary name, and org. Using it would mean adopting a direct competitor's identity, not
differentiating from it.

### Fighting on bead-storage instead of differentiating on the workspace layer

**Rejected as a strategy**, not a name. Competing with `bd`/`br`/`bw` on "whose bead-storage
engine is best" cedes the actual differentiator — this tool operates one layer up, as an
integration-plane driver over *any* beads-compatible storage. The chosen strategy inverts the
fight: treat `bd`/`br`/`bw` as interchangeable pluggable backends behind the JSONL interchange
(see [`bead-backend-abstraction.md`](bead-backend-abstraction.md)) rather than a rival to beat
on their own ground.

### Beadery / `bdws`

**Rejected — supersedes a prior locked decision.** Beadery/`bdws` was an earlier naming
decision for this same rebrand, since pivoted to Beadhive/`bh`. Recorded reasons for the pivot:

- **"Beadery" is already spoken for as the *meta-factory* concept name**, not a product
  identity — it's the name used by the `beadery-concepts` skill and the illustrative `bdry`
  command spelling for "the factory that runs AGF across planes" (see
  [`gas-frameworks-comparison.md`](gas-frameworks-comparison.md)). Reusing "Beadery" as *this
  specific tool's* brand would conflate "the factory" (the general AGF-on-beads concept) with
  "the workspace CLI" (one concrete implementation of it) — the same kind of self-collision the
  rename is trying to avoid with third parties.
- **`bdws` doesn't read as a crisp, memorable short token** the way `bh` does — it's a
  four-character consonant cluster with no obvious pronunciation, versus `bh` reading cleanly
  off "bead hive."
- The pivot to Beadhive/`bh` was made at the epic-kickoff level ('s locked
  decisions) before this ADR bead was filed; this record documents the *rationale* for that
  already-made pivot rather than re-litigating it. If the operator wants a different or more
  precise rationale on file, amend this section — the epic-level decision itself is not in
  question.

## Accepted tradeoff: `bh` is not a reservable standalone package name

The limn.1 collision check ([`limn-name-collision-check.md`](limn-name-collision-check.md))
found the canonical package name `beadhive` (and `bead-hive`, `beadhivecli`) fully clear across
PyPI, crates.io, npm, GitHub, and the target domains — **but `bh` itself is not free** as a
standalone package name:

| Registry | `bh` status |
|---|---|
| PyPI | taken — `bh` ("Fuzzy Linear Discriminant Analysis") |
| npm | taken — `bh` (Yandex BEM template engine, actively versioned) |
| crates.io | taken — `bh` ("BountyHub CLI", actively maintained) |
| Homebrew core formula | free |

This is a **deliberate, accepted tradeoff**, not an oversight: a package's registry name and
its installed executable name are independent (the npm package `typescript` installs the `tsc`
binary), so `bh` works fine as the **console-script / cargo binary name inside the `beadhive`
package** — no separate registry reservation needed, and no collision at that layer. What we
give up: we can never publish a package literally named `bh` on PyPI, npm, or crates.io — those
slots are permanently held by unrelated, unrelated-to-us maintainers, so `bh` can't be
advertised as a *directly pip/npm/cargo-installable* name, only as the binary you get after
installing `beadhive`. `bh` is also a commonly-aliased personal shell shortcut (`brew home`,
`git branch`, etc. — see limn.1's `gh search code` findings), so some users will need to
override a local `bh` alias; not something fixable from our end.

**Decision:** proceed with `beadhive` as the published package name and `bh` as the binary name
inside it. Do not attempt to acquire a standalone `bh` package on PyPI/npm/crates.io — it isn't
available and isn't required for the `bh` binary to work.

## Consequences

- Execution work is tracked by the epic's remaining children: `.2` (rename the Python package
  & entry points to `beadhive` / `bh`), `.4` (migrate injected/managed assets + marker), `.5`
  (`bh rig migrate` command), `.6` (docs update for the rename + brand/process framing), `.7`
  (skills & agent defs), `.9` (first release of `beadhive` to PyPI).
- The AGF-abstract-vs-Beadflow-impl layering means process docs (AGF tenets, seat roles,
  lifecycle verbs) stay framed as tracker-independent AGF concepts; only the concrete
  tool/package/binary identity changes to Beadhive/`beadhive`/`bh`.
- The bd/br/bw-as-backends strategy is out of scope here and lives in
  [`bead-backend-abstraction.md`](bead-backend-abstraction.md) as its own future epic.
