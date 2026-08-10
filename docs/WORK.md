# work — the integration-plane driver

`bh work` drives a single bead from **assigned → merged** through the Agentic Git
Flow lifecycle, so an agent (or human) drives the lifecycle through `bh` instead of
improvising raw `git`. It is a thin facade: each verb composes primitives that
already exist — `bd` (Beads), [`bh worktree`](WORKTREES.md), and per-agent identity.

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
| `bh work brief <id>` | Print the bead's requirements/goals + the hive's validation command. Read-only. |
| `bh work start <epic> --as disp/<name>` | **Dispatcher, epic-only.** Guard epic + `kickoff=approved` + dispatcher seat, open `mol/<epic>` off the integration branch (integration-plane kickoff), mark the epic `in_progress`. Alias of `claim` for an epic. |
| `bh work assign <id> --to <name>` | **Orchestrator-only.** Stamp assignee + provision the worktree with that identity. Leaves status `open`. Seat-typed: epic → `disp/<name>`, any other bead → `dev/<name>`. |
| `bh work claim <id> [--as <name>]` | Worker's ack: re-attach/provision the worktree with your identity + signing, refuse if it's someone else's or the wrong seat, then `bd update --claim` (→ `in_progress`). Prints the brief. |
| `bh work next [--as <name>] [--json]` | **Unattended driver.** Take the next ready bead atomically: pick from ready order, claim, then **re-verify the holder**, retrying the next candidate when another worker won the race. Declines cleanly (exit 3) when nothing is takeable. See [work next](#work-next--the-safe-inbound-transition). |
| `bh work show <id> [--view V]… [--json]` | Render the bead branch's local history (`base..wt/bead/<id>`) from several angles to judge noise before submit. Read-only. See [Self-refine](#self-refine-show--refine). |
| `bh work refine <id> (--plan F \| --autosquash \| --since REF) [--dry-run]` | Squash local checkpoint noise into conventional digests behind a backup branch + a byte-identical gate, retaining per-digest author dates. See [Self-refine](#self-refine-show--refine). |
| `bh work check <id>` | Run the hive's `validate_cmd` against the worktree; propagate its exit code. |
| `bh work submit <id>` | Verify clean conventional-digest history, validate the proposed hash from a **clean checkout**, (push for out-of-process review,) set `review:pending` + open a `bd gate`. Handoff, **not** "done" — leaves the worktree intact. |
| `bh work bounce <id> -m "<reason>"` | **Reviewer.** Send a submitted bead back for changes: resolve every open review gate (no orphan left blocking a later merge) then set `review:changes-requested`. With no open gate it warns and still records the bounce. Points the developer at `resume`. |
| `bh work resume <id> [--as …]` | After review returns `changes-requested`: re-attach a fresh worktree on the bead branch, print the feedback, re-assert the claim (GCs any review gate a raw bounce left open). Address it and `submit` again. |
| `bh work abandon <id> [--rm]` | Release the claim and record the abandon. `--rm` also removes the worktree. |
| `bh work land <id>` | **PR-governed hives only** (`work.landing: pr`): complete a `pr-pending` landing once GitHub reports the PR MERGED — resolve the `gh:pr` gate, close the bead/epic with the squash-proof close_reason. See [PR-governed landing](#pr-governed-landing--worklanding-pr). |

Merge is a **separate role** (the Refiner / merge owner) gated by `bd merge-slot` —
not driven by `bh work`. Never push `main` or run the merge yourself. The molecule wrap-up
`bh work finish <epic>` (alias of `bh work merge <epic> --molecule`) is likewise merge-owned.
On a PR-only-main hive (`work.landing: pr`) the merge owner's `merge`/`finish` opens a GitHub
PR instead of local-merging — see [PR-governed landing](#pr-governed-landing--worklanding-pr).

## Identity & signing

Each worktree gets a git identity stamped at `claim`/`assign`, configured under the
`work.identity` section of `config.yaml` (per-hive override under
`managed_repos[*].work`). Two modes:

- **`agent`** — stamp a distinct author (`user.name` = the seat identity,
  `user.email` = a stable attribution address) plus a dedicated **SSH signing key**
  (`gpg.format ssh`, `user.signingkey`, `commit.gpgsign`).
- **`supervised`** (or no `identity` block) — change nothing; the worktree inherits
  the human's existing git + signing config. This is the "under my keys, with direct
  supervision" mode.

The seat identity resolves: `--as <name>` → `work.identity.name` → `$BH_DEV`
(deprecated aliases `$WS_DEV`/`$WS_CREW` still work, with a warning) → git
`user.name` → `$USER`. The same name is passed to `bd`'s own `--actor` flag so the
claim/assign audit trail is per-agent — `bh`'s CLI flag is always `--as`; `--actor`
is `bd`'s flag, not `bh`'s (don't pass `--actor` to `bh work` verbs).

### The host underneath — `bh host identity`

`supervised` mode inherits the host's git config, which presumes there *is* one. On a
provisioned host there is no human and no config, so it inherits nothing: a commit there is
unattributed and unsigned, or refused outright by git for want of `user.email`.
**`bh host identity`** establishes that missing precondition. It also runs as a provisioning
step, immediately after `hq clone`.

| Half | Lives in | Established by |
|---|---|---|
| signing key (per host) | `host.yaml` | `bh config init` — minted once, never rewritten |
| operator name + email (fleet-wide) | `fleet.yaml`, under `work.identity` | `bh hq init`; reaches a host with `hq clone` |
| trusted public keys (fleet-wide) | `allowed_signers` in the HQ store | each host enrolls its own public key |

The two halves become available at *different* provisioning steps, which is exactly why
`config init` cannot own this: an operator's name and email do not exist on a host until HQ is
cloned, two steps later.

It **fills gaps only** — a value git already carries is always kept, there is no `--force`, and
a host with a working human identity comes out byte-identical. It never invents an identity:
name/email come from bh's config and nowhere else (not `$USER`, not `gh`, not the OS user). The
signing key is per-host and *referenced*, never copied — only public material is ever published.

A host that never provisions falls back to whatever git already has and is **warned**, not
blocked: `bh host provision`'s verify gate reports `git identity` as a first-class check, so the
state is loud rather than discovered at the first refused commit.

### Enforcing signatures at merge — `work.enforce_signing`

Off by default. Turned on, `merge` / `finish` / `merge --group` refuse a branch unless **every**
commit in the merge range verifies as trusted (`git log --format=%G?` = `G`), naming the
offending commits and merging nothing. `U` — signed, but by a key not in `allowed_signers` — is
a refusal too: the check is real trust, not signature presence.

**No grandfathering.** A branch whose commits predate the flag being turned on is refused like
any other. A commit date is trivially rewritable, so a date-scoped exemption would let precisely
the unsigned commits this gate exists to stop onto `main`, unauditably. Re-sign them
(`git rebase --exec 'git commit --amend --no-edit -S' main`), or leave the flag off.

It needs a real `gpg.ssh.allowedsignersfile`: without one git reports even a correctly signed
commit as `N`, and the gate would refuse everything. `bh host identity` wires it up.

## `work next` — the safe inbound transition

`bd update --claim` is **not** a compare-and-swap. Two drivers racing for the same bead can both
see exit 0; the last write wins. So every external driver that reads `bd ready` and then claims
what it picked reimplements the same race — separately, and usually wrong. `bh work next` is the
one entry point that gets it right:

1. **pick** the first eligible bead in ready order (dependency-ordered, release-scored when the
   hive configured a strategy — never re-sorted here, which would override the hive's policy);
2. **resolve the seat** server-side per AGF's recursive dispatch rule — an epic candidate needs
   `disp/<name>`, any other bead needs `dev/<name>`. A caller who declares no seat (`--as` names
   no `disp/`/`dev/` prefix) gets it resolved for them; a caller who DOES declare a seat is
   validated against each candidate, not trusted — a mismatch skips that candidate rather than
   claiming it wrong;
3. **claim** the resolved identity (`--as` > config > `$BH_DEV` > git, re-qualified per step 2);
4. **re-verify** by re-reading the bead: we hold it only if the store says the assignee is us
   *and* the bead left `open`. A zero exit from the claim proves nothing;
5. on a lost race, **retry with the next candidate** — losing is the normal case under an
   unattended dispatcher, not a failure;
6. once a claim is won, **provision the worktree** through the same `worktree.ensure` path
   `claim`/`assign`/`start` already use, stamp identity worktree-scoped, and report both in the
   `--json` envelope (`worktree` path + `identity`). A provisioning failure releases the claim
   (bead reopened, unassigned) rather than leaving it orphaned.

### Exit codes — the machine contract

`bh work next` is built for an **external scheduler**, so its exit codes are a stable contract,
not incidental to a CLI:

| Exit | `status` | Meaning |
|---|---|---|
| `0` | `claimed` | Held a bead; `worktree`/`identity` are populated in `--json`. |
| `1` | — | Generic hard error (e.g. a provisioning failure after a won claim). |
| `2` | — | Typer/click's own usage-error exit (bad flags etc.) — **not emitted by this verb's logic**, reserved so a caller can always tell "you passed a malformed flag" apart from a refusal. |
| `3` (`NEXT_DECLINE_EXIT`) | `declined` | Nothing takeable right now (`empty_queue` / `none_eligible` / `all_lost`) — a normal poll result; back off and retry. |
| `4` (`NEXT_REFUSE_EXIT`) | `refused` | The caller declared a seat (`disp/<name>`/`dev/<name>`) that mismatched every candidate it could otherwise have taken (`reason: seat_mismatch`) — a permanent refusal for this identity, not "try again later". |

`refused` fires only when EVERY surviving candidate was seat-mismatched; a mismatch elsewhere in
a mixed queue does not block a legitimate claim (the mismatched id still shows up in the
`refused` list for visibility).

### The `--json` envelope (schema v1, `NEXT_SCHEMA`)

One stable key set for every outcome (`claimed` / `declined` / `refused`) — a consumer branches
on `status`, never on which keys happen to be present:

| Key | Meaning |
|---|---|
| `schema_version`, `command` | the versioned-envelope convention every `bh … --json` payload shares (`jsonout.envelope`); `command` is `"work next"`. |
| `status` | `claimed` \| `declined` \| `refused` — see the exit-code table above. |
| `bead` | the claimed bead's id, or `""` on decline/refusal. |
| `actor` | the identity a claim was **attempted or held** under — server-resolved (step 2 above) when the caller declared no seat. |
| `seat` | `"developer"` \| `"dispatcher"`, derived from `actor`'s prefix. |
| `hive` | the resolved hive name. |
| `worktree`, `identity` | populated only once a claim is won and provisioned (bh-qczj.2); `None` on decline/refusal — nothing was provisioned. `identity` is `{mode, name, email, signing_key, sign}`. |
| `reason` | `""` on a claim; a decline reason (`empty_queue` \| `none_eligible` \| `all_lost`) or `"seat_mismatch"` on a refusal. |
| `tried` | every candidate an actual claim was **attempted** against, in order — the ones that survived `eligible()`'s filters and either won or lost a real race. Empty when nothing was even eligible. |
| `refused` | candidates skipped for a declared-seat mismatch, independent of whether the call ultimately claimed something else — always surfaced for visibility, even alongside a successful claim on a different candidate. |

### Decline semantics — three reasons, all "try again", none an error

A decline (exit 3) is the **normal** shape of an empty poll, not a failure — but the reason
still matters to a scheduler deciding *how* to retry:

| `reason` | When | What it means for the caller |
|---|---|---|
| `empty_queue` | `bd ready` returned nothing at all. | The hive genuinely has no ready work right now. Back off longer. |
| `none_eligible` | Ready rows existed, but every one was filtered — closed, in-flight, or assigned to somebody else. | Other workers already hold the visible queue. Back off normally. |
| `all_lost` | At least one candidate was tried, and every one was claimed out from under this caller. | Real contention: another driver is live and fast. Retry immediately is fine (there is no reason to expect the SAME candidates to still be free), but expect this to resolve once the other driver moves on. |

`refused` (exit 4) is a fourth, DISTINCT outcome and deliberately not folded into decline: it
means the caller declared a seat (`dev/`/`disp/`) that can never match what's in the queue —
retrying the same way will never help. `empty_queue`/`none_eligible`/`all_lost` all mean "ask
again later"; `refused` means "you're asking wrong."

### The concurrency guarantee — what is (and is NOT) promised

**The verb's whole reason to exist**: two drivers can never both end up believing they hold the
same bead. That is proven under real thread concurrency in `tests/test_work_next.py` (the
"concurrency contract" section) — two genuine racers against one ready bead, and against a queue
of several, with a mutation test showing the guard is load-bearing (below).

**What IS guaranteed:**

- At most one caller's `bh work next` ever reports `status: claimed` for a given bead. A caller
  that loses the race gets a clean `declined`/`all_lost` — never a crash, never a false belief
  that it holds a bead it does not.
- Losing a race **advances**, not drops: within one `next_()` call the loop tries the NEXT
  eligible candidate; across repeated calls (an unattended poll loop), a bead lost in one tick is
  simply absent from the NEXT tick's `bd ready` (someone else now holds it) and the loop moves on.
  No candidate is silently skipped because of a lost race.

**What is NOT guaranteed — and why:**

- `bd update --claim` is **optimistic, not a hard compare-and-swap**. Its exit code alone proves
  nothing: two callers racing the same bead can both get exit 0, with the last physical write
  winning the store. The guarantee above comes ENTIRELY from the re-verify step
  (`work_next.claim_won`) that runs after every claim attempt — it re-reads the bead and requires
  BOTH that the assignee is us AND that the bead actually left `open`. Skip that step (trust the
  exit code instead) and the SAME race produces two callers who both believe they won — this is
  not hypothetical, it is exactly what `test_claim_won_reverify_is_load_bearing_without_it_both_drivers_win`
  demonstrates by monkeypatching `claim_won` back to "always true" and re-running the identical
  race.
- There is a real, if small, window between a claim committing and this driver's re-read of it.
  `bh work next` closes that window for **this driver's own belief** (it never reports success
  falsely), but it does not make the underlying claim atomic — a losing driver can still have
  spent real work (a `bd update --claim` round trip) on a bead it never gets.

**What an external scheduler must therefore do**, because this verb's contract is "optimistic
claim + honest re-verify", not "atomic reservation":

1. Treat `declined`/`all_lost` as expected, ordinary traffic under contention — not a bug to
   retry-with-backoff-and-alert on. Retry the poll; do not retry the SAME candidate assuming it
   is still free.
2. Never infer "I hold bead X" from anything other than this verb's own `status: claimed`
   response for THAT call. In particular, do not build a scheduler that first reads `bd ready`
   itself and separately calls `bd update --claim` — that reimplements the exact race this verb
   exists to survive, without the re-verify step that makes it safe.
3. Poll, don't assume ordering: because the underlying store has no true CAS, two schedulers
   started at the same instant may both see the same candidate as "the" next one; only their
   respective `bh work next` calls, not their own `bd ready` reads, decide who actually gets it.

### The decision core (`beadhive/work_next.py`)

The companion pure module — no typer, no `bd`, no git, same shape as `schedule.py` /
`molecule.py` — answers the other half of the question: given a molecule, *what* should happen
next. It is a **12-row first-match priority table** (`done`, `not_dispatchable`,
`halt-on-escalation`, `start`, `resume-changes-requested`, `merge-exactly-one`, `review`,
`finish`, `wrap_up`, `dispatch-up-to-budget`, `wait`, `deadlock-escalate`) returning one
`Decision`, plus a **loop-breaker**: on the Nth identical `action:bead`
(`work.dispatch.max_action_retries`, default 2) the decision converts to `escalate` with a
closed-set reason code (`not_dispatchable` | `deadlock` | `repeated_changes_requested` |
`repeated_merge_failure` | `ambiguous_gate` | `stuck`).

**Nothing stores an attempts counter.** Failure causes are written to beads with
`bd set-state … --reason` (which records an event bead *and* refreshes the `<dim>:<value>` label
cache) and the count is **derived** by counting those event beads. A stored counter would be
runtime state living outside beads — which the runtime epic's invariant forbids — and would need
a staleness/reconcile rule that derivation does not.

Known limit: the event record is incomplete today (of 453 `issue_type='gate'` rows only 406 carry
a created event), so a derived count can **under**-count and the loop-breaker fires late rather
than early — the safe direction. `bh-gj0v9.2` owns classifying that as defect or by design.

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
  runtime: local                 # which scheduler wakes a role binary: claude | local | temporal
                                 # (default local — see "Runtime tiers" below)
  landing: local                 # how merge/finish land on the SHARED integration branch:
                                 # local (--no-ff merge, default) | pr (push + GitHub PR;
                                 # PR-only-main repos — see "PR-governed landing" below)
  push_remote: origin            # remote branch pushes target (submit's gh:* publish + landing: pr)
  integration_branch: "main"     # base the bead branch is measured against
  max_commits: 10                # submit rejects more commits than this over base
  enforce_signing: false         # true: merge refuses ANY commit in the range that isn't
                                 # verifiably signed (see "Enforcing signatures at merge")
  identity:
    mode: agent                  # agent | supervised
    name: "dev/claude"
    email: "agents@example.dev"
    signing_key: "~/.config/bh/keys/claude.pub"
    sign: true
```

`submit` only **pushes** the branch when `review_gate` is `gh:run`/`gh:pr` (CI must
see it); a purely local reviewer sharing the object store needs no push.

## Runtime tiers — `work.runtime`

AGF's lifecycle advances only while something wakes up and runs `bh work` verbs against a
ready bead. `work.runtime` picks **what does that waking up** — three tiers behind one config
key, mirroring the `beads.engine` seam in `engine.py` (a config key selects a thin
implementation, not a plugin framework). Full rationale, evidence, and the role-binary
contract every tier schedules the same way: `docs/design/work-runtime-tiers-adr.md`.

```yaml
work:
  runtime: local    # claude | local | temporal, default local
```

| Tier | Wake-up mechanism | Infra | Target | Status |
|---|---|---|---|---|
| `claude` | Task-tool sub-agent fanout — a human's (or headless) Claude Code dispatcher session issues `Task` calls directly | none | one session, harness-bound | **documented, not developed** — this is today's behavior, already works; bh never schedules anything in this tier (see below) |
| `local` | poll loop + `asyncio.create_subprocess_exec` | none | solo / small team — the harness-agnostic default | **implemented** — bh-c6dk.5, `beadhive.localloop`; run it with `bh work loop <epic>` |
| `temporal` | workers polling task queues | Temporal server | fleet, multi-hive, multi-machine | not yet implemented — bh-c6dk.4 |

**The invariant every tier must preserve — no runtime-only lifecycle state.** Gates are
resolved in beads (`bd gate`). Leases are held in beads (`bd heartbeat` / `bd reclaim`). The
merge slot lives in beads (`bd merge-slot`). In every tier, without exception. A runtime MAY
keep a richer *execution* record (retry counts, timings, why something was retried) but is
NEVER authoritative about whether a bead is claimed, blocked, approved, or done. Two
consequences follow directly:

1. **`bh work approve` works with no runtime running.** A human resolves a gate against beads
   directly; whatever runtime is active observes the resolution on its own schedule.
2. **Tiers are switchable mid-flight.** Because no tier holds authoritative state, stopping a
   local loop and starting Temporal workers against the same hive is a restart, not a
   migration.

`claude` cannot be the default precisely because it is the one tier bound to a specific
harness — `local` is the harness-agnostic floor every hive can run with zero infrastructure.

### The `Runtime` seam

`src/beadhive/runtime.py` names the `Runtime` protocol: three operations, nothing else —
`schedule` a role binary for a bead, `observe` whether it finished, and `on_gate_resolved`
react to a `bd gate` the scheduler was waiting on. `config.work_runtime()` reads the config key
(tolerant getter, falls back to `local`); `runtime.get_runtime()` resolves it to a concrete
implementation, raising `NotImplementedError` naming the sibling bead for any tier not yet
built (today: `temporal`, bh-c6dk.4).

### The `local` tier — `bh work loop <epic>`

`bh work loop <epic>` drives one molecule to completion with **no human present and no server
running**. One pass, repeated at `work.dispatch.poll_interval`:

```text
bd gate check -> bd reclaim -> renew the host lease (only while workers are active) ->
bd heartbeat each claim -> enforce the caps -> harvest finished seats (stdout-first) ->
work_next.decide -> claim through `bh work next` -> spawn the seat for the decided action
```

Everything it needs is re-derived from `bd` each pass, so **a restart is a no-op by
construction** and the process is meant to run under a supervisor that restarts it. Nothing is
persisted outside beads: the in-flight map is deliberately volatile, failure causes are written
onto the bead with `bd set-state` (an event bead + a `dispatch:<cause>` label), and retry counts
are DERIVED by counting those event beads rather than stored anywhere.

Three behaviors are correctness requirements rather than implementation choices, all measured in
spike molecule `bh-a7so` and recorded in the ADR's Amendment 2 §5:

- **Every seat runs in its own process group** (`start_new_session=True`) and is reaped with
  `os.killpg`, SIGTERM then SIGKILL, polling until the group is *actually* empty. Signalling only
  the direct child orphans the agent underneath it, which then runs the whole task to completion
  and spends a full run of tokens into a pipe nobody holds.
- **SIGINT is never sent.** A SIGINT-cancelled run exits `0`, colliding with the contract's
  `0 = done`. Attempting it raises `localloop.ForbiddenSignal`.
- **The pipe is held from spawn, and the envelope is read before the reap.** Cancelling through
  the ladder — cooperative wrap-up over stream-json stdin, then a `control_request` interrupt,
  then SIGTERM — always returns a priced envelope, so a cancelled run is still attributable.

A seat left running by a loop that was `kill -9`'d is **reaped, not adopted**: its pipes died
with its parent, so it could be neither cancelled nor priced. A restarting loop finds it from
the bead ids plus `ps` (no state on disk), kills the group, and records the cause.

Run the whole thing against a throwaway hive with
[`scripts/demo_local_loop.py`](../scripts/demo_local_loop.py).

### Why `claude` has no code to run

In the `claude` tier, `bh` itself never calls a `Runtime` object: the dispatcher session
issuing `Task` calls *is* the scheduler, entirely outside this seam. `runtime.ClaudeRuntime`
exists only so `get_runtime()` has a concrete instance to hand back — every one of its methods
raises `NotImplementedError` on purpose, so a caller that mistakenly tries to route through the
seam in this tier gets a loud, actionable error rather than a scheduler that silently does
nothing.

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
bh work show <id>                 # default `log` view
bh work show <id> --view sig --view stat
bh work show <id> --json          # machine input for building a refine plan
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
bh work refine <id> --plan plan.json     # explicit squash plan (or `-` for stdin)
bh work refine <id> --autosquash         # fold fixup!/squash! into their targets
bh work refine <id> --since <ref>        # fold <ref>..tip into one digest
bh work refine <id> --plan plan.json --dry-run   # print the would-be log; change nothing
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
`bh work refine <id> --autosquash` — the folds are contiguous, so the rebase is
conflict-free and per-digest dates reflect real cadence.

## Molecule integration branch (two-level)

Kickoff lives on the **integration** plane, not the planning plane. After `bh plan approve`
readies an epic's beads (it does *not* create a branch), a dispatcher opens the molecule:

```bash
bh work start <epic> --as disp/<name>
```

`start` guards that the bead is an epic, is `kickoff=approved`, and that you act as a
dispatcher (`disp/<name>`), then opens `mol/<epic>` off the integration branch and takes the
epic seat. (If `start` is skipped, the first `bh work assign`/`claim` of a child lazily opens
`mol/<epic>` too — as long as the epic is `kickoff=approved`.) Bead worktrees in that molecule
fork off `mol/<epic>` instead of `main`, so intra-molecule dependencies compose correctly —
bead B sees bead A's already-merged work.

`bh work merge <bead>` lands each bead into `mol/<epic>` (not `main`). When all beads are
merged, the dispatcher runs the wrap-up verb:

```bash
bh work finish <epic>            # epic-only alias of: bh work merge <epic> --molecule [--rm]
```

This:

1. Guards that the molecule is complete — all child beads are closed.
2. Validates the assembled `mol/<epic>` branch with the hive's `validate_cmd` (skipped under `loose`).
3. Lands `mol/<epic>` onto the integration branch as one `--no-ff` merge bubble.
4. Closes the epic + swarm and deletes `mol/<epic>`.

The result is a single merge bubble on the integration branch containing all of the
molecule's bead merges — `main` stays untouched and always-green until the whole molecule
is ready.

**Seat enforcement:** an epic may only be assigned to / started by a dispatcher
(`disp/<name>`); any other bead only by a developer (`dev/<name>`). A non-seat (human /
supervised) identity is exempt.

**Backward-compatible:** a bead whose epic has no `mol/<epic>` branch (older molecules or
beads filed outside a molecule) still targets the hive integration branch unchanged.

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
  never rewritten — the land was intentional; bh escalates loudly and leaves the bubble for a
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

## PR-governed landing — `work.landing: pr`

Some repos enforce a **PR-only `main`** (branch protection: no direct pushes). There, bh's
local `--no-ff` land can't be pushed — and a GitHub **squash-merge** of a hand-opened PR
defeats every local landed signal (no ancestry, no bh close_reason, no patch-id match),
leaving the seat UNMERGED forever. `work.landing: pr` makes the shared-branch boundary
PR-governed end to end:

- **`merge <bead>` / `finish <epic>` onto the integration branch** do NOT local-merge. They
  push the branch (`work.push_remote`) and open a PR via `gh pr create` (title from the bead
  digest; body carries the id + acceptance). The PR is recorded on the bead
  (`landing=pr-pending` state, reason `PR #<n> <url>`), a **`gh:pr` bd gate** blocks the bead,
  and the bead/epic **stays OPEN**. The assembled-molecule pre-land validation still runs (a
  red molecule never reaches the PR); the post-land tip validation's role passes to **CI on
  the PR**. Re-runs are idempotent — the open PR and its gate are reused.
- **Only the shared boundary is PR-governed.** A bead landing into its molecule container
  (`wt/bead/epic/<epic>`) merges locally `--no-ff` exactly as before; nested container lands
  are unchanged.
- **`land <id>` completes the landing** once GitHub reports the PR MERGED
  (`gh pr list --state merged --head <branch>`): it resolves the `gh:pr` gate (bd's own gate
  watcher may beat it — either order is fine) and closes the bead with close_reason `merged`
  (`molecule landed` for an epic — which also closes adopted origin reports and tears down the
  coordinator seat). It **refuses while the PR is unmerged** — completion is driven by PR
  state, never asserted.
- **Teardown is squash-proof.** `bh worktree prune`'s landed detection now also asks gh for a
  MERGED PR with the branch as head (GitHub-backed hives, best-effort, fail-closed), so a seat
  squash-merged on GitHub classifies LANDED and is reaped even without the `land` close.
- **Escape hatch:** `bh worktree mark-landed <bead-or-branch>` stamps the authoritative
  close_reason `merged` so an operator can assert a fully out-of-band landing (hand-squashed,
  landed from another machine, non-GitHub remote) and unstick a seat. Prefer `work land` when
  a PR exists to check.

`gh` is required only when the pr mode is actually used (never at import); with
`landing: local` (the default) behavior is byte-identical to before this mode existed.

## Conflict handling in `bh work merge`

`bh work merge` resolves merge conflicts through a four-tier ladder — each tier is tried
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
baseline). Per-hive override: `managed_repos[*].work.conflict.union_globs`.

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
2. **Validation-gated.** After a successful union merge the hive's `validate_cmd` is
   re-run from a clean checkout. On failure, the integration branch is hard-reset to its
   pre-union tip and the bead branch is restored from its backup ref before bouncing —
   the broken result is never committed.

## Batch groups — when not to batch

The default unit is one bead per worktree, and **that is the right call whenever beads are
independent**: separate worktrees give you parallel wall-time and each bead lands on its own
clean conventional history. A *work group* batches several beads into one shared
`wt/batch/<group>` worktree, validated and merged **once** as a single `--no-ff` bubble. The
claim/implement/merge mechanics are in the `developer` skill; the scheduling decision (three
triggers, four guards) is in the `dispatcher` skill. This section is the safety
reference — when batching is wrong and why.

### Completing a batch

A batch is claimed, implemented, reviewed, and landed **as a unit** — the per-bead
`submit`/`check` verbs do **not** apply to a batch member (they have no per-bead worktree, and
running them on one prints the batch procedure). The whole flow happens in the ONE shared
`wt/batch/<group>` worktree:

1. `bh work claim --group <ids> --as dev/<name>` — provision the shared worktree and claim every
   member. (For an epic's un-batched children, `bh work claim --collapse <epic>` is preferred: it
   synthesizes the `batch:<epic>` label and provisions the epic container so the batch lands into
   it, not `main`.)
2. Implement every member in that one worktree, one clean conventional commit (or few) per bead.
3. `bh work submit --group <ids>` — validate the shared branch once and open **one** review gate
   whose reason names every member.
4. `bh work approve <any member>` — one approval clears the single gate for the whole batch.
   `bh work bounce <any member>` bounces the **whole** batch back (resolves the one gate, sets
   `changes-requested`); address feedback in the shared worktree and `submit --group` again.
5. `bh work merge --group <ids>` — land the batch as one `--no-ff` bubble into the members'
   molecule container (per-bead commits preserved inside). Under `review_gate: human` this refuses
   unless the batch was submitted **and** approved (no zero-review landing).
6. `bh work finish <epic>` — once the molecule is complete, land the assembled container onto the
   integration branch.

### Guards — when a candidate is not batched

Any guard failure drops the candidate back to singletons. The guards exist because a batch
fails **as a unit** — there is no partial landing.

| Guard | Rule |
|---|---|
| **Cohesion** | Members must share a `component` or be contiguous in the dep DAG. A scattered group is hard to review and fails together. |
| **Size cap** | At most `work.batch_max_size` (default 5) members. Keeps the merge bubble small enough to review and bisect. |
| **Single model tier** | A group runs on one model; explicit `model:` conflicts are refused (members may omit `model` to inherit). |
| **No mixed review gates** | Members must share a review gate; mixing `gate:` overrides is refused so one approval covers the whole bubble. |

Planner-declared `batch:<group>` groups are validated against these rules at `bh plan file`
time (see [PLANNING-PLANE.md](PLANNING-PLANE.md)). Auto-detected linear chains are
re-validated by the scheduler at dispatch time (`bh work schedule`).

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

## Exclusive primary — which host may run a write verb

On a multi-host factory the write verbs are gated by `guard_primary()`
(`src/beadhive/guard.py`, beside `guard_hq_registry_write` / `guard_hub`). Only the host
holding a hive's **host lease** — `refs/bh/lease/<prefix>` in HQ, see
[design/multi-host-model-adr.md](design/multi-host-model-adr.md) — may write it.

| | verbs |
|---|---|
| **Gated** | `bh work assign` · `claim` · `submit` · `merge` · **`bh plan file`** |
| **Never gated** | `ready` · `list` · `show` · `brief` · `issue` · `review` · `bh sync`, plus every `--preview` / `--dry-run` path |

`bh plan file` is the non-obvious one and the most important: creating children under a
shared parent from two hosts is literally the
[beads#4796](https://github.com/gastownhall/beads/issues/4796) trigger — both allocate the
same child id, the next pull hits an unresolvable PK collision, and sync blocks indefinitely.
Filing from a follower is the known-broken path, not an edge case.

Behavior worth knowing:

- **A factory that has never adopted is not gated at all.** No host lease recorded means
  single-host default, unchanged; exclusive primary switches on when a host adopts.
- **The check is offline.** It reads the *cached* lease in this host's HQ clone, not HQ over
  the network, so an established primary keeps working while HQ is unreachable.
- **A lapsed lease is refused**, including this host's own — that window is exactly when
  another host may have taken over.
- **The refusal names the holder and its expiry**, so the next action is obvious: adopt on
  this host, or run the write on the host named.
- **The gate sits before the merge, never around its close.** A `bd close` (or an operator's
  `bd close --force` retry) after a merge that already landed is bookkeeping cleanup and is
  never blocked by this guard.

### The claim fencing token

`guard_primary` answers "may this host write *right now*?". A worker that claimed a bead six
hours ago needs the other question answered too — *is the claim I have been holding still
backed by the generation in force?* — so `bh work claim` / `resume` stamp the `ClaimRecord`
with this host's `host_id` and the current **epoch** (the adopt generation), and `bh work
submit` verifies it (`guard_claim_epoch`).

The two checks compose, and neither subsumes the other:

- the host lease lapses and nobody re-adopts → `guard_primary` refuses;
- the lease is lost and **this** host later re-adopts → the new generation is live, so
  `guard_primary` is satisfied, but every claim minted under the old epoch is superseded.
  Only the token sees that. The epoch always advances on adopt (never on `renew`) precisely so
  a stale token is detectable.

**A refused submit is not lost work.** The gate is on bead *writes*, never on code, so the
branch stays pushable exactly as it stands — the refusal says so, and prints both recovery
paths: re-adopt on this host then re-ack (`bh work claim <id> --as <seat>` re-stamps the token
under the new epoch) and re-submit, or push the branch and let the current primary land the
bead updates under its own epoch.

The epoch is read from the *cached* host lease — no network, so claiming stays cheap and
workers never poll. `refs/bh/epoch` beside the hive's data remains the enforcement truth (its
CAS is what makes the write itself safe); the two carry the same generation by construction,
because the two-phase adopt records phase 1's epoch into phase 2's lease.

A record with no token (written before this shipped, or on a factory that never adopted) fails
**open** — it is never refused for a token it was never issued.

### Direct `bd`: the pre-push fence hook + doctor warning

`guard_primary` only gates `bh work`'s own write verbs — a raw `bd` invocation never goes
through it at all. That gap is bounded (a direct `bd` write lands only in the local Dolt
replica; it cannot corrupt the remote, because the real fence is at push,
[design/multi-host-model-adr.md](design/multi-host-model-adr.md) Amendment 1 §2), but it can
still leave an operator building an hour of work on a doomed local state before discovering,
only at push time, that this host was never primary. Two defence-in-depth layers close that
gap early — both **local reads, neither needs HQ over the network**, both reusing
`guard.primary_state`'s cached-lease read so they can never disagree with `guard_primary`
about who is primary:

- **A `pre-push` git hook**, furnished by `bh hive init` (`src/beadhive/prepush.py`) —
  **independent of the furnish axis**: a git hook is never tracked in a repo's history
  regardless of a hive's declared footprint, so a `furnish: none` hive gets it exactly the
  same as a furnished one. Installed into every place a `refs/dolt/data` push can actually
  happen (the hive's own `.git/hooks/`, and any existing bd-embedded git-transport bare repo
  under `.beads/` — see the module docstring for why a not-yet-created transport repo is a
  harmless gap, not a real one). Non-destructive: an operator's own pre-existing `pre-push`
  hook is left untouched. The hook filters on the ref name in `sh` (no `bh`/python startup
  for an ordinary code push) and shells out to the hidden `bh hive check-push-fence` verb
  only for a push that actually touches `refs/dolt/data`.
- **`bh doctor`** reports "N local commits made while not primary" per hive: local
  `refs/dolt/data` commits dated after the CURRENT lease holder's `adopted_at`, for any hive
  this host does not currently hold the lease for. Bounded on purpose — only commits made
  strictly after primacy demonstrably passed elsewhere count, not every unpushed local commit
  a healthy single-host workflow racks up between pushes.

Both are **bypassable, and documented as such where an operator actually reads it** (the
hook's own refusal text): `git push --no-verify` skips the hook entirely, and that is fine —
it is a convenience, an early legible refusal, not the enforcement. The real backstop is the
same atomic `--force-with-lease` push fence beside the hive's own data (`refs/bh/epoch`,
`src/beadhive/host_fence.py`) named above: a stale-epoch push is rejected there regardless of
`--no-verify`.

## The role-binary contract

Every runtime tier (`claude` today; `local`/`temporal` per
[work-runtime-tiers-adr.md](design/work-runtime-tiers-adr.md)) schedules the same thing: a
compiled seat binary (`bh-<seat>`, built by `beadhive/baml-harness` from a bundle — a
**different repository**, not this one) that is spawned once per turn and reports back over
stdout and its exit code. This is the **normative** shape, matching
[work-runtime-tiers-adr.md Amendment 2 §1](design/work-runtime-tiers-adr.md#1-the-settled-contract)
— not Amendment 1's earlier, superseded contract block.

```text
bh-<seat> --workspace <path> --bead <id> --instructions <file|->
          --session_id <uuid> [--model <tier>]
```

| | |
|---|---|
| **Baked at build** | `PROVIDER` · `permissions` · `permission_mode` · `mcp_config` · `plugin_dirs` · packs digest · the seat prompt (including the interrupt protocol and the commit-after-every-step invariant). A runtime attempt to supply or override any of these is refused — authority is a property of the compiled binary, never of the invocation. |
| **`--workspace`** | The bead's worktree path. Validated before spawn: at minimum it must exist and be a directory; preferably it is a git worktree. A bad path yields a typed result (`beadhive.seatrun.validate_workspace`), never a raw `ENOENT` traceback indistinguishable from an unimplemented-provider panic. |
| **`--bead`** | First-class, not embedded in prose. Makes the round-trip checkable: did the agent work the bead it was handed? `RoleOutcome.bead_id` is cross-checked against it (`beadhive.seatrun.classify_run`'s `bead_id_mismatch`). |
| **`--instructions <file\|->`** | The task/brief — `-` reads stdin, so a brief that outgrows argv limits still fits. |
| **`--session_id <uuid>`** | **Required on create.** The scheduler mints it before spawn — it needs a stable key for the `{bead_id -> (proc, stdin, pgid)}` map it holds to cancel and to notify siblings, and for span/audit correlation from t=0. |
| **stdout** | One line of `SeatRun` JSON — the rich channel. `outcome.status` is the source of truth whenever stdout parses. |
| **exit code** | Target taxonomy: `0` done · `10` blocked · `11` handoff · anything else = did not complete (stdout may be absent). **Unbuilt upstream today** — see the warning below. |
| **RECOVERY** | Re-dispatch a fresh turn against the same worktree; the branch is the checkpoint. `--resume_session` stays on the binary as an optional continuation affordance (measured 1.30× the cost of a fresh turn), never the checkpoint primitive. |
| **CANCEL** | A three-rung ladder — see below. |
| **INVARIANT** | Re-running against an already-advanced bead is a no-op. Mechanized by `beadhive.seatrun.already_advanced`. |

### `SeatRun` / `RoleOutcome`

```text
SeatRun    { outcome: RoleOutcome, session_id, cost_usd, usage, packs }
RoleOutcome{ status: "done"|"blocked"|"handoff", summary, bead_id?, next_action? }
```

`status` is the escalation channel; `summary`/`next_action` are the payload; `packs` are
digest-pinned build provenance. `usage`/`cost_usd` are exact (agreement to ~0.02% against list
pricing) and are safe bases for a budget.

A killed run still emits a priced **envelope** roughly 0.63s later — `session_id`, `cost_usd`,
`usage`, and a `terminal_reason`, but no `outcome` (there was nothing to report). Parse it with
`beadhive.seatrun.parse_envelope`; `classify_run` falls back to it automatically whenever a full
`SeatRun` doesn't parse.

### ⚠ exit code `0` never means "succeeded"

The `0/10/11` taxonomy above is a **target**, unbuilt in the upstream binary as of this writing.
Today exit is a 2-value signal: `0` whenever a `SeatRun` came back on stdout *whatever its
`status` says* (a `blocked` result exits 0, same as `done`), `1` whenever the harness threw
before producing one. **Every caller must treat exit `0` as "go read stdout", never as
"succeeded."** `beadhive.seatrun.classify_run(exit_code, stdout, bead=...)` is the single shared
helper that encodes this rule — it is the only place any tier is meant to consult stdout/exit
classification, and it never derives `done` from a bare exit code. Use it rather than
re-deriving the rule per call site.

### CANCEL — a three-rung ladder

1. **Cooperative** — write the wrap-up instruction to the seat's stdin
   (`--input-format stream-json`); the seat finishes its in-flight tool call, commits
   `wip: interrupted`, emits `INTERRUPT_ACK`, and exits 0 with a `subtype: success` envelope.
2. **Hard** — `{"type":"control_request","request":{"subtype":"interrupt"}}` over the same
   stdin channel.
3. **Signal** — `SIGTERM` to the seat process. **Never `SIGINT`** — a SIGINT-cancelled run exits
   `0`, colliding head-on with `0 = done`.

In all three, **the scheduler must outlive the child and hold the read end of the pipe**, or the
priced envelope goes nowhere. This channel cannot live in BAML — `baml.sys.exec`'s
`ProcessOptions.stdin` is a static string fixed before launch and `exec` returns a result for an
already-finished process, so it structurally cannot hold a live bidirectional stream. Ownership
belongs to whichever supervisor spawns the seat and keeps its pipes — `asyncio` in the `local`
tier, the activity worker in `temporal` — not the harness.

### Scope boundary: what's built where

`beadhive/baml-harness` (a **different repository**) bakes the bundle, builds the seat binary,
and owns everything under "Baked at build" above, including provisioning credentials into a
non-`claude-code` provider's baked home directory (`CODEX_HOME`) as a first-class, reviewed
build step. None of that is this repo's code.

What ships in **this** repo (`bh-c6dk.2`):

- `src/beadhive/seatrun.py` — `SeatRun`/`RoleOutcome` parsing (`parse_seat_run`), the shared
  `classify_run` helper, `validate_workspace`, killed-run envelope parsing (`parse_envelope`),
  and `already_advanced`.
- `tests/fixtures/stub_seat.py` — a reference stub seat binary implementing this entire contract
  (including the full `CANCEL` ladder and the target `0/10/11` exit taxonomy) as a test double.
  It exists because `claude-code` is the only provider marked `implemented: true` upstream and
  there is no built `dist/` binary in this repo — the stub is how the loop is tested and
  demoed without a real provider, demonstrating harness-agnosticism rather than dodging it.

## Not yet wired

- Commit-trailer auto-injection (`Agent-Profile`/`Agent-Session`/`Agent-Model`) —
  needs a per-worktree `prepare-commit-msg` hook; config schema first, hook later.
- The merge-owner/Refiner role and the release-plane `cz check` hook are out of
  scope here (see the broader design docs).
