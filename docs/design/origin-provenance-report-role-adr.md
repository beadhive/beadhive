# ADR: `origin:` is provenance; the intake disposition and the binding carry the report role

- **Status:** Proposed (operator review before merge)
- **Date:** 2026-11-12
- **Decision owner:** Beadhive maintainers
- **Related work:** `bh-l1m3p` (this decision), `bh-c5nhv` (the narrow `finish` symptom, absorbed
  here), `bh-iv61r` (backfill create mode, blocked on this), D1 of
  [`backfill-history-pipeline-adr.md`](backfill-history-pipeline-adr.md) (`origin:backfill`
  stays on created beads), D-a and D-c of
  [`external-intake-module-adr.md`](external-intake-module-adr.md) (a bound report is never work)
- **Grounded in:** main `76bfee82`.

## Context

The `origin:` label had two meanings that disagree once intake hands a bead over to work.

1. **Provenance.** The intake vocabulary defines `origin:<channel>` as the closed channel a bead
   entered through: a durable source tag, orthogonal to the `intake:` queue state, which triage
   moves.
2. **Role.** `adopt.is_origin_report` treated any child with an `origin:` label (or
   `intake:promoted`) as an *adopted origin report*: provenance hung under an epic that carries no
   acceptance, demands no kickoff gate and never gates the land.

A plain `bh work accept <report>` keeps the report itself as the work bead (`intake:accepted`,
`origin:report` kept). Once such a bead is parented under an epic, every role consumer drops it.
`finish` refused `bh-69snf` because its bubble named a "non-child" (`bh-c5nhv`). The completeness
guard would also have let the epic land with that child still open. Backfill create mode
(`bh-iv61r`) writes `origin:backfill` on purpose (D1), so every bead it creates would be dropped
the same way.

## Decision

**`origin:` is provenance only.** No caller decides a bead's role from it. Any value, including
`origin:backfill`, says which channel the bead came in through and nothing else. Work children may
carry it, and `bh plan verify` no longer reports it as a state label on work.

**The role of an epic child is decided by markers the intake flow already writes:** its binding
and its `intake:` disposition. `adopt.report_role(bead)` is the one classifier:

| Child carries | Role | Work child? |
|---|---|---|
| the `intake.source` metadata document (a bound report) | `tracking` | no (D-a: a bound report is never work) |
| `intake:promoted` | `adopted` (adopt-path provenance) | no |
| `intake:rejected` or `intake:rerouted` | `declined` | no |
| `intake:untriaged` or `intake:needs-info` | `unfielded` (ambiguous) | no, and `plan verify` fails |
| `intake:accepted`, unbound (accept in place) | none | **yes** |
| no `intake:` value (any or no `origin:`) | none | **yes** |

A bound report wins over its `intake:` value. An accepted bound report tracks separate work through
`accept --work` / `--new-work`. It is never the work itself, even when someone parents it under an
epic by hand.

**Accept-in-place beads may be epic work children.** A plain accept says "this report *is* the
work". Parenting it under an epic is a valid shape. It counts for history policy, completeness (an
open one blocks `finish`) and `plan verify` (it needs acceptance, the identity triplet and a kickoff
gate when it is a root).

**An unfielded report under an epic is ambiguous, and `plan verify` reports it before kickoff.** A
report nobody triaged could be provenance or work. Verify refuses it with the remedy: run
`bh work promote <id>` to keep it as adopted provenance, or `bh work accept <id>` to make it work.
Until then, history and completeness exclude it, exactly as before, so nothing changes silently.

### Callers, classified

| Caller | Meaning it needs | After this change |
|---|---|---|
| `work_logic._direct_work_children` (behind `epic_history_policy`) | role | `adopt.is_report_child(row)` |
| `work_merge.impl__guard_molecule_children` (completeness) | role | `adopt.is_report_child(row)` |
| `work_merge.impl__close_land_origin_reports` (close on land) | role | `adopt.is_report_child(row)` |
| `plan._epic_molecule` (shared by `plan show`, `verify`, `check`, `repair`) | role | `adopt.report_role(row)`, which also feeds the ambiguity check |
| `plan._check_child_labels` (state labels on work) | role (the old ban) | `origin:` allowed; `kickoff:` still banned on leaf work |
| `plan._origin_report_card` (`plan show` card) | provenance | still `state.channel_of`, and the card now shows the role |
| `adopt.is_bound_report`, `_provenance`, `frame_from_beads` | provenance | unchanged |
| `triage`, `work intake`, `hq intake`, `report`, `escalate` (`origin_of`, `channel_of`, `is_report_origin`, `is_escalation_origin`) | provenance | unchanged |
| `backfill_bd.ORIGIN_LABEL` (`origin:backfill`) | provenance | unchanged; a backfilled child is work |
| `state.py` / `modules/intake/domain/vocabulary.py` | vocabulary | docstrings now say `origin:` never decides the role |

`adopt.is_origin_report(labels)` is removed. It was the only role reader of `origin:`, and a
labels-only signature cannot see the binding.

### Data compatibility

No migration is needed. Each role rests on a marker that existing beads already carry:

- Every report the adopt path filed carries `intake:promoted`. `bh plan adopt` has refused an
  unpromoted bead since the first release (`be616f74`). These reports stay excluded.
- Every tracking report is bound. `link` refuses an unbound report (`NOT_A_REPORT`), so these
  stay excluded.
- Hand-assembled molecules with untriaged reports as provenance (the `bh-er55` shape) stay
  excluded from history and completeness. `plan verify` now names each one as ambiguous instead of
  passing.
- Only two kinds of child change role. The first is accepted in place, which was wrongly excluded
  (`bh-c5nhv`). The second has an `origin:` label and no `intake:` value, such as backfill or a
  hand-labelled bead. Neither shape can come from the adopt or link paths.

## Alternatives rejected

- **A new role label (`intake:adopted`, `report:tracking`).** The intake disposition and the
  binding already say this. A new label would need writing at promote and link, plus a backfill
  migration for every existing adopted report. Until that migration ran, existing reports would be
  reclassified as work without anyone noticing.
- **Key the role on the `tracks` link.** `tracks` runs from a report to separate work, so it marks
  a report that is not under the epic. Adopted reports are linked `parent-child`, not `tracks`.
  Reading the edge also costs one dependency query per child on the hot `finish` path.
- **Make accept-in-place invalid for epic work** (accept always mints separate work, and parenting
  a report under an epic is refused). This breaks the one-bead plain accept that triage relies on
  for native reports. It also leaves `origin:backfill` children misclassified, and it needs a
  write-side guard on every `bd dep add` path, including raw `bh bd`.
- **Treat every unfielded child as work.** That would flip existing hand-assembled provenance
  children without warning, and an open one would suddenly block `finish`. Failing verify instead
  makes the operator decide once, before kickoff.
- **Warn instead of failing verify on an ambiguous child.** A warning is easy to miss, and the
  child's role decides whether the land waits for it. Verify is the pre-kickoff gate, and the
  remedy is a single triage verb.

## Consequences

- `bh work finish` accepts an epic with an accepted-in-place child (`bh-c5nhv`). An open one blocks
  the land.
- Backfill create mode can keep `origin:backfill` (D1).
- `plan verify` fails on a molecule that hangs untriaged reports under the epic. The fix is one
  triage verb per report, and `plan repair` still never gates or labels them.
