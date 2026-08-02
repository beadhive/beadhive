# Hooks-as-functionality ADR — `bh` exposes hook behavior, it does not generate scripts

> Status: **decided** (bh-smcj). `bh` must expose every git-hook behavior as a **callable
> verb**, and must not generate, install, or own hook script files. Companion to
> [`git-hooks-entrypoint-adr.md`](git-hooks-entrypoint-adr.md), which decides *who dispatches*
> hooks (lefthook); this decides *what bh contributes to* them.

## Context

`beadhive.prepush.hook_script(hive_dir)` returns a shell body — ref-name filter, `hive_dir`
baked in at install time, `exec bh hive check-push-fence` — and `install_for_hive` writes it
into up to two hooks directories. `bh` is, in effect, a code generator whose output is an
executable file it then owns on disk.

That worked while `bh` was the only thing installing hooks. It stops working the moment
anything else dispatches them, which is now the decided architecture: under
[`git-hooks-entrypoint-adr.md`](git-hooks-entrypoint-adr.md) lefthook owns the slot and every
producer contributes a *job*.

Wiring the fence into lefthook under the current design forces a bad choice:

1. **Transcribe the generated script into `lefthook.yml`'s own script directory.** Two
   implementations of one safety rule, free to drift silently. The `refs/dolt/data` stdin
   filter is not incidental — get it wrong in the copy and either every ordinary code push
   gets fenced, or the fence never fires at all. Both failures are quiet.
2. **Let `bh` keep installing its own hook.** It then either loses the slot to lefthook (which
   is what happens today: `_write_hook` sees a foreign hook and returns
   `"skipped (custom hook present)"`, so the fence is silently absent) or clobbers it.

Neither is acceptable for a safety mechanism. The generation step is the defect.

There is a second, quieter cost. A generated script bakes state in at **install** time —
`hook_script` embeds an absolute `hive_dir` precisely because a hook cannot discover its own
hive from cwd. That makes the installed hook a stale snapshot: move or re-clone the hive and
the hook still points at the old path, with nothing to detect it.

## Decision

**`bh` exposes hook behavior as CLI verbs. `bh` does not write hook files.**

1. **Every hook behavior is a verb** that reads whatever git provides on stdin/argv and exits
   with the hook's semantics. It is runnable by hand, testable without a git repo in a
   particular state, and callable by any dispatcher.
2. **`bh` never generates or installs a hook script.** No `hook_script`-style string-building,
   no writing into any `hooks/` directory, no marker-managed files it must later recognize as
   its own.
3. **The dispatcher is the integrator's choice.** lefthook here, but a bare `.git/hooks` file
   or another manager must work equally well by calling the same verb.
4. **Filtering lives in the verb, not in the caller.** The `refs/dolt/data` check is part of
   the fence's semantics. A dispatcher must never need to know which refs matter — it pipes
   stdin in and honors the exit code.
5. **Resolution happens at run time, not install time.** The verb resolves its own hive from
   cwd or an explicit flag, so there is no baked-in path to go stale.

### Target shape

```sh
bh hive hook pre-push < <git's ref list>     # exit 0 = allow, non-zero = refuse
```

```yaml
# lefthook.yml — the whole integration
pre-push:
  jobs:
    - name: bh-fence
      run: ${BH_EXEC:-bh} hive hook pre-push
      use_stdin: true
```

`bh hive check-push-fence` already exposes the *decision*; what is missing is the surrounding
hook contract (stdin protocol + ref filter + exit semantics). This ADR says that contract
belongs in `bh` too.

### `BH_EXEC` — the self-hosting bootstrap

A hook that invokes `bh` uses whatever `bh` is on `PATH`. For every hive that merely *consumes*
bh that is exactly right. For the repo that **authors** bh it is a chicken-and-egg: the
installed binary lags the working tree, so a hook either fences against yesterday's code or
fails outright on a verb that has not been installed yet — which is a *blocked push*, not a
degraded one.

So the invocation is indirected through an env var with the consumer-correct default:

```yaml
run: ${BH_EXEC:-bh} hive hook pre-push
```

- **unset → `bh`.** The released binary. Correct for every consuming hive, and for this repo
  once a version is out.
- **`BH_EXEC="uv run bh"`.** Runs the working tree. For the window where you are changing the
  fence itself, or before the first `just install` of a new verb.

A permanent per-machine preference belongs in `lefthook-local.yml` (gitignored), overriding the
job by name — never in the tracked config. This generalizes to any future `bh` hook job: they
all take `${BH_EXEC:-bh}`, so there is one switch rather than one per job.

## Consequences

- `prepush.install_for_hive` / `hook_script` are removed, and `bh hive init` / onboard stop
  furnishing a working-repo hook. **This is a behavior change for every hive `bh` manages**,
  not just this repo — it needs its own migration note.
- **The embedded-Dolt transport repo is the hard case.** `bd dolt push` invokes `git push`
  inside a hidden bare repo under `.beads/embeddeddolt/.../repo.git/`, which no user-facing
  dispatcher governs — it is not a repo anyone clones or commits in. Removing bh's install
  there leaves that path unhooked. Resolving this is the substantive design work in bh-smcj,
  and it may justify a *narrow, explicitly-scoped* exception to rule 2 for that location
  alone. If so, the exception is recorded here rather than assumed.
- Hook behavior becomes directly testable: a verb taking stdin and returning an exit code
  needs no installed hook, no worktree in a particular state, and no shell-string assertions.
- Third-party dispatchers (a plain hook file, husky, a CI runner) can reuse the same verb
  instead of reimplementing the contract.
- The fence stays absent from the working repo until bh-smcj lands. That is status quo, not a
  regression — it has been skipped all along.
