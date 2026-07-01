---
name: work
description: >-
  Reference for the `ws work` verbs that drive a bead through its lifecycle (brief,
  assign, claim, show, refine, check, submit, resume, abandon) — verb mechanics, flags,
  and behavior.
  Load this when following a role skill (developer / coordinator / merger) or taking any
  `ws work` step, instead of improvising the lifecycle with raw `bd` / `git`.
---

# ws work — verb reference

`ws work` drives a bead **assigned → merged**, composing `bd` + ws worktrees + identity.
It applies this repo's config defaults (validation command, review gate, identity +
signing) so you don't pass them by hand. Raw `git` is for the change **inside** the
worktree only — never the lifecycle around it.

| Verb | Does |
|---|---|
| `ws work brief <id>` | Print requirements/goals + the validation command. Read-only. |
| `ws work ready [--json] [--gated]` | List ready (unblocked, dependency-ordered) work — first-class `bd ready`, output byte/JSON-shape stable. Extra bd flags forward through. Read-only. |
| `ws work issue <id> [--json]` | Show one issue's fields (labels, `model:`/`harness:`) — first-class `bd show <id>`. Read-only. |
| `ws work list [--status <state>] [--json]` | List / filter issues — first-class `bd list`. Read-only. |
| `ws work start <epic> --as coord/<name>` | Coordinator, epic-only: guard epic + `kickoff=approved` + coordinator seat, open `mol/<epic>` (integration-plane kickoff), mark the epic in_progress. Alias of `claim` for an epic. |
| `ws work assign <id> --to <name>` | Orchestrator-only: stamp assignee + provision the worktree with that identity. Leaves status `open`. Seat-typed: epic → `coord/<name>`, else `crew/<name>`. |
| `ws work claim <id> [--as <name>]` | Worker ack: re-attach/provision the worktree with identity + signing, refuse if it's another actor's or the wrong seat, then `bd update --claim` (→ in_progress). |
| `ws work finish <epic>` | Merge-owner, epic-only: land the assembled `mol/<epic>` as one `--no-ff` bubble, close the epic, delete the branch. Alias of `ws work merge <epic> --molecule`. |
| `ws work show <id> [--view log\|sig\|diff\|stat]… [--json]` | Read-only: render the bead branch's local history (`base..wt/bead/<id>`) to judge noise before submit. `--json` is the machine input for a refine plan. |
| `ws work refine <id> (--plan F \| --autosquash \| --since REF) [--dry-run]` | Squash local checkpoint noise into conventional digests behind a backup branch + a byte-identical gate, retaining per-digest author dates. Worker-side, pre-submit. |
| `ws work check <id>` | Run the rig's validation against the worktree; propagate its exit code. |
| `ws work submit <id>` | Verify clean conventional-digest history, validate from a clean checkout, (push if review is out-of-process,) set `review:pending` + open a `bd gate`. Handoff, not "done". |
| `ws work resume <id>` | After changes-requested: re-attach a fresh worktree on the bead branch, print feedback, re-assert the claim. |
| `ws work abandon <id> [--rm]` | Release the claim and record the abandon; `--rm` also removes the worktree. |

## Key behaviors

- The durable artifact is the **`wt/bead/<id>` branch**, not the worktree directory — the
  directory may be reclaimed after `submit` and re-provisioned on `resume`.
- **Identity** resolves `--as` > config `work.identity.name` > `$WS_CREW` > git. `agent`
  mode stamps a distinct author + SSH signing (worktree-scoped, so concurrent agents don't
  clobber each other); `supervised` mode inherits your existing git config.
- `submit` pushes the branch only when the review gate is `gh:run` / `gh:pr`.
- `submit` rejects noisy history (more than `max_commits` over base, or non-conventional
  subjects) — `show` + `refine` are how you get under the bar. `refine` is a pure rewrite
  (byte-identical net tree, enforced); on conflict or gate failure it restores from the
  backup branch, so work is never lost. **Refine-as-you-go**: `git commit --fixup=<target>`
  while working, then `ws work refine <id> --autosquash` (contiguous → conflict-free).
- Tiered retention: `refine` squashes **local checkpoints only**, worker-side. The Merger
  still merges `--no-ff` and never squashes at the integration boundary.
- `claim` / `assign` / `resume` are idempotent and refuse a bead assigned to another actor.
- Defaults come from the `work` config section (per-rig overridable).

## More `ws`

- `ws work <verb> --help` for flags; `ws --help` for the full CLI.
- Per-rig defaults live in the `work` config section — `ws config path` shows where.
- Role duties: the `developer`, `coordinator`, and `merger` skills.
