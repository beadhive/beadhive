# Spike `bh-2m1yw.3` — exact Herdr Space and named-tab allocation with recovery

**Bead:** `bh-2m1yw.3` · **Seat:** `dev/codex-bh-2m1yw-3` · **Type:** research-only
**Baseline:** `9178568` · **Run date:** 2026-08-30 · **Herdr:** `0.8.2`

## Question

Can a Beadhive presentation adapter safely maintain exactly one Herdr Space for one stable,
exact-worktree scope and one named tab for each independently managed Agent, despite Herdr 0.8.2
having no atomic create-with-ownership-metadata or caller idempotency key?

The answer must cover concurrent first launch, an optional intentional split, exact cwd, failed
agent start, crash windows, cold restart, foreign content, compensation, and generation-fenced
close. It is not asking Herdr to own beads or worktrees, and it is not asking panes to replace the
baseline tab-per-Agent boundary.

## Method

I first inspected the installed interfaces (`herdr --version`, `herdr workspace`, `herdr tab`,
`herdr pane`, `herdr agent start --help`, `herdr api`, and `herdr --skill`). I then used only the
temporary named session `bh-spike-2m1yw-3`, always with `--no-focus`, and the temporary cwd
`/tmp/bh-2m1yw-3-probe.E67MtZ/exact-worktree`. No command selected, closed, renamed, or added
content to an operator Space. After the probe I stopped and deleted exactly the named test session;
`herdr session list --json` then contained only `default`.

The live sequence was:

```sh
herdr --session bh-spike-2m1yw-3 workspace create \
  --cwd /tmp/bh-2m1yw-3-probe.E67MtZ/exact-worktree \
  --label bh-scope-7d91 --no-focus
herdr --session bh-spike-2m1yw-3 tab rename w2:t1 agent-alpha
herdr --session bh-spike-2m1yw-3 tab create --workspace w2 \
  --cwd /tmp/bh-2m1yw-3-probe.E67MtZ/exact-worktree \
  --label agent-beta --no-focus
herdr --session bh-spike-2m1yw-3 pane split --pane w2:p1 \
  --direction right --cwd /tmp/bh-2m1yw-3-probe.E67MtZ/exact-worktree --no-focus

# Make beta's root pane intentionally unavailable, then exercise failed start + compensation.
herdr --session bh-spike-2m1yw-3 pane run w2:p2 'sleep 30'
herdr --session bh-spike-2m1yw-3 agent start probe-fail \
  --kind codex --pane w2:p2 --timeout 1000
herdr --session bh-spike-2m1yw-3 tab close w2:t2

# Cold stop, restart the exact named session, and reread authoritative state.
herdr session stop bh-spike-2m1yw-3 --json
env -u HERDR_ENV -u HERDR_PANE_ID -u HERDR_TAB_ID \
  -u HERDR_WORKSPACE_ID -u HERDR_SESSION herdr --session bh-spike-2m1yw-3
herdr --session bh-spike-2m1yw-3 api snapshot
```

To probe the recovery algorithm independently of timing, I also ran an eight-case, file-shaped
state-machine harness against the observed 0.8.2 boundary: Space creation and ownership reporting
are distinct mutations; tab creation is a separate mutation; every mutation is followed by an
exact reread. The harness used a per-scope mutex, a durable `creating` intent, monotonic lifecycle
generations, conservative adoption, and exact compensation. It ran with:

```sh
uv run pytest -q /tmp/bh-2m1yw-3-probe.E67MtZ/test_recovery.py
```

Recorded result: **8 passed in 0.49s**. The cases were first plus second named tab, concurrent
duplicate first launch, failed start compensation, crash after intent, crash after Space creation,
crash after tab creation, foreign-content refusal, and stale-generation close followed by exact
close. The harness was temporary evidence, not product code.

### Recovery protocol tested

Use a `scope_key` derived from stable Git worktree identity plus hive identity, not a live Herdr
topology/revision hash and not merely an absolute path. Persist the local record before mutation,
for example under git-common private state as
`bh/herdr/scopes/<scope-key>/agents/<agent-key>.json`. The minimum record is:

```text
scope_key; exact_worktree_id; exact_cwd
operation_id; state = creating | active | failed | lost | closed
lifecycle_generation
space_id?; tab_id?; pane_ids[]              # bounded, staleable pointers
created_by_this_operation[]                 # compensation capability
```

Allocation holds an exclusive lock for `scope_key`, writes and fsyncs `creating` with a newly
incremented generation, rereads the exact session, and then:

1. reuses exactly one owned Space whose tokenized `scope_key` and exact cwd agree;
2. otherwise treats one unmarked same-cwd Space as a create-window candidate and adopts it only
   when it is empty and the durable intent proves this operation could have created it;
3. refuses multiple candidates, wrong cwd, unknown ownership, or any foreign tab/pane;
4. creates or reuses exactly one named tab for `(scope_key, agent_key, generation)`, then starts
   the Agent in its root pane; and
5. rereads exact Space, tab, pane, cwd, ownership, and generation before committing `active`.

A duplicate operation with the same canonical input returns the stored result. A concurrent
different operation waits on the scope lock, rereads, and reuses the winner. A failed start closes
only IDs listed in `created_by_this_operation`, newest child first. It never closes a pre-existing
tab or Space.

On process or host restart, reconciliation ignores pointer existence alone. `creating` becomes
`active` only when the exact owned Space, exact named tab, exact cwd, and same generation are all
observed once; it becomes `retryable` for an empty, exactly owned create-window Space; it becomes
`lost` when nothing remains; and it refuses any ambiguous or foreign content for operator review.

Close takes the same scope lock and requires the caller's expected generation to equal the current
record. It closes only the exact owned tab for that generation. The Space may be closed only after
an exact reread proves it is adapter-owned and has no remaining tabs or panes. A stale close is a
no-op refusal, not cleanup authority.

## Evidence

1. **Herdr 0.8.2 can atomically establish exact cwd and a visible name, but not ownership.**
   `workspace create` accepts `--cwd`, `--label`, and `--no-focus`; its receipt returned Space
   `w2`, root pane `w2:p1`, label `bh-scope-7d91`, and the exact requested cwd. Ownership tokens
   require the separate `workspace report-metadata` command. `tab create` accepts `--workspace`,
   `--cwd`, and `--label`, but the 0.8.2 tab command group exposes no metadata-report command.
   Therefore labels and cwd are correlation evidence, never durable ownership authority.

2. **One exact-worktree Space can contain one named tab per independent Agent.** Renaming the
   root tab returned `w2:t1` with label `agent-alpha`. Creating beta returned `w2:t2`, label
   `agent-beta`, root pane `w2:p2`, and the same exact cwd. The snapshot reported one `w2` with
   `tab_count: 2` and both tabs. No second Space was needed.

3. **An intentional same-Agent split is feasible but remains optional.** Splitting alpha's root
   returned pane `w2:p3` in tab `w2:t1`, with the exact worktree cwd. The snapshot showed alpha
   with two panes and beta with one. This supports cooperative panes inside one Agent tab without
   weakening tab-per-Agent as the baseline.

4. **Failed start is detectable and narrowly compensable.** Starting `probe-fail` in an occupied
   beta pane returned `agent_pane_busy: agent target pane w2:p2 is not an available shell`.
   Closing exactly `w2:t2` succeeded; the following tab list preserved alpha and showed only
   `w2:t1`. No Agent was created and no sibling tab or Space was touched.

5. **Cold restart preserves topology but still requires local reconciliation.** After stopping
   and restarting the exact named session, a fresh 0.8.2 snapshot still returned Space `w2`, tab
   `w2:t1` named `agent-alpha`, panes `w2:p1` and `w2:p3`, and the exact cwd on both panes. IDs are
   useful bounded pointers, but the recovery decision must re-prove all fields because Herdr does
   not store the adapter's durable operation intent or generation.

6. **The local lock and intent close the duplicate-first-launch window.** Two simultaneous
   harness threads allocating the same scope and Agent serialized under one scope lock. Both
   returned the same active record; the fake 0.8.2 inventory contained exactly one Space and one
   tab. This is stronger than relying on a label lookup followed by create, which is inherently
   racy because Herdr accepts no idempotency key.

7. **All relevant crash windows reconcile deterministically.** A crash after durable intent but
   before Herdr mutation reconciled to `lost`; after Space create plus an exact ownership reread it
   reconciled to `retryable`; after tab create it committed `active` only when tab ownership and
   generation exactly matched. These transitions require no topology guess and do not adopt an
   arbitrary same-label object.

8. **Foreign content is an absolute refusal.** An unmarked same-cwd Space containing a manual tab
   caused allocation to raise `Refused` and left both the Space and tab byte-for-byte present in
   the harness inventory. The rule also refuses multiple same-scope candidates, partial ownership,
   wrong cwd, or a mismatched generation. A human may resolve those conflicts; automation may not
   delete or relabel them.

9. **Generation fencing prevents delayed cleanup from deleting a successor.** A close using the
   prior generation was refused with both Agent tabs intact. Closing with the current generation
   removed only that Agent's exact tab and preserved its sibling. Empty-Space close is separately
   gated by a final exact ownership and emptiness reread.

10. **The probe left no external test allocation behind.** `herdr session stop` and
    `herdr session delete` named only `bh-spike-2m1yw-3`; the final session list contained only the
    running `default` entry. The operator's default session and Spaces were never cleanup targets.

## Verdict — **GO**

Local recovery is sufficient. Herdr 0.8.2 supplies exact Space/tab/pane IDs, atomic cwd plus label
at each create boundary, persistent named-session topology, exact snapshots, and narrow close
operations. A durable local creating intent, per-scope lock, monotonic lifecycle generation,
exact post-mutation rereads, and conservative foreign-content refusal convert those primitives
into one-Space-per-worktree and one-tab-per-Agent behavior.

An upstream atomic create-with-metadata/idempotency capability is therefore **not required** for
correctness. It would reduce create-window ambiguity and recovery work, but local state must remain
the durable authority even if Herdr later adds it. Metadata and Herdr IDs remain bounded pointers.

## Recommendation

Implement the Herdr adapter as a small transaction/reconciler behind the core launch port from
`bh-2m1yw.1`:

- define stable `scope_key` and `agent_key` derivation from hive plus Git worktree identity;
- persist fsynced operation intents and generations in git-common private state;
- take a cross-process per-scope lock around allocate, reconcile, compensate, and close;
- baseline one named tab per Agent, with pane splits only on an explicit cooperation request;
- require exact cwd and tokenized ownership rereads after every Herdr mutation;
- retain only bounded Herdr pointers in metadata and receipts;
- refuse foreign, partial, ambiguous, or generation-mismatched inventory without mutation; and
- test each crash boundary with a restartable fake plus an opt-in isolated Herdr 0.8.2 live test.

Track an upstream idempotent `workspace/tab create --operation-id --metadata ...` request as an
optimization, not an implementation blocker. Even if added, keep the local generation fence and
foreign-content rules because delayed cleanup and operator-created content remain local lifecycle
concerns.
