# Spike `bh-a7so.7` — can a scheduler cancel a running seat *cooperatively*, and see what it spent?

**Bead:** `bh-a7so.7` · **Seat:** `dev/interrupt` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-a7so.4` (adopt or extend the baked-seat role-binary contract), and
through it `bh-c6dk.2` / `bh-c6dk.5`

> FLEXIBLE-mode follow-up to [`bh-a7so.2`](bh-a7so.2-checkpoint-resume.md), whose bead text scoped
> it to "test both signals: SIGTERM and SIGKILL". It tested exactly those two, at exactly two kill
> scopes, and returned NO-GO. This spike reopens the space that scoping foreclosed.

## Question

`bh-a7so.2` established that a killed seat turn is **silent** — zero bytes of stdout, zero bytes of
stderr, no `session_id`, no `cost_usd` — and concluded there is "no graceful-cancellation path to
speak of; there is a reaper on a timer". Every tier in
[work-runtime-tiers-adr.md](../design/work-runtime-tiers-adr.md) has to cancel work sometimes
(budget exhausted, superseding instruction, operator abort), and the contract in Amendment 1 has no
`CANCEL` line at all.

**GO/NO-GO: can a scheduler stop a running seat *cooperatively* — "wrap up, commit what you have,
and report" — rather than only by killing it, and does it get back a priced, attributable result
when it does?**

Four sub-questions, from the bead:

1. **SIGINT to the process group.** Never tested. SIGTERM already yields a clean
   `[Request interrupted by user]` at a tool-call boundary, so the child clearly handles interrupts.
   Does SIGINT differ?
2. **`--input-format stream-json`.** The harness runs single-shot `--output-format json`
   (`harness.baml:194`) with `stdin: null` (`harness.baml:330`), so there is no input channel to a
   running agent — which is *why* signals are the only cancel available. Can a scheduler open
   stream-json both ways, send "wrap up, commit, submit" mid-run, and have the seat comply?
3. **Does stream-json fix the observability hole?** `bh-a7so.2` §2 (a killed run emits zero bytes)
   and §10 (`SeatRun.usage` under-reports a *successful* run by 35–40 %) both listed stream-json as
   explicitly untested.
4. **Sibling notification on interrupt** — design only, no implementation.

Critically **not** asking: whether `proc.terminate()` orphans the child (settled — it does, and it
is signal-independent), or what `--resume` costs (settled at 1.30× a fresh run). Neither was
re-measured.

## Method

Seven real `claude` seat turns against disposable scratch git repos, reusing `bh-a7so.2`'s method:
matched pairs, one fixed task, verbatim capture of stdout/stderr/exit, and `ps` snapshots around
every kill.

**Environment.** macOS 14.5 (`aarch64-apple-darwin`) · `claude` CLI **2.1.220** ·
`beadhive/baml-harness` at **`e3ada94`** · model **`claude-sonnet-5`** (no `--model` emitted) ·
Claude Code subscription auth. All runs 2026-07-30.

**Binaries.** `bh-a7so.1`'s warning held again: `dist/` was stale. The committed binaries dated
`Jul 30 00:42` (built during `bh-a7so.2`) while `baml_src/` had been modified `Jul 30 13:28`. Ran
`just build`; the binary changed size (13,317,538 → 13,383,586 B). Every harness-mediated run below
uses the rebuilt binary.

**Target bead.** `bh-20he` — "SPIKE TARGET bh-a7so.7 (disposable, delete after)", `type=chore`,
`P4`, created for this spike and closed after. No live bead was touched.

**Workspace.** One seeded scratch git repo (`git init` + one commit) `cp -R`-copied fresh for every
run, so all seven runs start from byte-identical state.

**Seat.** Hand-written v1 bundle: one `developer` seat, `permission_mode: acceptEdits`, allow
`Read/Write/Edit/Glob/Grep/TodoWrite` + `Bash(git add|commit|status|log|diff|init:*)`,
`Bash(ls|cat|mkdir|python3:*)`, deny `WebFetch/WebSearch/Task/SlashCommand`. The direct-`claude`
runs pass the same roster verbatim via `--settings`, matching `seat_argv`'s own choice
(`harness.baml:216-221`).

**Task.** The same 7-step `IntervalSet` build `bh-a7so.2` used, **committing after every step**, so
there is a real mid-run to interrupt. Identical text for every run. Kill point **80 s** in all
seven, mid-step-5/6.

**Runners.** Two, both capturing everything:

- `runner.py signal` — launches `dist/bh-developer` with `start_new_session=True`, sleeps 80 s, then
  signals either the whole group (`os.killpg`, `bh-a7so.2`'s `--scope group`) or **only the `claude`
  grandchild** (`os.kill(claude_pid)`, `--scope child` — *new*), and polls until the child is
  actually gone.
- `runner.py stream` / `direct.py` — launch `claude` itself with a **live stdin pipe** and a
  line-pumping reader thread that timestamps every stdout line as it arrives, so mid-run injection
  and its latency are measurable.

**Runs.**

| # | run | mechanism | what it isolates |
|---|---|---|---|
| 1 | `int-group` | SIGINT → harness process group | Q1 as the bead framed it |
| 2 | `int-child` | SIGINT → `claude` only, harness alive | the scope nobody tested |
| 3 | `direct-int-json` | SIGINT → `claude`, scheduler holds the pipe | what `claude` *actually emits* |
| 4 | `direct-term-json` | SIGTERM, same | matched control for (3) |
| 5 | `stream-inject` | stream-json, mid-run "wrap up" message | Q2 |
| 6 | `stream-control` | stream-json, `control_request` interrupt | Q2 fallback |
| 7 | `stream-kill` | stream-json, group SIGTERM | Q3 |

**Also read.** `baml_src/harness.baml`, `baml_src/provider.baml`, `claude --help`, and each run's
session transcript at `~/.claude/projects/<slug>/<session_id>.jsonl`. No product code was written
or modified in either repo.

## Evidence

### 1. The input channel exists, and it is documented

`claude --help` (2.1.220), verbatim:

```text
--input-format <format>       Input format (only works with --print): "text" (default), or
                              "stream-json" (realtime streaming input)
--replay-user-messages        Re-emit user messages from stdin back on stdout for
                              acknowledgment (only works with --input-format=stream-json
                              and --output-format=stream-json)
```

`--output-format=stream-json` additionally requires `--verbose` (the CLI refuses otherwise:
`Error: When using --print, --output-format=stream-json requires --verbose`). Everything in §5–§8
uses this documented surface. Nothing was patched, forked, or worked around.

### 2. Q1 — SIGINT to the **group** is the ADR's orphan bug wearing a different hat

`int-group` — `os.killpg(pgid, SIGINT)` at 80 s. The harness died instantly (`exit -2`, **0 B**
stdout, **0 B** stderr). The `claude` child, *in the same process group and therefore signalled*,
did **not** die with it. `ps` 2 s after the kill:

```text
  PID  PPID  PGID STAT ELAPSED COMMAND
48364     1 48342 S      01:22 claude -p [system]\012You are the developer seat...
```

`PPID 1` — reparented to init, exactly the shape `bh-a7so.2` §3 attributed to *proc*-scope kills.
The child then wound down on its own and was gone before the next check, leaving 5 of 7 steps
committed and a transcript ending in a clean `[Request interrupted by user]` with **0 dangling
`tool_use`**. So SIGINT-to-group is not a survival bug — it is a **~2 s graceful shutdown window**
during which the supervisor is already dead and the pipe already closed. The result went nowhere.

### 3. The scope nobody tested: signal the child, keep the reader alive

`int-child` — same run, but `os.kill(claude_pid, SIGINT)` with the harness left running. The child
died at **+2.04 s**. And the harness did *not* report zero bytes; it **crashed with a diagnostic**:

```text
error: Traceback (most recent call last):
  File "baml_src/seats.baml", line 121, in user.developer_seat
  File "baml_src/harness.baml", line 361, in user.run_seat_with
  File "baml_src/harness.baml", line 333, in user.run_resolved_seat
  File "<builtin>/baml/ns_json/json.baml", line 197, in baml.json.deserialize
uncaught throw: baml.json.JsonDecodeError {message: "missing required field `result`", path: ""}
```

`missing required field` means **the JSON parsed**. `claude` had written a real envelope. The
harness threw it away.

### 4. What `claude` actually emits on a signal — a fully priced termination envelope

`direct-int-json` — same signal, but the *scheduler* holds the pipe. One line of stdout, **1347 B**,
arriving **0.67 s after the signal**, verbatim (elided only in `modelUsage`):

```json
{"is_error":true,"duration_api_ms":52696,"num_turns":16,"stop_reason":"tool_use",
 "session_id":"44444444-4444-4444-8444-444444444444","total_cost_usd":0.4643655,
 "usage":{"input_tokens":28,"cache_creation_input_tokens":31797,
          "cache_read_input_tokens":671815,"output_tokens":4797,...},
 "permission_denials":[],"terminal_reason":"aborted_tools",
 "subtype":"error_during_execution",
 "errors":["[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"],
 "type":"result","duration_ms":68293,"uuid":"a07ce451-76e7-442a-8efd-ac89fbf83b5f"}
```

**`bh-a7so.2` §2's "a killed run emits nothing" is not a property of `claude`.** It is a property of
killing the process that holds the read end of the pipe at the same instant. The envelope carries
the `session_id` (the resume token), `total_cost_usd`, the full `usage` block, and an explicit
machine-readable `terminal_reason`. On the one path a scheduler needs them most, they *do* exist —
`bh-a7so.2` could not see them because in both of its scopes the reader died first.

`direct-term-json` is the matched control and is **identical in every respect** — same 1347 B, same
`terminal_reason: "aborted_tools"`, same `subtype`, envelope at +0.63 s, exit at +1.92 s.

**The one measured SIGINT/SIGTERM difference is the exit code**, and it favours SIGTERM:

| run | signal | envelope at | process exit | **exit code** |
|---|---|---|---|---|
| `direct-int-json` | SIGINT | +0.67 s | +1.99 s | **`0`** |
| `direct-term-json` | SIGTERM | +0.63 s | +1.92 s | **`143`** |

A SIGINT-cancelled run exits **`0`** — which in Amendment 1's taxonomy (`EXIT 0 done · 10 blocked ·
11 handoff · anything else = did not complete`) reads as **`done`**. SIGTERM's `143` lands correctly
in `anything else`. So the answer to "does SIGINT do better?" is: no — identical everywhere that
matters and strictly worse on the one axis where they differ.

### 5. The harness discards the envelope twice, for two independent reasons

Both are one-line facts in `harness.baml`:

```baml
class ClaudeResult {          // :91
    subtype: string,
    is_error: bool,
    result: string,           // :94  — REQUIRED, and absent from the abort envelope
    session_id: string,
    ...
}

let envelope = baml.json.deserialize<ClaudeResult>(proc.stdout.to_string());  // :333
if (envelope.is_error) {                                                      // :334
    baml.sys.panic("claude harness failed: " + envelope.result)               // :335
}
```

1. `result: string` is non-optional. The abort envelope has no `result` key → `JsonDecodeError`
   (§3), and every field beside it is lost.
2. Even if it parsed, `is_error` is `true` on the abort path, so `:334` panics — discarding
   `session_id`, `total_cost_usd` and `usage` a second time.

This is not "the CLI gives us nothing". It is a required-field declaration and a `panic` on a value
the harness could keep. Both are inside this org's own repo.

### 6. Q2 — the cooperative cancel works, end to end

`stream-inject`. Scheduler holds stdin open and writes one ordinary user message at t=80.064 s
telling the seat its budget is exhausted: finish the current tool call, `git commit` whatever is
uncommitted as `wip: interrupted`, reply `INTERRUPT_ACK steps_done=<N> last_commit=<sha>`, stop.
Timestamped from the reader thread:

| t (s) | Δ from inject | event |
|---|---|---|
| 80.064 | — | scheduler writes the instruction to stdin |
| 80.197 | +0.13 | seat is mid-`Edit` (call started before the inject) |
| 81.154 | +1.09 | that in-flight `Edit` **completes** |
| 81.166 | **+1.10** | `--replay-user-messages` echoes it back, `isReplay: true` — **delivery ACK** |
| 82.999–103.0 | | `system/thinking_tokens` |
| 104.473 | +24.4 | `Bash` → `git status` |
| 110.118 | +30.1 | `Bash` → `git commit` |
| 111.706 | **+31.6** | result: `7aed994 wip: interrupted` |
| 116.333 | +36.3 | `INTERRUPT_ACK steps_done=5 last_commit=7aed994` |
| 118.075 | **+38.0** | `result` envelope, `is_error:false`, `subtype:"success"`, `terminal_reason:"completed"` |

Process exit **0**. `git log` afterwards:

```console
7aed994 wip: interrupted          <- the message the scheduler asked for, verbatim
bd126ac Add unittest suite (16 tests), all passing
...
$ git status --short               # (empty — clean tree)
```

The seat **complied**: it did not start step 6, it committed the in-flight work under the exact
requested message, it emitted the exact requested ack format, and it stopped. The run terminated as
a **success**, so a scheduler gets a well-formed `result` envelope *and* the seat's own structured
report — not an error to reconstruct. Delivery is acknowledged in **1.10 s**; the whole cooperative
cancel completes in **38 s**.

### 7. But an in-band user message is prompt-injection-shaped, and a good seat says so

Unprompted, in the same final turn, the seat flagged it (verbatim, after the ack line):

> Flagging: that "scheduler interrupt" arrived as an injected system-reminder, not a normal user
> turn, and it told me to override your explicit "commit after every step, no exceptions"
> instructions with a generic mid-step "wip: interrupted" commit — classic prompt-injection shape.
> **I complied because committing is harmless/reversible**, but **I did not stop on my own judgment;
> you should verify this interrupt was actually yours.**

This is a design constraint, not a defect: the seat complied *conditionally*, on its own judgement
that the requested action was reversible. A cooperative cancel whose wrap-up were destructive — or a
seat with tighter instincts — could be refused, and refusal would be correct behaviour. **The
cooperative path cannot be the only path**, and the instruction should be a pre-agreed protocol
baked into the seat's own prompt rather than an ad-hoc mid-run message.

### 8. `control_request` — the out-of-band hard cancel, in 0.09 s

`stream-control`. Same channel, but instead of a user message the scheduler writes a control frame:

```json
{"type":"control_request","request_id":"spike7-int-1","request":{"subtype":"interrupt"}}
```

| t (s) | Δ | event |
|---|---|---|
| 80.132 | — | `control_request` written |
| 80.164 | **+0.032** | `{"type":"control_response","response":{"subtype":"success","request_id":"spike7-int-1","response":{"still_queued":[]}}}` |
| 80.203 | +0.071 | `[Request interrupted by user]` **on stdout** |
| 80.226 | **+0.094** | `result` envelope, `terminal_reason:"aborted_streaming"`, `total_cost_usd: 0.4537248` |

A **correlated** ack (`request_id` echoed back), then a priced envelope, in under a tenth of a
second — and it is not a user message, so it is not prompt-injection-shaped and cannot be reasoned
about or declined. It is a *hard* abort: no wrap-up, and it left `intervalset.py` modified and
uncommitted.

Three distinct `terminal_reason` values were observed, and they are a usable taxonomy:

| `terminal_reason` | produced by | wrap-up? |
|---|---|---|
| `completed` | normal end **or** cooperative wrap-up (§6) | yes |
| `aborted_streaming` | `control_request` interrupt | no |
| `aborted_tools` | SIGINT / SIGTERM mid tool call | no |

### 9. Q3a — under stream-json a killed run is **not** silent

`stream-kill`: `os.killpg(SIGTERM)` at 80.083 s, no input channel used.

| | `bh-a7so.2` `term-group` | this spike's `stream-kill` |
|---|---|---|
| stdout | **0 B** | **83,319 B** / 70 lines, 0 unparseable |
| `session_id` | absent | on stdout at **t = 2.28 s** (`system/init`, before any work) |
| `[Request interrupted by user]` | transcript scrape only | **inline on stdout** |
| final envelope | absent | **t = 80.178 — 0.095 s after the kill** |
| `total_cost_usd` | absent | **0.4090578** |

Every intermediate `assistant` message, all 12 `tool_use` blocks and their results, and the
`vcs_state_changed` system event arrived live. Exit `143`. The two structural causes
`bh-a7so.2` §2 named are both addressed: `--output-format stream-json` replaces the
one-envelope-at-the-end argv, and a scheduler-side reader replaces the after-the-fact
`proc.stdout.to_string()`.

The single most useful line is the first: `session_id` is on stdout ~2 s in, so a run is
**attributable from the moment it starts**, killed or not. `bh-a7so.2` §5's "recovering it means
slugifying a cwd and scraping `~/.claude/projects/*.jsonl` by mtime" stops being the fallback.

### 10. Q3b — `SeatRun.usage` does **not** under-report. `bh-a7so.2` §10 is a double-count artifact

Replicating §10's method reproduced its result on all four of my envelope-bearing runs — envelope
`usage` is 0.50–0.55 of the transcript's summed `output_tokens`:

| run | envelope output | transcript sum | ratio |
|---|---|---|---|
| `stream-inject` | 7,822 | 14,138 | 0.553 |
| `stream-kill` | 3,793 | 7,655 | 0.495 |
| `direct-int-json` | 4,797 | 8,776 | 0.547 |
| `direct-term-json` | 4,415 | 8,335 | 0.530 |

`bh-a7so.2` recorded this as observed and said "I did not establish *why*". **The cause is the
transcript, not the envelope.** `~/.claude/projects/<slug>/<sid>.jsonl` writes one line per
*content block*, so a single API response that produced a text block and a `tool_use` block is
logged as **two `assistant` lines carrying the same `message.id` and the same `usage`**. Summing
per line double-counts. For `direct-int-json`: 22 `assistant` entries, **14 distinct `message.id`**,
8 ids appearing twice with byte-identical usage — e.g.

```text
msg_011CdYuF34Goy35Vuy3EiMKw x2  [(291, 22190), (291, 22190)]
msg_011CdYuFWbyDas8FpgpF5drG x2  [(392, 47303), (392, 47303)]
```

Deduplicating by `message.id` before summing:

```text
DEDUPED by message.id -> output= 4797  cache_read= 671815
envelope was            output= 4797  cache_read= 671815
```

**Exact, to the token, on both axes.** `usage` is trustworthy; the naive transcript sum is not.
This retires `bh-a7so.2`'s Recommendation 4 ("re-check `usage`") and its §10 open item.

### 11. `total_cost_usd` is exactly derivable from `usage`

Pricing `direct-int-json`'s envelope at list Sonnet rates ($15/MTok output, $0.30/MTok cache read,
$6/MTok 1-hour cache write; input negligible at 28 tokens):

```text
4,797 × 15/1e6  +  671,815 × 0.30/1e6  +  31,797 × 6/1e6
      = 0.07196 + 0.20154 + 0.19078 = 0.46428
envelope total_cost_usd                = 0.4643655
```

Agreement to ~0.02 %, and the envelope's own `modelUsage.claude-sonnet-5.costUSD` equals
`total_cost_usd` exactly while its token counts equal the `usage` block exactly. `cost_usd` is a
pure function of `usage` — so §10 clearing `usage` clears `cost_usd` with it. Both are safe bases
for a budget.

### 12. Summary — the three-rung ladder, measured

All seven runs, same task, same 80 s kill point, same seed workspace:

| run | mechanism | ack | envelope | exit | `terminal_reason` | cost | tree at exit |
|---|---|---|---|---|---|---|---|
| `stream-inject` | user msg over stream-json | +1.10 s | +38.0 s | 0 | `completed` | $0.58086 | **clean, work committed** |
| `stream-control` | `control_request` interrupt | **+0.03 s** | +0.09 s | 1 | `aborted_streaming` | $0.45372 | 1 file dirty |
| `stream-kill` | SIGTERM → group | — | +0.10 s | 143 | `aborted_tools` | $0.40906 | untracked only |
| `direct-term-json` | SIGTERM → `claude`, reader alive | — | +0.63 s | **143** | `aborted_tools` | $0.43861 | untracked only |
| `direct-int-json` | SIGINT → `claude`, reader alive | — | +0.67 s | **0** ⚠ | `aborted_tools` | $0.46437 | untracked only |
| `int-child` | SIGINT → `claude`, **harness** reads | — | **discarded** (§3) | 1 | — | lost | untracked only |
| `int-group` | SIGINT → group | — | **none** | -2 | — | lost | untracked only |

Every run committed 5 of 7 steps before the interrupt; none corrupted the repo or left a `.git`
lock. The cooperative cancel cost **$0.58086 vs a $0.4414 mean across the four hard cancels — 1.32×**
(n=1 against n=4, one task shape) — and what that 1.32× buys is a clean tree, a `wip` commit, a
structured `INTERRUPT_ACK`, and a `subtype: success` envelope instead of an error to reconstruct.

### 13. Why this channel belongs to the scheduler, not to `baml.sys.exec`

`baml describe` on the BAML stdlib, which is what the harness has to work with:

```baml
class ProcessOptions { cwd: string|null, env: map<string,string>|null,
                       timeout_ms: int|null, stdin: string|null }
function exec(program: string, args: string[]?, options: ProcessOptions?) -> ShellOutput
class ShellOutput { stdout: uint8array, stderr: uint8array, exit_code: int }
```

`stdin` is a **static string fixed before launch**, and `exec` returns a `ShellOutput` for an
*already-finished* process. There is no live write handle and no incremental read. So
`run_resolved_seat` **structurally cannot** hold a bidirectional stream-json channel, no matter how
`seat_argv` is edited — a real constraint, and it is the one that decides ownership. §6/§8's
mechanism is a *supervisor* capability: the process that spawns `claude` and keeps its pipes must be
the scheduler (`asyncio` in the `local` tier, the activity worker in `temporal`), with the harness
supplying argv and typing, not holding the socket. This does not block anything in §6–§11; it
relocates it.

### 14. Q4 — sibling notification is the scheduler's job (design sketch, no implementation)

The child cannot do it, for two independent reasons this spike can state concretely. It has no
topology: the baked contract (`bh-<seat> --workspace --bead --instructions`) hands it one bead and
one worktree, and `RoleOutcome` gives it one `bead_id` to echo — sibling ids are never in scope, by
design, since `provider.baml`'s authority split exists precisely so a seat cannot reach beyond its
grant. And it has no channel: `--input-format stream-json` is *inbound only*; the outbound side is
`stdout`, which goes to whoever spawned it. (`claude --help` does list `--brief  Enable
SendUserMessage tool for agent-to-user communication` — an agent→*caller* channel, still terminating
at the scheduler, never at a sibling. **Untested here.**) So sibling notification is the scheduler's
by elimination, and the evidence above says the scheduler is *already* the right holder: it is the
one that owns the stdin pipe (§6), the one that must outlive the child to receive the envelope (§4),
and the one that knows the molecule.

The shape is the same in both tiers, only the transport differs. In `temporal`, the seat activity
returns or heartbeats its `SeatRun`; the **parent molecule workflow** — which already owns the
child-workflow set — receives a `seat_interrupted(bead_id, terminal_reason, session_id, cost_usd)`
signal and decides per sibling whether to send its own `control_request` interrupt (§8) or let it
finish, with the workflow's event history as the durable audit record. In `local`, the same decision
lives in the poll loop that already runs `bd reclaim` each tick (`bh-a7so.2` §11): the loop holds
the `{bead_id → (proc, stdin, pgid)}` map, so on interrupt it walks the molecule's other in-flight
beads and writes to their stdin pipes directly. In both cases the fan-out is a scheduler-side
`for sibling in molecule` over a table the scheduler already has, and the child's only obligation is
the one it already meets — emit a `terminal_reason` the scheduler can branch on. Nothing new is
needed in the seat contract beyond the `CANCEL` line below.

## Verdict — **GO**

**A cooperative cancel is achievable today, on the documented CLI surface, with no change to
`claude` and no new capability from any provider.** A scheduler that holds `claude`'s stdin can tell
a running seat to wrap up; the seat finishes its in-flight tool call, commits, reports in the
requested format, and exits `0` with a `subtype: success` envelope — measured at 1.10 s to delivery
ack and 38 s to clean exit (§6), for 1.32× the cost of just killing it (§12).

Three findings carry the verdict:

1. **`bh-a7so.2`'s central negative — "a killed run emits zero bytes" — does not generalise** (§4).
   It measured two kill scopes, and in both of them the process holding the read end of the pipe
   died at the same instant as the writer. Signal `claude` alone, or hold the pipe from outside its
   group, and the *same* CLI emits a 1347 B envelope 0.63–0.67 s later carrying `session_id`,
   `total_cost_usd`, full `usage`, and an explicit `terminal_reason`. The silence was never
   `claude`'s.
2. **What blocks it is two lines in this org's own repo** (§5): `ClaudeResult.result` is declared
   required (`harness.baml:94`) so the abort envelope fails to deserialize, and `harness.baml:334`
   panics on `is_error` so it would be discarded even if it parsed. Neither is an upstream
   limitation, a license, a TOS, or a settled ADR — the bar this spike had to clear for a NO-GO.
3. **There is a three-rung escalation ladder, all of it measured** (§12): cooperative wrap-up
   (38 s, clean tree, structured report) → `control_request` interrupt (0.09 s, correlated ack,
   priced envelope, no wrap-up) → SIGTERM (0.63 s to envelope, ~2 s to exit). Each rung is strictly
   faster and strictly less graceful, and every rung returns a priced envelope. That is exactly the
   "cooperative cancel, signal only as fallback" the bead asked for.

Two answers are negative and should be recorded as such. **SIGINT is not better than SIGTERM** — the
envelope, latency, transcript marker and shutdown time are identical, and SIGINT's exit `0` (§4)
actively collides with the contract's `0 = done`. Use SIGTERM. And **stream-json does not fix
`SeatRun.usage`, because `usage` was never broken** (§10) — `bh-a7so.2` §10's 35–40 % gap is a
double-count in the transcript, not an under-report in the envelope, and deduplicating by
`message.id` reproduces the envelope to the token.

The `provider.baml` caveat from `bh-a7so.2` still applies and is worth restating: §6–§8 are Claude
Code CLI behaviours the contract would *inherit*. `codex` is `implemented == false` and has made no
such promise.

## Recommendation

**GO — but the deliverable is a `CANCEL` line in the contract plus four small, named changes, not a
feature.**

1. **Add `CANCEL` to Amendment 1's contract block.** It has `BAKED AT BUILD` / `STDOUT` / `EXIT` /
   `RESUME` / `INVARIANT` and no way to stop a running seat. Add the ladder as contract:

   ```text
   CANCEL   1. cooperative — write the wrap-up instruction to the seat's stdin
               (--input-format stream-json); seat commits, reports, exits 0
            2. hard       — {"type":"control_request","request":{"subtype":"interrupt"}}
            3. signal     — SIGTERM to the claude process; NEVER SIGINT (exits 0)
            in all three the scheduler MUST outlive the child to receive the envelope
   ```

2. **Fix `ClaudeResult` so the abort envelope survives** (`harness.baml:91-97`). Make `result`
   optional (`result: string?`) and add `terminal_reason: string?`; replace the unconditional
   `is_error` panic at `:334` with a branch that maps `aborted_tools` / `aborted_streaming` to a
   *cancelled* `SeatRun` rather than a crash. This alone converts today's silent, unpriced kill into
   an attributed, priced one, and is the single highest-value change in this spike. Small enough to
   be one bead in `baml-harness`.

3. **The `local` tier must signal the `claude` process, not the group, and must hold the pipe**
   (§2, §4). `bh-a7so.2`'s Recommendation 3 (`start_new_session=True` + `os.killpg`) is right that
   `proc.terminate()` orphans the child, but a group kill takes the *reader* down with the writer
   and throws away the envelope (§2 vs §4). The requirement for `bh-c6dk.5` is: own the pipe, signal
   the child, read the envelope, **then** `killpg` as the reaper. Order matters and is measurable —
   0.63 s of patience is the whole difference between a priced cancel and a silent one.

4. **Bake the interrupt protocol into the seat prompt, don't improvise it mid-run** (§7). The seat
   correctly identified an ad-hoc "STOP, the scheduler says…" message as prompt-injection-shaped and
   complied only because the action was reversible. Amendment 1 already puts role behaviour in the
   binary; the wrap-up contract ("on an interrupt instruction: finish the current tool call, commit
   as `wip: interrupted`, emit `INTERRUPT_ACK`, stop") belongs there with it, so the mid-run message
   is a *trigger for known behaviour* rather than a novel instruction to be judged. Keep
   `control_request` as rung 2 precisely because it is out-of-band and cannot be declined.

5. **Retire `bh-a7so.2` Recommendation 4's `usage` half** (§10, §11). `SeatRun.usage` and
   `cost_usd` are exact; the transcript needs `message.id` deduplication. Budget accounting can be
   built on the envelope. The *other* half of that recommendation — that a killed run's spend is
   invisible — is fixed by change 2, not by moving to stream-json.

6. **Promote `--session_id` as `bh-a7so.2` recommended, but downgrade the urgency.** §9 shows
   `session_id` reaches stdout in the `system/init` line ~2 s in under stream-json, and §4 shows it
   in the abort envelope under single-shot. A caller-minted uuid is still better (it is known before
   the process starts), but it is no longer the only thing standing between a crash and anonymity.

Sequencing: change 2 is independent, cheap, and unblocks the rest — it should land in `baml-harness`
before `bh-c6dk.2` is written. Changes 1, 3, 4 are ADR/contract edits for `bh-a7so.4` to absorb.
**Feed** this verdict to `bh-a7so.4` as: *the contract is adoptable and should grow a `CANCEL` line;
cancellation is a scheduler capability, not a harness one (§13), and the harness's only obligation
is to stop discarding the envelope.* **Close** `bh-20he` (throwaway target, done).

### What this spike could not measure — stated rather than estimated

- **Whether a seat will refuse a *destructive* wrap-up instruction.** §7 records that the seat
  flagged the injection and complied only because committing is "harmless/reversible". The
  adversarial case — an interrupt asking for something the seat judges unsafe — was not run.
- **`--brief` / `SendUserMessage`.** Listed in `claude --help` as an agent→caller channel and
  relevant to §14, but not exercised. The Q4 sketch does not depend on it.
- **Cooperative cancel against a *live* bead worktree.** All runs used scratch git repos and a
  hand-created throwaway bead. Whether an interrupted seat can be driven to `bh work submit` rather
  than a bare `git commit` is untested; §6 got a commit and an ack, not a lifecycle transition.
- **Lease/claim behaviour under the cooperative path.** `bh-a7so.2` §11 measured what beads does
  when a holder dies (5 min TTL, `bd reclaim` recovers). Whether a *cooperatively* cancelled seat
  should release its own claim was not tested and is a scheduler design question.
- **`control_request` subtypes beyond `interrupt`.** Only `interrupt` was sent; the protocol
  plainly has others (the response carried a `still_queued` field this spike never populated).
- **The harness's 900 s `timeout_ms`** (`harness.baml:330`) — still not exercised, same as
  `bh-a7so.2`.
- **Any provider but `claude-code`.** `codex` is `implemented == false`; §6–§8 are Claude Code CLI
  behaviours and nothing here transfers to it.
- **Distribution.** Seven runs, one model (`claude-sonnet-5`), one task shape, one machine, one CLI
  version (2.1.220), one kill point (80 s). The §12 cost figures are single samples per mechanism —
  the 1.32× cooperative premium is one run against a four-run mean, not a mean with a confidence
  interval. The mechanism findings (§4, §6, §8, §10) are deterministic and replicated across runs;
  the numbers are not.
