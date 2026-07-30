# Spike `bh-a7so.1` — how does `dist/bh-developer` actually talk to a caller, today?

**Bead:** `bh-a7so.1` · **Seat:** `dev/wire` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-a7so.4` (Decision: adopt the baked-seat role-binary contract as
`bh-c6dk.2`, or extend it) — the wire-format half of Amendment 1 to
`docs/design/work-runtime-tiers-adr.md`.

> Scope note: the bead text says "run against one small real ready bead." The dispatcher
> overrode that instruction before this spike started: running a live agent against real
> backlog work as a spike side effect was judged unacceptable. Instead this spike created and
> used its own disposable throwaway bead (`bh-1a05`) in this hive, closed and cleaned up at the
> end. No real backlog bead (`bh-c6dk.*`, `bh-a7so.*`, or any other) was ever targeted by the
> harness.

## Question

Can a scheduler drive `dist/bh-developer` **as it exists right now, unmodified**, and if so
how — which stream carries the typed result, does the exit code carry `RoleOutcome.status`,
does the bead's `bd` state actually move, is `--workspace` validated, what happens on a bad
bead id, and is `RoleOutcome.bead_id` a checked round-trip or just echoed prose? This is
explicitly **not** asking whether the *proposed* contract in Amendment 1 is a good idea (it
already is, per the ADR) — it is asking how far short of it today's binary falls, in
concrete, evidenced terms, so `bh-a7so.4` can price the delta.

## Method

1. Read `bh-a7so.1`'s brief, the parent epic `bh-a7so`'s design field (the contract under
   test), and `docs/design/work-runtime-tiers-adr.md` Amendment 1 in full.
2. Read `beadhive/baml-harness`'s `README.md` and `baml_src/harness.baml` (`SeatRun`,
   `RoleOutcome`, `seat_argv`, `run_resolved_seat`, `run_seat_with`) and `baml_src/seats.baml`
   (`developer_seat`'s actual parameter list) before spending anything.
3. Ran the two free/cheap probes first: `./dist/bh-developer --help` and `just providers`.
4. Created one disposable throwaway bead, `bh-1a05`, in this hive (`bh bd create`), with a
   trivial self-contained task ("create `scratch/spike-bh-a7so-1/hello.txt` containing the
   word hello"), as the target for the one real invocation the dispatcher's override
   permitted, instead of the epic's originally-specified "one small real ready bead."
5. Hand-wrote a minimal bundle (`spike-bundle.json`, kept outside both repos, in the session
   scratchpad — the README states a hand-written bundle is exactly as valid as a
   hitch-emitted one) granting the `developer` seat `Bash(*)`/`Read`/`Write`/`Edit`/`Glob`/
   `Grep` allow rules and instructions that name the `bh work` verb sequence, since a
   hand-rolled bundle carries its own `instructions` and there was no real beadhive-emitted
   bundle available in the harness checkout's `testdata/`.
6. Ran `dist/bh-developer` five times total, each invocation's raw stdout/stderr/exit code
   captured to separate files and read back verbatim (never inferred): (a) a nonexistent
   `--workspace`, (b) an unimplemented `--provider`, (c) the real throwaway bead with a
   Bash-capable bundle, (d) a nonexistent bead id with the same bundle, plus the two free
   `--help`/`providers` probes.
7. Independently verified every claim the harness's own `RoleOutcome.summary` made against
   `bd`/`git` ground truth (bead status before/after, worktree/branch/commit, the specific
   lint-error lines it cited) rather than trusting the model's self-report.
8. Cleaned up: `bh work abandon bh-1a05 --rm`, `bh bd close bh-1a05`, deleted the stray local
   branch `wt/bead/issue/bh-1a05` the harness run created in the main clone. No product code
   in either repo was modified; `src/` was never touched.

## Evidence

### 1. The compiled binary's argv was stale — and changed mid-session, unprompted

First `--help`, before any real run:

```text
$ ./dist/bh-developer --help
function developer_seat(task: string, workspace: string, bundle: string, provider: string [optional]) -> SeatRun

Usage: bh-developer [OPTIONS]

Options:
      --task <string>
      --workspace <string>
      --bundle <string>
      --provider <string>   [optional]
      --json-args <SOURCE>  Pass arguments as JSON …
  -h, --help                Print help
```

This matches the epic's own claim verbatim ("Current argv is `--task --workspace --bundle
--provider`"). But `baml_src/seats.baml`'s `developer_seat` signature (read in Method step 2,
already committed per `git log -1 --format=%cI -- baml_src/seats.baml` → `2026-07-29T21:03:46`,
and `git log -3 --oneline -- baml_src/seats.baml` showing `ba95459
feat(bh-baml-8u9.11): add dispatch-time --tier/--model overrides` as the tip) already declares
five more parameters: `resume_session`, `session_id`, `fork_session`, `tier`, `model`. The
packed `dist/bh-developer` (mtime `2026-07-27T17:11`, 12,723,106 bytes at session start) was
simply **stale relative to its own source tree**.

Mid-session, with no `just build`/`just pack` run by this spike, `dist/bh-developer` and
`dist/bh-dispatcher` were silently rebuilt — `stat` showed the mtime move to
`2026-07-30T00:42:15`, size 12,723,106 → 13,317,538 bytes — and a second `--help` then showed
all nine parameters: `--task --workspace --bundle --provider --resume_session --session_id
--fork_session --tier --model`. The harness checkout (`/Users/brian/workspace/github/beadhive/
baml-harness`) is a single shared, unsandboxed clone, not a per-spike worktree; something else
running concurrently against the same epic (plausibly the sibling checkpoint/resume spike
`bh-a7so.2`, which needs `--resume_session`) rebuilt it. **This is itself a delta-relevant
fact, not just a methodology footnote**: a scheduler dispatching a *packaged* binary is
trusting that packaging is current, and nothing in this repo enforces that today.

### 2. `--workspace` is not validated as anything — it is the literal OS process `cwd`, full stop

`baml_src/harness.baml:330` passes `workspace` straight into
`baml.sys.ProcessOptions { cwd: workspace, … }`, and `seat_argv` (harness.baml:200-203) only
ever reads it to push `--add-dir <workspace>`. No git check, no existence check, anywhere in
`baml_src/*.baml` (confirmed by grepping every `workspace` occurrence in the harness source).

Verbatim invocation with a nonexistent path:

```text
$ ./dist/bh-developer \
    --task "Say hello. Reply with RoleOutcome status=done, summary=hello, bead_id=null, next_action=null." \
    --workspace "/tmp/definitely-does-not-exist-zzz-bh-a7so-1" \
    --bundle ""
EXIT: 1
```

**stdout (verbatim):** *(empty — zero bytes)*

**stderr (verbatim):**

```text
error: Traceback (most recent call last):
  File "baml_src/seats.baml", line 121, in user.developer_seat
  File "baml_src/harness.baml", line 361, in user.run_seat_with
  File "baml_src/harness.baml", line 330, in user.run_resolved_seat
uncaught throw: baml.errors.Io {message: "Failed to spawn 'claude': No such file or directory (os error 2)"}
```

The failure is a bare OS `ENOENT` on the `claude` subprocess spawn (because its `cwd` doesn't
exist), surfaced as an uncaught BAML exception — not a typed `RoleOutcome`, not a graceful
error. Any path that merely *exists* (verified below: the beadhive hive root, a plain git
clone, not even a bead-specific worktree) works fine — the harness makes zero distinction
between "a git repo," "a bead worktree," and "any directory."

### 3. A bad `--provider` fails the same shape as a bad `--workspace`

Both collapse to exit 1, empty stdout, an "uncaught throw" traceback on stderr.

```text
$ ./dist/bh-developer --task "Say hello." \
    --workspace "/Users/brian/workspace/github/beadhive/beadhive" \
    --bundle "" --provider anthropic-api
EXIT: 1
```

**stdout:** *(empty)*

**stderr (verbatim):**

```text
error: Traceback (most recent call last):
  File "baml_src/seats.baml", line 121, in user.developer_seat
  File "baml_src/harness.baml", line 361, in user.run_seat_with
  File "baml_src/harness.baml", line 313, in user.run_resolved_seat
  File "baml_src/provider.baml", line 169, in user.require_supported_provider
uncaught throw: baml.panics.UserPanic {message: "provider 'anthropic-api' is not implemented by this harness.\n  auth:       ANTHROPIC_API_KEY. Billed per token against the API account, NOT against a Claude Code subscription.\n  bounded by: Nothing local — there is no tool loop. The model returns text and cannot touch this machine. Seat permission rules are inert.\n  supported today: claude-code"}
```

Two structurally different failures — a process-spawn OS error and an application-level
`require_supported_provider` panic (`provider.baml:157-172`) — are **indistinguishable from
the outside**: same exit code (1), same empty stdout, same "uncaught throw" shape on stderr.
A caller can only tell them apart by string-matching the traceback text.

### 4. A real run: stdout carries the whole `SeatRun`, exit is 0 even though `status` is `"blocked"`

Throwaway target `bh-1a05` before the run (`bh work issue bh-1a05 --json`):
`"status": "open"`, no `assignee`.

Verbatim invocation:

```text
$ ./dist/bh-developer \
    --task "Work bead bh-1a05 in this hive. Its acceptance: create a file scratch/spike-bh-a7so-1/hello.txt containing the word hello, then commit and submit it through bh work verbs. Report back status=done if you got it submitted, bead_id=bh-1a05." \
    --workspace "/Users/brian/workspace/github/beadhive/beadhive" \
    --bundle "<scratchpad>/spike-bundle.json" \
    --provider claude-code
EXIT: 0
```

**stdout (verbatim, one line):**

```json
{"outcome":{"status":"blocked","summary":"Created scratch/spike-bh-a7so-1/hello.txt containing 'hello' and committed it (1c5cc70) on wt/bead/issue/bh-1a05. `bh work check` and `bh work submit bh-1a05` both fail: uv/ruff pass, but markdownlint-cli2 fails with 5 pre-existing MD040 errors in docs/design/temporal-control-plane-adr.md (lines 58, 140) and docs/design/work-runtime-tiers-adr.md (lines 151, 232, 254) — fenced code blocks missing a language tag. These files are unrelated to bh-1a05's acceptance criteria and I did not modify them. Submit aborted with 'clean-checkout validation failed (exit 1) — nothing submitted'.","bead_id":"bh-1a05","next_action":"Pre-existing markdownlint debt in docs/design/*.md (unrelated to this bead) blocks the repo-wide lint gate used by `bh work check`/`submit`. Needs a separate bead/fix to add language tags to those fenced code blocks before bh-1a05 (or any bead) can pass submit; escalate to coordinator/merger rather than fixing inline since it's out of this bead's scope."},"session_id":"c649a0af-3415-46c4-be24-85ce1a918981","cost_usd":0.5336196000000001,"usage":{"input_tokens":14,"output_tokens":2310,"cache_creation_input_tokens":64855,"cache_read_input_tokens":365992},"packs":[]}
```

**stderr:** *(empty)*

Independently verified, not taken on faith:

- `bh work issue bh-1a05 --json` after the run: `"status": "in_progress"`,
  `"assignee": "dev/spike-a7so1"`, `started_at`/`heartbeat_at` populated, lease active — `bd`
  state genuinely moved. `claim → work` happened; `submit` did not.
- `bh worktree path --bead bh-1a05` → a real worktree; inside it, `git log -1 --oneline` →
  `1c5cc70 chore(spike): add hello.txt for bh-a7so.1 harness wire-format spike target` on
  branch `wt/bead/issue/bh-1a05`, `cat scratch/spike-bh-a7so-1/hello.txt` → `hello`,
  `git status --short` → clean (no stray uncommitted mess left by the failed submit).
- The cited lint failure is real, not confabulated: `docs/design/temporal-control-plane-adr.md`
  line 57 and `docs/design/work-runtime-tiers-adr.md` line 150 are both fenced code blocks
  opened with a bare ` ``` ` (no language tag) — exactly the MD040 shape the agent described,
  in files this spike never touched.
- No review gate was opened on `bh-1a05` (`dependent_count`/`dependency_count` both `0`) —
  consistent with `submit` genuinely aborting rather than partially succeeding.

**`RoleOutcome.bead_id` was `"bh-1a05"`** — it matches what `--task` said, because the model
copied it into its structured reply; nothing in `harness.baml` cross-checks it against
anything (there is no `--bead` input to check it against — see Evidence 6).

### 5. A nonexistent bead id: same wire shape, `bd` untouched, `bead_id` still just echoes the prompt

```text
$ ./dist/bh-developer \
    --task "Work bead bh-zzzz99 in this hive. Start with: bh work brief bh-zzzz99, then bh work claim bh-zzzz99 --as dev/spike-a7so1-b. Report exactly what happened, including bead_id=bh-zzzz99 in your reply." \
    --workspace "/Users/brian/workspace/github/beadhive/beadhive" \
    --bundle "<scratchpad>/spike-bundle.json" \
    --provider claude-code
EXIT: 0
```

**stdout (verbatim, one line):**

```json
{"outcome":{"status":"blocked","summary":"bh work brief bh-zzzz99 failed with '✗ no such bead: bh-zzzz99' (exit code 1). bh work claim bh-zzzz99 --as dev/spike-a7so1-b failed the same way. The bead does not exist in this hive, so no worktree was claimed and no code changes were made.","bead_id":"bh-zzzz99","next_action":"Verify the bead id is correct — check for a typo or confirm it was actually filed in this hive (bd show/bd search) — then reassign."},"session_id":"4eb19295-ceb2-457b-820e-247bdcbdf92d","cost_usd":0.243,"usage":{"input_tokens":6,"output_tokens":567,"cache_creation_input_tokens":31913,"cache_read_input_tokens":143330},"packs":[]}
```

**stderr:** *(empty)*

Independently verified: `bh work issue bh-zzzz99` → `Error fetching bh-zzzz99: no issue found
matching "bh-zzzz99"` (exit 1); `bh worktree path --bead bh-zzzz99` → `✗ no managed worktree`
(exit 1). Nothing was created — the harness correctly did nothing beyond what its (fully
Bash-capable) seat chose to attempt and report. The harness itself never looked up or
validated the bead id; the seat did, via ordinary `bh work` calls it was told to run, and
reported the result in prose that the model then folded into `bead_id`.

### 6. `--bead` does not exist as an input, anywhere, today

`baml_src/seats.baml`'s `developer_seat`/`dispatcher_seat`/`any_seat` take
`task/workspace/bundle/provider/resume_session/session_id/fork_session/tier/model` — no
`bead` parameter, confirmed by the `--help` dump in Evidence 1 both before and after the
mid-session rebuild. A bead id can only enter today through free-text `--task`, and
`RoleOutcome.bead_id` can only ever be **what the model chose to write back**, verbatim
confirmed identical to the `bh-1a05`/`bh-zzzz99` strings each task named (Evidence 4, 5).
There is no code path anywhere in `harness.baml`/`seats.baml` that reads a `--bead` flag,
extracts an id from `--task`, or cross-checks `RoleOutcome.bead_id` against anything. The
"did the agent work the bead it was handed?" round-trip check Amendment 1 proposes (`docs/
design/work-runtime-tiers-adr.md:284`) is **not buildable today without first adding the
`--bead` input it would check against** — right now there is nothing to check.

### 7. Exit code is a 2-value signal, fully decoupled from `RoleOutcome.status`'s 3 values

Collecting every exit code observed across this spike:

| Invocation | `status` (if any) | stdout | stderr | Exit |
|---|---|---|---|---|
| bad `--workspace` | *(none — no SeatRun produced)* | empty | traceback | **1** |
| unimplemented `--provider` | *(none — no SeatRun produced)* | empty | traceback | **1** |
| real bead, submit blocked on unrelated lint debt | `"blocked"` | full JSON | empty | **0** |
| nonexistent bead id | `"blocked"` | full JSON | empty | **0** |

Both `"blocked"` results — which Decision 4 explicitly says must **never** be retried — exit
**0**, the same code a `"done"` result would exit (not separately observed here, since no run
in this spike reached a clean submit, but the code path is uniform: the CLI wrapper exits 0
whenever `run_seat_with`/`developer_seat` returns a `SeatRun` at all, regardless of what
`.outcome.status` says — nothing in `harness.baml` or `seats.baml` branches on `status` to
choose an exit code). The proposed taxonomy (`0 done · 10 blocked · 11 handoff · anything
else = did not complete`) **does not exist in the binary today** — there is no 10, no 11,
only 0 (`SeatRun` returned, any status) and 1 (an uncaught BAML `throw`/`panic`, any cause,
`SeatRun` absent).

### 8. Authority is 100% runtime — nothing about the packed binary constrains a bundle

The same `dist/bh-developer` binary ran against a hand-written bundle (Method step 5) that
sets `"permissions": {"allow": ["Bash(*)", …], "ask": [], "deny": []}` — an unrestricted Bash
grant supplied entirely at invocation time via `--bundle <path>`, and the binary accepted it
without complaint. Nothing baked into the packaged artifact narrowed it. This is exactly the
gap Amendment 1 names under "Why bake the bundle" (`docs/design/work-runtime-tiers-adr.md:
267-280`): today, "anything that can spawn the process can hand it a bundle granting
`Bash(*)`" is not a hypothetical, it is what this spike's own invocation did.

### 9. Cost

Two real runs: `$0.5336196` (bh-1a05) + `$0.243` (bh-zzzz99) = **$0.777** total token spend for
this entire spike, plus whatever the two free `--help`/panic probes cost (zero — both failed
before spawning `claude`).

### 10. This bead's own submit independently hit the identical blocker Evidence 4 found

Not a harness finding, but a direct dogfood confirmation filed while writing this artifact.
`bh work check bh-a7so.1` and `bh work submit bh-a7so.1 --as dev/wire` — run for this bead's
own commit, containing only this file — both fail with the exact same five pre-existing
`MD040` errors in `docs/design/temporal-control-plane-adr.md` and
`docs/design/work-runtime-tiers-adr.md` that the throwaway-bead run in Evidence 4 hit
independently, and the exact same terminal message: `✗ clean-checkout validation failed
(exit 1) — nothing submitted`. This is repo-wide, pre-existing debt (unrelated to this
bead's content, unrelated to `bh-1a05`'s content) that blocks `bh work check`/`submit` for
**every** bead in the `bh-a7so` molecule, not just this one — `bh-a7so.2`/`.3` have not
committed yet, so they have not hit it, but will the moment they try to submit against the
same lint gate. Filed as escalation `hq-79r` rather than fixed inline, per this spike's own
"no product code, do not fix anything you find" scope: touching two unrelated ADR docs is
out of scope for a spike whose only sanctioned deliverable is this file. This bead's commit
(`4fafa74`) is complete and correct on `wt/bead/issue/bh-a7so.1`; only the repo-wide gate
blocks `submit`.

## Verdict — **GO**

A scheduler **can** start driving `dist/bh-developer` today, but only by matching the
*corrected* Amendment 1 model exactly — "stdout `SeatRun` is the rich channel, `status` is
the source of truth whenever stdout parses, exit code is only the degraded-mode signal" — and
**not** the specific `0/10/11` exit-code taxonomy the same amendment's `EXIT` row still lists,
which is unbuilt. Concretely: stdout reliably carries one line of well-formed `SeatRun` JSON
whenever the process completes (confirmed twice, including a `blocked` outcome with a real,
independently-verified failure the seat reasoned about correctly); stderr stays empty on
every completed run and is reserved for BAML's own uncaught-exception tracebacks when the
process never reaches `SeatRun` at all; `bd` state genuinely advances when the seat is told to
and given Bash. That is enough surface for a scheduler to build against **if it treats exit
code as binary (0 = check stdout, anything else = infra retry) and never as a status proxy.**

The delta to the proposed contract, priced for `bh-a7so.4`:

1. **No `--bead` input, anywhere.** `RoleOutcome.bead_id` is unchecked, model-echoed prose,
   not a verified round-trip. Adding `--bead` is a prerequisite for the round-trip check
   Amendment 1 wants, not a decoration on top of it.
2. **Exit code carries no status information today.** 0 means "a `SeatRun` came back,
   whatever it says"; 1 means "BAML threw before producing one," for causes ranging from a
   typo'd `--workspace` to an unimplemented `--provider`, indistinguishable without parsing
   stderr text. The `10`/`11` taxonomy is unbuilt.
3. **`--workspace` has zero validation** — not "accepts any path" as a design choice, but
   literally never checked against anything; a bad path surfaces as a raw OS spawn error on
   stderr, not a graceful result.
4. **Authority is still 100% runtime**, carried by `--bundle <path>` alone; nothing about the
   packed binary bakes permissions/permission_mode/mcp_config/plugin_dirs the way Amendment 1
   proposes.
5. **The resume flag name and shape differ from the proposal**: today's binary (once rebuilt
   to match current source) exposes `--resume_session` + separate `--session_id`/
   `--fork_session`, not the single `--resume <session_id>` the amendment specs — a rename/
   consolidation, not a new capability (this spike did not exercise resume; that is
   `bh-a7so.2`'s question).
6. **Packaging currency is an unmanaged risk.** The binary this spike started with was three
   days stale relative to its own committed source and changed under this session without
   this session doing it — in a shared, non-isolated checkout. A scheduler dispatching
   `dist/bh-<seat>` needs either a rebuild-on-deploy step or a version check; neither exists.

None of these are unknowns anymore — each is now a specific, evidenced line item rather than
an inference.

## Recommendation

For `bh-a7so.4`: adopt Amendment 1's contract as the target, but scope the resulting
`bh-c6dk.2` to include the six delta items above as explicit build work, not "already done":
add `--bead` (and use it to populate/verify `bead_id`), implement the `0/10/11` exit-code
mapping from `RoleOutcome.status` in the CLI wrapper, decide and validate what `--workspace`
must be (at minimum: exists; probably: is a git worktree, since the scheduler's own contract
assumes one), design the build-time authority baking, and reconcile the `--resume_session`/
`--session_id`/`--fork_session` shape against the proposed single `--resume`. None of this
invalidates the contract — the stdout/`SeatRun` half is real, tested (`harness.baml`'s own
unit tests cover `seat_argv` extensively), and this spike's live runs behaved exactly as that
half promises. Do not schedule against exit code alone until item 2 lands; until then, any
caller (including a `local`-tier poll loop) must treat exit 0 as "go read stdout," not as
"succeeded."
