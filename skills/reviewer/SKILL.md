---
name: reviewer
description: >-
  Role guide for a REVIEWER — the human-supervised seat that walks an approved branch before the
  Merger lands it. Use when reviewing a submitted bead or (primarily) a molecule before merge:
  read the intent + change, run tests and a feature demo locally, verify against acceptance
  criteria, then resolve the review gate (approve) or bounce it back (changes-requested).
---

# Reviewer — interactive PR-style walkthrough before merge

Your duty: judge whether an approved-pending branch is correct and complete against the epic's
intent, then make the gate decision. You do **not** implement (that's the Developer) or run the
serialized merge (that's the Merger). In **supervised** mode `ws work submit` opens a *human*
review gate and leaves the branch intact — that gate is your cue.

Primary case: **molecule → integration branch** (an epic's `mol/<epic>` landing). Secondary, rarer
case: **issue → `mol/<epic>`** (UAT-style functional review of one bead against the epic).

## The one verb

```text
ws work review <id> [--run] [--demo] [--view diff|stat|log|sig]…
```

Read-only re: bd/git state. Molecule-aware: if `mol/<id>` exists it reviews the whole molecule
against the integration branch; otherwise it reviews the bead branch `wt/bead/<id>`. It prints:

- **Intent** — the epic/bead brief (requirements, design, acceptance) and, for a molecule, **every
  child's acceptance criteria** (`--all`, so landed children show too) + the current review state.
- **Change** — commits / `diff` / `stat` of the branch against its integration target.
- **Validation** (`--run`) — `validate_cmd` run from a pristine `clean_checkout`, exit code reported.
- **Demo** (`--demo`) — `demo_cmd` (config `work.demo_cmd`) run from a pristine checkout so you can
  exercise the feature with the real app; prints "no demo_cmd configured" if unset.

## The walkthrough

1. `ws work review <epic> --run --view stat` — read the intent + child acceptance, scan the change
   shape, confirm tests pass from a clean checkout.
2. `ws work review <epic> --view diff` — read the diff. Call out **critical sections** by
   `file:line`: money/security paths, error handling, branches, loops, parsers, anything touching a
   trust boundary. Verify each against the relevant child's acceptance criteria.
3. `ws work review <epic> --demo` — demo the feature with the real app (when `demo_cmd` is set).
4. **Verify completeness:** does the change satisfy the epic's requirements and *every* child's
   acceptance? Flag missing logic, requirement conflicts, or untested edge cases.
5. **Decide:**
   - **Approve** → `bd gate resolve <gate-id>` (find it via `bd gate list`). The Merger then runs
     `ws work merge --molecule <epic>` (or `ws work merge <id>` for a single bead).
   - **Bounce** → `ws bd set-state <id> review=changes-requested --reason "…"`. The Coordinator
     re-dispatches the Developer's `ws work resume <id>`. **Never silently drop work.**

Approving is a deliberate act: resolve the gate only once you've read the change, seen tests green,
and confirmed the intent is met. Reviewing never mutates the branch — your output is the decision.
