# Worktrees

bh-managed git worktrees live in a **shadow tree outside `$GIT_WORKSPACE`**, mirroring the
triplet path:

```text
<root>/<provider>/<org>/<repo>/<leaf>/
```

`<root>` depends on `worktrees.ephemeral` (default **true**):

| `ephemeral` | root | grants | lifecycle |
|---|---|---|---|
| `true` (default) | `<os-temp>/ws-worktrees` | none needed (temp is sandbox-writable) | session-scoped, disposable |
| `false` | `worktrees.path` (default `~/.ws/worktrees`) | `bh hive init --claude` writes per-hive grants | persistent |

Default-ephemeral keeps adoption zero-config: agents create a worktree, use it, and dispose
of it. There's no resume of abandoned long-running tasks yet, so persistence is opt-in.
`$WS_WORKTREES` overrides the root in either mode (advanced / testing).

Each is an ordinary linked `git worktree` of the hive's main clone
(`$GIT_WORKSPACE/<provider>/<org>/<repo>`) — the git admin files stay under the main clone's
`.git/worktrees/`, so `git worktree list` from either side sees it. Keeping the *working
dir* outside the workspace means:

- no collision with git-workspace's repo roots (it never manages anything under the root),
- "ours vs hand-made" is a pure path-prefix test (`bh worktree list` filters on it),
- bulk cleanup is one subtree — `bh worktree prune`.

Override the root with `$WS_WORKTREES`, or (persistent mode) `worktrees.path` in `config.yaml`.

## Naming

Every managed branch is auto-prefixed **`wt/`** (applied centrally, never doubled), so each
mode only sets the suffix:

| Command | Branch | Leaf (dir) |
|---|---|---|
| `bh wt add -r R --bead ag-infra-7` | `wt/bead/ag-infra-7` (`worktrees.bead_branch`) | `ag-infra-7` |
| `bh wt add -r R --branch spike-xyz` | `wt/spike-xyz` (prefixed, not overridden) | `spike-xyz` |
| `bh wt add -r R` | `wt/session/<ts>-<rand>` (`worktrees.session_branch`) | `<ts>-<rand>` |

The leaf is the sanitized **last segment** of the branch (bead ids and session ids are
already unique, so the namespace prefix is dropped for a clean dir name).

The session fallback uses `ts` = UTC `YYYYMMDDTHHMMSSZ` (fixed-width, so a plain `ls` sorts
chronologically) plus a 4-hex-char random suffix for same-second collisions. `-r/--hive` is
optional — omitted, the hive is derived from the current directory.

## Batch worktrees — `wt/batch/<group>` and `batch:<epic>` synthesis

A **batch** (or collapsed) run puts several beads in ONE shared worktree instead of one each.
Its branch is `wt/batch/<group>` (leaf: `<group>`) — the same `wt/` prefixing as every other
managed branch. Every member is claimed and merged as a unit through this one worktree
(`claim_group` / `merge_group` in `src/beadhive/work_group.py`), forked off the molecule base.
The batch completes **as a unit** — `submit --group` → `approve` → `merge --group` → `finish`,
not per-bead `submit`/`check`; see
[WORK.md — Completing a batch](WORK.md#completing-a-batch).

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

`worktrees.init` is a list of `{run, if_exists?, verify?}` rules. `if_exists` is a glob
evaluated in the new worktree; omit it to always run. Global rules run first, then the hive's
`worktree_init` extras. Each command is best-effort — a failure (or missing binary) warns and
the rest continue. Severity principle for the shipped defaults: optional provisioning
conveniences no-op (at most an info echo) when a repo hasn't configured them; the ⚠
warn-and-continue path is reserved for rules that actually ran and failed — which is why the
default justfile rule probes for a `setup` recipe before running it.

```yaml
worktrees:
  root: ~/.ws/worktrees
  bead_branch: "bead/{id}"
  session_branch: "wt/session-{ts}-{rand}"
  init:
    - {if_exists: ".mise.toml", run: "mise trust", verify: true}
    - {if_exists: "pyproject.toml", run: "uv sync", verify: true}
    - {if_exists: "justfile", run: "sh -c 'if just --show setup >/dev/null 2>&1; then just setup; else echo \"just setup: not configured in this repo\"; fi'"}

managed_repos:
  - {provider: github, org: acme, repo: api, prefix: ac-api, kind: org-native,
     worktree_init: [{run: "just bootstrap"}]}
```

`mise trust` as a per-worktree rule is the fix for the mise trust-hash collision across
worktrees — each worktree is trusted explicitly on creation. Re-run the rules on an existing
worktree with `bh wt init <path>`.

### Declared toolchains (`toolchain:`) — knowledge-only

A repo can **declare** what it uses:

```yaml
worktrees:
  toolchain: just            # or a list: [uv, just]; per-hive: managed_repos[*].toolchain
```

The declaration is **knowledge-only metadata** — it never changes what runs. Init rules
come only from the explicit config above, and validation only from `work.validate_cmd`.
What it powers is discovery and suggestion: `bh toolchain list` (declared names + the
registry), `bh toolchain show <name>` (the entrypoints that toolchain reports in the
hive's main clone, plus the template's propose-only suggestions), and
`bh toolchain exec -- <argv...>` (invoke an entrypoint explicitly). Agents use those to
SUGGEST `worktrees.init` / `work.validate_cmd` values to the operator, who sets them
explicitly. `worktrees.toolchains: {name: template}` overrides the registry per name
(replace, not merge). Full design:
[design/toolchain-declaration.md](design/toolchain-declaration.md).

### The verify-environment contract (`verify: true`)

`bh work submit` / merge validate from a **throwaway clean checkout** (an ephemeral
`verify-*` worktree), so the result never depends on dirty local state. That checkout does
**not** get the full init pass — only rules flagged `verify: true` run there, after the
checkout and before `validate_cmd`. Observaloop provisioning and unflagged rules (seat
provisioning like `just setup`) never run per validation.

"Provisioned enough to validate" is the contract: after the verify-flagged rules, the bare
checkout must be able to run `validate_cmd`. Typically that means dependency sync (`uv sync`)
and trust stamps (`mise trust`). Flagged rules run on **every** validation (each gets its own
per-invocation verify dir), so keep them idempotent and cache-friendly — `uv sync` from a
warm cache is seconds. When validation fails in a verify checkout, the error output includes
a bare-checkout hint pointing here.

Upstream-native alternatives, if you'd rather not flag rules:

- **uv self-provisioning** — declare dev deps under `[dependency-groups]` and (optionally)
  `tool.uv.default-groups` in `pyproject.toml`; a bare `uv run` then syncs what it needs.
  Note extras (`[project.optional-dependencies]`) are **never** synced by default.
- **git post-checkout hook** — per githooks, `git worktree add` fires `post-checkout`, so a
  hook can provision every new checkout. bd's marker-managed shim at
  `.beads/hooks/post-checkout` preserves content outside its markers if your hive prefers
  hook-based provisioning. Caveats: it fires on every checkout, and a failing hook fails the
  checkout itself.

### The validation verdict ledger

Every clean-checkout validation records its verdict — keyed by **(commit sha, validate-cmd
hash)**, with a timestamp — in `<hive>/.git/bh-validation-ledger.json` (repo-local, untracked,
dies with the clone). `bh work submit` reuses a fresh **green** verdict for the exact key and
skips the redundant checkout (`✓ validation verdict reused …`), so re-submitting an unchanged
sha is a true no-op; `bh work review --run` reuses only with an explicit `--no-fresh`. A red,
stale (older than 24h), or command-changed verdict always revalidates. The ledger is a local
optimization for trusted-local seats — landing-boundary validations (merge, post-land, finish,
batch land) never consult it, so the gate at landing always runs fresh.

## Cleanup

`rm` and `prune` remove now-empty triplet dirs (`<repo>`, then `<org>`, then `<provider>`)
up to — but never including — the shadow root. This only ever removes **empty** dirs:
another live worktree under the same hive stops the climb. Disable with
`worktrees.rmdir_empty: false` (omitting it is treated as `true`).

## Worktree status and safe prune

### `bh worktree status` — classification pre-flight

`bh worktree status` shows each managed worktree's determined status and whether it is
**SAFE** to remove:

```text
bh worktree status [-r HIVE] [--json]
```

Each worktree is classified into one of seven states:

| Classification | Meaning | Safe? |
|---|---|---|
| `SAFE` | Bead is **closed** + branch is a git ancestor of its parent + worktree is **clean** | Yes |
| `REVIEW` | Branch merged into parent, clean, but bead not yet closed (waiting on close) | No |
| `DIRTY` | Uncommitted changes in the working tree | No |
| `UNMERGED` | Bead is closed but branch is not a git ancestor of its parent | No |
| `ACTIVE` | Bead is open / in-progress | No |
| `DETACHED` | No branch checked out (detached HEAD) | No |
| `ABANDONED` | No bead id (session or batch worktree with no bead) | No |

**SAFE** is a conservative three-way conjunction: a worktree must satisfy *all* three
conditions — `closed AND merged AND clean` — before `prune` will touch it.  Missing any
one condition leaves the worktree in place.

**Scoping rules:**

- `--hive <id>` — that hive only.
- No `--hive`, cwd is inside a hive root — that hive.
- No `--hive`, at the hub (not inside a hive) — all managed hives.

`--json` emits a JSON array of `WtStatus` records (`hive`, `leaf`, `branch`, `path`,
`bead_id`, `classification`, `merged`, `dirty`, `safe`) for downstream tooling.

The command **always repopulates fresh metadata** before classifying — it never reads stale
cache data.

### `bh worktree prune` — SAFE-set removal

```text
bh worktree prune [-r HIVE]
```

`prune` removes **only** the worktrees classified `SAFE` every run.  It never touches
`DIRTY`, `UNMERGED`, `ACTIVE`, `DETACHED`, or `ABANDONED` worktrees.

- **No confirmation prompt** and **no `--force` flag** — `bh worktree status` is the
  operator's pre-flight view.  Inspect the status output to understand what will and will
  not be removed before running prune.
- For each SAFE worktree removed, prune reports the path and branch.
- After removal, prune reports the count of SAFE worktrees pruned and lists any skipped
  non-SAFE worktrees with their classification.
- `--hive <id>` limits scope to one hive (same scoping as `status`).

**The SAFE invariant**: `prune` can never leave a hive with lost work because the SAFE
definition requires the branch to already be a git ancestor of its parent (`mol/<epic>` or
the integration branch) — the commits are already integrated before the worktree is touched.

**Squash-merged / PR-landed branches** (bh-v0wu): a branch landed by a GitHub **squash-merge**
is never a git ancestor of its parent. The landed detection for closed non-ancestor branches
therefore accepts, in order: the bead's authoritative close_reason (`merged` /
`molecule landed` — written by bh's land paths and `bh work land`), patch-id equivalence
(`git cherry` — rebase/cherry-pick lands), and finally a **merged GitHub PR with the branch as
head** (`gh pr list --state merged --head …`; GitHub-backed hives with `gh` on PATH,
best-effort and fail-closed). For a landing with no discoverable signal at all,
`bh worktree mark-landed <bead-or-branch>` stamps the authoritative close_reason so the seat
unsticks — an operator assertion; prefer `bh work land` when a PR exists to check.

**Observaloop note**: `prune` never tears down a hive's observaloop profile.  The profile is
shared across all of a hive's worktrees; use `bh plugin observaloop down` to take it down separately.

## Commands

```text
bh worktree add    [-r HIVE] [--bead ID | --branch NAME] [--dry-run|--preview] [--json]  # short: bh wt add
bh worktree list                                                      # managed only
bh worktree path   [-r HIVE] [--bead ID | REF]                        # abs path (for scripts)
bh worktree init   PATH                                               # re-run init ops
bh worktree rm     [-r HIVE] [--bead ID | REF] [--force] [--json]
bh worktree status [-r HIVE] [--json]                                  # classification pre-flight
bh worktree prune  [-r HIVE]                                           # SAFE-set only (no confirm)
bh worktree mark-landed [-r HIVE] (BEAD | BRANCH)                      # assert out-of-band landing
```

## Driving bh worktrees from an orchestrator

The commands above are also a **stable porcelain** for an external orchestrator (an agent
harness like Orca, or any script) that wants to drive bh worktrees from *outside* a `bh`
process — the inverse of the plugin seam (`wt_create`/`wt_remove`, see
[Non-goals](#non-goals) and `src/beadhive/plugins.py`) that lets a plugin take *over* bh's
own worktree creation. `add`, `path`, `rm`, and `status` each speak `--json` (or, for
`path`, a script-stable plain-text form), so a driver never has to scrape human-formatted
output.

### The flow: preview → create → run → submit → prune

```sh
# 1. preview — read-only, zero side effects. What WOULD `add` do?
bh worktree add --bead bh-73rz.4 --preview --json

# 2. create — the same call without --preview/--dry-run. Idempotent: re-running it after
#    the worktree already exists just re-attaches (`would: reuse`/`attach` from step 1).
bh worktree add --bead bh-73rz.4 --json

# 3. run — hand the bead to a coding agent IN that worktree. `bh worktree path` (or
#    `bh work` verbs, which accept --bead the same way) resolves the directory.
opencode run "implement bh-73rz.4" --dir "$(bh worktree path --bead bh-73rz.4)"

# 4. submit — inside the worktree (or scripted with `bh work submit <id>` from anywhere,
#    since `work` verbs resolve the worktree themselves) once the change validates.
bh work submit bh-73rz.4

# 5. prune — reclaim SAFE (closed + merged + clean) worktrees once review/merge lands.
bh worktree prune
```

`bh work claim <id> --preview --json` / `bh work assign <id> --to <name> --preview --json`
give the same read-only preview **plus** the identity (author/email/signing) that verb
would stamp — useful when the orchestrator, not `bh`, is the one launching the coding agent
process and needs to pre-flight what identity that agent will commit as. They are read-only:
no `bd` write, no git write, nothing provisioned.

### The JSON schema

`add --dry-run`/`--preview` and non-preview `add --json` (once it has actually created or
attached the worktree) emit the **same shape**, so an orchestrator parses both phases with
one parser. Preview additionally computes `would`/`start_point`; the real, post-create call
replaces `would` with `created: true`:

```jsonc
// bh worktree add --bead <id> --preview --json  (read-only; nothing on disk changes)
{
  "op": "add",
  "hive": "github/org/repo",
  "bead": "bh-73rz.4",
  "branch": "wt/bead/issue/bh-73rz.4",
  "path": "/abs/path/to/the/worktree",
  "would": "create",          // "reuse" (dir already live) | "attach" (branch exists, no dir) | "create" (neither)
  "start_point": "main",      // only set when would == "create": the integration_base it would fork from
  "branch_exists": false,
  "path_exists": false,
  "init": [ /* the resolved worktrees.init + worktree_init rule list — {run, if_exists?, verify?} */ ]
}
```

```jsonc
// bh worktree add --bead <id> --json  (real, non-preview: worktree now exists)
{
  "op": "add",
  "hive": "github/org/repo",
  "bead": "bh-73rz.4",
  "branch": "wt/bead/issue/bh-73rz.4",
  "path": "/abs/path/to/the/worktree",
  "created": true
}
```

`bh work claim|assign --preview --json` add one field on top of the same base contract —
`identity`, the profile `claim`/`assign` would stamp into the worktree's git config:

```jsonc
{
  "op": "claim",                // or "assign"
  "hive": "github/org/repo",
  "bead": "bh-73rz.4",
  "branch": "wt/bead/issue/bh-73rz.4",
  "path": "/abs/path/to/the/worktree",
  "would": "create",
  "start_point": "main",
  "branch_exists": false,
  "path_exists": false,
  "init": [ /* … */ ],
  "identity": {
    "mode": "agent",             // "agent" (distinct stamped author + signing) | "supervised" (inherits git config)
    "name": "dev/orch",
    "email": "agents@example.dev",
    "signing_key": "",
    "sign": false
  }
}
```

`bh worktree status --json` emits a JSON **array** (not a single object — one worktree per
element) of `WtStatus` records: `hive`, `leaf`, `branch`, `path`, `bead_id`,
`classification`, `merged`, `dirty`, `safe` (see
[`bh worktree status` — classification pre-flight](#bh-worktree-status--classification-pre-flight)
for the `classification` enum and what `safe` gates).

`bh worktree rm --json` emits `{op: "rm", hive, path, removed: true}` on success (same
raise-on-failure contract as every other verb here — a non-zero exit means nothing was
removed, with the reason on stderr; there's no JSON error payload to parse).

`bh worktree path` has **no** `--json` flag, deliberately — it doesn't need one. Its entire
stdout contract *is already* the machine-readable form: on success it is **exactly** the
absolute path and nothing else (no progress lines, no trailing prose), so
`"$(bh worktree path --bead <id>)"` is already script-stable; a failure exits non-zero with
`✗ …` on stderr and empty stdout.

### Compat expectations: additive-only evolution

Every field documented above is part of the stable contract. Evolution of this JSON surface
is **additive only**:

- New fields may be added to any object (`add`/`preview`, the `identity` block, a
  `WtStatus` row) at any time — a driver **must** ignore unknown keys rather than fail on
  them (parse leniently: index by field name, never by exact key-set/ordinal position).
- Existing field **names and value types never change** (`would` stays one of
  `reuse`/`attach`/`create`, booleans stay booleans, `path` stays an absolute-path string).
- A field is never silently repurposed. If a genuinely breaking change is ever required, it
  ships as a new field alongside the old one (not a rewrite in place), with the old field
  deprecated in this doc before removal.
- `op` and `hive`/`bead`/`branch`/`path` are present on every one of these payloads
  (`add`/`preview` and the `claim`/`assign --preview` superset) — a driver can rely on that
  common core across the whole family without branching on which verb produced it.

## Claude Code sandbox (persistent mode)

This applies only when `worktrees.ephemeral: false`. Ephemeral worktrees live in the OS temp
dir, which the sandbox already makes writable — no grant is involved, and `bh hive init
--claude` says so and writes nothing.

In persistent mode the shadow root lives under `$HOME`, outside any project. Claude Code's
optional sandbox makes the project cwd and the session tmpdir writable but **not** `$HOME` —
so a sandboxed session can't create or use worktrees there until granted.

`bh hive init --claude` writes that grant: this hive's subtree
(`<root>/<provider>/<org>/<repo>`) into the hive clone's **`.claude/settings.local.json`**
(host-local — the path is machine-specific, so it stays out of the shared `settings.json`),
under both `sandbox.filesystem.allowWrite` (bash) and `permissions.additionalDirectories`
(tools). The file is added to `.git/info/exclude` best-effort so it doesn't show in
`git status`.

Caveat: a grant is read at **session start**, so it provisions *future* sandboxed sessions —
the session that first writes it isn't retroactively unblocked.

If `worktrees.root` / `$WS_WORKTREES` moves, each hive's grant goes stale; `bh doctor` flags
the drifted hives and the fix is to **re-run `bh hive init --claude`** in them — the writer
replaces the old entry rather than piling on.

## Non-goals

- **`safe.directory` / global git config:** not touched. Same-owner worktrees don't need it;
  the mise trust-collision pain is handled by the per-worktree `mise trust` rule. Add a
  `safe.directory` entry yourself only if an ownership error ever appears.
- **Branch base ref:** branches off the main clone's current `HEAD`.
- **No gastown coupling.** gastown's `polecats/` live *inside* a hive; this shadow tree is
  separate and non-conflicting (`--branch polecat/...` still works if you want that name).
