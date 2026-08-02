# Git-hooks entrypoint ADR — lefthook owns the slot, everything chains from it

> Status: **decided** (bh-x87q). Establishes that **lefthook is the single owner of git-hook
> dispatch** in a bh working repo, and that every other hook producer — beads, commitizen,
> `just`, and bh's own pre-push fence — registers as a lefthook job rather than installing its
> own hook. Companion to
> [`cli-mcp-naming-conventions-adr.md`](cli-mcp-naming-conventions-adr.md) (conventions the
> pre-commit job enforces) and [`multi-host-model-adr.md`](multi-host-model-adr.md) (the fence
> the pre-push job protects).

## Context

Git gives a repository **exactly one** hook dispatch point: `core.hooksPath`, defaulting to
`.git/hooks`. It is a single slot with no built-in composition. Three tools in this repo each
assume they own it.

| Claimant | How it installs | State today |
|---|---|---|
| **beads** (`bd hooks install`) | sets `core.hooksPath` → `.beads/hooks`, writes marker-managed shims | **holds the slot** |
| **`.githooks/`** (tracked) | `just hooks` sets `core.hooksPath` → `.githooks` | **never enabled**; reachable only via one hand-written forwarder |
| **bh** (`prepush.install_for_hive`) | resolves `git rev-parse --git-path hooks`, writes `pre-push` | installs into whichever dir wins |

The result is not a tie — it is a silent partial loss:

- **beads holds `core.hooksPath`**, so `.githooks/pre-commit` (which runs `just check`) has
  never executed. The repo's fast gate has never gated anything.
- **`.githooks/commit-msg` runs only by accident of a hand-written forwarder** at
  `.beads/hooks/commit-msg`, which execs it. Conventional-commit enforcement rests on one
  undocumented shim that no convention requires anyone to maintain.
- **beads' own four sync shims are provable no-ops.** They exist to keep a git-committed
  `.beads/issues.jsonl` in lockstep with the local DB — the pre-Dolt (bd ≤0.49) architecture.
  This hive is Dolt-authoritative: `.beads/issues.jsonl` and `.beads/embeddeddolt/` are both
  gitignored, so `pre-commit` flushes to a file git will never stage and `post-merge` /
  `post-checkout` import from a file git will never change. Both ends of the pipe are outside
  git. `docs/BEAD-BACKENDS.md` already records this: *"Daemon / hooks: none since v0.59 (bd
  hooks exist but optional)."* They still cost ~0.38s of `bd` startup each, and
  `post-checkout` fires on every `git worktree add` — which bh does constantly.

### The failure that makes this urgent

`prepush.install_for_hive` is deliberately **non-destructive**: `_write_hook` leaves an
existing unmarked `pre-push` alone and returns `"skipped (custom hook present)"`. That is
correct, polite behavior — and it means **any hook manager that installs a `pre-push` shim
silently disables bh's multi-host fence.** A safety mechanism turns off, reports success, and
the only evidence is a status string nobody reads.

Adding a hook framework without deciding ownership does not fix the contention; it adds a
fourth claimant to a one-slot resource.

## Decision

**lefthook is the sole owner of git-hook dispatch in a bh working repo.** Every other producer
chains from `lefthook.yml`.

Three rules, each mechanically checkable:

1. **`core.hooksPath` is unset.** lefthook installs its shims into the default `.git/hooks`.
   Any tool that sets `core.hooksPath` has broken the convention.
2. **No tool writes `.git/hooks/*` directly.** Hooks are *declared* in `lefthook.yml`, never
   installed by a side effect of another command.
3. **Chaining is a job, not a hook.** A tool that needs to run at a git event contributes a
   `run:` line, so ordering, parallelism, skipping, and output are governed in one file.

### Why lefthook and not the alternatives

| Option | Verdict |
|---|---|
| **lefthook** | Single binary, no per-hook language envs, respects `core.hooksPath`, `{1}`-style git-arg passing, `extends`/`remotes` for composition, `lefthook-local.yml` for per-machine overrides, and an `rc` file for PATH setup — which this repo needs, since the existing hooks shell through `mise exec --`. **Chosen.** |
| **pre-commit** (Python) | Installs to `.git/hooks/<type>`. Its legacy-chaining is good, but it would be **silently dead** the moment anything sets `core.hooksPath`, which is exactly the failure mode being fixed. Also spins isolated envs per hook, redundant next to `uv`. |
| **Native `core.hooksPath` + `.githooks/` + `just`** | What the repo already has, and genuinely sufficient — but composition is hand-written forwarders, which is precisely how the current mess arose. Rejected in favor of one declarative file. |
| **Do nothing** | Leaves the fence silently skippable and the convention gate unwired. |

lefthook's `cleanHook` renames any existing non-lefthook hook to `.old`. Under this ADR that
is a feature (one owner, cleanly taken) but it is also the migration's sharpest edge — see
the ordering constraint below.

## Scope boundary: what this ADR does *not* govern

`prepush.install_for_hive` has **two** install locations (its module docstring, bh-ytbb.12):

1. the hive's own working repo — **governed by this ADR**, and the reason `pre-push` becomes a
   lefthook job calling `bh hive check-push-fence`;
2. bd's embedded-Dolt git transport: a **hidden bare repository** under
   `.beads/embeddeddolt/<db>/.dolt/git-remote-cache/<hash>/repo.git/`, because that is where
   `bd dolt push` actually invokes `git push`.

Location 2 is **a different git repository**. `core.hooksPath` in the working repo does not
reach it, lefthook cannot manage it, and it must not try. That hook stays bh-managed, installed
by `bh hive init` / onboard, and is explicitly **out of scope**. The convention governs the
repo a human or agent commits in — not every git repo bd happens to create underneath it.

This is a real limit, not a loophole: the fence's *enforcement* has never been the hook anyway
(see `multi-host-model-adr.md` Amendment 1 §2 — the atomic `--force-with-lease` epoch push is
the backstop, and survives `--no-verify`). The hook is the early, legible refusal.

## The hook map

| Git hook | lefthook job | Replaces |
|---|---|---|
| `pre-commit` | `just conventions` — ruff + the naming-ADR lint (~1.5s) | `.githooks/pre-commit` (which ran the ~6-minute `just check`) |
| `commit-msg` | `uv run cz check --commit-msg-file {1}` | `.githooks/commit-msg`, reached via a hand-written forwarder |
| `prepare-commit-msg` | `bd hooks run prepare-commit-msg` | `.beads/hooks/prepare-commit-msg` — the one beads hook still doing work |
| `pre-push` | `bh hive check-push-fence --hive-dir <hive>` | `prepush.install_for_hive`'s working-repo copy |

Deleted outright, as no-ops: beads `pre-commit`, `post-merge`, `post-checkout`, `pre-push`.

**`pre-commit` runs the fast gate, not the full one.** `just check` takes ~5m51s (3111 tests);
`just conventions` is ~1.5s. A six-minute pre-commit gets `--no-verify`'d within a week, which
returns the repo to an ungated state while *looking* gated. `just check` / `check-all` remain
the real gates, run deliberately.

## Migration ordering (the one constraint that matters)

`lefthook install` writes into **`repo.HooksPath`** — the *currently configured* one. With
`core.hooksPath` still pointing at `.beads/hooks`, `lefthook install` would write its shims
into beads' directory and rename beads' shims to `.old` in place.

> **`core.hooksPath` must be reset BEFORE `lefthook install` runs, never after.**

`lefthook install --reset-hooks-path` does both in the right order and is the supported path.

`.beads/hooks/` is untracked *and* covered by a global ignore (`~/.config/git/ignore`), so its
contents cannot be recovered from this repo's history — **back it up before deleting.**

## Consequences

- One file (`lefthook.yml`) describes every git-event behavior; ordering and skipping are
  declarative instead of emergent from whoever installed last.
- `bh hive init` / onboard must stop installing the working-repo `pre-push` copy when lefthook
  owns the slot, or detect it and no-op loudly rather than silently. Its transport-repo copy is
  unaffected. **This is a behavior change in bh and needs its own bead.**
- A new dependency (one Go binary) enters the toolchain — installed in `just bootstrap`
  alongside the existing Homebrew/mise setup.
- `lefthook install` is required per clone (hooks are never tracked by git, by construction).
  `just bootstrap` covers it; a fresh clone that skips bootstrap has no hooks — same as today.
- The drift check (rule 1 + 2 above) becomes the thing that catches a future tool quietly
  claiming the slot, which is the failure this ADR exists to make impossible to repeat.
