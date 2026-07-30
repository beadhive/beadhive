# Spike `bh-a7so.2` — is restart cost actually bounded by SIGTERM checkpoint + `session_id` resume?

**Bead:** `bh-a7so.2` · **Seat:** `dev/resume` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-a7so.4` (adopt or extend the baked-seat role-binary contract), and
through it `bh-c6dk.2` / `bh-c6dk.5`

> Settles item 2 of *"What is still unvalidated"* in
> [work-runtime-tiers-adr.md — Amendment 1](../design/work-runtime-tiers-adr.md#amendment-1--the-contract-is-baml-harnesss-already-and-authority-bakes-into-the-binary).

<!-- -->

> **PARTLY SUPERSEDED — read this before citing anything below.**
> [`bh-a7so.7`](bh-a7so.7-graceful-interrupt.md) §3/§4/§9/§10 retracted three findings in this
> document. **§2** ("a killed run emits nothing"), **§9** ("the dollar figure for a killed run is
> emitted by nothing") and **§10** (`SeatRun.usage` under-reports by 35–40 %) are **WITHDRAWN**,
> and **Recommendation 4** with them — do not file the `usage` re-check bead it proposes.
> The cause in every case was this spike's own method, not the tool: both kill scopes measured here
> destroyed the process holding the **read end of the pipe** at the same instant as the writer.
> Hold the pipe and the same CLI emits a 1347-byte envelope 0.63 s later carrying `session_id`,
> `total_cost_usd` **and** full `usage`; the 35–40 % gap is a transcript double-count (one line per
> content block sharing a `message.id`), and deduplicating by `message.id` reproduces the envelope
> to the token. **§11**'s "no graceful-cancellation path to speak of" is superseded too —
> `bh-a7so.7` §12 measures a three-rung cancel ladder.
>
> **What this document still governs, unretracted:** **§3** (the `proc.terminate()` orphan —
> signal-independent, and still the most important result in the molecule) and **§7/§8/§12**
> (resume costs 1.30× a fresh turn; the 0.38–0.42 restart bound comes from committed git history in
> the worktree, not from the session). See
> [ADR Amendment 2 §7](../design/work-runtime-tiers-adr.md#7-where-bh-a7so2-was-superseded-and-where-it-still-governs).

## Question

Both runtime tiers in [work-runtime-tiers-adr.md](../design/work-runtime-tiers-adr.md) rest on one
premise: **the supervised unit is small and checkpointed, so restart cost is bounded** — which is
what makes let-it-crash affordable when a crash costs tokens. Amendment 1 names the mechanism:

```text
RESUME          --resume <session_id>
```

and states `session_id` is "the checkpoint primitive both tiers need". Nobody had measured it.

**GO/NO-GO: is restart cost bounded, and is `--resume <session_id>` what bounds it?**

Decomposed into the five things a scheduler must know:

1. What does a **baseline** run cost, in wall time, `cost_usd` and `usage`?
2. Under **SIGTERM** mid-run — does the `claude` child exit cleanly, does a partial `SeatRun`
   reach the caller or is stdout simply lost, is the bead left claimed, is the worktree dirty?
3. Under **SIGKILL** — the same questions. SIGTERM and SIGKILL may differ, and the difference is
   the design.
4. Does `--resume <session_id>` work after each kill, and does it resume somewhere *useful*?
5. **The number** — token/cost of resume vs starting the same bead fresh, and the ratio.

Critically **not** asking: whether the harness's typed `SeatRun` wire format is right (that is
`bh-a7so.1`), whether `codex` can express the same boundary (`bh-a7so.3`), or whether the baked
bundle is the right authority split (`bh-a7so.4`). This spike is about one thing: the price of a
crash.

## Method

Nine real `claude` seat turns against a disposable throwaway bead, on one machine, one model, one
provider. No product code was written or modified in either repo.

**Environment.** macOS 14.5 (`aarch64-apple-darwin`) · `claude` CLI **2.1.220** ·
`beadhive/baml-harness` at **`ef63c3f`** · model **`claude-sonnet-5`** (no `--model` emitted; the
seat declared none) · Claude Code subscription auth (`provider: claude-code`).

**Binaries.** `dist/` in baml-harness is gitignored and the committed binaries predated the resume
work — `dist/bh-developer --help` at `ef63c3f` listed only `--task --workspace --bundle
--provider`. Ran `just build` to repack from source; the rebuilt binary exposes
`--resume_session`, `--session_id`, `--fork_session`, `--tier`, `--model`. All measurements below
use the rebuilt binary. *(Note for anyone reproducing: the shipped `dist/` artifacts cannot resume
at all.)*

**Target bead.** `bh-ukq0` — "SPIKE TARGET bh-a7so.2 (disposable, delete after)", `type=chore`,
`P4`, created for this spike and closed after. No live bead was touched.

**Workspace.** Each run got a fresh scratch git repo (`git init`, one seed commit) rather than a
real bead worktree, so a killed agent could not damage hive state. Where two runs had to start
from *identical* repo state (resume vs fresh), the advanced workspace was `cp -R`-snapshotted
before either ran.

**Seat.** A hand-written v1 bundle (`harness.json`) declaring one `developer` seat:
`permission_mode: acceptEdits`, allow `Read/Write/Edit/Glob/Grep/TodoWrite` +
`Bash(git add|commit|status|log|diff|init:*)`, `Bash(ls|cat|mkdir|python3:*)`, deny
`WebFetch/WebSearch/Task/SlashCommand`.

**Task.** One fixed 7-step ask (`task.txt`), chosen so there is a real mid-run to interrupt: build
a pure-Python `IntervalSet` library — README, core class + `normalize()`, `add`/`contains`, set
algebra, a ≥12-case `unittest` suite that must pass, `measure()` + 3 more tests, then a README
status section — **committing after every step**. Identical text for every run, including resumes.

**Harness.** `runner.py` starts `dist/bh-developer` with `start_new_session=True` (its own process
group), sleeps N seconds, sends a signal, and records exit code, wall time, stdout/stderr bytes,
parsed `SeatRun`, and `ps` process-group snapshots before the kill and after exit. Two kill
**scopes**, because they are different experiments:

- `--scope proc` → `os.kill(pid, sig)` — signals only the harness binary. **This is what the ADR's
  `local` tier does**: `asyncio.TaskGroup` cancellation reaches
  `asyncio.subprocess.Process.terminate()`, which signals the direct child, not the tree.
- `--scope group` → `os.killpg(pgid, sig)` — signals the harness *and* its `claude` child. What a
  supervisor that knows about the tree would do.

Kill point 80 s, ~mid-run against a 165 s baseline.

**Runs.**

| # | run | signal | scope | purpose |
|---|---|---|---|---|
| 1 | `baseline` | — | — | full task, clean workspace, to completion |
| 2 | `term-proc` | SIGTERM | proc | the ADR's `local`-tier cancellation |
| 3 | `term-group` | SIGTERM | group | the graceful path with tree awareness |
| 4 | `kill-group` | SIGKILL | group | machine death |
| 5 | `nosid-kill` | SIGKILL | group | same, but **no** caller-minted `--session_id` |
| 6 | `resume-term` | — | — | `--resume_session` after (3), same workspace |
| 7 | `fresh-advanced` | — | — | **fresh session**, snapshot of (3)'s workspace |
| 8 | `resume-kill` | — | — | `--resume_session` after (4), same workspace |
| 9 | `fresh-advanced-2` | — | — | **fresh session**, snapshot of (4)'s workspace |

(6)/(7) and (8)/(9) are the two comparison pairs: identical repo state, identical task, differing
only in whether the prior conversation was resumed.

**Also inspected.** Claude Code's own session store,
`~/.claude/projects/<slugified-cwd>/<session_id>.jsonl` — line count, per-entry
`message.usage`, and the tail shape after each signal. Bead state via `bh work issue bh-ukq0`
before/after each kill, and recovery via `bh bd reclaim --id bh-ukq0`. Sources read:
`baml_src/harness.baml`, `baml_src/provider.baml`, `baml_src/seats.baml`, `README.md`,
`scripts/e2e-resume-roundtrip.sh`.

## Evidence

### 1. Baseline — one full seat turn

`exit 0` · **165.35 s** · stdout 1623 B · parsed `SeatRun`:

```json
{"session_id":"f15a1484-b913-4c13-af3a-1afd294a80b3",
 "cost_usd":0.9140196,
 "usage":{"input_tokens":54,"output_tokens":8348,
          "cache_creation_input_tokens":61080,"cache_read_input_tokens":1407192}}
```

`outcome.status = "done"`, `bead_id = "bh-ukq0"` (echoed back correctly), all 7 steps committed.

### 2. A killed run emits **nothing** — under either signal, at either scope

> **WITHDRAWN** (`bh-a7so.7` §3/§4) — an artifact of this spike killing the pipe *reader*
> alongside the writer. Hold the pipe and a priced envelope arrives in 0.63 s.

| run | signal | scope | exit | wall | stdout | stderr | `SeatRun` |
|---|---|---|---|---|---|---|---|
| `term-proc` | SIGTERM | proc | `-15` (sh 143) | 80.24 s | **0 B** | **0 B** | none |
| `term-group` | SIGTERM | group | `-15` (sh 143) | 80.26 s | **0 B** | **0 B** | none |
| `kill-group` | SIGKILL | group | `-9` (sh 137) | 80.24 s | **0 B** | **0 B** | none |
| `nosid-kill` | SIGKILL | group | `-9` (sh 137) | 40.39 s | **0 B** | **0 B** | none |

Not a partial `SeatRun` — **zero bytes**, and zero bytes of stderr diagnostics too. This is
structural, not incidental:

- `harness.baml:194` — the argv is `--output-format json`, not `stream-json`, so `claude` prints
  one envelope at the very end or nothing at all.
- `harness.baml:333` — `baml.json.deserialize<ClaudeResult>(proc.stdout.to_string())` runs
  **after** `baml.sys.exec` returns. There is no incremental read, so there is nothing to flush.

Consequence: `session_id`, `cost_usd` and `usage` — the resume token and the entire budget
channel — are delivered **only on success**. On the one path where a scheduler needs them most,
they do not exist.

The exit codes are at least honest: `-15`/`-9` fall into Amendment 1's `anything else = did not
complete (stdout may be absent)` bucket, and neither collides with `0/10/11`.

### 3. SIGTERM to the harness alone **orphans a live agent that runs to completion**

This is the ADR's own `local`-tier cancellation, and it is the most expensive finding in the spike.

`ps` immediately after the kill (`term-proc`), harness pid 81706 gone, child reparented to init:

```text
  PID  PPID  PGID STAT     ELAPSED COMMAND
81728     1 81706 S          01:22 claude -p [system]\012You are the developer seat...
```

The orphan was still running 90 seconds later. Left alone, it **finished the entire 7-step task**:

```console
$ git -C ws/term log --oneline
d5fc306 docs: status          <- committed ~2.5 min AFTER its supervisor was terminated
1b01683 feat: measure
3ff9ea1 test: IntervalSet suite
...
$ git -C ws/term status --short     # (empty — clean tree)
```

Its transcript is 43 assistant turns, 13,579 output tokens, 2,241,597 cache-read tokens — within
6 % of the baseline's 42 turns / 14,393 / 2,162,297, which the harness priced at **$0.914**.

And the result went nowhere: `runs/term-proc.stdout` is **0 bytes**. `baml.sys.exec` captures
stdout through a pipe, so when the harness died the read end died with it; the orphan's final
envelope was written into a pipe nobody was holding. **A full run's tokens were spent, the
worktree was mutated for minutes after the supervisor believed it had cancelled, and no channel
reported either.**

`--scope group` (runs 3, 4, 5) killed both processes; no survivors in `ps`. The tree-aware kill
is the only one that actually cancels.

### 4. The transcript on disk **is** the checkpoint, and it survives both signals

`~/.claude/projects/<slug>/<session_id>.jsonl` is written through as the run proceeds, not on
exit. After each mid-run kill the file was present, complete to the last finished step, and fully
parseable (0 unparseable lines in every case).

| run | signal | bytes at kill | lines | `[Request interrupted by user]` marker | dangling `tool_use` (no result) |
|---|---|---|---|---|---|
| `term-group` | SIGTERM | 140,495 | 55 | **yes** | 0 |
| `kill-group` | SIGKILL | 144,063 | 55 | no | **1** |

**This is the SIGTERM/SIGKILL difference, and it is small.** SIGTERM is handled: `claude` flushes
and appends an explicit terminal marker —

```json
{"type":"user","content":[{"type":"text","text":"[Request interrupted by user]"}]}
```

— leaving the transcript at a clean tool-call boundary. SIGKILL cannot run a handler, so the file
just stops, ending on an `assistant` `tool_use` whose `tool_result` never arrived. Both remained
resumable (§6). The graceful path exists; it just belongs to the `claude` child, exactly as
provider.baml warns ("someone else's permission engine enforces and we merely configure it"), and
nothing about it reaches the harness or the caller.

### 5. Without a caller-minted `--session_id`, the resume token is recoverable only by scraping

`nosid-kill` was launched with no `--session_id`. It was killed, emitted 0 bytes, and its identity
exists in exactly one place on the machine:

```text
~/.claude/projects/-private-tmp-...-spike-ws-nosid/c04c6fde-4e9e-4f1b-93ed-f9b5b5d14086.jsonl
```

Nothing on any documented channel ever told the caller that id. The only recovery is to slugify
the cwd, list that directory, and pick by mtime.

The inverse also holds, and is the one clean affordance found: **the caller can mint the id up
front.** `seat_argv` emits `--session-id` when set (`harness.baml:246`), and every run in this
spike that passed `--session_id <uuid>` had that exact uuid honored by `claude` and echoed back in
`SeatRun.session_id`. Identity can be known before the process starts — it just isn't required to
be today.

### 6. `--resume_session` works after **both** signals, and resumes somewhere genuinely useful

| run | resumed from | exit | wall | `SeatRun.session_id` | picked up at |
|---|---|---|---|---|---|
| `resume-term` | SIGTERM-killed `29a14547…` | 0 | 74.61 s | **same** `29a14547…` | step 6 of 7 |
| `resume-kill` | SIGKILL-killed `82a592cb…` | 0 | 75.91 s | **same** `82a592cb…` | step 6 of 7 |

Both resumed **in place** (same session id back, transcript grew from 55 lines to 105/102 in the
same file), both retained the prior context, and **neither redid steps 1–5**. The dangling
`tool_use` left by SIGKILL did not block resumption. Functionally, `--resume` does what Amendment 1
says it does.

### 7. THE NUMBER — resume costs **more** than a fresh session on the same worktree

The honest comparator is not "resume vs full redo from an empty repo". In the ADR's design the
durable artifact is the bead **branch**, so a restart faces the worktree exactly as the killed run
left it. Both members of each pair below started from byte-identical repo state (`cp -R`
snapshot taken before either ran) and the identical task; the only difference is `--resume_session`.

| pair | resumed run | fresh run on the *same* worktree | **resume ÷ fresh** |
|---|---|---|---|
| A (after SIGTERM) | `resume-term` **$0.48001** · 74.61 s | `fresh-advanced` **$0.34724** · 57.71 s | **1.382** |
| B (after SIGKILL) | `resume-kill` **$0.46888** · 75.91 s | `fresh-advanced-2` **$0.38308** · 92.34 s | **1.224** |
| | | | **mean 1.303** |

Token detail (`SeatRun.usage`):

| run | output | cache_creation | cache_read |
|---|---|---|---|
| `resume-term` | 2,755 | 39,686 | **668,336** |
| `fresh-advanced` | 2,179 | 30,995 | **428,443** |
| `resume-kill` | 3,028 | 39,754 | **616,239** |
| `fresh-advanced-2` | 3,069 | 33,896 | **445,377** |

The mechanism is visible in `cache_read`: a resumed turn replays the dead session's entire history
(≈ +50 % read tokens), including its dead ends — both killed sessions had burned turns on a
`git rm -r --cached __pycache__` that the permission roster refused, and both resumed sessions
inherited that detour and narrated it in their final summary. The fresh sessions never saw it;
they read `git log`, saw steps 1–5 committed, and did steps 6–7.

Wall clock gave no consistent signal (74.6/57.7 vs 75.9/92.3) — with n=2 it is noise.

### 8. Against a full fresh run, restart cost **is** bounded — by the worktree

Same two comparisons against the `baseline` full run ($0.914 from an empty repo):

| | cost | ÷ baseline |
|---|---|---|
| `resume-term` | $0.48001 | **0.525** |
| `resume-kill` | $0.46888 | **0.513** |
| `fresh-advanced` | $0.34724 | **0.380** |
| `fresh-advanced-2` | $0.38308 | **0.419** |

Restarting after losing 5 of 7 committed steps cost **0.38–0.42** of a full run with a fresh
session, and **0.51–0.53** with `--resume`. The premise "restart cost is bounded" holds. The
named mechanism is not what holds it: the committed git history is, and `--resume` degrades it.

### 9. The kill's sunk cost is real, and invisible to every documented channel

> **PARTLY SUPERSEDED** (`bh-a7so.7` §4) — the central claim, that a killed run's spend is
> invisible, is **WITHDRAWN**: the envelope carries `total_cost_usd`. Same root cause as §2 —
> the reader died with the writer, so nothing observed it. The token figures below are
> *transcript sums* and are inflated ~2× by the §10 double-count.
> **What still stands:** `cost_usd` is per *invocation*, not cumulative for the session — a
> scheduler summing `SeatRun.cost_usd` across a kill-and-resume never sees the killed
> segment. That remains a real scheduler requirement.

Splitting each resumed transcript at the >60 s timestamp gap (kill → resume) separates the two
invocations:

| session | killed segment (lost) | resumed segment (reported as `cost_usd`) |
|---|---|---|
| `term-group` | 25 turns · 7,946 out · 1,211,189 cache_read | 22 turns · 5,059 out · 1,151,721 cache_read → **$0.480** |
| `kill-group` | 27 turns · 9,108 out · 1,324,661 cache_read | 20 turns · 5,092 out · 1,014,676 cache_read → **$0.469** |

Two things follow. First, `cost_usd` is **per invocation**, not cumulative for the session — the
resumed run reports only its own turn, so a scheduler summing `SeatRun.cost_usd` across a
kill-and-resume never sees the killed segment at all. Second, the killed segment is *larger* in
tokens than the segment that was billed at ≈$0.47–0.48. **The dollar figure for a killed run is
emitted by nothing** — I am reporting its tokens as measured and declining to convert them, since
no channel priced them.

The tokens are recoverable, though: every `assistant` entry in the `.jsonl` carries
`message.usage`, so a scheduler willing to read the transcript can account for a crash after the
fact.

### 10. `SeatRun.usage` under-reports the session's real token consumption

> **WITHDRAWN** (`bh-a7so.7` §10) — a transcript double-count, one line per content block
> sharing a `message.id`. Dedup reproduces the envelope exactly. `usage` is trustworthy.

Summing `message.usage` across the transcript vs what the envelope reported, same run:

| run | source | output_tokens | cache_read_input_tokens |
|---|---|---|---|
| `baseline` | `SeatRun.usage` | 8,348 | 1,407,192 |
| `baseline` | transcript sum (42 turns) | **14,393** | **2,162,297** |
| `fresh-advanced` | `SeatRun.usage` | 2,179 | 428,443 |
| `fresh-advanced` | transcript sum (14 turns) | **3,380** | **650,290** |

`SeatRun.usage` is ~0.58–0.65 of the transcript total, consistently. I did not establish *why*
(candidates: the envelope's `usage` block aggregating only some model calls). Recorded as observed;
**`usage` is not a safe basis for a token budget without knowing what it excludes.** `cost_usd`
was not cross-checkable this way and is not implicated.

### 11. Bead state — nothing releases a claim, and `bd reclaim` is the only recovery

> **PARTLY SUPERSEDED** (`bh-a7so.7` §12) — the bead-state observation stands; "no
> graceful-cancellation path to speak of" does not. A three-rung cancel ladder is measured.

`bh-ukq0` was claimed (`bd update --claim`) before the kills. Lease TTL observed at **5 minutes**.

- Before: `IN_PROGRESS` · `Assignee: Brian Cripe` · `Lease: expires in 4 mins (heartbeat just now)`
- Immediately after SIGTERM: `IN_PROGRESS` · `Lease: expires in 1 min (heartbeat 3 mins ago)`
- 15 min later: `IN_PROGRESS` · `Lease: expires expired (heartbeat 15 mins ago)`
- `bh bd reclaim --id bh-ukq0 --older-than 0s` →
  `✓ Reclaimed 1 stale-lease issue(s): bh-ukq0 (was held by Brian Cripe)` → `OPEN`, assignee cleared.

So: **yes, the bead is left claimed by a dead process**, in exactly the same way under both
signals, and `bd reclaim` is the only thing that recovers it. Neither the harness nor the `claude`
child touches beads on the way out — there is no graceful-cancellation path to speak of; there is
a reaper on a timer. This is fine *provided the scheduler runs one*, which the ADR's `local` loop
already does (`bd("reclaim")` each tick) — but it is time-based recovery, not cancellation, and
under `--scope proc` (§3) the "dead" holder is still alive and still committing while its lease
ages out.

### 12. Worktree state after a kill is clean and recoverable — because the seat commits per step

| run | committed at kill | dirty at kill |
|---|---|---|
| `term-proc` | 5 of 7 steps | 2 modified files (later finished by the orphan; ended clean) |
| `term-group` | 5 of 7 steps | **none** |
| `kill-group` | 5 of 7 steps | **none** |

No corruption, no `.git` lock left behind, no manual repair needed in any run; the two fresh
restarts (§7) picked the worktrees up as-is. Note the causality, because it is the whole finding:
the workspace was recoverable *because the task mandated a commit after every step*. The
`term-proc` snapshot at kill time is the counterexample — two uncommitted modified files, i.e. one
step's work at risk. **The bound measured in §8 is a function of commit granularity, nothing else.**

### 13. The "re-run is a no-op" invariant held — by agent judgment, not enforcement

Amendment 1 asserts `INVARIANT re-run against an already-advanced bead is a no-op`. Both
`fresh-advanced` runs, handed the full 7-step task against a worktree with steps 1–5 committed,
read the repo state and did only steps 6–7 (`"Steps 1-5 were already committed at session
start"`). That is the invariant holding. But it held because the model looked at `git log` and
chose well — there is no mechanism in the binary that enforces it, and nothing in this spike tested
an adversarial case.

## Verdict — **NO-GO**

**Restart cost is bounded — measured at 0.38–0.42 of a full fresh run — but `--resume
<session_id>` is not what bounds it, and is measurably worse than not using it.** The premise
survives; the mechanism Amendment 1 names for it does not.

Three findings carry the verdict:

1. **`--resume` costs 1.30× a fresh session on the same worktree** (§7: 1.382 and 1.224 over two
   independent pairs, same direction both times). It replays the dead conversation, dead ends and
   all, for ~50 % more cache-read tokens than reading the repo from scratch. A recovery primitive
   that is more expensive than no recovery primitive bounds nothing.
2. **The resume token is not delivered when a run is killed** (§2, §5). Stdout is zero bytes under
   both signals; `session_id` ships only inside the success envelope. Absent a caller-minted
   `--session_id`, recovering it means slugifying a cwd and scraping
   `~/.claude/projects/*/**.jsonl` by mtime — an undocumented filesystem dependency, not a contract.
3. **The ADR's own `local` tier does not cancel anything** (§3). `asyncio`-style
   `proc.terminate()` signals the harness only; the `claude` child is orphaned to init, runs the
   task to completion, keeps committing to the worktree for minutes, and spends ≈ a full run's
   tokens (43 turns / 13,579 output / 2.24 M cache-read, vs a $0.914 baseline of 42 / 14,393 /
   2.16 M) with its result written into a pipe nobody holds. That is not a bounded restart cost;
   it is an *unbounded and unobservable* one, and it is what the ADR as written would ship.

What *is* real: the checkpoint exists — Claude Code write-throughs its session `.jsonl` and
survives SIGKILL (§4) — and the bound comes from **git commits in the worktree** (§8, §12). The
supervised unit is already checkpointed; the checkpoint is just the branch, which is what
Decision 1 said all along ("beads owns lifecycle state"). SIGTERM vs SIGKILL differ by exactly one
transcript marker (§4) and by nothing a scheduler needs to branch on.

Because the tiers are meant to be harness-agnostic, this is worth naming plainly: everything good
in §4 and §6 is `claude`-CLI behavior that the contract inherits rather than owns.
`provider.baml`'s "someone else's permission engine enforces and we merely configure it" applies to
checkpointing too — and `codex`, declared with `executes_tools_locally == true` and
`implemented == false`, has made no such promise.

## Recommendation

The bead framed the NO-GO exits as **(a) shrink the supervised unit so a full redo is affordable**
or **(b) baml-harness grows an explicit checkpoint hook**. The evidence says **(a) is already
substantially true and is far cheaper** — the worktree *is* the checkpoint, and §12 shows the bound
is purely a function of commit granularity. A checkpoint hook (b) would rebuild in the harness what
git already provides. But (b)'s *plumbing* half — not the checkpoint, the identity and accounting
around it — is small, and three of its pieces are load-bearing.

**Amend the contract before `bh-c6dk.2` is written:**

1. **Demote `--resume` out of the contract's `RESUME` line.** Recovery is: re-dispatch a fresh
   seat turn against the same worktree. Replace

   ```text
   RESUME          --resume <session_id>
   ```

   with something like `RECOVERY  re-dispatch a fresh turn against the same worktree; the branch is
   the checkpoint`. Keep `--resume_session` on the binary as an optional *continuation* affordance
   (it works — §6, and `scripts/e2e-resume-roundtrip.sh` already guards it), but stop calling it
   the checkpoint primitive. Cost: an ADR edit.

2. **Promote `--session_id` from optional to required in the contract.** It is the one clean thing
   this spike found (§5): a caller-minted uuid is honored and echoed back, so a killed run is
   attributable to a transcript without scraping. Without it a crash is anonymous. Cost: an ADR
   line plus a scheduler-side `uuid4()`.

3. **The `local` tier must kill the process *group*, not the process** (§3). Non-negotiable and
   independent of everything else here: `asyncio.create_subprocess_exec` + `TaskGroup`
   cancellation as sketched in Decision 2 orphans a live, spending, worktree-mutating agent. This
   belongs in `bh-c6dk.5` as an explicit requirement (`start_new_session=True` + `os.killpg`, with
   SIGTERM-then-SIGKILL escalation), not as an implementation detail.

4. **WITHDRAWN — do not action.** `bh-a7so.7` §9/§10 retired both halves of this
   recommendation: `SeatRun.usage` is exact (the 35–40 % gap was a transcript double-count), and
   a killed run's envelope *is* priced once the reader outlives the writer. Do not file the
   `usage` re-check bead this item proposes. ~~Do not build budget accounting on stdout, and
   re-check `usage`.~~

5. **Make commit-per-step a seat-instruction invariant.** This is exit (a), and it is nearly free:
   the measured 0.38–0.42 bound exists only because the task mandated a commit after every step,
   and `term-proc`'s snapshot (§12, two uncommitted files at kill) shows the failure mode when it
   does not. The baked seat prompt is the right place for it — it is role behavior, which
   Amendment 1 already puts in the binary.

No change is needed to Decision 1 or 2's semantics, to `RoleOutcome`, or to the exit-code taxonomy
— `-15`/`-9` land correctly in `anything else = did not complete` (§2), and Amendment 1's
"exit codes AND stdout, not either/or" correction is vindicated: stdout is absent exactly when a
scheduler most needs to react.

**Close** `bh-ukq0` (throwaway target, done). **Feed** this verdict to `bh-a7so.4` as: the contract
is adoptable with the `RESUME` line replaced and `--session_id` promoted.

### What this spike could not measure — stated rather than estimated

- **The dollar cost of a killed run.** Tokens measured (§9); no channel prices them, so no figure
  is given.
- **`--output-format stream-json` under a kill.** Not tested. Recommendation 4 assumes it would
  surface partials; that assumption is untested.
- **The harness's own 900 s `timeout_ms`** (`harness.baml:330`). Not exercised; whether a timeout
  loses stdout the same way a signal does is unknown.
- **Real bead-worktree behavior.** Runs used scratch git repos and a hand-claimed throwaway bead,
  not `bh work claim` → `bh work submit` on a live `wt/bead/<id>`. §11 measured what beads does
  when a holder dies; it did not measure a seat driving the full lifecycle.
- **Any provider but `claude-code`.** `codex` is `implemented == false`; nothing here transfers to
  it, and §4/§6 are specifically Claude Code CLI behavior.
- **Distribution.** n=2 comparison pairs, one model (`claude-sonnet-5`), one task shape, one
  machine, one CLI version. The *direction* of §7 replicated; the magnitudes (1.382, 1.224) are two
  samples, not a mean with a confidence interval.
