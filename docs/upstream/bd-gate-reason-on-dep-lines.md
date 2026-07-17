# Upstream ask: `bd show` gate dep lines should render the gate's reason, not just `Gate: <await-type>`

Status: draft — to be filed against the beads project (`steveyegge/beads`).
Tracked here as bead `bh-53tq` (companion to `bh-i371`); follows the `beads(upstream)`
convention established by `bh-g5cy` / `bh-0h53`.

Observed on `bd version 1.1.0`.

---

The section below is the proposed issue body, self-contained for the beads issue tracker.

---

## Title

`bd show`: render a gate's reason snippet on dependency lines, not just `Gate: <await-type>`

## Problem

When a bead depends on gate beads, `bd show <id>` renders every gate dependency identically:

```text
DEPENDS ON
  → ○ bh-swcz: Gate: human ● P2
```

The line exposes only the gate's *await type* — **who** resolves it (`human`, `timer`, …) —
but not **which** gate it is or **why** it exists. A bead blocked by a kickoff gate, a review
gate, and a stray ad-hoc gate shows three indistinguishable `Gate: human` lines. The only way
to tell them apart is to run `bd gate show` (or `bd show`) on each gate id, one at a time.

### Concrete debugging story

We hit this while debugging a duplicate-review-gate deadlock in our tooling built on beads
(our bead `bh-c3il`): a re-submit opened a *second* review gate for the same bead, an approve
resolved one of them, and the merge step refused because "a" gate was still open — while a
second approve reported "nothing to approve". Diagnosing which of the open gates was the
canonical review gate (vs a stale duplicate vs an unrelated kickoff gate) required running
`bd gate show` on every gate id individually, because `bd show` on the blocked bead rendered
them all as the same `Gate: human` line. Had the dep lines carried each gate's reason, the
duplicate would have been visible at a glance.

## Current behavior (real output)

A gate bead carries its reason in its description. From a live database:

```text
$ bd show bh-swcz
○ bh-swcz · Gate: human   [● P2 · OPEN]
Owner: Brian Cripe · Type: gate

DESCRIPTION

  Ad-hoc gate blocking bh-pctz

  Reason: review c79b4fa

BLOCKS
  ← ◐ bh-pctz: (BUG) work submit: molecule submit aborts when ... ● P1
```

But the bead blocked by it drops all of that on the dep line:

```text
$ bd show bh-pctz
◐ bh-pctz [BUG] · work submit: molecule submit aborts when ...   [● P1 · IN_PROGRESS]
...
DEPENDS ON
  → ○ bh-swcz: Gate: human ● P2
```

`Gate: human` is the gate bead's *title*; the disambiguating reason lives one lookup away in
its description and never reaches the dep line.

## Proposed behavior

When a dependency is a gate bead, append a short reason snippet after the title:

```text
DEPENDS ON
  → ○ bh-swcz: Gate: human — review a1b2c3d ● P2
```

Suggested sourcing and truncation rules:

1. **Source**: prefer an explicit `Reason:` line in the gate bead's description (beads
   already writes one via `bd gate create --reason`); otherwise fall back to the first
   non-empty description line.
2. **Truncate**: cap the snippet (e.g. 40–60 chars, first line only) and elide with `…` so
   dep lines stay single-line and scannable.
3. **Scope**: apply only to `type: gate` dependencies — regular dep lines already render a
   meaningful title, so nothing changes for them.
4. **Degrade gracefully**: a gate with no description/reason renders exactly as today.

## Why the data already exists

Tooling that automates beads (in our case, beadhive's agentic git-flow driver) already stamps
a machine-readable reason on every gate it creates: `kickoff <epic>` for epic kickoff gates,
`review <sha>` for review gates, and `security:*` markers for security holds. The
disambiguating data is sitting in the gate bead's description at creation time — `bd show`
just doesn't surface it on the dep line. No schema change is needed; this is a
rendering-layer ask.

## Alternatives considered

- **A `--verbose` flag on `bd show`** that expands gate deps with their descriptions. Works,
  but the default rendering is where the confusion happens — debugging sessions start from
  plain `bd show`, and the extra flag has to be known in advance. A short snippet in the
  default output helps exactly when the user doesn't yet know they need more detail.
- **A first-class `kind`/`reason` field on gate beads** (e.g. `bd gate create --kind review`)
  rendered on dep lines. Cleaner long-term and would make gates queryable by kind, but it's a
  schema addition; the snippet approach delivers most of the value with a rendering-only
  change and stays backward compatible. The two are complementary — a kind field could later
  replace the snippet's fallback heuristic.

Happy to provide more real-world output or test against a branch — thanks for considering.
