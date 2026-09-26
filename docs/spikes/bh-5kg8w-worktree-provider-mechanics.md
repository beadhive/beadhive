# Spike `bh-5kg8w` — must the worktree allocator and the Herdr process owner be the same tool?

**Bead:** `bh-5kg8w` · **Seat:** supervised developer · **Type:** research-only (no product code)
**Feeds decision on:** `bh-mr9tk.1` (lifecycle table) → `bh-mr9tk.2` (provider contract ADR)

> Pre-research for the `bh-mr9tk` provider-contract spike. It records how each candidate
> *tracks* worktrees, so the lifecycle table in `bh-mr9tk.1` can be scoped. It does not select
> the contract; that verdict stays with `bh-mr9tk.2`.

## Question

If Herdr is the dispatch-loop process owner (workspaces, panes, agents), must Herdr also be the
tool that creates and removes the worktree for it to render and manage that worktree? Or can a
different allocator (native Git, Worktrunk) create the worktree, with Herdr adopting it
afterwards?

Not asked: which provider Beadhive should select, the exact method signatures, or whether Orca
stays supported.

## Method

Disposable repositories under the session scratchpad; nothing touched the live Herdr session,
user Worktrunk config, or any hive.

- **Herdr 0.9.1**: `/home/linuxbrew/.linuxbrew/bin/herdr`, run as an isolated headless server:
  `herdr --session bhprobe server`, then every command as `herdr --session bhprobe …`. The
  session directory `~/.config/herdr/sessions/bhprobe` was deleted afterwards. Persisted state
  was read from that session's `session.json`.
- **Worktrunk v0.79.0**: release asset `worktrunk-x86_64-unknown-linux-musl.tar.xz` from
  `max-sixty/worktrunk`, sha256 verified, run from the scratchpad with an isolated
  `HOME`/`XDG_CONFIG_HOME` (no user config, no shell integration).
- **Native Git 2.55.0**: plain `git worktree add|remove`.
- **Orca**: `/home/bees/.local/bin/orca` is an Electron AppImage and fails here with
  `dlopen(): error loading libfuse.so.2`; `APPIMAGE_EXTRACT_AND_RUN=1` only unpacks it.
  Orca was **not** probed. Its behaviour below comes from `src/beadhive/orca.py` only.
  `bh-icn6b.1` shows how to run an isolated `orca serve` if a later probe needs it.
- Read-only cross-check against the live repository: `git worktree list` versus
  `herdr worktree list --cwd <main clone>`.

Branches used Beadhive's real shape (`wt/bead/issue/<id>`) so slash handling was exercised.

## Evidence

### Where each tool records "worktrees it manages"

**E1.** **Git is the only existence registry.** Every tool's inventory is derived from
`.git/worktrees/*`. On the live repository, `herdr worktree list --cwd <main clone>` returned
the same 21 entries as `git worktree list`, including worktrees Beadhive created natively
under `/tmp/bh-worktrees/...`. Each row carries `branch`, `path`, `is_linked_worktree`,
`is_prunable`, and `open_workspace_id` only when a workspace is bound.

**E2.** **Herdr's only worktree state is a workspace-to-checkout binding.** Each workspace in
`session.json` has `identity_cwd` and, when bound, a `worktree_space` object
(`key` = repo `.git`, `repo_root`, `checkout_path`, `is_linked_worktree`). There is no
"created by Herdr" flag. A worktree made with `worktree create` and one adopted with
`worktree open` have identical records.

**E3.** **Worktrunk keeps no membership state.** `wt list --format json` lists every Git worktree,
including natively created ones. The only writes observed were `.git/config` keys
`worktrunk.default-branch`, `worktrunk.history`, and `worktrunk.hints.worktree-path`,
plus `.git/wt/cache/*` (ahead/behind, diff stats, merge probes) and `.git/wt/logs/*`.

**E4.** **Orca keeps a registry** in `orca-data.json`. `orca.py:411` and `orca.py:444` warn that
a native removal leaves "orca's registry … stale; a later `orca worktree rm` attempt clears
it". Whether Orca can adopt an externally created worktree is untested.

### Herdr: adopt, create, attach

**E5.** **Adopting after the fact is supported.** Starting from
`git worktree add -b wt/bead/issue/p-1 <P>/wts/p-1 main`,
`herdr worktree open --cwd <repo> --path <P>/wts/p-1 --no-focus` returned
`"already_open": false` and a new workspace. Its `worktree_space` matched a
`worktree create` workspace field for field.

**E6.** **A plain workspace in a linked worktree is not bound.**
`herdr workspace create --cwd <P>/wts/p-2` produced a workspace with no `worktree_space`.
`herdr worktree remove --workspace <it>` then refused with
`not_linked_worktree: workspace is not a Herdr-managed worktree checkout`.
Beadhive's launch path creates workspaces this way today
(`src/beadhive/integrations/herdr/application_services.py:1489`).

**E7.** **`worktree open` upgrades an existing plain workspace in place.** Running
`worktree open --path <P>/wts/p-2` while the plain workspace from (E6) existed returned
`"already_open": true` with the same workspace ID, which then carried `worktree_space`.

**E8.** **`worktree create` honours an exact path and slashed branch.**
`worktree create --cwd <repo> --branch wt/bead/issue/p-3 --base main --path <P>/wts/p-3`
created exactly `<P>/wts/p-3` on `wt/bead/issue/p-3`.

**E9.** **`worktree create` on an existing branch attaches it.** For branches with one commit ahead
of `main`, both `--base main` and no `--base` checked out the existing tip. The branch
tip was unchanged before and after, and `--base` was silently ignored.

**E10.** **`worktree open` cannot attach a branch without a worktree.** `open --branch <b>` for a
branch with no worktree returned `worktree_not_found`. `--path` and `--branch` are
mutually exclusive (usage error), so `open` only finds existing worktrees.

**E11.** **Side effect: a main-checkout workspace.** `worktree list --cwd <repo>` alone created no
workspace. The first successful `worktree create`/`open` in a fresh session also created a
workspace for the repository's main checkout (`w1`).

**E12.** **Machine output.** Every `herdr worktree|workspace` command returns a JSON envelope
(`{"id": …, "result": …}` or `{"error": {"code": …, "message": …}}`).
`herdr worktree --help` exposes no separate `--json` flag.

### Herdr: removal and out-of-band changes

**E13.** **Remove is keyed by workspace and closes it.** `herdr worktree remove --workspace W`
returned `worktree_removed` with the path, and the workspace was gone.

**E14.** **Remove keeps the branch.** `git branch --list` still showed the branch after every Herdr
removal. No flag to delete it was observed.

**E15.** **Dirty refusal.** With an untracked file present, `worktree remove` returned
`dirty_worktree_requires_force` and left the worktree. `--force` exists.

**E16.** **Adopted worktrees are removable.** The natively created, `open`-adopted worktree from (E5)
was removed by `herdr worktree remove`. "Herdr-managed" means "bound through the worktree
verbs", not "created by Herdr".

**E17.** **Herdr does not reconcile an external removal.** After `git worktree remove` under an
open, bound workspace, the live server kept the workspace and its `worktree_space`. `worktree list`
no longer showed the path. `herdr worktree remove --workspace W` then failed with
`worktree_remove_failed: '<path>' is not a working tree`, and the workspace stayed.

**E18.** **After a restart, the stale binding is hidden but persisted.** Following an out-of-band
removal, `server stop` and a restart:

- `workspace list` showed the workspace labelled `~` with no `worktree` block;
- `session.json` still held `worktree_space`, and `identity_cwd` / pane cwd were
  `<path> (deleted)`;
- `herdr workspace close W` returned `ok` and removed it.

`workspace close` is the working cleanup for a workspace whose worktree someone else removed.

### Worktrunk

**E19.** **Exact path per run.** The default template produced `<repo>.wt-bead-issue-q-1` next to
the repository. A literal path with no template variables created exactly `<P>/wts/q-2`:

```sh
wt --config-set 'worktree-path="<P>/wts/q-2"' \
  switch -c wt/bead/issue/q-2 --no-cd --no-hooks -y --format json
```

The same override worked when attaching an existing branch
with no worktree (`"created_branch": false`).

**E20.** **Adopting is a no-op.** `wt switch <branch>` for a natively created worktree returned
`{"action": "existing", "path": <native path>}` and changed nothing.

**E21.** **Branch deletion on remove is on by default and decided by content.** `wt remove` deletes
the branch when it "would add no changes" (three-dot diff empty). A branch whose only extra
commit was empty was deleted by a default `wt remove` (`"branch_outcome": "deleted"`).
`--no-delete-branch` retained the branch (`"not_attempted"`). `-D` force-deletes unmerged
branches.

**E22.** **Remove runs in the background by default.** The command returns immediately. Output goes
to `.git/wt/logs/<branch>/internal/remove.log`, and the branch outcome may be `deferred`.
`--foreground` blocks until the removal completes.

**E23.** **Dirty refusal.** `wt remove` refused an untracked file with
`Cannot remove worktree: … has uncommitted changes`. `-f/--force` overrides it.

**E24.** **Hooks.** `pre-start`/`post-start`, `pre-remove`/`post-remove` and others run unless
`--no-hooks` is given. They would duplicate Beadhive's `worktrees.init` rules.

**E25.** **Machine output.** `list`, `switch`, and `remove` accept `--format json`. `list` emits a
versioned envelope (`"schema": 2`).

### Current Beadhive seam, for context

**E26.** **Orca delegation already requires an exact path.** `orca.create_worktree`
(`src/beadhive/orca.py:585`) hard-fails unless Orca's returned path equals Beadhive's
computed target.

**E27.** **Herdr's launch proofs recompute identity rather than reading it from the provider.**
`_managed_worktree_location` (`application_services.py:1522`) re-derives the path with
`worktree.locate()` and checks the branch. Readiness then compares pane cwd with that path.

## Verdict — **GO**

The allocator and the Herdr process owner do not need to be the same tool. Herdr's only worktree
state is a workspace binding (E2). `herdr worktree open --path` produces that binding for any
existing worktree, idempotently, including upgrading an existing workspace (E5, E7). The worktree
itself can come from native Git or Worktrunk, and both can meet Beadhive's exact-path
requirement (E8, E19).

The coupling that remains is on removal. Herdr does not watch Git (E17, E18), so a removal by
another tool must be preceded by the process owner releasing its workspace.

## Recommendation

For `bh-mr9tk.1` and `bh-mr9tk.2`:

1. **Split the roles.** One exclusive **mechanics provider** per hive handles create, attach and
   remove. The **process owner** (Herdr or Task fanout) is chosen independently. Observers
   (Repowise, Observaloop) stay a separate list.
2. **Inventory stays native Git.** Every probed tool derives its inventory from Git (E1, E3), so
   inventory does not need to be a provider method. Orca's registry is the exception to verify.
3. **Two ordering points:**
   - after allocation, the process owner adopts with `herdr worktree open --path`;
   - before removal, the process owner releases its workspace (`workspace close`), then the
     provider removes.

   The existing teardown saga already has this order (stop agents → close Space → classify →
   remove).
4. **Herdr as both allocator and owner is the special case.** `herdr worktree remove` fuses
   "close Space" with "remove worktree" (E13). Classification must therefore happen before the
   close, which reorders the saga.
5. **Worktrunk adapter flags are mandatory:** `--config-set worktree-path="<exact>"`,
   `--no-hooks`, `--no-delete-branch`, `--foreground`, `--format json`, `-y`.
6. **Independent of the provider decision:** Beadhive's launch should bind workspaces with
   `herdr worktree open --path` instead of `workspace create --cwd` (E6, E7). That gives bead
   workspaces Herdr's worktree grouping and makes them removable through the worktree verbs.
7. **Rows still to prove in `bh-mr9tk.1`:**
   - Orca adoption and registry behaviour, via an isolated `orca serve` as in `bh-icn6b.1`;
   - Worktrunk `wt remove` on an out-of-band-deleted path, and prune;
   - Herdr behaviour when the process owner and the provider race on the same path;
   - unpushed-commit refusal: neither Herdr nor Worktrunk checks for unpushed commits, so it
     stays with Beadhive's classifier.
