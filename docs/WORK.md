# work — the integration-plane driver

`ws work` drives a single bead from **assigned → merged** through the Agentic Git
Flow lifecycle, so an agent (or human) drives the lifecycle through `ws` instead of
improvising raw `git`. It is a thin facade: each verb composes primitives that
already exist — `bd` (Beads), [`ws worktree`](WORKTREES.md), and per-agent identity.

> Raw `git` is for the change **inside** the worktree only — never the lifecycle
> around it (`claim`/`submit`/`merge`). The worktree is already provisioned and the
> branch is already `wt/bead/<id>`; don't `git clone`, `git checkout -b`, or
> `gh pr create`.

## Lifecycle

```text
brief → claim → (work in worktree) → show → refine → check → submit → [review] → resume → submit → [merge]
```

| Verb | What it does |
|---|---|
| `ws work brief <id>` | Print the bead's requirements/goals + the rig's validation command. Read-only. |
| `ws work assign <id> --to crew/<name>` | **Orchestrator-only.** Stamp assignee + provision the worktree with that identity. Leaves status `open`. |
| `ws work claim <id> [--as crew/<name>]` | Worker's ack: re-attach/provision the worktree with your identity + signing, refuse if it's someone else's, then `bd update --claim` (→ `in_progress`). Prints the brief. |
| `ws work show <id> [--view V]… [--json]` | Render the bead branch's local history (`base..wt/bead/<id>`) from several angles to judge noise before submit. Read-only. See [Self-refine](#self-refine-show--refine). |
| `ws work refine <id> (--plan F \| --autosquash \| --since REF) [--dry-run]` | Squash local checkpoint noise into conventional digests behind a backup branch + a byte-identical gate, retaining per-digest author dates. See [Self-refine](#self-refine-show--refine). |
| `ws work check <id>` | Run the rig's `validate_cmd` against the worktree; propagate its exit code. |
| `ws work submit <id>` | Verify clean conventional-digest history, validate the proposed hash from a **clean checkout**, (push for out-of-process review,) set `review:pending` + open a `bd gate`. Handoff, **not** "done" — leaves the worktree intact. |
| `ws work resume <id> [--as …]` | After review returns `changes-requested`: re-attach a fresh worktree on the bead branch, print the feedback, re-assert the claim. Address it and `submit` again. |
| `ws work abandon <id> [--rm]` | Release the claim and record the abandon. `--rm` also removes the worktree. |

Merge is a **separate role** (the Refiner / merge owner) gated by `bd merge-slot` —
not driven by `ws work`. Never push `main` or run the merge yourself.

## Identity & signing

Each worktree gets a git identity stamped at `claim`/`assign`, configured under the
`work.identity` section of `config.yaml` (per-rig override under
`managed_repos[*].work`). Two modes:

- **`agent`** — stamp a distinct author (`user.name` = the crew identity,
  `user.email` = a stable attribution address) plus a dedicated **SSH signing key**
  (`gpg.format ssh`, `user.signingkey`, `commit.gpgsign`).
- **`supervised`** (or no `identity` block) — change nothing; the worktree inherits
  the human's existing git + signing config. This is the "under my keys, with direct
  supervision" mode.

The crew identity resolves: `--as <name>` → `work.identity.name` → `$WS_CREW` →
git `user.name`. The same name is passed to `bd --actor` so the claim/assign audit
trail is per-agent.

## Configuration

```yaml
work:
  validate_cmd: "just check"     # default validation for any boundary without an override
  validation: relaxed            # merge re-test depth: relaxed | conservative | loose (see below)
  validate:                      # optional per-boundary overrides (fall back to validate_cmd).
                                 # a `<phase>-main` key wins when the op targets the integration branch.
    molecule: "just check-all"   #   mol→main pre-land: the full (unit+integration) gate
    merge-main: "just check-all" #   ad-hoc bead → main: the full gate (plain merge→mol stays fast)
  review_gate: "human"           # gate at submit: human | timer | gh:run | gh:pr
  integration_branch: "main"     # base the bead branch is measured against
  max_commits: 10                # submit rejects more commits than this over base
  identity:
    mode: agent                  # agent | supervised
    name: "crew/claude"
    email: "agents@example.dev"
    signing_key: "~/.config/ws/keys/claude.pub"
    sign: true
```

`submit` only **pushes** the branch when `review_gate` is `gh:run`/`gh:pr` (CI must
see it); a purely local reviewer sharing the object store needs no push.

## Self-refine: `show` + `refine`

`submit` rejects a branch with more than `max_commits` commits over the integration
branch, or any non-conventional subject. `show` + `refine` are the worker-side,
**pre-`submit`** tools to get there — squash local checkpoint noise into a few
conventional digests. This is a *worker* step: the Refiner still merges with
`--no-ff` and never squashes at the integration boundary (tiered retention).

Both act on the range `base..wt/bead/<id>`, where `base = git merge-base
<integration_branch> wt/bead/<id>`.

### `show` — read the history before you rewrite it

```bash
ws work show <id>                 # default `log` view
ws work show <id> --view sig --view stat
ws work show <id> --json          # machine input for building a refine plan
```

| View | Shows |
|---|---|
| `log` (default) | one line per commit + noise flags; header counts commits / flagged / `max_commits`. |
| `sig` | author, email, and verified-signature glyph (`✔`/`~`/`✗`/`·`) per commit. |
| `diff` | `git diff base..tip` — the *net* change the whole branch produces. |
| `stat` | files ranked by how many commits touched them (hotspots ≈ fold-able noise). |

Noise flags are **signals, not decisions** (no semantic grouping without a human/agent):

- `marker` — a `fixup!`/`squash!` commit.
- `fixup→<short>` — this commit's files are a subset of an earlier commit's; that
  earlier commit is the likely fold target.
- `run` — shares a conventional `type(scope)` with the previous commit.

`--json` emits `{"base", "max_commits", "commits": [<row + flags>…]}` — the agent
reads this and writes a squash plan.

### `refine` — squash behind a safety net

Exactly one input mode:

```bash
ws work refine <id> --plan plan.json     # explicit squash plan (or `-` for stdin)
ws work refine <id> --autosquash         # fold fixup!/squash! into their targets
ws work refine <id> --since <ref>        # fold <ref>..tip into one digest
ws work refine <id> --plan plan.json --dry-run   # print the would-be log; change nothing
```

The wrapper is the point: it creates a **backup branch** (`wt/bead/<id>.refine-<ts>`),
runs the squash as a non-interactive `rebase`, then enforces a **byte-identical gate**
(`git diff --quiet backup tip`). If the rebase conflicts or the gate fails, it aborts
and hard-resets back to the backup — your work is never lost. On success it leaves the
backup in place (delete it once satisfied) and prints the restore one-liner.

**Squash-plan schema** (sparse — commits in no group pass through unchanged):

```jsonc
{ "base": "<optional ref override; default merge-base>",
  "groups": [
    { "keep": "<hash>",                 // retained commit: identity + (default) author date
      "fold": ["<hash>", "…"],          // folded into keep (changes kept, messages dropped)
      "subject": "<optional override>", // omit → keep's subject
      "body": "<optional>",             // omit → bullet list of folded subjects
      "date": "keep|last|<iso-8601>" }  // default "keep"
  ] }
```

One group = refine-as-you-go; N groups = end-of-branch cleanup.

**Timestamps are retained.** Squashing N→1 collapses N timestamps to one per digest,
but each digest keeps its `keep`'s author **identity and author date**, so the timeline
stays spread (not stamped "now"). `date: "last"` or an explicit ISO overrides it.

**Refine as you go** (recommended): `git commit --fixup=<target>` during work, then
`ws work refine <id> --autosquash` — the folds are contiguous, so the rebase is
conflict-free and per-digest dates reflect real cadence.

## Molecule integration branch (two-level)

When a molecule is kicked off, `ws plan approve` creates a `mol/<epic>` branch off the
integration branch. Bead worktrees in that molecule fork off `mol/<epic>` instead of `main`,
so intra-molecule dependencies compose correctly — bead B sees bead A's already-merged work.

`ws work merge <bead>` lands each bead into `mol/<epic>` (not `main`). When all beads are
merged, the coordinator runs the wrap-up verb:

```bash
ws work merge <epic> --molecule [--rm]
```

This verb:

1. Guards that the molecule is complete — all child beads are closed.
2. Validates the assembled `mol/<epic>` branch with the rig's `validate_cmd` (skipped under `loose`).
3. Lands `mol/<epic>` onto the integration branch as one `--no-ff` merge bubble.
4. Closes the epic + swarm and deletes `mol/<epic>`.

The result is a single merge bubble on the integration branch containing all of the
molecule's bead merges — `main` stays untouched and always-green until the whole molecule
is ready. See [PLANNING-PLANE.md](PLANNING-PLANE.md) for how kickoff creates the branch.

**Backward-compatible:** a bead whose epic has no `mol/<epic>` branch (older molecules or
beads filed outside a molecule) still targets the rig integration branch unchanged.

### Validation modes — keeping the integration branch green

`work.validation` tunes how aggressively the integration tip is re-tested across merge
boundaries. Each bead is always validated in isolation at `submit`; the modes govern the
*combination*:

| Mode | Per-bead merge into `mol/<epic>` | Assembled `mol/<epic>` pre-land | Post-land `main` tip |
| --- | --- | --- | --- |
| `relaxed` (default) | — | re-validated | only if `main` moved (staleness backstop) |
| `conservative` | re-validated after every merge | re-validated | re-validated |
| `loose` | — | skipped (trusts submits) | skipped (warns if `main` moved) |

An **ad-hoc bead** (no molecule) merges straight into `main`. That land is itself a main-merge
gate, so — in *every* mode except `loose` — it always gets a final re-validation of the post-merge
`main` tip (the `on_main` rule), which also covers a `main` that moved under the bead. A bead
merging into its `mol/<epic>` does not (the mol→main land is its backstop).

Two properties hold regardless of mode:

- **Always-green rollback — for branches safe to rewrite.** If a re-validation fails, the
  integration tip is reset to its pre-merge sha *while the merge slot is still held* — no broken
  tip is observable — **but only when the branch is safe to rewrite**: a private `mol/<epic>`
  branch, or an integration branch with no upstream (unpushed). A per-bead combined-state failure
  also bounces the bead to `review=changes-requested`. A **shared (pushed)** integration branch is
  never rewritten — the land was intentional; ws escalates loudly and leaves the bubble for a
  **forward fix** (revert or follow-up), with the epic left open. The source branch is always
  preserved either way.
- **Staleness backstop.** If `main` advanced since the molecule was cut, the `--no-ff` land
  combines validated-mol content with newer-main content — a tree that was never validated.
  `relaxed` and `conservative` re-validate that landed tip even though `relaxed` skips post-land
  re-tests otherwise; on red the safe-to-rewrite rule above applies (roll back if unpushed, else
  escalate to forward-fix). The cost is paid only when `main` actually moved.

**Tiered test commands.** Each boundary's command is overridable per-point via
`work.validate.<phase>` (phases: `submit`, `merge`, `molecule`, `postland`, `union`). A
`<phase>-main` key is preferred when the operation targets the integration branch — so an ad-hoc
bead's merge resolves `merge-main` while a molecule member's merge into `mol/<epic>` resolves the
plain `merge`. The `just` recipes provide the two tiers: `just check` (lint + unit — the fast
default) and `just check-all` (lint + unit + the real-`bd` integration harness). The recommended
wiring runs integration **only at the two main-merge gates** and keeps everything else fast:

```yaml
work:
  validate_cmd: "just check"       # fast default everywhere
  validate:
    molecule: "just check-all"     # mol→main pre-land
    merge-main: "just check-all"   # ad-hoc bead → main
```

`postland` (fires only when `main` moved) and intermediate bead→`mol/<epic>` merges stay on the
fast `just check`; bump them to `just check-all` if integration-level conflicts start surfacing.

## Conflict handling in `ws work merge`

`ws work merge` resolves merge conflicts through a four-tier ladder — each tier is tried
in order; the first to succeed wins. If all fail the bead is **bounced** for rework; the
bead branch is always restored from a backup ref so no work is lost.

| Tier | What happens |
|---|---|
| **clean** | `--no-ff` merge lands without conflicts — done. |
| **rebased** | The bead branch is rebased onto the current integration tip and the merge is retried. Resolves conflicts caused by trivially diverged history (see). |
| **union** | Path-scoped auto-resolution via git's built-in `union` driver — see below. |
| **bounce** | All automatic tiers failed. `merge` exits non-zero, the bead branch is restored, and the bead is bounced for manual rework. |

### Union tier

Union is a **bounded** auto-resolution strategy: when two branches both append lines to
the same region, git's `union` driver keeps both sets. This is safe for append-only files
such as changelogs, `*.jsonl` ledgers, and registry/list files. It is **unsafe for
arbitrary source code, configuration, or schema files** — do not add those to the
whitelist.

The tier is controlled by `work.conflict.union_globs`, a list of fnmatch globs (default
`[]`, which **disables union** and makes merge behaviour identical to the pre-union
baseline). Per-rig override: `managed_repos[*].work.conflict.union_globs`.

```yaml
work:
  conflict:
    union_globs:
      - "CHANGELOG.md"
      - "*.jsonl"
      - "registry/*.txt"
```

Good candidates: `CHANGELOG.md`, `*.jsonl` ledger files, flat registry/list files.
Unsafe candidates: source files, YAML config, JSON schema — leave those out so conflicts
surface for a human.

**Two safety guarantees — union never lands broken code:**

1. **Path-scoped.** Union applies only when *every* conflicted path matches at least one
   glob. A single out-of-whitelist path skips union entirely and falls straight to the
   bounce.
2. **Validation-gated.** After a successful union merge the rig's `validate_cmd` is
   re-run from a clean checkout. On failure, the integration branch is hard-reset to its
   pre-union tip and the bead branch is restored from its backup ref before bouncing —
   the broken result is never committed.

## Batch groups — when not to batch

The default unit is one bead per worktree, and **that is the right call whenever beads are
independent**: separate worktrees give you parallel wall-time and each bead lands on its own
clean conventional history. A *work group* batches several beads into one shared
`wt/batch/<group>` worktree, validated and merged **once** as a single `--no-ff` bubble. The
claim/implement/merge mechanics are in the `developer` skill; the scheduling decision (three
triggers, four guards) is in the `coordinator` skill. This section is the safety
reference — when batching is wrong and why.

### Guards — when a candidate is not batched

Any guard failure drops the candidate back to singletons. The guards exist because a batch
fails **as a unit** — there is no partial landing.

| Guard | Rule |
|---|---|
| **Cohesion** | Members must share a `component` or be contiguous in the dep DAG. A scattered group is hard to review and fails together. |
| **Size cap** | At most `work.batch_max_size` (default 5) members. Keeps the merge bubble small enough to review and bisect. |
| **Single model tier** | A group runs on one model; explicit `model:` conflicts are refused (members may omit `model` to inherit). |
| **No mixed review gates** | Members must share a review gate; mixing `gate:` overrides is refused so one approval covers the whole bubble. |

Planner-declared `batch:<group>` groups are validated against these rules at `ws plan file`
time (see [PLANNING-PLANE.md](PLANNING-PLANE.md)). Auto-detected linear chains are
re-validated by the scheduler at dispatch time (`ws work schedule`).

### Blast radius

A batch fails and bounces **as a unit**:

- If `merge --group` validation fails, no member lands — the whole group bounces for rework.
- If review returns `changes-requested`, every member stays open; the developer must
  address feedback and resubmit the whole group together.
- There is no way to land half a batch; the `--no-ff` merge is all-or-nothing.

This is the price of one merge/validate instead of N. Keep groups small and cohesive so the
blast radius stays acceptable — the size cap of 5 exists for exactly this reason. If
mid-point testability matters (you need to validate bead A before starting bead B), stay
with singletons even when the chain is linear.

### Lossless guarantee

Although members merge together, per-bead commits are preserved **inside** the `--no-ff`
bubble — the batch branch tip is the merge parent, never a squash. `git bisect` can still
reach individual bead commits. The history budget is relaxed to `max_commits × members` at
`merge --group` time to accommodate several beads' worth of commits on one branch.

### Cost trade-off

| | Batch | Singleton (default) |
|---|---|---|
| **Merges / validates** | 1 for N beads | N (one per bead) |
| **Wall-time** | Serial (one agent implements in order) | Parallel (N agents run concurrently) |
| **Failure blast radius** | Whole group bounces on any failure | Only the failing bead bounces |
| **Bisect granularity** | Per-bead commits preserved inside the bubble | Per-bead branch |
| **Review scope** | One gate covers all members | One gate per bead |

Batch wins when wall-time parallelism does not matter (a linear chain cannot be parallelized
anyway) or when validation cost dominates (expensive integration-test setup amortized once).
Stay with singletons when beads are independent and cheap-to-validate — parallel wall-time
is then the dominant win and per-bead isolation makes failures cheap.

## Not yet wired

- Commit-trailer auto-injection (`Agent-Profile`/`Agent-Session`/`Agent-Model`) —
  needs a per-worktree `prepare-commit-msg` hook; config schema first, hook later.
- The merge-owner/Refiner role and the release-plane `cz check` hook are out of
  scope here (see the AGF design).
