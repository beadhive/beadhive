---
name: work
description: >-
  Reference for the `bh work` verbs that drive a bead through its lifecycle (brief,
  assign, claim, show, refine, check, submit, resume, abandon) — verb mechanics, flags,
  and behavior.
  Load this when following a role skill (developer / coordinator / merger) or taking any
  `bh work` step, instead of improvising the lifecycle with raw `bd` / `git`.
---

# bh work — verb reference

`bh work` drives a bead **assigned → merged**, composing `bd` + bh worktrees + identity.
It applies this repo's config defaults (validation command, review gate, identity +
signing) so you don't pass them by hand. Raw `git` is for the change **inside** the
worktree only — never the lifecycle around it.

| Verb | Does |
|---|---|
| `bh work brief <id>` | Print requirements/goals + the validation command. Read-only. |
| `bh work ready [--json] [--gated]` | List ready (unblocked, dependency-ordered) work — first-class `bd ready`, output byte/JSON-shape stable. Extra bd flags forward through. Read-only. |
| `bh work issue <id> [--json]` | Show one issue's fields (labels, `model:`/`harness:`) — first-class `bd show <id>`. Read-only. |
| `bh work list [--status <state>] [--json]` | List / filter issues — first-class `bd list`. Read-only. |
| `bh work start <epic> --as coord/<name>` | Coordinator, epic-only: guard epic + `kickoff=approved` + coordinator seat, provision the seat worktree on the container branch `wt/bead/epic/<epic>` (forked off `integration_base`; integration-plane kickoff), mark the epic in_progress. Alias of `claim` for an epic. |
| `bh work assign <id> --to <name>` | Orchestrator-only: stamp assignee + provision the worktree with that identity. Leaves status `open`. Seat-typed: epic → `coord/<name>`, else `crew/<name>`. |
| `bh work claim <id> [--as <name>]` | Worker ack: re-attach/provision the worktree with identity + signing, refuse if it's another actor's or the wrong seat, then `bd update --claim` (→ in_progress). |
| `bh work finish <epic>` | Merge-owner, epic-only: land the assembled container `wt/bead/epic/<epic>` **up one level** (onto `integration_base` — `main` for a top-level epic, the workstream container for a nested one) as one `--no-ff` bubble, close the epic, tear down the seat + delete the branch. Alias of `bh work merge <epic> --molecule`. |
| `bh work show <id> [--view log\|sig\|diff\|stat]… [--json]` | Read-only: render the bead branch's local history (`base..wt/bead/<type>/<id>`) to judge noise before submit. `--json` is the machine input for a refine plan. |
| `bh work refine <id> (--plan F \| --autosquash \| --since REF) [--dry-run]` | Squash local checkpoint noise into conventional digests behind a backup branch + a byte-identical gate, retaining per-digest author dates. Worker-side, pre-submit. |
| `bh work check <id>` | Run the rig's validation against the worktree; propagate its exit code. |
| `bh work submit <id>` | Verify clean conventional-digest history, validate from a clean checkout, (push if review is out-of-process,) set `review:pending` + open a `bd gate`. Handoff, not "done". |
| `bh work approve <id> [--as <name>]` | Reviewer/coordinator: resolve a submitted bead's HUMAN review gate through the convention layer (attributes the actor, wraps `bd gate resolve` — **no `WS_BD_PASS_ENABLED`**). Refuses a non-review or out-of-process `gh:*` gate. |
| `bh work resume <id>` | After changes-requested: re-attach a fresh worktree on the bead branch, print feedback, re-assert the claim. |
| `bh work abandon <id> [--rm]` | Release the claim and record the abandon; `--rm` also removes the worktree. |

## Intake and escalation verbs

These verbs are used by the coordinator (rig-level triage) and the superintendent (fleet-wide
routing). The developer uses `bh escalate` (a top-level `bh` verb, not a `bh work` subcommand).

| Verb | Does |
|---|---|
| `bh escalate '<msg>'` | Fire-and-forget escalation to HQ: files an `intake:untriaged` item with `origin:escalation`. Developer bottom-rung; non-blocking. Requires `bh hq init`. |
| `bh work intake [--source <channel>]` | List this rig's untriaged intake queue (source-agnostic). `--source report\|github\|import` narrows by channel. Coordinator read-only surface. |
| `bh work accept <id> [--type T] [--priority P]` | Accept an intake report: set type/priority (both optional) and clear `intake` → backlog. |
| `bh work reject <id> --reason "…"` | Close a report with a reporter-visible reason. |
| `bh work reroute <id> --to <rig>` | Re-file a mis-routed report into the right rig. `--super <seat>` bounces an ambiguous item to the superintendent (stays in the fleet-wide inbox). |
| `bh work promote <id>` | Hand a feature/epic-shaped report to the planner (`intake:promoted`). |
| `bh hq intake` | Superintendent's fleet-wide inbox: all `intake:untriaged` items across every rig. |

## Key behaviors

- The durable artifact is the **`wt/bead/<type>/<id>` branch** (`<type>` ∈ `epic` | `issue`), not
  the worktree directory — the directory may be reclaimed after `submit` and re-provisioned on
  `resume`.
- **Identity** resolves `--as` > config `work.identity.name` > `$WS_CREW` > git. `agent`
  mode stamps a distinct author + SSH signing (worktree-scoped, so concurrent agents don't
  clobber each other); `supervised` mode inherits your existing git config.
- `submit` pushes the branch only when the review gate is `gh:run` / `gh:pr`.
- `submit` rejects noisy history (more than `max_commits` over base, or non-conventional
  subjects) — `show` + `refine` are how you get under the bar. `refine` is a pure rewrite
  (byte-identical net tree, enforced); on conflict or gate failure it restores from the
  backup branch, so work is never lost. **Refine-as-you-go**: `git commit --fixup=<target>`
  while working, then `bh work refine <id> --autosquash` (contiguous → conflict-free).
- Tiered retention: `refine` squashes **local checkpoints only**, worker-side. The Merger
  still merges `--no-ff` and never squashes at the integration boundary.
- `claim` / `assign` / `resume` are idempotent and refuse a bead assigned to another actor.
- Defaults come from the `work` config section (per-rig overridable).

## More `bh`

- `bh work <verb> --help` for flags; `bh --help` for the full CLI.
- Per-rig defaults live in the `work` config section — `bh config path` shows where.
- Role duties: the `developer`, `coordinator`, and `merger` skills.
