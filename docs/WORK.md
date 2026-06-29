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
  validate_cmd: "just check"     # check/submit validation (cwd = worktree)
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
2. Validates the assembled `mol/<epic>` branch with the rig's `validate_cmd`.
3. Lands `mol/<epic>` onto the integration branch as one `--no-ff` merge bubble.
4. Closes the epic + swarm and deletes `mol/<epic>`.

The result is a single merge bubble on the integration branch containing all of the
molecule's bead merges — `main` stays untouched and always-green until the whole molecule
is ready. See [PLANNING-PLANE.md](PLANNING-PLANE.md) for how kickoff creates the branch.

**Backward-compatible:** a bead whose epic has no `mol/<epic>` branch (older molecules or
beads filed outside a molecule) still targets the rig integration branch unchanged.

## Not yet wired

- Commit-trailer auto-injection (`Agent-Profile`/`Agent-Session`/`Agent-Model`) —
  needs a per-worktree `prepare-commit-msg` hook; config schema first, hook later.
- The merge-owner/Refiner role and the release-plane `cz check` hook are out of
  scope here (see the AGF design).
