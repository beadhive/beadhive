# Worktrees

ws-managed git worktrees live in a **shadow tree outside `$GIT_WORKSPACE`**, mirroring the
triplet path:

```text
<root>/<provider>/<org>/<repo>/<leaf>/
```

`<root>` depends on `worktrees.ephemeral` (default **true**):

| `ephemeral` | root | grants | lifecycle |
|---|---|---|---|
| `true` (default) | `<os-temp>/ws-worktrees` | none needed (temp is sandbox-writable) | session-scoped, disposable |
| `false` | `worktrees.path` (default `~/.ws/worktrees`) | `ws rig init --claude` writes per-rig grants | persistent |

Default-ephemeral keeps adoption zero-config: agents create a worktree, use it, and dispose
of it. There's no resume of abandoned long-running tasks yet, so persistence is opt-in.
`$WS_WORKTREES` overrides the root in either mode (advanced / testing).

Each is an ordinary linked `git worktree` of the rig's main clone
(`$GIT_WORKSPACE/<provider>/<org>/<repo>`) — the git admin files stay under the main clone's
`.git/worktrees/`, so `git worktree list` from either side sees it. Keeping the *working
dir* outside the workspace means:

- no collision with git-workspace's repo roots (it never manages anything under the root),
- "ours vs hand-made" is a pure path-prefix test (`ws worktree list` filters on it),
- bulk cleanup is one subtree — `ws worktree prune`.

Override the root with `$WS_WORKTREES`, or (persistent mode) `worktrees.path` in `config.yaml`.

## Naming

Every managed branch is auto-prefixed **`wt/`** (applied centrally, never doubled), so each
mode only sets the suffix:

| Command | Branch | Leaf (dir) |
|---|---|---|
| `ws wt add -r R --bead ag-infra-7` | `wt/bead/ag-infra-7` (`worktrees.bead_branch`) | `ag-infra-7` |
| `ws wt add -r R --branch spike-xyz` | `wt/spike-xyz` (prefixed, not overridden) | `spike-xyz` |
| `ws wt add -r R` | `wt/session/<ts>-<rand>` (`worktrees.session_branch`) | `<ts>-<rand>` |

The leaf is the sanitized **last segment** of the branch (bead ids and session ids are
already unique, so the namespace prefix is dropped for a clean dir name).

The session fallback uses `ts` = UTC `YYYYMMDDTHHMMSSZ` (fixed-width, so a plain `ls` sorts
chronologically) plus a 4-hex-char random suffix for same-second collisions. `-r/--rig` is
optional — omitted, the rig is derived from the current directory.

## Batch worktrees — `wt/batch/<group>` and `batch:<epic>` synthesis

A **batch** (or collapsed) run puts several beads in ONE shared worktree instead of one each.
Its branch is `wt/batch/<group>` (leaf: `<group>`) — the same `wt/` prefixing as every other
managed branch. Every member is claimed and merged as a unit through this one worktree
(`claim_group` / `merge_group` in `src/ws/work_group.py`), forked off the molecule base.

There are **two ways** a set of beads becomes a runnable batch, and they meet at the same
`wt/batch/<group>` path:

- **Planner-authored batch group.** The planner declares a shared `batch:<group>` label on
  each member up front (cohesion/size validated at plan time). `resolve_group` reads those
  existing labels and refuses a member with no `batch:` label or a mix of groups — the label
  is the precondition for the shared worktree. See
  [AGF.md — Batch groups](AGF.md#batch-groups-the-exception-to-one-bead-per-worktree) and
  [WORK.md — Batch groups](WORK.md#batch-groups--when-not-to-batch) for the guards and cost
  trade-off.
- **Ad-hoc `batch:<epic>` synthesis (collapsed claim).** A collapsed run over an epic the
  planner **never** batched has no `batch:` labels to satisfy `resolve_group`. Rather than
  weaken `resolve_group`'s refusal logic, `claim_collapsed` runs a **pre-step**
  (`synthesize_batch_labels`) that stamps a synthetic `batch:<epic>` label onto every ready
  child that carries no `batch:` label yet, so `resolve_group`'s existing precondition simply
  holds. It then delegates to the very same `claim_group` path the planner-batch flow uses.

The synthesis is **additive and idempotent**: a member already carrying a batch label
(planner-authored, or a prior collapse stamp) is left untouched and no other label is ever
removed, so re-running a collapse is safe. The result is one code path — the shared
`wt/batch/<group>` worktree — whether the `batch:` label was authored by the planner or
synthesized ad-hoc at collapsed-claim time. The dispatch config that triggers a collapsed
claim is documented in
[CONFIGURATION.md — work.dispatch](CONFIGURATION.md#workdispatch--collapsed-dispatch).

> **Constraint: `--collapse` requires fully un-batched epics.** A partially planner-batched epic
> (some children carry `batch:planner` labels, some do not) cannot be collapsed: `synthesize_batch_labels`
> refuses to stamp a mix of batch groups, and `resolve_group` rejects the mixed set with a loud error
> (`members span multiple batch groups`). This is safe — no data loss — but means collapsed dispatch
> targets only epics the planner never batched. If an epic has partial planner batching, fall back to
> per-group fanout or explicitly un-batch all children before collapse.

## Post-create init (declarative)

`worktrees.init` is a list of `{run, if_exists?}` rules. `if_exists` is a glob evaluated in
the new worktree; omit it to always run. Global rules run first, then the rig's
`worktree_init` extras. Each command is best-effort — a failure (or missing binary) warns and
the rest continue.

```yaml
worktrees:
  root: ~/.ws/worktrees
  bead_branch: "bead/{id}"
  session_branch: "wt/session-{ts}-{rand}"
  init:
    - {if_exists: ".mise.toml", run: "mise trust"}
    - {if_exists: "pyproject.toml", run: "uv sync"}
    - {if_exists: "justfile", run: "just setup"}

managed_repos:
  - {provider: github, org: acme, repo: api, prefix: ac-api, kind: org-native,
     worktree_init: [{run: "just bootstrap"}]}
```

`mise trust` as a per-worktree rule is the fix for the mise trust-hash collision across
worktrees — each worktree is trusted explicitly on creation. Re-run the rules on an existing
worktree with `ws wt init <path>`.

## Cleanup

`rm` and `prune` remove now-empty triplet dirs (`<repo>`, then `<org>`, then `<provider>`)
up to — but never including — the shadow root. This only ever removes **empty** dirs:
another live worktree under the same rig stops the climb. Disable with
`worktrees.rmdir_empty: false` (omitting it is treated as `true`).

## Commands

```text
ws worktree add  [-r RIG] [--bead ID | --branch NAME] [--dry-run]   # short: ws wt add
ws worktree list                                                     # managed only
ws worktree path [-r RIG] [--bead ID | REF]                         # abs path (for scripts)
ws worktree init PATH                                                # re-run init ops
ws worktree rm   [-r RIG] [--bead ID | REF] [--force]
ws worktree prune [-r RIG]                                           # remove all (or one rig's)
```

## Claude Code sandbox (persistent mode)

This applies only when `worktrees.ephemeral: false`. Ephemeral worktrees live in the OS temp
dir, which the sandbox already makes writable — no grant is involved, and `ws rig init
--claude` says so and writes nothing.

In persistent mode the shadow root lives under `$HOME`, outside any project. Claude Code's
optional sandbox makes the project cwd and the session tmpdir writable but **not** `$HOME` —
so a sandboxed session can't create or use worktrees there until granted.

`ws rig init --claude` writes that grant: this rig's subtree
(`<root>/<provider>/<org>/<repo>`) into the rig clone's **`.claude/settings.local.json`**
(host-local — the path is machine-specific, so it stays out of the shared `settings.json`),
under both `sandbox.filesystem.allowWrite` (bash) and `permissions.additionalDirectories`
(tools). The file is added to `.git/info/exclude` best-effort so it doesn't show in
`git status`.

Caveat: a grant is read at **session start**, so it provisions *future* sandboxed sessions —
the session that first writes it isn't retroactively unblocked.

If `worktrees.root` / `$WS_WORKTREES` moves, each rig's grant goes stale; `ws doctor` flags
the drifted rigs and the fix is to **re-run `ws rig init --claude`** in them — the writer
replaces the old entry rather than piling on.

## Non-goals

- **`safe.directory` / global git config:** not touched. Same-owner worktrees don't need it;
  the mise trust-collision pain is handled by the per-worktree `mise trust` rule. Add a
  `safe.directory` entry yourself only if an ownership error ever appears.
- **Branch base ref:** branches off the main clone's current `HEAD`.
- **No gastown coupling.** gastown's `polecats/` live *inside* a rig; this shadow tree is
  separate and non-conflicting (`--branch polecat/...` still works if you want that name).
