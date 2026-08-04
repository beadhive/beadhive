# Spike `bh-ukit.2` — does a dolt server simplify or break the lease + epoch fence?

**Bead:** `bh-ukit.2` · **Seat:** `dev/Brian Cripe` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-areg.6` — fix(fence): the epoch fence and pre-push hook find no
transport repo under a dolt server

## Question

Does the epoch fence / host lease model of
[`multi-host-model-adr.md`](../design/multi-host-model-adr.md) still hold, **unchanged**, under
each storage mode — embedded, bd's shared server (mode (a)), and an external server (mode (c))?
Where it does not, exactly which ADR section changes and how.

It is **not** asking which mode the fleet should run. That was settled by `bh-ukit.4` on disk,
latency, concurrency, process shape and addressability, and nothing here can move it: the fence
is enforcement, and enforcement follows the data wherever it lands.

Two inputs were handed over by `bh-u562.1` with file:line evidence rather than resolved there,
and this spike owes both an answer:

1. `host_fence.transport_repos()` cannot locate the git-transport bare repo under a server,
   because it lives outside `hive_dir` entirely.
2. Whether `bd dolt push`'s actual `git push` fires from the hive's own repo or from the nested
   transport repo under any server mode was **UNKNOWN** — `bh-u562.1`'s no-`bd dolt push`
   constraint forbade checking. To be resolved "with an instrumented non-push test or a direct
   read of bd's push-path source".

## Method

**Read.** `host_fence.py` (the fence and `transport_repos`), `prepush.py` (the hook and its
two documented install locations), `multi-host-model-adr.md` Amendment 1 §2 (the fence
formulation), `guard.primary_state` (the predicate both the hook and `bh work` share).

**Ran** an instrumented non-push test — exactly the resolution route open item 2 named. Fully
local by construction: scratch bare repos as remotes (`file://`), and an isolated
`BEADS_SHARED_SERVER_DIR` + `BEADS_DOLT_SERVER_PORT=3399` so the host's real shared server is
never touched and no scratch database is added to it (`bh-lxpf`). Per mode:

```sh
git init --bare originX.git && git init hiveX && cd hiveX
git remote add origin ../originX.git && git commit --allow-empty -m init && git push origin HEAD:main
bd init --prefix ex --non-interactive            # mode B adds --shared-server
bd create --title one -t task -p 2 && bd dolt push   # bd creates its transport repo lazily here
# install a LOGGING pre-push hook (exit 0 — observes, never blocks) in EVERY candidate:
#   the hive checkout (at `git rev-parse --git-path hooks`) and every */repo.git found under
#   the hive's .beads/ and under the server's data dir
bd create --title two -t task -p 2 && bd dolt push   # ...then push again and read the log
git -C <each candidate> for-each-ref               # what does it hold LOCALLY?
```

Recording, per push: which hook fired, its `cwd`, and the refspecs git handed it on stdin.

`bd` under test: `HEAD-af076b6` (the Brewfile's pin). The control was re-run against
`/opt/homebrew/Cellar/beads/1.1.0/bin/bd` — the exact binary `bh-ytbb.7` measured on — to date
the behaviour rather than assume it.

**Not run.** Mode (c) (external server) was not stood up. Its answers below are derived from
the measured mechanism plus the mode's own definition, and are labelled as such.

## Evidence

1. **The transport repo exists under a shared server, one directory root over.** Embedded puts
   it at `<hive>/.beads/embeddeddolt/<db>/.dolt/git-remote-cache/<hash>/repo.git`; under the
   shared server the identical relative path hangs off the server's data dir instead:
   `<BEADS_SHARED_SERVER_DIR>/dolt/<db>/.dolt/git-remote-cache/<hash>/repo.git`. Both are bare;
   both carry an `origin` pointing at the hive's own remote. The layout is a property of the
   *database directory*, not of the hive.

2. **`transport_repos()` returns `[]` under server mode.** Its glob
   (`host_fence.py:64`) is rooted at `hive_dir/.beads`, and a server-mode hive has no
   `embeddeddolt/` there at all — confirmed by the probe finding no `repo.git` under `hiveB`.

3. **Open item 2, RESOLVED: the `git push` fires from the transport repo in BOTH modes, never
   from the hive's own checkout.** The logging hook fired only in the transport repo, twice per
   `bd dolt push`, under embedded and under the shared server alike. The hive checkout's hook
   never fired for a data push in either mode.

4. **Under the shared server the push runs inside the server process.** The hook's `cwd` was
   `<BEADS_SHARED_SERVER_DIR>/dolt` — the server's own working directory — where the embedded
   run's `cwd` was the hive directory. Same code path, different process and different owner.

5. **`bd` pushes TWO refs per `bd dolt push`**, seen on the hook's stdin:

   ```text
   refs/dolt/blobstore/origin/dolt/data/<uuid>  <sha>  refs/dolt/data              <old>
   refs/dolt/info/__dolt_remote_info__          <sha>  refs/heads/__dolt_remote_info__  <old>
   ```

6. **`refs/dolt/data` is NOT a local ref in the transport repo — in either mode.** After a
   successful push the transport repo holds only `refs/dolt/info/__dolt_remote_info__` and
   `refs/remotes/origin/__dolt_remote_info__`. The local side of the data push is a transient
   `refs/dolt/blobstore/origin/dolt/data/<uuid>` (evidence 5) that does not survive it. This
   contradicts `transport_repos()`'s own docstring — "the repos that actually hold a LOCAL
   `refs/dolt/data`" (`host_fence.py:147-148`).

7. **That is not a regression.** The control re-run on `bd 1.1.0`, the binary `bh-ytbb.7`
   measured against, gives the identical result: transport repo present, `refs/dolt/data`
   local: **NO**. The docstring's claim was wrong when written; nothing about server mode
   changed it.

8. **`bd init` sets `core.hooksPath` on the hive checkout** to `<hive>/.beads/hooks` — in both
   modes. `prepush._hooks_dir` already asks git (`rev-parse --git-path hooks`) rather than
   assuming `.git/hooks`, so it installs where git will actually look. Worth knowing anyway: a
   hook placed in `<hive>/.git/hooks` by hand would be inert.

9. **The multi-host model is not in force on this fleet today**, so none of the above is
   currently breaking anything: one host is registered and stale, no fence hooks are installed
   on any transport repo, and `guard.primary_state` returns `None` — the hook's own allow path
   (`prepush.py:196`) — until an adopt happens, which needs a second host.

## Verdict — **GO** (with one ADR amendment, and it is not caused by server mode)

Server mode does not break the lease + fence model. Nothing in the lease half touches storage
at all: leases live in HQ, `guard_primary` reads a cached ref, and none of that knows where a
Dolt database sits. The fence half needs one locator change per mode, and that change is
mechanical — the transport repo is exactly as findable under a server as under embedded
(evidence 1), it is simply not under `hive_dir`.

Per mode, against "does the model hold unchanged?":

| mode | lease half | fence half |
|---|---|---|
| **embedded** | holds | holds — the locator already points at the only place it can |
| **shared server (a)** | holds, unchanged | holds, once the locator stops being hive-relative; the transport repo is at `<server data dir>/dolt/<db>/…` (evidence 1) |
| **external (c)** | holds, unchanged | **(c)-local**: same as (a) — same code path, path known from the server's own data dir. **(c)-remote**: does **not** hold — the transport repo is on another machine, so no local hook and no local push can reach it. Not measured; derived from the mode's definition. |

The "simplifies" branch of this bead's title is **rejected on the evidence**. A shared server
is one writer per *host*, not one writer per *fleet*: two hosts each run their own server
against the same remote, so the contention the lease exists to arbitrate is untouched. No lease
machinery becomes redundant.

**The one real finding is mode-independent and predates this thread.** The ADR's fence
formulation (Amendment 1 §2) is not implementable as written, in *any* mode:

```sh
git push --atomic --force-with-lease=refs/bh/epoch:<held> \
  origin refs/dolt/data refs/bh/epoch
```

`bh` never owns that push — `bd` does, from a repo where `refs/dolt/data` does not exist
locally and the ref it actually pushes from is a transient blobstore ref bh cannot reproduce
(evidence 5, 6, 7). This is the gap `bh-areg.6` has to close, and it is larger than
"re-path a glob".

## Recommendation

**`bh-areg.6` proceeds, with its scope set by this verdict.** Three things, in order:

1. **Make the locator mode-aware, in `store_locator`, not in `host_fence`.** The parent that
   holds the databases is the only thing that differs: `<hive>/.beads/embeddeddolt/` (embedded)
   versus `<server data dir>/dolt/` (server). Everything below — `<db>/.dolt/git-remote-cache/
   <hash>/repo.git` — is identical, so it is one glob rooted at a mode-dependent parent.
   `bh-z9h7` already made `store_locator` the single owner of store-path facts and named the
   two levels distinctly (`embedded_store_dir` = the parent, `embedded_database_dir` = one
   database); add the server-mode parent there.

2. **End the empty-list ambiguity, as `bh-areg.6`'s design already requires.** `[]` currently
   means both "no dolt transport (a nodb/JSONL hive)" and "transport not found" — and after
   this spike a third: "(c)-remote, transport is on another machine, unreachable by
   construction". Those are three different answers and a safety mechanism must not collapse
   them into one falsy value.

3. **Put the fence where the push actually is.** Since `bh` cannot wrap `bd`'s push atomically,
   there are two honest options and the ADR should name the one chosen:
   - **Hook-side refusal (recommended).** The transport repo's `pre-push` hook sees the real
     refspecs on stdin (evidence 5) and a non-zero exit aborts the push *before* any ref moves.
     That is genuine enforcement at the write, not a convenience — which upgrades
     `prepush.py`'s current framing. It costs `--no-verify` bypassability, which is exactly why
     it cannot be the *only* fence.
   - **Sequenced CAS then push**, i.e. `host_fence._fallback_push`'s existing shape with
     `bd dolt push` as the data leg. Already implemented, already documented as leaving a
     narrow unfenced window.

**ADR amendment — `multi-host-model-adr.md`, Amendment 1 §2 ("The fence splits from the
lease").** Keep the lease/fence table and both stated consequences; they survive intact. Replace
the `git push --atomic …` block and the sentence introducing it ("The fence is co-located with
the data, so the check is **atomic with the write**") with the measured mechanism: the data push
is `bd`'s, issued from a bare transport repo beside the database, and `refs/dolt/data` exists
only on the remote — so the fence is enforced at that repo's `pre-push`, with the sequenced CAS
as the documented fallback. Consequence 2 ("HQ becomes a *coordination* dependency, not a
write-path one") is unaffected either way. Also correct `host_fence.transport_repos`'s docstring
claim about a local `refs/dolt/data` (evidence 6, 7) — it has been wrong since `bh-ytbb.7`.

**Do not rule out any mode on fence grounds.** (a) is fine. (c)-remote's fence problem is real
but belongs to `bh-3mik`, which is already downstream, and it is a reason for that bead to carry
the constraint — not a reason to hold mode (a).

**Not urgent, and say so plainly:** evidence 9. Nothing is unfenced today because the model is
not in force. It must be correct *before* a second host is ever adopted, which is what
`bh-areg.6` is for.
