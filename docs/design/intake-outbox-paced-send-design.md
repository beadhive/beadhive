# Design: the paced serial outbox write path (80 write requests a minute, 500 an hour)

- **Status:** Proposed (revision 4, after three reviews). It must be reviewed and approved before
  `bh-eqtxv` (writeahead-impl) starts.
- **Date:** 2026-11-10
- **Bead:** `bh-natxe`, molecule `bh-womf3` (patch release 0.35.8)
- **Implemented by:** `bh-eqtxv`, after `bh-gjo05` (binding from the listing), `bh-n0h15` (org
  field definitions once per command) and `bh-vy7dx` (one HTTP client per command)
- **Builds on:** [`external-intake-module-adr.md`](external-intake-module-adr.md) sections 4
  (one lock) and 10 to 11 (the O1 to O19 machine, writes and gates); `bh-pc9rf` (the batch
  scope); `bh-25fvb` (the store session, `guard.primary_epoch`, the per-run token cache)
- **Amends:** nothing in the ADR. The outbox document, the O1 to O19 machine, the per-report
  hold and the single-send path are unchanged. The sender gains one hook before each write-ahead,
  and the GitHub adapter's transport gains a write gate and a wait observer.
- **Operator decisions (2026-11-10):**
  1. Every outbox write request is capped at **80 a minute** with a **500 an hour** budget,
     inside GitHub's documented secondary limits. Faster pacing for type, fields and labels waits
     for a later sandbox measurement (a follow-up the dispatcher files).
  2. The target for a 270-op `send-approved` is **about 4 minutes**.
  3. Sends stay **serial**, with the pacer only. Parallel sends across reports are recorded as a
     rejected-for-now alternative with a trigger.
  4. Pacing is **on by default** in 0.35.8.

## Decision summary

Keep `send-approved` **serial**: one report at a time, each under its own hold of the binding's
lock, each operation in `SEND_ORDER` with its own digest, grant, write-ahead and receipt, exactly
as today. Pace **every write request** the outbox sends to GitHub, not every operation: one
operation can send several (a `labels.change` is one `POST` plus one `DELETE` per removed label;
an `issue.fields` can `POST` twice on one grant; a rate-limited write is resent by the adapter's
retry). A **host-level write ledger** counts the requests, shared by every command on the host:
at most **80 starts in any minute** and **500 in any rolling hour**. The batch **stops on the
first rate-limit answer** GitHub gives, and **stops before an operation** whose requests would
not fit in the hour (`stopped=hourly_budget`, the rest left `approved`).

Two keys in `intake.external.outbox`, both on by default:

| Key | Default | Allowed |
|---|---:|---|
| `send_writes_per_minute` | `80` | `1` to `80` |
| `send_writes_per_hour` | `500` | `1` to `500` |

A value above the bound is refused at validation as a safety limit (see
[Configuration](#configuration)). The unit is one write request, whatever the key's name says.

**Do not build** option 1 (merging an operation's receipt with the next write-ahead) or option 2
(parallel sends across reports). Under the 80-a-minute pace a serial batch is already held back by
the pace at the modeled latency, so neither makes the target faster. Option 2 is
**rejected for now**, with a trigger to revisit it (see
[Parallel sends: rejected for now](#parallel-sends-rejected-for-now)).

Modeled, after the three cheap levers, for the type and Priority of 135 issues (270 operations,
270 write requests when no field write is retried):

| Path | Nominal latency | Every per-report latency 50% worse |
|---|---:|---:|
| Serial, unpaced (today after the levers; no longer allowed) | 164.2 s, about 103 requests a minute | 242.6 s |
| **Serial, paced at 80 (this design)** | **209.8 s (3.5 min)** | **242.6 s (4.0 min)** |
| Same, if every field write is retried once (405 requests; worst case) | 311.0 s | 311.0 s |
| Option 1, serial, paced at 80 (rejected) | 209.8 s | 217.4 s |
| Option 2, 2 workers, paced at 80 (rejected for now) | 209.8 s | 209.8 s |

The paced serial batch is held at the pace until per-report latency reaches about 1.29 times the
model, and stays under 4 minutes until about 1.48 times.

## Context: where the send time goes after the levers

`OutboxService.send_approved` (`src/beadhive/modules/intake/application/sender.py`) reads every
outbox document in one pass. It lists the due operations per report in `SEND_ORDER` (`REOPEN,
COMMENT, LABELS, TYPE, FIELDS, CLOSE`, `domain/sending.py`) and enters the lock's batch scope.
Then, for each report, `_send_report` takes **one** hold through `threads.held()`: the binding's
`IntakeLock` plus one store session. Under that hold it runs `_send_held` for each operation:

1. `_open`: read the document, then O13 (recover any `sending` operation as "holder gone").
2. If the operation is still `approved`, the O7 re-check (`machine.invalidate`).
3. `machine.begin_send` (O9). It re-renders the operation, checks the digest against the
   approval, consumes the single-use grant and records an `Attempt` with the hold's epoch. Then
   comes **one compare-and-set write** (the write-ahead).
4. **The tracker write** with the grant (`_write`): one or more requests (below).
5. O10 receipt, O11 refusal or O12 unknown, then **one compare-and-set write** (the outcome).
6. For a sent comment, `_notify` (`store.set_state`, which is `bd set-state notified=`). After
   the hold ends, the completion policy runs (`_completed`, which takes the lock again).

`send` (one operation) runs the same `_send_held` under its own hold. These two verbs are the
only callers of `_send_held`, and so the only places the outbox writes to the tracker. `labels
sync` drafts, `labels approve --batch` approves and `reconcile` reads; none of them writes to the
tracker.

Latencies, from `tests/test_intake_outbox_batch_perf.py` (measured 2026-11-09 unless marked):

| Cost | Seconds | Count in a 270-op batch, after the levers | Total |
|---|---:|---:|---:|
| `bh` process | 4.4 | 1 | 4.4 |
| host check and epoch (`guard.primary_epoch`), recheck 300 | 1.85 | 1 | 1.9 |
| outbox listing | 0.85 | 1 | 0.9 |
| org issue-field definitions (`bh-n0h15`: once per command) | 0.35 | 1 | 0.4 |
| one report read under its hold (`bh-gjo05` removes the second) | 0.077 | 135 | 10.4 |
| compare-and-set write (66 ms on scratch, scaled to the factory: modeled) | 0.124 | 540 | 67.0 |
| GitHub write after `bh-vy7dx` (0.35 s request, 0.07 s token, minus the client reuse) | 0.294 | 270 | 79.4 |
| **Serial total, unpaced** | | | **164.2** |

The per-request 0.294 s is derived. It takes the measured 0.35 s request and the 0.07 s token,
then subtracts the sibling estimate for client reuse (about 34 s over the batch). It is not a
measurement. The epic's modeled ranges (plus or minus 30 to 50%) apply to every latency-bound
figure in this note.

Code facts the design depends on, each checked against the tree (and, where marked, against the
molecule's container branch `wt/bead/epic/bh-womf3`):

- **One operation can send several write requests.** Every write goes through one method,
  `Session.send` (`packages/beadhive-tracker-github/src/beadhive_tracker_github/adapter/transport.py`),
  which runs `_run`: each attempt is one request.
  - `labels.change` (`adapter/labels.py`, `_send_changes`): one `POST .../labels` when labels are
    added, then one `DELETE .../labels/{name}` **per removed label**. So `1 + removals` requests
    (or `removals` alone when nothing is added).
  - `issue.fields` (`adapter/issue_values.py`, `set_issue_fields`, on the container branch after
    `bh-n0h15`): one `POST .../issue-field-values`; on a `422` or `404` built from the memoized
    field ids it re-reads the definitions and **`POST`s once more on the same grant**. So 1, or
    2 at worst.
  - `issue.type` (`PATCH`), `comment` (`POST`), `issue.close` and `issue.reopen` (`PATCH`): one
    request each.
  - A write refused with a rate limit is **resent** by `_run` after the wait, within
    `RetryBudget`; each resend is another request.
- **The adapter retries a rate limit itself.** For a write, it retries only a rate-limit
  refusal (proof that nothing was written), within `RetryBudget` (3 retries, at most 60 s for one
  wait and 120 s in total), sleeping through the injectable `Transport.sleep(seconds)`. A wait
  longer than the budget raises at once, without sleeping. When it gives up, `RateLimited` is in
  `PROOF_OF_NO_WRITE` (`application/outbox.py`), so the operation goes to O11
  `failed(refused:RateLimited)`, and a retry needs the operator's `retry` and a new approval. A
  read (`GET`, including the reads inside a write operation: the field definitions, the label
  fallback read) also retries a server error or a timeout through the same `sleep`. The decision
  is `retry.next_wait(refusal, ...)`, and the `Refusal` it gets carries the error:
  `RateLimited` (with its `kind`, `throttle` or `quota`) for a rate limit, `TransientFailure`
  for a server error or timeout. `Transport.sleep` itself sees only the seconds.
- **The compare-and-set route does not re-read before a write.** `write_outbox` checks the
  expected `seq` against `_seq_row`. On the compare-and-set route (`_compare_and_set()` true),
  `_seq_row` returns the **open session's cached row** (`_row`), not a fresh read: the
  compare-and-set is the check. So there, a concurrent writer is caught by the swap: the service
  answers `swapped: false`, `SessionReportApi.set_document` raises
  `ReportApiError(refused=True)` ("changed since it was read, nothing was written"), and
  `ApiReportStore._set_document` turns it into `IntakeStoreError`, reported `store_error`.
  `IntakeRefused(OUTBOX_MOVED)` there means only that the session's own row disagrees with the
  caller's `expect`. On the `bd` CLI route `_seq_row` re-reads the store, `OUTBOX_MOVED` is how a
  concurrent writer shows, and the write itself overwrites.
- **The token source.** The adapter calls its injected `TokenSource` once per request. With the
  opt-in per-run cache (`plugins.tracker-github.credential.per_invocation_cache`, `bh-25fvb`)
  the first token is kept for the command. With the cache off, every write resolves afresh: the
  environment, then a `gh auth token` subprocess (about 70 ms) when no environment variable holds
  a token, so up to 80 subprocesses a minute at the cap. The cache is recommended. Serial sends
  call the source from one thread, so the cache needs no lock in this design.
- **Today's batch has no stop on a rate limit.** A `RateLimited` outcome is reported in `failed`
  and the batch goes on to the next operation. Only `not_primary` stops it, and `stopped` is the
  refusal's detail text. An unexpected exception (anything but `IntakeRefused` and
  `IntakeStoreError`) propagates out of `send_approved`.
- **Config sections forbid unknown keys** (`_QuietSection`, `extra="forbid"`). A host on an
  older release rejects a config that carries a key it does not know.

## GitHub's secondary rate limits

**Verified** by fetching GitHub's documentation on 2026-11-10 ("Rate limits for the REST API",
sections "About secondary rate limits" and "Calculating points for the secondary rate limit":
<https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#about-secondary-rate-limits>,
and "Best practices for using the REST API":
<https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api>).
The quotes are exact:

- "No more than 100 concurrent requests are allowed. This limit is shared across the REST API and
  GraphQL API."
- "Make too many requests to a single endpoint per minute. No more than 900 points per minute are
  allowed for REST API endpoints". "Most REST API `POST`, `PATCH`, `PUT`, or `DELETE` requests"
  cost 5 points, and most `GET` requests cost 1.
- "Some REST API endpoints have a different point cost that is not shared publicly."
- "No more than 90 seconds of CPU time per 60 seconds of real time is allowed. [...] You can
  roughly estimate the CPU time by measuring the total response time for your API requests."
- "In general, no more than 80 content-generating requests per minute and no more than 500
  content-generating requests per hour are allowed. Some endpoints have lower content creation
  limits."
- "Content creation limits include actions taken on the GitHub web interface as well as via the
  REST API and GraphQL API."
- "These secondary rate limits are subject to change without notice. You may also encounter a
  secondary rate limit for undisclosed reasons."
- Best practices: "Avoid concurrent requests: [...] you should make requests serially instead of
  concurrently." "Pause between mutative requests: If you are making a large number of `POST`,
  `PATCH`, `PUT`, or `DELETE` requests, wait at least one second between each request."
- On a rate limit: honour `retry-after`, else `x-ratelimit-reset` when `x-ratelimit-remaining` is
  0, "Otherwise, wait for at least one minute before retrying." Then: "Continuing to make requests
  while you are rate limited may result in the banning of your integration." The adapter's
  `retry.py` already implements exactly this.

**What the cap does and does not guarantee.** The pacer counts **write requests** at the one
method that sends them, so on this host the outbox's own write requests, from every command and
every hive, stay at or under 80 starts in any minute and 500 in any rolling hour, whether or not
GitHub counts a type, fields or labels write as content-generating. Two bounded exceptions, both
written to the ledger so the following requests pay for them: a request that is part of an
operation already past its write-ahead is never refused (an extra field `POST` or a rate-limit
resend may run up to one minute's spacing early, and may go over the hour), and the stop then
keeps the next operation from starting. It does **not** cover:

- other writers on the same account: other hosts (a ledger is host-local), bh's own non-outbox
  GitHub writes (label definitions, the projection), `gh` and the web interface. "Content
  creation limits include actions taken on the GitHub web interface as well as via the REST API
  and GraphQL API";
- lower or hidden limits: "some endpoints have lower content creation limits", and "some REST API
  endpoints have a different point cost that is not shared publicly".

So the cap is the ceiling the outbox holds itself to, and the stop on the first rate-limit answer
is what happens when the account's real budget is smaller.

**The 80 a minute leaves the other figures far behind:** 80 writes at 5 points is 400 points a
minute against 900 (whether that limit is per endpoint or aggregate); a serial batch has one
request in flight; and 80 writes a minute at about 0.3 s each is about 24 s of response time a
minute against 90 s of CPU. It is faster than the best-practice "one second between each"
mutative request (60 a minute); the operator keeps 80 for now (see the open questions).

**My inference, not verified:** whether `PATCH /repos/{owner}/{repo}/issues/{n}` (the type),
`POST .../issues/{n}/issue-field-values` (the fields) and the labels endpoints are
"content-generating" is not documented. If they are not, a pace above 80 might be safe for them.
That is a measurement, not a design choice; see [Faster pacing later](#faster-pacing-later).

## The design: a pacer per write request

### Two seams: the operation hook and the transport's write gate

**The operation hook (intake, before the write-ahead).** `_send_held` gains one optional
argument, `before_write: Callable[[OutboxOperation], None]`, which it calls **after `_open` and
the O7 re-check, right before `machine.begin_send`**. It:

1. checks the command's stop event;
2. asks the pacer to **admit the operation**: its expected request count `n` (1 for every kind,
   `1 + removals` for a `labels.change` with additions, `removals` without) must fit in the hour
   (`ledger count + n <= send_writes_per_hour`), and the operation's **first** request slot is
   taken here, which may wait for the minute spacing;
3. checks the stop event again.

A stop, or an hour that cannot fit `n`, raises `SendStopped(reason)` from the hook. That happens
before `begin_send`, so the operation stays `approved` with its grant still minted, and nothing
durable is left behind. `_send_report` catches it, reports the operation with the stop reason, and
the batch reports every later operation the same way without opening its report.

**The write gate (GitHub adapter, per request).** `Transport` gains an optional injected
`write_gate: Callable[[str], None]` (a small port in the tracker contracts, `WriteGate`), which
`Session._run` calls **before every attempt of a write** (`send`: `POST`, `PATCH`, `DELETE`;
each rate-limit resend included). The composition root wires the outbox pacer into it only for
the adapter that `send` and `send-approved` use; every other adapter keeps no gate, as today.

- The first request of an operation uses the slot the hook already took (it is recorded in the
  ledger at that moment), so the common one-request operation never waits after its write-ahead.
- Every further request (the second `DELETE` of a labels change, the field `POST` retry, a
  rate-limit resend) takes its **own slot**: it may wait for the minute spacing (at most
  `60 / per_minute`, 0.75 s), then is recorded. This wait is after the write-ahead, which is safe:
  the operation is already `sending`, and a crash in the wait is the same as a crash in the
  request (row 6). It is never refused: the operation is mid-write, and refusing would leave an
  operation half-applied. If the hour is already spent, the request is still recorded (an
  overdraft the following admissions pay for) and the command's stop event is set with
  `hourly_budget`, so no further operation starts.

The hook runs inside the report's hold, so a pacer wait lengthens that hold. The gaps between
holds stay milliseconds, as today, so other intake verbs on the host see no practical change.

### The pacer and the write ledger

One `WritePacer` per command, over one shared **host-level write ledger**.

- **Minute spacing inside a command, on the monotonic clock.** The pacer keeps
  `next = max(now, last + 60 / send_writes_per_minute)` on `time.monotonic()`, so a wall-clock
  step never shortens or lengthens the spacing. The wait is `stop.wait(delay)` on the command's
  stop event, so a stop ends it at once and the slot is given up.
- **Every slot is one atomic ledger step (shared by concurrent commands).** Taking a slot is:
  take the exclusive `flock` on the ledger's **lock file**, read the data file, prune entries
  older than the hour, count the entries in the last minute and in the last hour, and either
  **append** the slot and release, or release and say why not. All of it under the one lock, at
  every slot, so two
  commands on the host (two `send-approved` runs, a script calling `send` in a loop, two hives
  bound to the same account) grant slots from the same current count, never from a stale copy
  read at the start. When the minute window is full, the pacer releases the lock, waits until
  the oldest entry in the window leaves it (on the stop event), and tries again. When the hour
  cannot fit the operation, the hook stops (above); the per-request gate overdrafts (above).
- **Wall clock only in the ledger.** Entries are wall-clock seconds (they must compare across
  processes). An entry dated in the future (the clock stepped back) is **kept and counted** until
  real time passes it by the window, which errs towards fewer writes. A **torn last line** (a
  crash mid-append) is skipped and the rest of the file is used; an unreadable file as a whole
  is reported once on stderr and treated as holding a full minute (one minute's wait), never as
  empty.
- **The lock is a separate, permanent file.** A `flock` belongs to the open file's inode, so it
  must never be taken on a file that is replaced. The lock is `<api host>.ledger.lock`, created
  once and never renamed, truncated or deleted (bh never removes it; an operator deleting it while
  no send runs is harmless). The data file `<api host>.ledger` is separate. The prune rewrites
  the data file through a temporary file in the same directory, `fsync`, then `rename` over the
  data file, all while holding the lock file's `flock`. Every reader and appender opens the data
  file by path **after** taking the lock, so it always sees the current inode, and no process can
  run the read-prune-count-append step while another one does. Appends are one `write` of one
  line, followed by `fsync`, under the same lock.
- **Scope: per host and per GitHub API host, not per hive.** GitHub's limits are per account, and
  the ledger cannot know the account behind a token without an extra request. So it lives in a
  host-level directory (`$XDG_STATE_HOME/beadhive/github-writes/`, falling back to
  `~/.local/state/...`; the data file `<api host>.ledger` and its lock `<api host>.ledger.lock`),
  shared by every hive and every command on the host that sends through the
  outbox. Two accounts used from one host share one budget, which errs towards fewer writes. A
  primary move to another host starts from that host's ledger.
- **The hourly budget's outcome.** When the hook stops on the hour, the outcome says how many
  operations were not started and the time at which enough entries leave the hour (when a
  re-run can continue). Every operation not started stays `approved` with its grant, so the
  re-run needs no new approval. It does not wait (that could keep the command running for up to
  an hour). A single `send` whose request does not fit is refused with a new `RefusalReason`,
  `HOURLY_BUDGET` (`hourly_budget`).
- **Ports and fakes.** The ledger is a port (`WriteLedger.take(now, per_minute, per_hour, n) ->
  Granted | MinuteFull(until) | HourFull(until)`, one atomic call) with a memory fake shared
  between pacers in tests. The pacer takes an injected monotonic `clock`, wall `clock` and `wait`.
  Unit tests never sleep.
- **Reads are not paced** (listings, the field definitions, a fresh issue read, the labels
  fallback read). They cost 1 point and are not content-generating.

### Stopping on the first rate limit

- **The wait observer.** `Transport` gains an optional `observe_wait: Callable[[Refusal, float],
  None]`, called by `_run` **whenever `next_wait` returns a wait** (any value, `0` included),
  before the `if wait > 0` sleep, with the `Refusal` that `next_wait` decided on. So a rate limit
  with `retry-after: 0` still reaches the observer and still stops the batch. The composition
  root's observer **classifies by the refusal**, not by when it happens:
  - `RateLimited` (a 429, or a 403 with GitHub's rate-limit signals; `kind` `throttle` or
    `quota`), on any method: set the stop event with `rate_limited` **at the first such wait**,
    and move the pacer's next slot past `now + seconds`. Then the adapter sleeps as asked, so the
    request that saw the limit finishes its own retries within `RetryBudget` (each resend takes a
    gate slot, above).
  - `TransientFailure` (a `GET`'s server error or timeout, including a read inside a write
    operation): move the next slot only. It is not a rate limit and never stops the batch.
- **The O11 backstop.** An outcome of O11 `refused:RateLimited` also stops the batch with
  `rate_limited`. On its own this fires only **after the adapter's retries are spent** (up to
  three waits, 120 s in total), or at once when GitHub asks for a wait longer than the budget
  (then the adapter raises without calling `sleep`, so the observer never sees it). The stop at
  the first wait is the primary trigger; this is the backstop.

### Interrupts: the stop event comes from the composition root

The application layer never installs a signal handler. The composition root (the
`send-approved` CLI command, in the **main thread only**: it checks
`threading.current_thread() is threading.main_thread()` and skips the handler otherwise) creates
a `threading.Event` and installs a `SIGINT` handler for the duration of the call, keeping the
handler it replaced. On the first signal the handler sets the event with `interrupted` and
switches the process's `SIGINT` handler to `signal.default_int_handler`, so the **second**
`SIGINT` raises `KeyboardInterrupt` as today. When the call ends (normally, by a stop, or by an
exception), a `finally` restores **the previous handler**, whatever it was (not the default). The
event is passed to `OutboxService` (`BatchSend.stop`). The
pacer and the hook wait on that same event, so the first interrupt ends a pacer wait at once.

### Stop reasons

The command has one stop event and one stop reason, and the first stop wins. `stopped` becomes a
**reason code** (`not_primary`, `rate_limited`, `hourly_budget`, `interrupted`, `error`) with the
detail in a separate `stopped_detail`.

- `not_primary`: the host check refused. Today's only stop (whose `stopped` was the detail text).
- `rate_limited`: the first rate-limit wait, or an O11 `refused:RateLimited`. This replaces
  today's "report it and continue".
- `hourly_budget`: the next operation's requests do not fit in the rolling hour, or a mid-write
  request overdrew it.
- `interrupted`: the first `SIGINT`. The operation in flight (past its write-ahead) finishes its
  requests and writes its outcome (**draining**); no later operation starts.
- `error`: an unexpected exception from one operation. `_send_report` catches it (today it
  propagates out of the batch), reports it for its operation, sets the stop, and the command
  exits non-zero after printing the outcome.

After any stop, no new write-ahead starts: the check runs in the hook right before
`begin_send`. **Every operation not started is listed in `failed` with the stop reason** and
stays `approved` with its grant, so **re-running `send-approved` continues where the batch
stopped** with no new approval.

### Output

The batch JSON gains `"pacing": {"writes_per_minute": 80, "writes_per_hour": 500, "requests":
<count>, "waited_seconds": <total>}`, the reason-coded `stopped` and `stopped_detail`, and the
not-started operations in `failed`. `bh-17g6b`'s per-phase timings gain the pacer's total wait.

### Invariants: every operation keeps its own digest, grant, write-ahead and receipt

Nothing in the send path below the hook changes:

- **Digest:** `begin_send` re-renders the operation and refuses unless the digest equals the
  one its own approval recorded (O7 invalidates first when the render moved).
- **Single-use grant:** O9 consumes this operation's grant in this report's document, and the
  grant authorizes exactly this operation's requests (the adapter's own contract, unchanged).
- **Write-ahead:** its own compare-and-set, durable before its first request.
- **Receipt:** its own O10, O11 or O12 compare-and-set right after its last request.
- **Order:** operations in `SEND_ORDER`, one report at a time, as today.
- **Approval:** nothing becomes `approved` without an operator approval minting a fresh grant.
  A stop leaves operations `approved`. It never re-approves anything.

The only things that move are the pacer's waits (before each write-ahead, and before a further
request of an operation) and the new stops.

### Crash and retry semantics

Per operation, the events are: **S** (the hook: stop check, hour check, first slot), **A**
(`begin_send`, in-process), **W1** (write-ahead compare-and-set), **Q** (the tracker requests,
with a gate slot before each further one), **W2** (outcome compare-and-set), **N** (`_notify`,
comments only), then **C** (completion, after the report's hold, as today).

| # | Interleaving | State left behind | Recovery | Duplicate write possible? |
|---|---|---|---|---|
| 1 | crash before or during S (a pacer wait included), or A | `approved`, grant minted; the slot may be in the ledger | re-run `send-approved` sends it | no: nothing was requested |
| 2 | W1 refused: a lost compare-and-set (`swapped: false`, reported `store_error`, "nothing was written"); `OUTBOX_MOVED` on the CLI route, or when the session's row disagrees | whatever the other writer left; this operation not begun | reported; the batch goes on as today | no: no request without a durable write-ahead |
| 3 | W1 ambiguous (`IntakeStoreError` from a transport failure) and it applied | `sending`, never requested | next `_open`: O13 to `unknown`; reconcile, two complete absences, `failed(absent)`; operator `retry` and approve | no |
| 4 | W1 ambiguous and it did not apply | `approved` | re-run sends it | no |
| 5 | crash after W1, before the first request reaches GitHub | `sending` | as 3 | no |
| 6 | crash during Q (in a request, or in a gate wait between two requests), before W2 | `sending` (some or all requests may have applied) | O13 to `unknown`; reconcile reads it back: O14 `sent` (reconciled), or absences | no: `unknown` is never sendable, and a retry needs k absences plus an operator |
| 7 | Q answers a proof of no write (`RateLimited` included) | O11 `failed(refused:...)` | operator `retry` and approve; a `RateLimited` stops the batch if the first wait had not already | no |
| 8 | Q ambiguous (5xx, timeout, unreadable 2xx) | O12 `unknown` | reconcile; the batch continues | no |
| 9 | W2 fails: a lost compare-and-set (`store_error`), or a transport failure (`store_error`, it may have applied); or a second `SIGINT` / process crash between W1 and W2 | `sending` (or the outcome, if a transport failure applied it) | as 6; `_notify` does not run | no |
| 10 | crash after W2, before N | settled; `notified` missing | as today: reported, never un-sends | no |
| 11 | crash after the hold, before C | settled; report not completed | as today: the window is one report | no |
| 12 | first `SIGINT` | the in-flight operation drained and settled | `stopped=interrupted`; re-run continues | no |
| 13 | an unexpected error in one operation after its request (in `translate.receipt`, `record_receipt`, or a store raise that is not `IntakeStoreError` on W2) | that operation `sending` | `stopped=error`; as 6 | no |
| 14 | `rate_limited` or `hourly_budget` stop | the in-flight operation settled (sent after its retry, or O11); the rest `approved` | re-run continues (after the budget frees for `hourly_budget`) | no |
| 15 | crash while holding the ledger lock file's `flock` | the kernel drops the lock; at most a torn last line, or a leftover temporary file from a prune (the data file is the old one or the new one, never half of each, because of the rename) | the next slot skips the torn line; a leftover temporary file is ignored and removed by the next prune | no |

A crash leaves at most one operation `sending`, as today.

Lock loss and primary changes are unchanged from today's serial batch (`bh-pc9rf`): per-report
holds, the batch scope's reuse window (`batch_recheck_seconds`), O13 recovery by epoch, and a
lost write reported as above. The pace bounds how many requests start inside a reuse window after
the primary moves (at most 80 a minute). The new host has its own ledger, so in the hour after a
primary move the two hosts together can start more than 500 requests; the stop on a rate limit is
the backstop.

## Modeled numbers

Constants as in [Context](#context-where-the-send-time-goes-after-the-levers): fixed costs of
7.45 s (process, one admission, listing, field definitions), and per report 1.161 s (one read,
four compare-and-sets, two GitHub writes), so about 0.58 s per request.

- **Paced at P requests a minute, R requests**, while the sender keeps up with the pace: 7.45 +
  0.077 + (R - 1) × 60 / P + 0.54 (the last operation's tail). For the type and Priority batch
  R = 270 when no field write is retried: **P = 80: 209.8 s (3 min 30 s).**
- **The field retry.** Each retried field write adds one request. The retry happens only when the
  memoized field ids are stale (a field or option changed during the command), so normally none.
  In the worst case, all 135 (R = 405): 311.0 s, and 405 of the hour's 500.
- **The serial path keeps up with 80** at nominal latency: it sustains 60 / 0.58 = 103 requests
  a minute, so the pace binds and the batch takes 209.8 s. It keeps up until per-report latency
  is about **1.29 times** the model (1.5 s per report). Beyond that it is latency-bound: with every
  per-report latency 50% worse, 7.45 + 135 × 1.742 = **242.6 s**, and it stays under 4 minutes
  until about 1.48 times.
- **The ledger step** (lock, read, prune, append, at most about 500 lines at 500 an
  hour) is a local file operation, well under a millisecond against the 0.75 s spacing. It is not
  in the model.
- **At the default `batch_recheck_seconds: 0`** every report re-admits (1.85 s each): about
  250 s of admission alone, so the target needs the factory's opt-in 300 (already set; a 210 s
  batch fits in one window). This is unchanged from today.
- **The hourly budget:** a 270-request batch uses 270 of 500. A second such batch within the hour
  stops after 230 requests with `hourly_budget`, and its re-run continues once the first batch's
  requests leave the hour. A labels batch counts `1 + removals` per operation.
- **Uncertainty:** while the pace binds, the batch time is the pace arithmetic plus about 8 s of
  fixed cost, so its error is the fixed costs' error (a few seconds), not the plus or minus 30 to
  50% of the latency model. Once latency exceeds about 1.29 times the model, the latency model's
  error applies again.

### Faster pacing later

Pacing type, fields and labels faster than 80 a minute is **out of this design**. It needs a
sandbox measurement first: an operator-approved run on a sandbox repository that raises the pace
for those kinds step by step and records where (and whether) GitHub answers with a secondary
limit, and how that interacts with comments and with web-interface activity on the same account.
If a later decision raises the cap on that evidence, the validation bound is raised by a code
change in that release (and might become per kind). The dispatcher files the follow-up.

## Configuration

The keys go in `IntakeOutboxConfig` (`intake.external.outbox`), next to `batch_recheck_seconds`,
with the same strict validation (pydantic, `extra="forbid"`; the schema artifact
`config-v1.schema.json` is regenerated).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `send_writes_per_minute` | int, `>= 1` and `<= 80` | `80` | The most outbox write requests started in any minute, by `send` and `send-approved`, across every command and hive on this host (the write ledger). |
| `send_writes_per_hour` | int, `>= 1` and `<= 500` | `500` | The most outbox write requests started in any rolling hour, on the same terms. A batch whose next operation does not fit stops with `hourly_budget`. |

The ledger is host-level, but the keys are per hive. Each command applies its own hive's values
to the shared count, so a hive with lower values is held to them and a hive at the defaults is
held to 80 and 500 of everything on the host.

**Why the upper bounds are hard, and why there is no "off".** These are safety limits, not
tuning defaults. 80 and 500 are GitHub's documented general content-creation figures, and
GitHub warns that "continuing to make requests while you are rate limited may result in the
banning of your integration". Whether type, fields and labels writes count is not documented, so
the safe reading is that they do. A configuration that asks for more is refused at validation
with a message that names the reason, rather than accepted and silently clamped. Lower values
stay configurable (for an account shared with heavy web-interface use, or a sandbox). The bounds
move only by a code change after a measured decision ([Faster pacing later](#faster-pacing-later)).
There is no `0` meaning unpaced: an unpaced serial batch after the levers starts about 103 write
requests a minute.

`labels approve --batch`, `labels sync` and `reconcile` do not write to the tracker and are not
paced. A single `send` takes its slots from the same pacer and ledger, so a script calling `send`
in a loop is held to the same pace.

## Migration, rollout and rotation

- **No data migration.** The outbox document, its schema and the machine are unchanged.
- **Behaviour change on upgrade, with no config change.** UPGRADING lists every item:
  - every outbox write request is paced at 80 a minute (a 270-op batch that would take 164 s
    after the levers takes 210 s; today's batch, before the levers, starts far fewer than 80 a
    minute and is not slowed in practice), counted across every command and hive on the host;
  - a batch stops before an operation whose requests do not fit in 500 an hour, with
    `hourly_budget`;
  - a batch stops on the first rate limit, where today it reports the operation and goes on;
  - a batch stops on the first `SIGINT` after draining the operation in flight, and on an
    unexpected error with `stopped=error`, where today both abort the batch;
  - **JSON contract change:** `stopped` becomes a reason code (`not_primary`, `rate_limited`,
    `hourly_budget`, `interrupted`, `error`) with the text in a new `stopped_detail`; today
    `stopped` is the `not_primary` detail text. Scripts that test `stopped` for truthiness keep
    working; scripts that parse its text do not;
  - **operations not started are now listed in `failed`** with the stop reason (today a
    `not_primary` stop leaves later reports out of the outcome);
  - a single `send` can be refused with a new `RefusalReason`, `hourly_budget`;
  - **two new host-level files**, the write ledger and its lock
    (`$XDG_STATE_HOME/beadhive/github-writes/<api host>.ledger` and `<api host>.ledger.lock`),
    created on first use. Deleting the ledger forgets the last hour; both are safe to delete while
    no send runs.
- **Rotation order.** The defaults apply as soon as a host runs 0.35.8, with no config written.
  Only a hive that wants **lower** values sets the keys, and because config sections forbid
  unknown keys, it does so after every host that loads the hive's config runs 0.35.8 (installed
  and restarted per the frame rotation runbook, then `bh hive ready`; `release_matches` or
  conformance can read false for a moment just after an install, which is not a reason to roll
  back). That is a fleet config write, so it needs the operator's binding.
- **First run.** The next real batch, or an operator-approved sandbox repository, with the
  defaults and `batch_recheck_seconds: 300`. Record the batch JSON's timings (including the pacer
  wait and the request count) and any `rate_limited` stop on the epic. If a secondary limit stops
  it, the operations not started are still approved: lower `send_writes_per_minute`, re-run, and
  `retry` plus re-approve only the one operation that failed.
- **Rollback.** Lowering the keys is the only knob; there is no unpaced mode. Downgrading a host
  below 0.35.8 restores unpaced sends there; remove the keys first if they were set.

## Test plan

No timing-dependent unit tests. Unit tests use the memory store, the memory tracker and injected
clocks. Wall clock appears only in the `-m perf` suite.

**Unit (`tests/unit/modules/intake/`, fakes):**

1. The existing batch tests pass with the pacer on a fake clock that never makes them wait. The
   hold count, write count (two per operation) and order are pinned.
2. The pacer: slots are `60 / per_minute` apart on the fake monotonic clock; the hook runs after
   `_open` and O7 and before `begin_send`; a stop during a wait gives up the slot and the
   operation stays `approved`.
3. The hourly budget: with a ledger holding 499 entries in the last hour, the second operation
   stops the batch with `hourly_budget`; the outcome lists the operations not started in `failed`
   and the time the budget frees; they stay `approved`, and a re-run after the fake clock passes
   it sends them with no new approval. A ledger holding 80 entries in the last minute delays the
   first slot. A single `send` with a spent budget is refused `hourly_budget`. A labels operation
   with 2 removals and 1 addition is not admitted when only 2 requests fit.
4. Config bounds: `send_writes_per_minute: 81`, `send_writes_per_hour: 501` and `0` for either
   are refused with a message that names GitHub's figure; the defaults are 80 and 500;
   `tests/test_config_intake.py`'s key list gains both, and the schema artifact is regenerated.
5. Rate limit, first wait, **through the real GitHub adapter over `httpx.MockTransport`** (with
   the injected `sleep`, gate and observer): a `429` with `retry-after` on operation j's request
   makes the observer set `stopped=rate_limited` and move the next slot; the resend takes a gate
   slot and succeeds, and j is `sent`; no later write-ahead starts; the later operations stay
   `approved` and a re-run sends them.
6. Rate limit, backstop: a `RateLimited` raised with no sleep (a `retry-after` longer than the
   budget, through the same mock transport) gives O11 for j and `stopped=rate_limited`.
7. A `GET` backoff inside a write operation: a `503` on the field-definitions read of an
   `issue.fields` (mock transport) reaches the observer as `TransientFailure`, moves the next slot
   and does **not** stop the batch; the operation is then `sent`.
8. Per-request counting (mock transport): a `labels.change` with one addition and two removals
   records 3 ledger entries, the second and third after a gate slot each; an `issue.fields` whose
   first `POST` answers `422` on stale memoized ids records 2; a rate-limit resend records one
   more. With the hour already spent, the mid-write request is recorded (overdraft), the operation
   completes, and `stopped=hourly_budget` keeps the next one from starting.
9. Drain after interrupt (row 12): the stop event set (as the composition root's handler would)
   while operation j is between W1 and W2 lets j write its outcome, sets `stopped=interrupted`,
   starts no later write-ahead, and lists the rest in `failed`. A separate CLI-level test checks
   that the handler is installed only in the main thread, that the first `SIGINT` switches the
   handler to `signal.default_int_handler` (so a second `SIGINT` raises `KeyboardInterrupt`), and
   that a custom handler installed before the call is the one restored after it, on a normal end
   and on an exception.
10. The error stop (row 13): the error is injected **after the request**: in `translate.receipt`,
    in `machine.record_receipt`, and as a store raise that is not `IntakeStoreError` on W2. Each
    sets `stopped=error`, leaves the operation `sending`, and a fresh service's `_open` and
    `reconcile` recover it; the command's exit code is non-zero.
11. Lost compare-and-set mid-batch: a memory store that answers `swapped: false` on one
    operation's W1 reports it `store_error` ("nothing was written") with the operation still
    `approved`, and on another operation's W2 reports `store_error` with the operation `sending`;
    the batch does not stop.
12. Two pacers sharing one ledger: two pacers (two commands) over one memory ledger, interleaved
    by a scripted fake clock, never grant more than `per_minute` entries in any minute or
    `per_hour` in any hour between them; and the file ledger does the same for two processes
    (a `multiprocessing` test with a barrier, asserting the counts, not the timing).
    **Two processes, one prunes:** the file ledger is seeded with entries older than the hour, so
    the first slot of either process prunes (rewrites the data file and renames it). Two
    processes released together by a barrier each take N slots; afterwards the data file holds
    exactly 2N entries newer than the hour (no append lost to the rename), and a probe that
    records which process held the lock file around each step shows no overlap.
13. A clock stepping back: entries dated in the future after a wall-clock step back are kept and
    counted; the monotonic spacing inside the command is unaffected.
14. The ledger file: prune to the hour, append under the lock, a torn last line skipped with the
    rest counted, a whole unreadable file treated as a full minute and reported once, a missing
    file starting empty, a leftover temporary file from a crashed prune ignored and removed, and
    the lock file's inode unchanged across prunes.
15. The wait observer on `retry-after: 0` (mock transport): a `429` with `retry-after: 0` reaches
    the observer (with no sleep, since the wait is 0) and stops the batch with `rate_limited`.

**Crash injection** (memory store and memory tracker; a `_Crash(BaseException)` raised at a named
point; then a fresh service, as a new process with a new lock holder, runs `send_approved` and
`reconcile`). For each point (in a pacer wait, W1 applied then crash, after the first of two
label requests, after Q applied, after Q not applied, after W2, between reports):

- no issue is written twice: the memory world counts writes per grant id, and each request of a
  grant runs at most once;
- every operation ends `sent` (returned or reconciled), `failed(refused:...)`,
  `failed(absent)`, or still `approved` when it never began;
- no operation reaches `approved` without a new operator approval;
- the re-run sends exactly the operations that never began.

**Perf (`tests/test_intake_outbox_batch_perf.py`, `-m perf`):**

- Model the sibling levers: field definitions once, a GitHub write of 0.294 s and the binding
  from the listing.
- **The pacer interval is scaled with the suite's `SCALE`** (0.05): the pacer gets the suite's
  scaled `wait` and a clock in model seconds, so its 0.75 s interval sleeps 37.5 ms and the
  modeled batch time (`slept / SCALE` plus overhead) is comparable with the latencies. Without
  this the pacer would dominate the scaled run and the assertion would measure nothing. The
  ledger is the memory fake.
- Run paced at 80 with latency factors 1.0 and 1.5. Assert the modeled send under 240 s at 1.0,
  and print 1.5 as a reference (about 243 s). Assert from recorded fake-tracker request
  timestamps, in model seconds, that no 60 s window holds more than 80 write-request starts.
- Keep the existing assertions for the serial structure (holds, reads, writes per report).

## Alternatives rejected

### Charging each operation its worst-case request count up front

Considered for B1 and not chosen. It keeps every wait before the write-ahead, but it overcharges
the common case: `issue.fields` would cost 2, so the type and Priority batch would be charged 405
requests (311 s and 405 of the hour) for 270 real ones. Counting at `Session.send` charges what is
sent; the only waits after a write-ahead are for an operation's further requests, and a crash
there is already row 6.

### Parallel sends: rejected for now

**Trigger to revisit:** a measured factory batch whose per-report latency exceeds about **1.3
times** the model (the point where serial stops keeping up with 80 a minute), or a measured
decision that raises the cap above what one serial sender sustains (about 103 a minute). Either
one: file a follow-up bead that picks up this section.

**Why not now.** At 80 a minute serial and parallel both take about 210 s for 270 operations at
the modeled latency. Parallel buys margin only: two workers keep up with the pace until about 2.6
times the model, against serial's 1.29 times. The design below costs threads, a batch-long lock
and a wider completion crash window for that margin.

**The design, as reviewed (revision 2, `65c16f77`):**

- **Units.** Partition the due operations into units, one per issue URL, each in `SEND_ORDER`,
  run on `send_workers` threads (recommended 2), merged back into the serial order for output.
- **One batch-long hold** (`hold_shared`). Workers cannot each `held()`: a second flock of the
  same file in one process is refused (probed). So the coordinator takes the binding's flock once
  for the batch, and each unit calls a thread-safe `Admission.epoch()` that serializes the
  admission (`guard.primary_epoch`'s `_ADMITTED` hand-over is a `ContextVar`, which a new thread
  does not see; probed). O13 stays correct because no other unit touches a unit's report. The
  flock would be held for the whole batch (about 3.5 minutes), refusing other intake verbs with
  `INTAKE_BUSY` for that time.
- **One store per worker** from an injected `store_factory` (the store session is not
  thread-safe). **Every worker store's route is resolved before any unit starts** (a public
  `uses_compare_and_set()`); if any is on the `bd` CLI route or raises, the whole batch runs
  serially, because W concurrent `bd update` processes on one Dolt store are unmeasured and the
  CLI route has no compare-and-set for the record half of the lock.
- **`_notify` serialized.** `set_state` has only the `bd` CLI route, so notify runs under a
  batch-wide lock right after the receipt (deferring it would widen the `notified` crash window
  to the whole batch).
- **Deferred completion.** The completion hook takes the binding's lock, which the batch holds,
  so milestone completions run on the coordinator after the hold. That widens the completion
  crash window to the whole batch; recovery would need stderr lines per pending completion, a
  `completion_pending` list in the JSON and a read-only listing of open reports whose close rule
  holds, plus a way to run their completion.
- **Shared adapter state.** The grant ledger is atomic. The field-definitions cache and the
  adapter's read cache are filled before workers start; the HTTP client is per thread. The token
  source's per-run cache is an unguarded list: W workers calling it first together would each run
  `gh auth token`, so the coordinator resolves it once first and the cache gets a lock. The write
  gate and the ledger step above are already safe for concurrent callers.
- **At the capped pace requests barely overlap** (a slot every 0.75 s against about 0.58 s per
  request), so GitHub sees a concurrency of 1, sometimes 2; and each worker's Beads session open
  is a cost the model does not include.
- **Crash bound:** at most W operations `sending` after a process crash, one per worker.

### Option 1: merge a receipt with the next write-ahead (rejected)

**Protocol.** For one report's due operations o1 to on, the writes become `W1 =
write-ahead(o1)`, then `Q1`, then `W2 = outcome(o1) + write-ahead(o2)`, and so on, ending with
`Wn+1 = outcome(on)` alone. `W(k+1)` is one compare-and-set after two pure applications: O10 (or
O11 or O12) for `o(k)`, then `begin_send` for `o(k+1)`. The document `seq` advances by 2 in one
write. If `begin_send(o(k+1))` refuses, or O7 invalidates it, the outcome of `o(k)` is written
alone. Under the pacer, the slot for `o(k+1)` would be taken before `W(k+1)`, which delays the
outcome of `o(k)` by the wait.

**What it changes.** The outcome of `o(k)` is durable **if and only if** the write-ahead of
`o(k+1)` is. One ambiguous store write that applied leaves `o(k+1)` `sending` with no request,
which costs it two absences, `failed(absent)` and a re-approval. The sender's "one durable write
per step" and the machine's "one applied call is one document write" stop being true.

**Numbers.** It removes 135 of the 540 store writes: 16.7 s unpaced. Paced at 80 a serial batch
is held at the pace at nominal latency (209.8 s) with or without it; at 50% worse latency it gives
217.4 s against 242.6 s. Not worth coupling two operations' durability. Revisit only if a measured
batch is store-bound with the pace not binding.

### Others

- **Unpaced or above-80 sends:** about 103 write requests a minute serially after the levers.
  They exceed GitHub's documented content-creation figure if these writes count, and GitHub warns
  that persisting under a limit may ban the integration.
- **Waiting out the hourly budget** instead of stopping: it would keep the command running for up
  to an hour.
- **A per-hive ledger:** simpler to place, but GitHub's limits are per account, and two hives
  bound to one account on one host would each grant the full budget.
- **Batching writes on GitHub's side** (one GraphQL request with many mutations, or type and
  fields in one call): this breaks "exactly one request per grant", and the fields endpoint is
  separate. It is a different contract, out of scope for a patch.
- **Batching store writes across reports** (`issues:batchApply`): it couples unrelated reports'
  failures, and under the pace the store is off the critical path anyway.

## Operator decisions and open questions

**Answered (2026-11-10):**

1. Pacing ships **on by default** in 0.35.8.
2. Sends stay **serial**; parallel sends are rejected for now, with the trigger above.
3. Defaults: `send_writes_per_minute: 80`, `send_writes_per_hour: 500`,
   `batch_recheck_seconds: 300` on the factory; no workers.
4. **80, not 60, for now**, although GitHub's best practice asks for one second between mutative
   requests (60 a minute; 270 s for this batch).
5. The **host-local ledger is acceptable**, with the stop on the first rate limit as the backstop
   for writes it cannot see (another host after a primary move, the web interface, other
   clients on the same account).
6. The **sandbox measurement** for faster type, fields and labels pacing is a follow-up the
   dispatcher files.

**Still open:**

1. **Does the Beads service run concurrent `casMetadata` writes in parallel?** Not needed for
   this design; it matters only if parallel sends are revisited. A probe on `beadhive-scratch`
   would settle it and needs operator approval, because it writes to a scratch hive.
