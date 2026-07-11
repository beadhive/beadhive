---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: apply
  title: Apply the approved proposal — stamp links and file NEW beads
  requires: [emit-proposal]
  performer: either
  action:
    type: script
    script: scripts/reconcile.sh
    timeout_seconds: 300
  verify:
    type: script
    script: scripts/reconcile.sh
    args: ["--verify"]
    success_exit: 0
  on_failure:
    - reason: stamp-failed
      strategy: ask
    - reason: label-validate-failed
      strategy: ask
  effect: reversible
  estimated_duration_minutes: 4
  tags: [apply, write]
---

# What this step does

Writes the approved proposal into the rig:

- **Stamps:** `scripts/reconcile.sh <rig-path> --apply` runs `bd update <bead> --external-ref
  <doc>` for every `PRESENT-needs-stamp` row. Update is safe with plain `bd`: it targets beads
  that already carry the rig triplet and never strips labels, so `bh labels validate` stays green.
- **NEW beads (if any):** filed from the classify step's list, each with `--external-ref`,
  `--label origin:backfill`, `--label source:<kind>`, and `--status closed` where history says
  done. **Create through `bh bd create`** (run inside the rig): it injects the rig's
  `provider:/org:/repo:` triplet — which plain `bd create` omits and which `bh labels validate`
  requires. This is the one place the tool's generic `bd` is not enough.
  - A **handful** → one `bh bd create` per bead (agent performs these; they are judgment items).
  - **Bulk** (an empty/import rig with many NEW-with-deps, e.g. a GSD `.planning` tree) → do not
    hand-create dozens with dependency edges. Emit one JSONL (all beads + deps + `external_ref` +
    status **+ the rig triplet labels** — `bd import` upserts raw and does *not* inject the triplet
    the way `bh bd create` does, so the emitter must include it) and upsert with **`bh bd import`**
    — idempotent by `external_ref`. That emitter is not built yet (parked importer mapper); until
    it lands, bulk apply is manual `bh bd create` calls.

**Wiring the phase→plan hierarchy (`--planning` source).** bd does **not** infer the epic→child
link from the dotted id (`hl-ph10` ↔ `hl-ph10.1`) — the dotted id is only a name. The link is an
explicit **`parent-child` dependency edge on the child**. So every dotted plan issue in the import
JSONL needs, alongside any `blocks` edges, a
`{"issue_id":"hl-ph10.1","depends_on_id":"hl-ph10","type":"parent-child"}` — otherwise the epic
shows no CHILDREN. `reconcile.sh --planning` names the parent in the `parent:<name>` column but does
not emit the edge; the emitter adds it. (`depends_on:` frontmatter → `blocks` edges are surfaced by
`deps_of()`, which now reads block-style lists too — verify with `reconcile.sh --selftest`.)

Then `bh labels validate` must be green in the rig.

# Verification (script)

`scripts/reconcile.sh <rig-path> --verify` exits 0 only when no `PRESENT-needs-stamp` rows
remain — i.e. every doc-backed bead is now linked. A non-zero exit means a stamp did not take;
do not proceed.

# Failure shape

| Reason | Strategy | Next |
|---|---|---|
| `stamp-failed` | `ask` | A `bd update` errored; human inspects and retries or skips the row |
| `label-validate-failed` | `ask` | A NEW bead is missing the triplet/registry labels; fix labels, re-validate |
| (any other) | discovered friction | Runtime captures the unknown failure |

# Notes

`effect: reversible` — stamps and backfilled beads can be un-set / closed-out if a mistake is
found. Prefer fixing forward (re-stamp, re-label) over deleting history.
