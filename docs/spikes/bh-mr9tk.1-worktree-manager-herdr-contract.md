# Spike `bh-mr9tk.1` — does the manager/binding contract hold under both composition modes?

**Bead:** `bh-mr9tk.1` · **Seat:** dev/mr9tk-1 · **Type:** research-only (no product code)
**Feeds decision on:** `bh-mr9tk` (epic verdict, already GO on the split) → `bh-mr9tk.2` (provider
contract ADR) → the Option A implementation molecule

> Extends `bh-5kg8w` (`docs/spikes/bh-5kg8w-worktree-provider-mechanics.md`, evidence E1–E27,
> branch `wt/bead/issue/bh-5kg8w`, not yet on `main`). That spike settled the GO/NO-GO question
> ("must the allocator and the Herdr process owner be the same tool?") and recorded ownership
> state, adoption, create/attach, plain removal, out-of-band removal, and restart behavior. This
> bead does not re-litigate that verdict — it proves the specific rows bh-5kg8w's own
> Recommendation §7 flagged as unproven, against the two-role contract the epic (`bh-mr9tk`)
> confirmed on 2026-09-26: a **WorktreeManager** (create/attach/remove, exactly one per hive) and
> a **WorkspaceBinding** (bind/release, zero or one, composed independently unless the manager
> already binds as a side effect). Evidence below is numbered **E28** onward to avoid colliding
> with bh-5kg8w's E1–E27, which are cited by number where reused.

## Question

For both valid compositions of the two-role contract — **Option A** (native Git as the exclusive
`WorktreeManager`, Herdr composed in only as a `WorkspaceBinding`) and **Option B** (Herdr as the
exclusive `WorktreeManager`, with implicit binding as a side effect of its own create/remove) —
does the same ten-row lifecycle (create, attach, bind/implicit-bind, release, remove-with-branch-
kept, dirty refusal, Herdr-unavailable, partial failure, crash+restart recovery, binding-reference
round trip) behave the way the epic's design assumes, and does Option B's `worktree remove` really
require classification to happen *before* the call (not just conventionally, but because the
call's own fusion of close+remove makes an after-the-fact classification structurally
impossible)?

Not asked: which option Beadhive should ship first (the epic already sequences Option A before
Option B), the exact Python method signatures, or anything about Worktrunk/Orca lifecycle
behavior (out of scope for this bead — see the non-preclusion note at the end of Evidence).

## Method

Disposable repositories under a scratch directory outside any managed hive or the live Herdr
session; nothing here touched the operator's Herdr session, `~/.config/herdr`, or this repo's own
git history.

- **Herdr 0.9.1** (`/home/linuxbrew/.linuxbrew/bin/herdr`, same binary and version bh-5kg8w used),
  run as an isolated headless server under a private `XDG_CONFIG_HOME`:
  `XDG_CONFIG_HOME=/tmp/bh-mr9tk1-probe/xdgcfg herdr --session mr9tk1 server`. Every command ran
  as `herdr --session mr9tk1 …` with that same `XDG_CONFIG_HOME`, so the session's
  `session.json`/sockets lived entirely under the scratch tree; the session directory was removed
  and the server stopped when the probe finished (see final cleanup, E42).
- **Native Git 2.55.0**, plain `git worktree add|remove`, against a disposable repo
  (`/tmp/bh-mr9tk1-probe/work/repo`, one `init` commit, `main` branch) with linked worktrees under
  `/tmp/bh-mr9tk1-probe/work/wts/<name>`. Branches followed Beadhive's real shape
  (`wt/bead/issue/<name>`).
- **Crash simulation:** a temporary `post-checkout` hook (git invokes it on every
  `worktree add`, confirmed to fire — E38) was set to sleep several seconds; the herdr server
  process was `kill -9`'d while a `worktree create` request was blocked inside that hook, so the
  client got a dropped connection while the underlying `git worktree add` subprocess kept
  running to completion. This reproduces "Git succeeded, the manager's own bookkeeping did not"
  without needing to patch or fork Herdr.
- All state assertions cross-checked two independent sources per bh-5kg8w's method: native
  `git worktree list`/`git branch --list` versus `herdr worktree list --cwd <repo>` and
  `herdr workspace list`.
- Read-only reference reads of `bh-5kg8w`'s spike doc from its own worktree
  (`/tmp/bh-worktrees/github/beadhive/beadhive/bh-5kg8w`, branch `wt/bead/issue/bh-5kg8w`) —
  nothing there was edited.
- Repo seam mapping: `grep`/`Read` over `src/beadhive/modules/worktrees/**`, `src/beadhive/worktree.py`,
  `src/beadhive/worktree_cleanup.py`, `src/beadhive/plugins.py`, `src/beadhive/orca.py`,
  `src/beadhive/hitch_plugin.py`, and
  `src/beadhive/integrations/herdr/application_services.py` in this bead's own worktree
  (`wt/bead/issue/bh-mr9tk.1`) — read-only, no edits.

## Evidence

### Option A — native `WorktreeManager` + Herdr `WorkspaceBinding`

**E28. Bind after create.** `git worktree add -b wt/bead/issue/a-1 <P>/wts/a-1 main` followed by
`herdr worktree open --cwd <repo> --path <P>/wts/a-1 --no-focus` returned `"already_open": false`
and workspace `w2`, whose `worktree` block matched the native branch/path exactly (extends
bh-5kg8w E5, now against a brand-new create rather than an adopted pre-existing worktree).

**E29. Bind after attach.** `git branch wt/bead/issue/a-2 main` then
`git worktree add <P>/wts/a-2 wt/bead/issue/a-2` (no `-b`, i.e. attach) followed by the same
`worktree open --path` bound it as workspace `w3`. Bind is agnostic to whether the native
manager's prior step was create or attach.

**E30. Release before remove — clean ordering.** For a third worktree (`a-3`): `worktree open`
bound it as `w4`; `herdr workspace close w4` returned `{"type":"ok"}`; the immediately following
`git worktree remove <P>/wts/a-3` exited 0, branch `wt/bead/issue/a-3` remained
(`git branch --list` still showed it), and `herdr workspace list` afterward contained no residue
for `w4`. Release-then-remove leaves no stale state on either side.

**E31. Partial failure — remove without release first leaves a stale, unbound-but-orphaned
workspace (extends E17).** Skipping the release step, a native `git worktree remove <P>/wts/a-2`
succeeded (branch kept) while workspace `w3` was still bound. Immediately after,
`herdr worktree list` no longer listed the path (Git-derived, so it can't), but
`herdr workspace list` still showed `w3` labelled `"a-2 (deleted)"`, and
`herdr worktree remove --workspace w3` failed with
`worktree_remove_failed: '<path>' is not a working tree` — the exact E17 signature, now shown as
a *consequence of composing Option A without the mandated release-before-remove ordering*, not
just an accidental out-of-band change. `herdr workspace close w3` was the only working cleanup
(`{"type":"ok"}`), matching bh-5kg8w's E18 recommendation, and no server restart was even needed
to reach it this time — the stale record was reapable live.

**E32. Dirty refusal (native).** With an untracked file present in `a-3`'s worktree, plain
`git worktree remove <P>/wts/a-3` (no binding involved) exited 128:
`fatal: '<path>' contains modified or untracked files, use --force to delete it`. Removing the
file let the same command exit 0 on retry.

**E33. Herdr unavailable at bind time — native claim retained, binding missing and
re-bindable.** With `a-4` created natively (`git worktree add -b wt/bead/issue/a-4 …`), the herdr
server was stopped (`herdr server stop`). `herdr worktree open --path <P>/wts/a-4` then failed
with `server_not_running: no herdr server is running at …/herdr.sock; run \`herdr session attach
mr9tk1\` to start or attach it` — but `git worktree list` still showed `a-4` untouched. The native
claim was never at risk from Herdr's absence.

**E34. Crash between create and bind, then idempotent re-bind across a real server restart
(extends E7).** With the server still down from E33, the server was restarted fresh (new process,
same session directory, so `session.json` was reloaded rather than recreated).
`herdr workspace list` after restart still showed `w1`/`w2` from before the crash, and
`herdr worktree list` showed `a-4` present in Git's inventory with **no** `open_workspace_id`
field — the create-without-bind gap survived the restart intact and visibly. Calling
`worktree open --path <P>/wts/a-4` then returned `"already_open": false` with a new workspace
`w3`; calling the identical command again immediately returned `"already_open": true` with the
same `workspace_id: w3`. Bind-after-crash is idempotent whether or not a server restart happened
in between (bh-5kg8w's E7 only showed idempotency within one live session).

**E35. Binding-reference round trip.** Every bind above returned a `workspace_id` in its own
response (e.g. `w2` for `a-1`); an independent, later `herdr worktree list --cwd <repo>` call
showed the same id under `open_workspace_id` for the matching `path`/`branch` row, with no shared
process state between the two calls beyond the session file (confirms bh-5kg8w E1 generalizes
past adoption to a manager+binding composition).

### Option B — Herdr as the exclusive `WorktreeManager`, implicit binding

**E36. Create with an exact path and exact `wt/…` branch, workspace bound implicitly.**
`herdr worktree create --cwd <repo> --branch wt/bead/issue/b-1 --base main --path <P>/wts/b-1
--no-focus` created exactly `<P>/wts/b-1` on `wt/bead/issue/b-1` (`git worktree list` /
`git branch --list` both confirm) and returned `workspace_id: w4` **in the same response** — no
separate bind call exists or is needed; the manager's `create` *is* the bind.

**E37. `--base` is silently ignored on an existing branch even when the branch has since
diverged from that base — the guard bh-5kg8w's E9 flagged must be enforced by Beadhive, not by
Herdr.** Branch `wt/bead/issue/b-2` was built natively with one extra commit
(`768dd0e`) on top of the old `main` tip, then `main` was advanced past that point
(`3db946e`) with an unrelated commit, with `b-2`'s worktree removed in between (no worktree open
at attach time). Running
`herdr worktree create --branch wt/bead/issue/b-2 --base main --path <P>/wts/b-2` attached the
existing branch at its unchanged tip `768dd0e` — verified both via
`git rev-parse wt/bead/issue/b-2` (unchanged) and
`git -C <P>/wts/b-2 log --oneline` (no merge, no rebase onto the newer `main`). If Beadhive's
caller assumed `--base` would rebase or fast-forward
an existing branch, it silently would not; Beadhive must pre-check itself whether an existing
branch's start point still matches intent before calling `create` on it.

**E38. `herdr worktree create` really does shell out to `git` and its hooks (needed for E40's
crash technique to be valid).** A `post-checkout` hook that appends a timestamp to a marker file
fired during a plain `worktree create` call, proving the create path is not a from-scratch libgit2
reimplementation that would skip hooks.

**E39. Saga ordering demonstrated, not inferred: classify-then-remove is the only order that can
ever work, because `worktree remove` fuses close+remove and destroys the classification
evidence.** Using a simplified stand-in for Beadhive's real classifier (`worktree_cleanup.py`'s
SAFE/LANDED_REBASED logic; here: `git status --porcelain` empty ⇒ classified `SAFE`, non-empty ⇒
`UNSAFE (dirty)`) run as a real shell function against the live checkout:

- `classify(<P>/wts/b-1)` before any remove call returned `SAFE (clean)`.
- `herdr worktree remove --workspace w4` (b-1's workspace) then succeeded in one call:
  `{"type":"worktree_removed","path":"<P>/wts/b-1","forced":false,"workspace_id":"w4"}`,
  `git worktree list` lost the row, `herdr workspace list` lost `w4`, and `git branch --list`
  still showed `wt/bead/issue/b-1` (branch kept).
- Calling `classify(<P>/wts/b-1)` **again, now after** the remove, returned
  `UNKNOWN (path unreadable: fatal: cannot change to '<path>': No such file or directory)` —
  the classification signal is not merely stale, it is **permanently unobtainable** once
  `worktree remove` has run, because the working tree it needs to inspect no longer exists.
  A "remove, then classify" order is not just wrong by convention; for Option B it cannot be
  implemented at all once the fused call has returned.
- As the gating counter-case: a second worktree (`b-3`) was made dirty (`echo … >
  scratch.txt`, untracked). `classify(<P>/wts/b-3)` returned `UNSAFE (dirty)` **before** any
  remove call was attempted, so the saga's classify step is what must decide whether `remove` is
  even called; `b-3` was never passed to `worktree remove` as a result. As independent
  defense-in-depth, `herdr worktree remove --workspace w6` (b-3) was still attempted and itself
  refused with `dirty_worktree_requires_force` (reconfirms bh-5kg8w E15) — but that generic
  dirty check is not a substitute for Beadhive's classifier (it has no notion of
  unmerged/unpushed/landed-rebased, per bh-5kg8w's Recommendation §7 last bullet), so classify
  must still run and gate the call rather than relying on Herdr's own refusal.

**E40. Herdr unavailable at claim time refuses loudly — no worktree, no silent native
fallback, because Option B has no separate native step to fall back to.** With the server
stopped, `herdr worktree create --branch wt/bead/issue/b-4 --path <P>/wts/b-4 …` returned
`server_not_running` and **created nothing**: `git worktree list` showed no `b-4` row and
`<P>/wts/b-4` did not exist on disk. Because Option B's manager *is* Herdr, this refusal is
automatically the whole-operation refusal Beadhive needs — there is no code path here that could
quietly retry with `git worktree add` instead without Beadhive adding one back in (which is
exactly the behavior the epic's design forbids: "no silent fallback and no mixed-manager hive").

**E41. Partial failure — Git succeeds, the workspace does not, reproduced by killing the
manager process mid-request.** A `post-checkout` hook set to `sleep 4` was installed; a
`worktree create` for `wt/bead/issue/b-6` was launched in the background; ~1.5s in (while the
hook was still sleeping, i.e. after `git worktree add` had already checked out the branch and
files but before the request could return), the herdr server process was `kill -9`'d. The client
got `Error: Custom { kind: Other, error: EmptyResponse }`. After the dust settled:

- `git worktree list` **did** show `<P>/wts/b-6` on branch `wt/bead/issue/b-6` — the git-level
  effect had completed.
- The herdr server process was gone entirely (killed), so nothing was queryable until restart.
- On restart (same session directory, `session.json` reloaded), `herdr workspace list` had
  **no** entry for `b-6`, and `herdr worktree list` showed the `b-6` row with **no**
  `open_workspace_id` field at all — the fused call's git-side effect landed durably; its
  Herdr-side bookkeeping did not.
- Recovery used the same primitive as Option A's bind:
  `herdr worktree open --cwd <repo> --path <P>/wts/b-6 --no-focus` adopted it cleanly
  (`"already_open": false`, new workspace `w9`).
  **The recovery path for a partial Option-B failure is identical to Option A's ordinary bind
  step** — Beadhive needs exactly one reconciliation primitive regardless of which manager is
  selected.

**E42. Crash and restart recovery (full success case, for contrast with E41's partial
failure).** Workspaces created successfully before both crashes in this probe (`a-1`/`w2`,
`a-4`/`w3` after E34, `b-2`/`w5`, `b-3`/`w6`, `b-4`/`w7`, `b-5`/`w8`) all survived every server
stop/restart in this session with their bindings intact — `herdr workspace list` and
`herdr worktree list` after each restart showed identical `open_workspace_id` values before and
after. Only the two deliberately interrupted operations (E34's create without a bind call, E41's
killed mid-create) lost their bindings; a clean create/bind/remove sequence is durable across a
manager restart. (Final cleanup: the probe session was stopped and its directory removed; the
scratch repo and worktrees under `/tmp/bh-mr9tk1-probe` were deleted after this evidence was
captured.)

**E43. Binding-reference round trip (Option B).** `worktree create` for `b-1` returned
`workspace_id: w4` inline; the later, independent `herdr worktree list --cwd <repo>` call (used
throughout E39/E41) showed `open_workspace_id: "w4"` for that same path/branch before it was
removed, and `open_workspace_id: "w8"` for `b-5` (created without incident) — the reference is
re-derivable from `list` the same way bh-5kg8w's E1 established for adoption, now shown for
implicit-bind create too.

### Worktrunk / Orca — non-preclusion only, no lifecycle rows

Per the epic's scope note and this bead's replan, Worktrunk and Orca get no rows here. Nothing
probed above assumes anything beyond the `create(exact path, exact branch)` /
`attach(exact path, existing branch)` / `remove(handle, force)` method shapes exercised against
native Git and Herdr. bh-5kg8w already showed Worktrunk can meet an exact path per invocation
(E19) and that Orca's existing `wt_create`/`wt_remove` hooks already hard-fail on a path mismatch
(E26) — neither of those results depends on anything this bead changed. **Confirmed: nothing here
precludes a later exact-path `WorktreeManager` adapter for either.**

### Current seam map (this repo, read-only)

**E44.** `WorktreeProvisioner` (`src/beadhive/modules/worktrees/contracts/ports.py:19`) is a
five-phase `Protocol` (`prepare`/`create`/`created`/`remove`/`removed`) implemented by both
`NativeGitWorktreeProvisioner` (`adapters/native_git.py`) and `PluginWorktreeProvisioner`
(`adapters/plugin.py`), composed by `WorktreeLifecycleService`
(`application/services.py:19-52`) as "ask the plugin first, native runs only if the plugin
reports `unhandled`". `_worktree_lifecycle_service`
(`src/beadhive/integrations/herdr/application_services.py` — wired from `worktree.py`'s
`_consult_wt_create`/`_consult_wt_remove`, `:508-597`) is the composition root; `orca.py:671-672`
wires `wt_create=create_worktree, wt_remove=remove_worktree` as the one enabled delegating plugin
today.

**E45.** `WorktreeInventory` (`contracts/ports.py:33`, `application/services.py:56-70`,
`adapters/inventory.py`) is already a thin typed wrapper over native Git reads
(`CallbackWorktreeInventory`) — it does not participate in the create/remove delegation chain at
all.

**E46.** The plugin delegation seam already has a hard-refusal primitive
(`typer.Exit` propagates through `_consult_wt_create`/`_consult_wt_remove`,
`worktree.py:508-597`) that Orca opts into by default (`orca.py:15-16`,
`_wt_remove_fail`/`create_worktree` raise `typer.Exit` unless
`config.orca_worktrees_fallback`/an explicit fallback flag is set) — so "refuse loudly" is not a
new capability to build from scratch. What the current seam does *not* have is a mode where the
refusal is the *only* possible outcome for the whole hive: today a hook returning `None` (as
opposed to raising) always means "not handled, fall through to native" (`plugin.py:68-78,83-86`),
which is precisely the silent-fallback shape Option B's evidence (E40) shows must not happen once
Herdr is the selected manager.

**E47.** `hitch_plugin.py:19-40` documents, as a deliberate decision from a different bead
(bh-og0q.5), exactly the failure-mode mismatch this bead's Option B evidence reinforces: the
`wt_create`/`wt_remove` hook's "return `None` to fall through" contract is unsuitable for
anything that must fail loudly rather than best-effort.

**E48.** Herdr's launch path binds via the wrong primitive today.
`_workspace()` (`application_services.py:1483-1502`) calls `workspace create --cwd <target>
--label bh:<hive> --no-focus` — a **plain** workspace with no `worktree_space` (bh-5kg8w E6),
not `herdr worktree open --path`. `_managed_worktree_location`
(`application_services.py:1522-1546`) then re-derives the exact path/branch via
`worktree.locate()` on every call rather than reading a stored binding reference — there is no
`WorktreeHandle`-shaped object anywhere on this path that a binding id could round-trip through
yet.

**E49.** The teardown saga (`docs/spikes/bh-2m1yw.4-authoritative-completion-safe-teardown.md`,
mirrored in `worktree_cleanup.py`'s `cleanup_one_if_safe`/`_prune_remove_one`) is physically
ordered `stop-agents → verify-final-lease → space-close → cleanup-classified → remove`
(`bh-2m1yw.4` §5). E39 above demonstrates this exact order is *correct* for Option A (native
remove and Herdr's `workspace close` are two independent effects, so classify can sit right
before the *native* remove, after the Herdr close) but would be **impossible** for Option B,
where `herdr worktree remove` fuses close+remove into one call — classify has nowhere to sit
"after close, before remove" because there is no gap between them.

### Seam map summary

| Seam | Outcome | Why (evidence) |
| --- | --- | --- |
| `WorktreeProvisioner` (`ports.py:19`) | **Narrow** | Split `create(new_branch)` into `create`/`attach`; drop the plugin-first-fallback race (E44) |
| `WorktreeLifecycleService` composition | **Replace** | "First plugin, else native" contradicts "exactly one configured manager, refuse if unavailable" (E44, E40) |
| `WorktreeInventory`/`CallbackWorktreeInventory` | **Keep** | Already native-Git-only reads, matches the target design (E45) |
| `PluginWorktreeProvisioner` / `wt_create`/`wt_remove` delegation | **Replace** | Silent-fallback-on-`None` default is exactly what Option B must not do (E44, E46, E47) |
| `wt_creating`/`wt_created` observer hooks | **Keep** | Cross-cutting observers (Repowise) stay a separate list per the epic design |
| Herdr launch `_workspace()` (`workspace create --cwd`) | **Replace** | Must become `herdr worktree open --path` to actually bind (E48, bh-5kg8w E6/E7) |
| `_managed_worktree_location`/`worktree.locate()` re-derivation | **Narrow** | Keep as the native-truth resolver, but launch should consult a stored binding reference first (E48) |
| Teardown saga order (`bh-2m1yw.4` §5) | **Narrow** (mode-branch) | Correct as-is for Option A; needs a classify-before-fused-remove variant for Option B (E39, E49) |

## Verdict — **GO**

The two-role contract holds under both compositions for all ten lifecycle rows, including the
crash and partial-failure paths bh-5kg8w's Recommendation §7 left open. Three results matter most
for the ADR:

1. Herdr's absence is safe in both directions: Option A never loses a native claim to it (E33),
   and Option B's absence is a total, correctly-empty refusal rather than a partial one (E40) —
   there is nothing for Beadhive to accidentally leak a fallback into.
2. Exactly one recovery primitive (`herdr worktree open --path`) repairs every binding gap this
   bead produced — crash-before-bind (E34) and crash-during-Option-B's-fused-create (E41) recover
   identically. The contract does not need a second, Option-B-specific repair path.
3. The saga ordering question is settled by a structural fact, not a style preference: once
   `herdr worktree remove` returns, the working tree is gone and classification of it is provably
   impossible after the fact (E39). Option B's saga variant is not optional.

## Recommendation

For the Option A implementation molecule (native manager in `beadhive-worktrees`, Herdr binding
replacing `workspace create --cwd` in launch, per the epic's sequencing):

1. **Replace** `_workspace()`'s `workspace create --cwd` call with `herdr worktree open --path
   <exact>` (E48; bh-5kg8w E6/E7), and thread the returned `workspace_id` onto whatever
   `WorktreeHandle`-shaped value the launch path already carries, so later reads (`_launch_target`,
   readiness checks) consult that field instead of re-deriving identity via
   `_managed_worktree_location`/`worktree.locate()` on every call.
2. **Narrow** `WorktreeProvisioner` into the epic's explicit `create`/`attach`/`remove` methods
   (splitting today's single `create(new_branch: bool)` into two), and **retire the
   plugin-first-fallback composition** (`WorktreeLifecycleService`, E44) in favor of selecting
   exactly one configured `WorktreeManager` (`worktrees.manager`, default native) — no
   registry-order race between plugins.
3. **Keep** `WorktreeInventory`/`CallbackWorktreeInventory` as-is (E45) — it already matches the
   "inventory stays native Git, not a manager method" design; only its package placement moves
   per the epic's `beadhive-worktrees` note.
4. **Keep** the `wt_creating`/`wt_created` *observer* half of the plugin hooks (Repowise's
   `_seed_worktree` consumer) as a cross-cutting notification list, separate from the manager
   contract, per the epic's "observers stay a separate list" design.
5. Implement release-before-remove for Option A as two ordered calls (`workspace close` then
   native `remove`) exactly as demonstrated in E30, and treat any state resembling E31 (an
   orphaned `"<label> (deleted)"` workspace) as a call to `workspace close`, not
   `worktree remove --force`.

For the Option B molecule (filed only after Option A ships and is proven, per the epic):

1. **Replace** the delegation seam's default-to-fallback semantics (E44/E46) with a hard
   `worktree-manager-unavailable` refusal at claim time whenever `worktrees.manager: herdr` is
   configured and the server is unreachable (E40) — reuse the existing `typer.Exit`
   hard-fail primitive (E46) rather than inventing a new exception contract; Orca's own
   `create_worktree`/`remove_worktree` wrappers are a working reference for that shape.
2. **Change the teardown saga** (`bh-2m1yw.4` §5, E49) to a Herdr-manager variant that classifies
   *before* calling `herdr worktree remove` — collapsing the current
   `space-close → cleanup-classified → remove` tail into `classify → remove` (which performs the
   close as a side effect) — since E39 shows there is no order in which classify could run after
   that call and still see real data.
3. Guard the `--base` no-op on attach (E37) explicitly in whatever Beadhive-side pre-check decides
   a branch's start point, rather than trusting Herdr's `--base` flag to enforce it.
4. No lifecycle rows for Worktrunk or Orca are needed before either molecule — confirmed above
   (E19, E26 carried forward; nothing here narrows that further).
