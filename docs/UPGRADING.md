# Upgrading `bh`

Narrative upgrade notes for releases that change the shape of a user's on-disk state or CLI
surface — what you must run, what's safe to delete, and what must never be copied between
machines. For the mechanical per-commit list, see [CHANGELOG.md](../CHANGELOG.md); this file
exists for the releases where "read the changelog" isn't enough to act on.

## 0.35.7 → 0.35.8 — outbox speed (bh-womf3)

One new HOST-only key, `hq.authority_expiry_recheck_s` (default `300`, `0` re-reads on every
command), sets how often a command re-reads the authority status for the expiry warning. Two new
outbox keys pace every outbox write (on by default, below), and two new host-level files hold the
pace's ledger; no record or document changes shape. The changes below make an outbox batch, or a
single send, cheaper or measurable, and one paces and stops it.

### Every outbox write is paced: 80 a minute, 500 an hour, and a stop on the first rate limit (bh-eqtxv)

**Behaviour change on upgrade, with no config change.** `bh work outbox send` and `send-approved`
pace every GitHub **write request** they make (not every operation: a `labels.change` is one `POST`
plus one `DELETE` per removed label, and a rate-limit resend counts again), counted across every
command and every hive on the host. Design:
[`intake-outbox-paced-send-design.md`](design/intake-outbox-paced-send-design.md).

- **Every outbox write request is paced at 80 a minute** (GitHub's documented limit of 80
  content-generating requests a minute), counted in a host-level ledger that records each
  request as it starts, so at most 80 start in any 60 s on the host (the exact bounds and
  their exceptions are below). A 270-operation batch
  that would take about 164 s after this release's other changes takes about 210 s (about 3.5
  minutes); today's batch, before them, starts far fewer than 80 a minute and is not slowed in
  practice. Sends stay serial.
- **A batch stops before an operation whose requests do not fit in 500 an hour**, with
  `stopped: hourly_budget`; the outcome says when enough of the hour frees
  (`pacing.budget_frees_at`, and the time in `stopped_detail`). It never waits the hour out.
- **A batch stops on the first rate limit** GitHub answers (`stopped: rate_limited`), including a
  `retry-after: 0`; the request that saw it finishes its own retries. Before, the operation was
  reported and the batch went on.
- **A batch stops on the first `SIGINT`** after the operation in flight finishes and writes its
  outcome (`stopped: interrupted`; a second `SIGINT` aborts as before), and **on an unexpected
  error in one operation** (`stopped: error`, the command exits non-zero; that operation may be
  left `sending`, which the next run recovers and `bh work outbox reconcile` settles). Before,
  both aborted the whole batch.
- **JSON contract change:** `stopped` is now a **reason code** (`not_primary`, `rate_limited`,
  `hourly_budget`, `interrupted`, `error`) and its text moves to a new `stopped_detail`. Before,
  `stopped` was the `not_primary` detail text. Scripts that test `stopped` for truthiness keep
  working; scripts that parse its text do not. The batch JSON also gains
  `"pacing": {"writes_per_minute", "writes_per_hour", "requests", "waited_seconds",
  "budget_frees_at"}`, and the `timings` block a `pacer_wait` phase.
- **Operations not started are now listed in `failed`** with the stop reason (before, a
  `not_primary` stop left later reports out of the outcome). Every one stays `approved` with its
  grant, so **re-running `send-approved` continues where the batch stopped**, with no new approval.
- A single `bh work outbox send` can be refused with a new refusal reason, **`hourly_budget`**,
  when its request does not fit in the hour.
- **Two new host-level files**, created on first use: the write ledger
  `$XDG_STATE_HOME/beadhive/github-writes/<api host>.ledger` (falling back to
  `~/.local/state/beadhive/github-writes/`; `<api host>` is `api.github.com`, or the GHES host of
  `plugins.tracker-github.base_url`) and its lock `<api host>.ledger.lock`. Deleting the ledger
  forgets the last hour; both are safe to delete while no send runs. An unreadable ledger is
  reported once and counted as a full minute; a ledger that cannot be written stops the batch
  with `stopped: error` before any further write-ahead (a single `send` fails `store_error`).

**The bounds, exactly** (derived from the code and each pinned by a test):

- **Minute: at most 80 request starts in any 60 s on the host, strictly**, across every command
  and hive: each request start is one locked ledger step that waits while the last 60 s hold 80.
  Two exceptions: a **forward step of the wall clock** makes the ledger's entries look older, so
  in the minute after the step **at most 80 extra (100% over) may start, once per step** (a step
  back only delays); and a ledger that **stops being writable mid-operation** lets that one
  operation's remaining requests go unrecorded (at most its own requests, below), then the batch
  stops with `error`.
- **Hour: at most 500 request starts in any rolling hour**, except that a request inside an
  operation is never refused. **One command alone** goes over by at most its in-flight
  operation's **unexpected** requests (its expected ones are checked before the write-ahead):
  3 rate-limit resends per request plus, for `issue.fields`, one retry on stale field ids with
  its own 3 resends, so **at most 7 extra (1.4% over) for `issue.fields`, 3 × (1 + removals) for
  `labels.change`, 3 (0.6%) for every other kind**. **k commands racing** on the shared ledger
  can each also start their own in-flight operation's requests past the same last free slot, so
  **at most the sum of their in-flight operations' requests plus those extras** (two other
  one-request operations: 2 extra, 0.4%). Every request over the hour is recorded and stops its
  command with `hourly_budget`.

**The keys** (`intake.external.outbox`, per hive; each command applies its own hive's values to the
shared host count):

| Key | Default | Allowed |
|---|---:|---|
| `send_writes_per_minute` | `80` | `1` to `80` |
| `send_writes_per_hour` | `500` | `1` to `500` |

These are safety limits, not tuning knobs: a value above GitHub's documented figures is **refused
at validation** (never clamped), and there is **no unpaced mode** (no `0`). Lower values stay
configurable (an account shared with heavy web-interface use, a sandbox). The cap covers this
host's outbox writes only, not other hosts, bh's label-definition writes, `gh` or the web
interface on the same account; the stop on the first rate limit is the backstop for those.

**Rotation.** The defaults apply as soon as a host runs 0.35.8, with nothing written. Only a hive
that wants **lower** values sets the keys, and because config sections forbid unknown keys it does
so only after every host that loads the hive's config runs 0.35.8 (a fleet config write: it needs
the operator's binding). **Rollback:** lowering the keys is the only knob; downgrading a host below
0.35.8 restores unpaced sends there (remove the keys first if they were set). No wire change: the
operation catalog is unchanged, so the wire stays at 2.25.0; the intake outbox document keeps
schema 1.

### One GitHub client and one field-definitions read per command

- **One HTTP client per command** (bh-vy7dx). A command (`bh work outbox send-approved`,
  `bh work labels approve --batch`, the other intake verbs) builds one GitHub client and
  connection pool for the whole command instead of one per operation, which cost about 34 s over
  a 270-operation batch. The credential is unchanged: the token is still resolved once per
  operation and sent on each request, so with `credential.per_invocation_cache` off a token
  rotated or revoked mid-command is still picked up by the next operation. The host daemon's
  discovery timer keeps its adapters for its whole life and still builds a client per
  operation.
- **The org's issue-field definitions are read once per command** (bh-n0h15). An `issue.fields`
  write used to `GET /orgs/{org}/issue-fields` first every time (135 extra requests, about 47 s,
  in a 270-operation batch). The answer is now remembered for the command. It is checked, not
  trusted: a field or option missing from it, or a `404`/`422` from the write, drops it, reads
  the definitions once more and retries once (on the same single-use grant, since a `4xx` wrote
  nothing); a second failure is the normal error. The daemon remembers nothing across
  operations.

### Every batch measures itself (`timings` in `--json`, bh-17g6b)

`bh work labels sync`, `bh work labels approve --batch`, `bh work outbox send-approved` and
`bh work outbox send` add a measured `timings` object to their `--json` output: process start to
first work, the config load, the intake lock and host check, the store's reads and writes and
GitHub's requests (each as a count and a total in milliseconds), in total and per hold of the
intake lock. It is additive output only: no config key, no state change, and the outbox record and
receipt shapes are unchanged, so the wire stays at 2.25.0. The shape is documented in
[CONFIGURATION.md](CONFIGURATION.md#where-a-batch-spends-its-time-the-timings-block-bh-17g6b). The
`-m perf` model now reads a recorded profile (`tests/fixtures/intake_batch_profile.json`) instead of
constants in the test.

### The outbox, thread and labels verbs start faster (bh-lwz8a)

`bh work outbox <verb>`, `bh work thread <verb>` and `bh work labels <verb>` no longer import the
whole command tree: the entry point composes just those groups from the operation catalog, with the
same root callback (config and identity memos, migrations, warnings, telemetry, setup gate) and the
same handlers, so flags, output, exit codes and `--help` are unchanged. On the factory this cut the
per-process floor of one of these commands (wall clock less the verb) from about 4.9 s to about
3.3 s; `timings.startup` in `--json` shows it per run. Logging is configured as before (`log.level`
/ `log.format` for every record). Shell completion, `bh --help`, `bh work --help`, a group's own
`--help` (`bh work outbox --help`) and every other verb still go through the full tree. Commands run
outside a managed worktree also skip importing the worktree module for the telemetry overlay.
Nothing to do on upgrade. The single-send target of 3 s is not met yet: the process floor alone is
still about 3.3 s, before the host check and GitHub's requests (a single send models at about 6.8 s,
was 7.9 s); bh-wmmfo follows it up.

## 0.35.6 → 0.35.7 — typer 0.29 supported, the typer cap lifted

`bh hq authority --duration` and `bh hq placement --tenure` no longer use the typer internals that
typer 0.29 removed; they parse `7d` / `36h` / plain seconds through typer's public
`Option(parser=...)`, so the value on the wire is still whole seconds, `--help` still shows
`INTEGER`, and a bad value is still a usage error. 0.35.7 therefore allows `typer>=0.12,<0.30`,
and a fresh install resolves typer 0.29. Nothing else changes: no new keys and no state change.
From typer 0.28 on, typer itself prints other option types as `<int>`, `<str>` and `<bool>` in
`--help`; the two duration flags keep the explicit `INTEGER`.

If you repaired a 0.35.5 install with `--with 'typer==0.26.8'`, drop the pin when you upgrade:

```sh
uv tool install --force 'beadhive[otel,github]==0.35.7'
```

The repository's own `uv.lock` stays on typer 0.26.8, because its dev-only SDK generator
(`openapi-python-client`) requires `typer<0.28`. The release extras smoke installs the wheel with
fresh resolution, so it is the check that runs the newest allowed typer before each publish.

## 0.35.5 → 0.35.6 — a typer cap (fresh installs of 0.35.5 and earlier are broken)

typer 0.29.0 (released 2026-11-09) removed a private module and an option parameter that `bh hq
authority` and `bh hq placement` build on, and every bh since 0.22.6 allowed any typer from 0.12
up. A fresh install of any of those versions therefore resolves typer 0.29 and fails on every
command except `bh --version` with `ImportError: cannot import name 'types' from 'typer._click'`.
An install made before 2026-11-09 keeps its older typer and works until it is reinstalled or
upgraded.

0.35.6 requires `typer>=0.12,<0.29` and changes nothing else: no new keys and no state change.
Upgrade, or repair an existing 0.35.5 install in place by pinning the last typer it was tested
with, keeping the extras you installed:

```sh
uv tool install --force 'beadhive[otel,github]==0.35.5' --with 'typer==0.26.8'
```

The release smoke now runs `bh --help`, `bh hq authority --help` and `bh hq placement --help` in
each install it checks, not only `bh --version` (which answers before the CLI is imported), so a
dependency release that breaks the CLI fails the release job before publication. 0.35.7 supports
typer 0.29 itself and lifts the cap (bh-q2a2k; see above).

## 0.35.4 → 0.35.5 — `outbox reopen`, faster batches, no `delivery` on a `duplicate-of`

### Reopen a GitHub issue on the operator's say (`outbox reopen`, bh-ih484)

0.35.3 closes the GitHub issue of a fixed, rejected or duplicate report, and a close that is wrong
(or one somebody else made) had no way back through bh. This release adds the `issue.reopen`
tracker operation behind `plugins.tracker-github.write.issue_reopen.enabled` (default `false`), an
`issue.reopen` operation kind in the outbox (its own digest, single-use grant and receipt) and one
new verb, `bh work outbox reopen <report> [--text T | --reply-file F] [--dry-run] [--json]`. The
PATCH carries only `state: open` and never the labels.

**A reopen is only ever the operator's explicit act.** Nothing drafts one on its own: `triage
apply` and `wake` do not. The verb drafts, for ONE report whose bound issue reads closed (closed by
anyone: the operator chose to), a reply carrying the reopen. Its text is `--text`, the contents of
`--reply-file` (either one must hold some text: blank is refused as `invalid_request`), or a short
template when neither is given. It refuses (a stable `reason` in `--json`) with the key off
(`reopen_disabled`), for an issue that is not closed or cannot be read (`reopen_not_needed`), and
while a reopen for the issue is already pending, unsent and unretired (`reopen_pending`, naming the
record: send it or `outbox discard` it first, so there is never a second one). `--dry-run` makes
every check and prints the planned reopen (`state: open`) and the reply text, writing nothing.
Nothing is approved or sent by the verb: review with `bh work outbox show <id>`, approve with `bh
work outbox approve <id> --op reopen:<digest>` (and `--op comment:<digest>` for the reply), and send
with `bh work outbox send-approved`.

**A reopen goes before its reply.** The reply's comment is refused (`reopen_not_ready`, before any
request) until the reopen is sent or retired, and the reopen is refused while another record of the
same issue still holds an unsent close. Per report the order, and the `send-approved` batch order,
is reopen, comment, labels, close. **Only `send-approved` guarantees that order across separate
records**: a question drafted on its own (`needs-info`) for a still-closed issue is a separate
record and is not held back by a pending reopen, so send the reopen first or use the batch.
Discarding the reopen retires its reply with it, and a report rejected or rerouted afterwards
retires its pending reopen and reply too.

The new verb is an additive wire change: the wire release moves from 2.24.0 to 2.25.0 (one new
operation, `work.outbox.reopen`); the intake outbox document keeps schema 1.

### Faster outbox, labels and intake commands (bh-25fvb)

Nothing to migrate: no record, receipt or document shape changes, and no new verb. What changes is
how often the work is done:

- **The intake lock's host check is about 1.9 s, not 13 s** (factory). The lock asked
  `guard_primary` and then `live_epoch` on every hold, and each re-ran the whole frame-eligibility
  evaluation; it now takes both from ONE evaluation (`guard.primary_epoch`), the lease
  `require_intake` just read is not read again, and the pure parts of an eligibility read are
  memoized: SSH key fingerprints (410 `ssh-keygen` processes per check before), the committed config
  carrier's validation, the HOST file inside one command, and the authority-expiry warning's read
  (skipped in trusted mode, at most once per 300 s otherwise). Refusals are unchanged.
- **Every one-shot `bh` command starts about 1 to 1.2 s sooner than on 0.35.3.** The operation
  catalog is no longer parsed quadratically at import (0.8 s), the config partition's leaf paths are
  found in linear time, and the stale-schema nudge reads `schema_version` from the config the
  command already loaded instead of fetching the SQL fleet snapshot a second time. On the factory
  `bh work outbox --help` went from about 5.1 s to 3.9 s and `bh work outbox show <id>` from about
  6.5 s to 5.3 s.
- **The batches pay per report, not per operation.** Under each hold a report is read once (the
  store session); `bh work outbox send-approved` sends a report's operations under one hold, and
  `bh work labels approve --batch` writes a report's approvals as one document write. Every
  operation still has its own digest, single-use grant, write-ahead and receipt.
  `bh work labels sync` now drafts inside the same batch scope, so
  `intake.external.outbox.batch_recheck_seconds` covers it too.

Modeled on measured latencies (`tests/test_intake_outbox_batch_perf.py`, `-m perf`) for the type
and Priority of 135 issues, with `batch_recheck_seconds: 300`: labels sync about 57 s (745 s
before), labels approve --batch about 46 s (1440 s), send-approved about 256 s (1770 s; 238 s with
the token cache below); a single send about 8 s (about 21 s).

**New opt-in key, default off:** `plugins.tracker-github.credential.per_invocation_cache` keeps the
first GitHub token resolved in memory for the rest of one command instead of running
`gh auth token` for every operation. It is the one exception to the never-cached credential: never
written or logged, and a token rotated mid-command is picked up by the next command. The host
daemon's discovery timer never caches, whatever the key says: it keeps its adapters for the
daemon's whole life, so it derives the token per operation as before. Set it only after every
host runs this release (see the rotation order below).

**Operator step after rollout:** once every host runs this release, set
`intake.external.outbox.batch_recheck_seconds: 300` at the factory's host scope (the default `0`
still checks the host on every hold, about 1.9 s each).

### A `duplicate-of` no longer carries `delivery` (bh-5kpwc)

`bh work triage apply --reviewed` linked a `duplicate-of` report to its canonical report's work
through the ordinary link, which wrote `delivery:pending` on the duplicate. The label projection
then published `bh:delivery/pending` on the closed duplicate's issue, and a later merge, release,
claim or notice could draft a milestone note for it, repeating the one the canonical report gets.
Now a `duplicate-of` link writes no `delivery` value (and clears one already there) and catches
nothing up; no milestone trigger drafts for a rejected report; and the label projection emits no
`bh:delivery/*` or `bh:progress/*` for one, so a `labels sync` drafts the removal of those labels.
A `tracks` link (`accept --work`, `--new-work`, `bh work link`) still writes `delivery:pending`. This
change adds no config key and no wire change.

**Repair reports already in this state.** List them first (the default writes nothing), then clear
the value, then draft the label removal on GitHub and review it as any label batch:

```sh
uv run python scripts/repair_duplicate_delivery.py --repo <hive checkout>
uv run python scripts/repair_duplicate_delivery.py --repo <hive checkout> --apply
bh work labels sync --include-closed
```

`bd set-state` cannot clear a dimension, so `--apply` removes the `delivery:<value>` label cache
entry with `bd label remove`; the bead's event history keeps the old value. Nothing is sent to
GitHub and no outbox record is written until an operator approves the label batch.

### Rotation order for both new keys (the 0.35.0 pattern)

This release adds two config keys, both default off:
`plugins.tracker-github.write.issue_reopen.enabled` and
`plugins.tracker-github.credential.per_invocation_cache`. An older host rejects either one
(`extra=forbid`), and fails closed on an `intake.outbox` document holding an `issue.reopen`
operation. So:

1. Install the release on **every** host that reads the hive's outbox (any host that runs
   `bh work outbox`, `bh work triage apply`, `bh work wake`, `bh doctor` or a merge / release hook
   on that hive).
2. Only then set either key in a shared config.

With `write.issue_reopen.enabled` unset nothing is read, drafted, approved or sent for a reopen;
with `credential.per_invocation_cache` unset every GitHub operation derives its token as before.

## 0.35.2 → 0.35.3 — close the GitHub issue after the reply and labels (switch default off)

`bh work triage apply --reviewed` closed fixed, rejected and duplicate reports in beads but left
their GitHub issues open. 0.35.3 adds the `issue.close` tracker operation behind
`plugins.tracker-github.write.issue_close.enabled` (default `false`) and an `issue.close`
operation kind in the outbox. With the switch on, apply drafts the close on the reply record
(`fixed` → `completed`, `reject` → `not_planned`, `duplicate-of` → `duplicate`; `needs-info` never
closes). Each close has its own digest, single-use grant and receipt: approve it with
`bh work outbox approve <id> --op close:<digest>` and send it with
`bh work outbox send <id> --kind close`. A close is refused (`close_not_ready`, before any
request) until the record's comment is sent, no other comment of the report is unsent (send or
discard a newer reply first), and the report's latest labels record is sent (an abandoned or failed
one needs a `bh work labels sync` first), so send per report in the order comment, labels, close.
The close PATCH never carries labels.

**`duplicate-of` changes whatever the key says.** While `intake.external.outbox.enabled` is on,
apply now drafts a reply for a `duplicate-of` report too (the reviewed `reporter_text`, or "This is
a duplicate of #N; the work is tracked there."), and every reply is drafted BEFORE the report is
linked or rejected, so a reply that cannot be drafted leaves the report untouched; the steps are
`draft reply, link, reject`. A rerun drafts the same reply once. With the outbox off (the default)
a `duplicate-of` is linked and rejected with no reply and no close, exactly as on 0.35.2.

`bh work outbox backfill-close [--dry-run]` drafts the close of every report already closed in
beads whose bound issue is still open and whose applied triage outcome was fixed, reject or
duplicate-of, on its latest reply. A report that never got a reply (a 0.35.2 `duplicate-of`, for
example) gets one drafted from the proposal's reviewed `reporter_text`, or the template apply uses
(`fixed` with the fix version computed now), with the close attached. It only drafts and never
drafts twice; approve both digests and send as above.

**A duplicate closes as a duplicate OF its canonical, and every `#N` a reply names is checked**
(bh-iamru). A `duplicate-of` close now names the canonical report's issue: the close's digest shows
`duplicate_of: <url>`, and the PATCH carries GitHub's `duplicate_issue_id` (one extra GET of the
canonical before the write; same switch, same grant). A canonical with no issue in the same
repository closes as a plain duplicate. `bh work triage check` and `apply --reviewed` now refuse a
`reporter_text` whose `#N` is not an issue of the bound repository (`mention_missing`), or whose
`#N` cannot be verified because no tracker with `issues.read` is bound (`mention_unverified`):
rewrite the text, or bind the tracker. One issue listing per run, only when a `#N` appears. A `#N`
that is another bound report is recorded in beads as a `relates-to` between the two reports.
GitHub issue dependencies and sub-issues stay unused: no bead edge means the same thing.

**Wire 2.23.0** (`docs/schemas/wire/v2.23.0/`): additive, the one new verb
`bh work outbox backfill-close`.

**Rotation order (the 0.35.0 pattern).** The new key and the new outbox operation kind both need
every host first: an older host rejects the key (`extra=forbid`) and fails closed on an
`intake.outbox` document holding an `issue.close` operation. So:

1. Install 0.35.3 on **every** host that reads the hive's outbox (any host that runs
   `bh work outbox`, `bh work triage apply`, `bh doctor` or a merge / release hook on that hive).
2. Only then set `plugins.tracker-github.write.issue_close.enabled: true` in a shared config.
3. Only then run `bh work triage apply --reviewed` or `bh work outbox backfill-close`.

With the key unset no close is drafted, approved or sent; the only change is the `duplicate-of`
reply above, and only while the outbox is on.

### The issue type and the Priority field, beside the labels (switches default off)

Type and priority reached GitHub only as `bh:type/*` and `bh:priority/*` labels. 0.35.3 adds the
`issue.type` and `issue.fields` tracker operations, each behind its own switch,
`plugins.tracker-github.write.issue_type.enabled` and `write.issue_fields.enabled` (default
`false`; each also gates reading the issue's type or field values), and the matching
`issue.type` / `issue.fields` operation kinds in the outbox. The type is set with
`PATCH /issues/{n}` carrying only `type`; a field value with
`POST /issues/{n}/issue-field-values` (field ids from one `GET /orgs/{org}/issue-fields`), which
keeps every other field. Both endpoints were checked against GitHub's REST reference.

With a switch on, `bh work labels sync` (and the `labels.on_transition` refresh) maps the bead's
type and priority, from the tracked work and falling back to the report, through
`intake.external.labels.issue_type.map` (default `bug`→`Bug`, `feature`→`Feature`,
`task`→`Task`, `chore`→`Task`) and `intake.external.labels.priority_field` (`Priority`; default
`P0`→`Urgent`, `P1`→`High`, `P2`→`Medium`, `P3`/`P4`→`Low`). An unmapped value, or one the issue
was not read for, is skipped with a note, never guessed. A difference is reported like label drift
and drafted on the issue's same `labels` record, so `bh work labels review` and
`bh work labels approve --batch <digest>` approve it with the labels, each operation with its own
digest, single-use grant and receipt. Send with `bh work outbox send <id> --kind type` and
`--kind fields`. Labels are unchanged: the `bh:type/*` and `bh:priority/*` labels are still
projected. Setting the type or a field needs the write role on the repository. Reading the
organization's fields needs `read:org` on a classic token; a fine-grained token or a GitHub App
needs the organization permission "Issue Fields" (read) for `GET /orgs/{org}/issue-fields` and the
repository permission "Issues" (write) for `POST .../issue-field-values` and the type's `PATCH`.

**Rotation order (the same 0.35.0 pattern).** An older host rejects the new keys (`extra=forbid`)
and fails closed on an `intake.outbox` document holding an `issue.type` or `issue.fields`
operation, so:

1. Install 0.35.3 on **every** host that reads the hive's outbox (any host that runs
   `bh work outbox`, `bh work labels`, `bh work triage apply`, `bh doctor` or a transition, merge
   or release hook on that hive).
2. Only then set `write.issue_type.enabled` / `write.issue_fields.enabled` (and any
   `intake.external.labels.issue_type` / `priority_field` override) in a shared config.
3. Only then run `bh work labels sync`.

With both keys unset nothing is drafted, read or sent for the type or fields, and a labels record
is byte-identical to one 0.35.2 wrote.

No new verb or option for these: `bh work outbox send|discard --kind` and `approve --op` accept
`type` and `fields`. The operation catalog moves to wire 2.24.0 only for `bh-pc9rf`'s new
`bh work outbox send-approved` verb (below).

### Faster outbox verbs and a one-process batch send

`bh work outbox show`, `approve` and `send <id>` now find a record through a local outbox-id index
(`<hive>/.bh/intake/outbox-index.json`, `bh-pc9rf`) and read ONE report instead of every report's
outbox. The index is only a cache: every answer is confirmed against the report's own document, a
miss scans once and rebuilds it, and deleting it is always safe. A one-shot `bh` verb also loads
the effective config once (one `outbox show` used to re-qualify the SQL fleet snapshot 7 times);
a config publish inside the same command drops that memo, and the long-lived `bh host` / `bh mcp`
never use it. Deriving a GitHub token now runs `gh auth token` alone and only on its failure
`gh auth status` (for the diagnostic), one subprocess per GitHub operation instead of two; the
token is still never cached. On the factory `bh work outbox show <id>` went from about 21 s to
about 8 s.

`bh work outbox send-approved [--kind comment|labels|type|fields|close] [--bead <id>]...` sends
every approved operation in one process and one store session, per report the comments, then the
labels, the type, the fields, then the close. Each operation still spends its own single-use grant
and writes its own receipt, and a refused or unknown one is reported while the rest go (the command
exits non-zero unless every operation was sent; a close whose comment did not go out is refused as
`close_not_ready`; losing the primary host stops the batch and reports what was sent).
`bh work labels approve --batch` runs on the same path. By default either batch still checks the
primary host and reads the epoch on every operation (about 13 s each on the factory). The new key
`intake.external.outbox.batch_recheck_seconds` (default `0`) opts a batch into reusing ONE check
for at most that many seconds; every operation still takes its own lock, grant and receipt. An
older host rejects the key (`extra=forbid`), so the factory sets it to `300` only after every host
runs 0.35.3. **Wire 2.24.0** (`docs/schemas/wire/v2.24.0/`): additive, the one new verb
`bh work outbox send-approved`.

## 0.35.0 → 0.35.2 — triage apply fixes and a hishel cap

0.35.1 was tagged but never published: hishel 2.0.0 (released the same day) fails githubkit's
import, and every githubkit allows it, so the release smoke of `beadhive[github]` failed. 0.35.2
carries 0.35.1's fixes plus `hishel<2` in the `[github]` extra. A fresh install of any earlier
`beadhive[github]` resolves hishel 2.0.0 and fails the same way; install 0.35.2, or add
`hishel<2` yourself.

No new keys and no state change. `bh work triage apply --reviewed` on 0.35.0 could leave a reject
half done (reply drafted, `intake:rejected` set, close refused because the reply was longer than
the 255-character close reason Beads stores) and refused needs-info questions whose comments the
analysis had read. On 0.35.2 rerun `bh work triage apply --reviewed`: it closes the half-rejected
reports without drafting their replies again and asks the questions. A proposal re-stored only to
move its `thread_seq` past a `thread considered` write needs no further change.

## 0.34.x → 0.35.0 — GitHub label projection and review-gated triage (switches default off)

0.35.0 adds two supervised features on top of intake and the outbox. The **label projection**
derives GitHub labels under one owned prefix (`bh:` by default) from the dimension registry and
keeps every bound issue's owned labels converged on bead state. **Review-gated batch triage**
stores an analyst's proposal on each report, records verifier verdicts, runs deterministic
pre-apply checks and applies only reviewed proposals. Every new capability is behind a switch that
defaults to **off**, nothing is sent without an operator's approval, and bh makes no LLM call, so
upgrading changes no behaviour until you turn something on.

**Wire 2.22.0.** The wire contract moves to 2.22.0 (`docs/schemas/wire/v2.22.0/`, `latest` in
`docs/schemas/wire/index.json`); the change is additive (two new `bh work` groups).

**New `bh work` verbs.** All refuse naming the key they need:

- `bh work labels sync [<report>|--all] [--include-closed] [--dry-run] [--prune-unmapped]`,
  `labels define [--dry-run] [--prune] [--approve <digest>]`, `labels review` and
  `labels approve --batch <digest>` (`intake.external.labels.enabled`; drafting and approving also
  `outbox.enabled` and `outbox.labels_enabled`). `define --approve` and `approve --batch` are
  operator-seat only, like `bh work outbox approve`.
- `bh work triage review --pending [--batch-size N] [--out DIR]`, `triage propose <report>
  <file|-> [--review-of <pass>]`, `triage check [--pending]` and `triage apply --reviewed
  [--dry-run] [--reroute-to <report>=<hive>]` (`intake.external.enabled`; `review` also
  `comments.enabled` and the tracker's `read.comments.enabled`). `apply` is operator-seat only and
  only drafts: replies and questions are sent with `bh work outbox`. `bh work intake` is unchanged.
- `--review-of` takes the review pass number (1, 2, ... in recording order); the batch skill's
  `B` and `C` are accepted for passes 1 and 2 of a proposal's first round.

**New config keys** (`docs/CONFIGURATION.md`): the `intake.external.labels` section (`enabled`,
`prefix`, `project`, `aliases`, `colors`, `overrides`, `on_transition`) and
`plugins.tracker-github.read.labels.enabled` / `write.label_definitions.enabled`. All switches are
`false`.

**Rotation order (bh-hmtjn pattern).** An older host rejects the new keys (`extra=forbid`) and
cannot decode an `intake.outbox` document holding the new `labels` record type (the issue label
changes `labels sync` drafts, with their own supersede group). So:

1. Install 0.35.0 on **every** host of the fleet, every host that reads a hive's outbox included
   (any host that runs `bh work outbox`, `bh doctor` or a merge / release hook on that hive).
2. Only then set the new keys in a shared config: `plugins.tracker-github.read.labels.enabled`,
   `write.label_definitions.enabled` (with `bh:` in `write.labels.owned_prefixes`) and
   `intake.external.labels.*`.
3. Only then run `bh work labels sync` (the first `labels` record), or turn on
   `labels.on_transition` (which drafts one on every transition).
4. Define the labels before the first sync: `bh work labels define`, review the plan, then
   `bh work labels define --approve <digest>` from an operator seat.

The triage proposal store (`intake.proposal`, a private document on the report bead) is new too
and only 0.35.0 reads it, so run the `bh work triage` verbs from 0.35.0 hosts.

## 0.33.x → 0.34.0 — the supervised GitHub triage loop (all new switches default off)

0.34.0 adds the supervised triage loop on top of intake and the outbox: it can read a bound
report's issue comments, ask the reporter for more information through a supervised reply, wait
for the answer, and tell the reporter how the tracked work is going. Every new capability is
behind a switch that defaults to **off**, and nothing is sent without an operator's approval, so
upgrading changes no behaviour until you turn something on.

**Wire 2.21.0.** The wire contract moves to 2.21.0 (`docs/schemas/wire/v2.21.0/`, `latest` in
`docs/schemas/wire/index.json`). Run the same release on every host before any new config
key is set (see below).

**New `bh work` verbs.** All are inert until their switch is on and refuse naming the key:

- `bh work thread show | ingest | considered`: read, ingest and mark-as-considered a bound
  report's comment thread (`comments.enabled`; `ingest` also needs
  `plugins.tracker-github.enabled` and `plugins.tracker-github.read.comments.enabled`). Reads only.
- `bh work needs-info <report> --ask <text>` and `bh work wake <report>` / `wake --all`
  (`needs_info.enabled`; `wake --all` also `needs_info.sweep`; the question also needs
  `outbox.enabled` and `comments.enabled`). The question is drafted as a supervised reply; it posts
  only after an operator approves it.
- `bh work investigate <report> (--work <id> | --new-work) [--remove]`: relate reproduction work
  to a report with an `investigates` edge that never fires delivery milestones and is separate from
  `tracks` (off unless `intake.external.enabled`).
- `bh work delivery <event>` notices: `started` (`milestones.draft_on_start`), `blocked` and
  `deprioritised` (`milestones.draft_on_blocked`, `milestones.draft_on_deprioritised`, each with a
  public `--reason`), plus `milestones.catch_up_on_link` for work that merged before it was linked.
  They only draft records in the outbox; they never move a dimension or complete a report.

**New config keys** (`docs/CONFIGURATION.md`): `comments.enabled`, `comments.deletion_confirmations`,
`comments.purge_deleted_bodies`, `comments.max_body_chars`, `needs_info.enabled`,
`needs_info.sweep`, `milestones.catch_up_on_link`, `milestones.draft_on_start`,
`milestones.draft_on_blocked`, `milestones.draft_on_deprioritised`. All switches are `false`.

**Do not use the new vocabulary before the fleet rollout (bh-hmtjn).** An older host rejects the
new keys (`extra=forbid`), the `intake:needs-info` label value (`bh label validate`) and an
`intake.outbox` document holding the new `started` / `blocked` / `deprioritised` records. So
until 0.34.0 is installed on every host of the fleet: set none of the keys above in a shared
config, do not run `bh work needs-info`, and do not apply `intake:needs-info` or the new
milestone events to any report.

The intake store keeps following `work.beads.route` (0.33.1): the new store operations behave the
same over the Beads service and over the `bd` CLI.

## 0.33.0 → 0.33.1 — `beadhive[github]` and `beadhive[xtdb]` install from PyPI

0.33.0 published `beadhive[github]` and `beadhive[xtdb]` extras that named unpublished
workspace packages (`beadhive-tracker-github`, `beadhive-beadlog-xtdb`), so
`uv tool install 'beadhive[otel,github]==0.33.0'` failed to resolve ("beadhive-tracker-github was
not found in the package registry"). 0.33.1 ships those plugin import packages
(`beadhive_tracker_github`, `beadhive_beadlog_xtdb`, `beadlog_xtdb`, each with its `plugin.json`)
inside the root wheel, like `beadhive-core`, and the extras now carry only the third-party
dependency: `beadhive[github]` is `githubkit`, `beadhive[xtdb]` is `psycopg[binary]` (LGPL-3.0).

What you need to do:

- **Installed the adapter with `--with ./packages/beadhive-tracker-github`** (the 0.33.0
  workaround)? Reinstall plainly; the `--with` is no longer needed and would shadow the wheel's
  copy:

  ```sh
  uv tool install --force 'beadhive[otel,github]==0.33.1'
  ```

- **Want the XTDB store?** `uv tool install --force 'beadhive[xtdb]==0.33.1'` (add `otel,github`
  as needed).
- **Bare install?** Nothing changes. `import beadhive` still never imports githubkit or psycopg,
  and no plugin is enabled by installing either extra.

A plugin that is **enabled without its extra** now refuses cleanly, naming the extra (for example
``install `beadhive[github]` ``), instead of failing later on an `ImportError`;
`bh plugin beadlog-xtdb doctor` reports the missing `psycopg` dependency. No config key, CLI verb or
wire format changed.

**Intake follows `work.beads.route`.** The intake verbs (`import`, `discover`, `intake`, `accept`,
`reject`, `link` and the rest) now follow `work.beads.route` like the work verbs, so under the
default `api` they need the hive's Beads service (`bh host beads start --hive <hive>`) or
`work.beads.route: api+cli-fallback`. `bh report` / `bh escalate` and `bh doctor`'s intake-hold
sweep stay on the `bd` CLI.

## 0.15.x → 0.16.0 — private state moves under the canonical roots

0.16.0 stops writing `.git/bh-validation-ledger.json` and `.bh/testreport/<tree>/` immediately.
Execution manifests and reusable green pointers now live below
`<git-common-dir>/bh/validation/`; raw artifacts and bounded per-tree summaries live below
`<hive>/.bh/validation/`. The legacy paths are read-only compatibility inputs in 0.16.x and
0.17.x. `bh doctor` names hives that still retain them. The fallback and warning may be removed
in **0.18.0**, but not before.

Per-worktree observability provisioning also moves from `.bh/otel.env` to
`.bh/observability/otel.env`. The loader checks the canonical file first and consults the legacy
file only when canonical state is absent; provisioning and self-healing never write the legacy
path. No manual copy is required. Re-provision the worktree (or let self-healing run with a live
collector), verify the canonical overlay works, then archive/remove `.bh/otel.env` during the
same bounded cleanup window. Both paths are covered by the local `.bh/` exclude, so a successful
upgrade leaves `git status --short` empty.

Migration is automatic, atomic, and best-effort. Readers ask the canonical path first and consult
only the exact bounded legacy input on a miss. A canonical write also promotes relevant old state.
The source stays untouched for rollback; deterministic imported run IDs and atomic summary/index
writes make retries idempotent. No Git ref, tracked file, validation subject, or published history
is rewritten.

| Legacy input | Canonical result | Authority after import |
|---|---|---|
| fresh exact `(tree, command hash)`, integer `rc=0` ledger row | completed run + green verdict pointer | reusable green under the old exact-match and TTL rule |
| stale zero or malformed row | retained as non-attesting history, or skipped if structurally unreadable | never green |
| ordinary nonzero ledger/triage row | completed red legacy run | exit-code evidence with explicit legacy provenance/confidence |
| `rc=143` | completed `none`, signal 15, reason `interrupted` | interruption, never code red or green |
| counts-only row or missing reports | manifest/summary with available counts | detail only; missing detail is not invented |
| multiple observed shas for one tree | capped `shas` metadata on the imported ledger run/pointer | metadata only; tree remains identity |
| per-tree fail→pass retries | canonical `.summary/<tree>/results.json` history | preserves identical-tree flake detection; imported green triage detail cannot attest |
| malformed file or unavailable canonical root | bounded miss / read-only fallback | fresh validation remains the safe path |

### Removal procedure (0.18.0 or later only)

1. Upgrade through a 0.16.x or 0.17.x release and exercise each retained tree (or let normal
   validation reads/writes promote it).
2. Run `bh doctor`; inspect any legacy-validation warning and confirm corresponding canonical
   manifests/summaries exist. A malformed legacy file may intentionally have no promoted record.
3. Archive the legacy paths outside the checkout if rollback evidence is required, then remove
   `.git/bh-validation-ledger.json`, `.bh/testreport/`, and any legacy worktree
   `.bh/otel.env`. Do not copy them into the canonical roots by hand.
4. Run one normal validation. It should use/write only canonical paths and leave `git status`
   unchanged. Keep the archive until the next successful release/merge boundary.

The source-code removal gate is tested: canonical writers may not recreate either retired path,
and fallback constants/warnings/documentation remain pinned through the two-minor window.

## The hub / HQ split — the aggregate leaves HQ (bh-89wxf)

**Who this is for:** anyone who has run `bh hq init`. HQ's Dolt database has been carrying two
different things — HQ's own authoritative `hq-`prefixed beads, and a per-host DERIVED
cross-hive aggregate that every host rebuilt wholesale and pushed. bd's rule is one database
per remote path, and since every mutation touches `updated_at`, a per-host wholesale rebuild
inside a replicated database is the maximal-conflict shape there is. The aggregate has moved
to the hub, where it belongs. Full reasoning: [HQ — Hub vs HQ](HQ.md#hub-vs-hq).

### What you must run

```sh
bh sync                            # builds the hub (rebuilds a legacy one automatically)
bh hq prune-aggregate              # dry run: how many hive-derived beads HQ is carrying
bh hq prune-aggregate --confirm    # delete them; HQ keeps its own hq- beads and fleet config
bh hq push                         # publish the now-single-purpose database
```

No Dolt surgery, and nothing to hand-edit. `bd delete` is an ordinary bd mutation, so the
cleanup replicates through `refs/dolt/data` — a second host picks it up on its next pull
instead of each host repairing its own copy. Nothing is lost: every pruned bead is a derived
copy of a bead that still lives in its own hive.

A hub minted before this change (bd prefix `hub`) is moved aside to
`~/.beadhive/hub.legacy-<epoch>` and rebuilt prefix-less on the next `bh sync`. It is renamed,
not deleted — delete it yourself when you want the disk back.

### What changed on the CLI

| Before | Now | Why |
|---|---|---|
| `bh hq bd …` — the cross-hive aggregate | `bh hub bd …` | two stores, two surfaces |
| `bh hq bd …` | HQ's OWN `hq-`prefixed beads | HQ is authoritative-only now |
| `bh hq intake` — fleet-wide inbox | `bh hub intake` | it was a cross-hive read wearing HQ's name |
| `bh hq intake` | HQ's own escalations | the surface names the scope |
| `bh hub …` (deprecated alias) | un-deprecated, means the hub | it is no longer an alias |
| `bh hq push --no-sync` / `--git-only` | **removed** | they only existed to dodge the aggregate refresh, which is gone |

`bh hq intake` prints a pointer to `bh hub intake`, so the rename doesn't strand a habit
mid-command.

## Ad-hoc PyPI → the managed path (any version)

Not tied to a release. This is for anyone who installed `bh` with `uv tool install` (or pipx,
pip, brew) and wants the managed toolchain instead — the route
[INSTALL.md](../INSTALL.md) now recommends. `bh doctor` tells you whether this applies to
you: it reports the install plane, and `pypi` means this section is for you.

**What you gain:** `bd`, `dolt`, `gh` and `git-workspace` become installed and version-pinned
together via `flake.lock`, instead of being whatever your machine happened to carry. **What it
costs:** nix, which needs root — see INSTALL.md's requirements before starting.

**Docker is deliberately out of scope here.** The managed path means nix. The container image
is a different deployment shape with its own guide ([CONTAINER.md](CONTAINER.md)), not a step
on this route.

### The order matters, and it is a safety property

Do these in order. The reasoning is worth understanding, because someone who understands it
won't reorder it:

1. **Sync your beads first**, so nothing exists only on this machine. If a later step goes
   wrong, your work is already durable.
2. **Archive the old workspace second**, while the old install still works. Archiving before
   reinstalling means the old tree is intact if the new install misbehaves.
3. **Install the managed path third.** By now nothing is at risk.

### 1. Sync beads

```sh
bh sync            # every hive
bh hq push         # if you have an HQ remote; skips harmlessly if you don't
```

**Verify it landed** — not just that the command exited 0:

```sh
bh hive list       # every hive present and not reporting unsynced work
```

### 2. Archive the old workspace

**There is no single `bh` verb for this yet, and this guide will not pretend otherwise.**
The migration that turns an external workspace into the internal one is
tracked as `bh-cgcg.3` and is **not yet released**. Until it lands this step is manual:

```sh
# with everything synced above, move the old tree aside rather than deleting it
mv ~/workspace ~/workspace.pre-managed
```

Two verbs that look like they fit and do **not**:

- `bh host retire --backup` decommissions **a host** — it releases leases, deregisters this
  machine's manifest and pushes HQ. That is the right shape but the wrong scope: you are
  migrating a workspace on a machine you are keeping, not retiring the machine.
- The `git-workspace:import` skill covers the **opposite** direction — bringing an existing
  tree under management.

**Verify it landed:** the archived tree is still readable at its new path, and the beads you
synced in step 1 are still listed by `bh hive list`.

### 3. Reinstall on the managed path

Follow [INSTALL.md](../INSTALL.md)'s managed path. `bh` itself is reinstalled by the same
`uv tool install` you already use, so this replaces your existing `bh` in place.

**Verify it landed:**

```sh
bh setup check     # expect 4 of 4 — this is the whole point of the migration
bh doctor          # install plane should no longer report pypi
```

If `bh setup check` still reports missing tools, your shell is finding the old `bh` or the nix
profile is not on `PATH` — `~/.nix-profile/bin` must precede the old locations.

### 4. Point the workspace at the internal root

Once `bh-cgcg.3` lands this becomes a command. Until then, re-register your hives against the
internal workspace root as you would on a fresh machine — see
[ONBOARDING.md](ONBOARDING.md)'s git-workspace and hive-onboarding phases.

## 0.11.x → 0.12.0 — the validation ledger re-keys; every entry goes cold once

0.12.0 ships **Attested Green** — a re-keyed validation ledger plus the landing-boundary reuse
and `work.always_run` / `work.validate_subset` tuning it enables. Full design:
[design/attested-green-adr.md](design/attested-green-adr.md); the two new operator-facing keys
are documented in [WORK.md](WORK.md#configuration) and shipped in `config.example.yaml`.

**What changes on disk, and why you'll notice:** the ledger's key shape moves from
`(commit sha, cmd hash)` to `(tree hash, cmd hash)` (attested-green-adr.md, "Consequences").
An old-shape entry can never match a new-shape lookup, so **every entry your ledger already
holds goes cold the moment you upgrade** — there is no migration, because there is nothing to
migrate a sha-keyed row *into*. The first validation for each `(tree, command)` pair you hit
after upgrading pays the full run again; every one after that is a normal reused hit, same as
before.

**This is expected and self-healing, not a regression** — it happens exactly once per
`(tree, command)` you still care about, and it's the same "harmless and self-healing"
degradation the ledger already handles for a TTL expiry or a size-cap eviction, just all at
once instead of trickling in. If the release that advertises skipping redundant validation
seems to be re-running everything right after you install it, this is why.

Nothing to do here — no flag, no migration command. If you want to confirm it for yourself,
inspecting `<hive>/.git/bh-validation-ledger.json` will show old sha-keyed rows aging out
under the existing TTL/cap eviction rather than being read back.

## 0.7.x → 0.8.0 — the store engine moves to bd's shared dolt server

0.8.0 changes **where a hive's beads physically live**. Until now every hive ran bd's *embedded*
Dolt engine: an in-process engine under the repo's own `.beads/embeddeddolt/`, holding an
exclusive file lock for as long as a command ran. 0.8.0 makes **bd's own shared
`dolt sql-server`** — one server per host, spawned and supervised by `bd`, on `127.0.0.1:3308` —
the default for newly onboarded hives, and ships a verb to move the hives you already have. The
decision and the measurements behind it are in
[dolt-server-mode-adr.md](design/dolt-server-mode-adr.md); the two-things-called-"server"
distinction is [DOLT.md](DOLT.md).

> **Nothing moves on its own.** Installing 0.8.0 migrates no existing hive. Everything you have
> keeps working exactly as it did, embedded, until you run `bh hive migrate-storage` yourself
> (§4). If you stop reading here, you have lost nothing and broken nothing.

### 1. BEHAVIOR CHANGE: newly onboarded hives are born on the shared server

`bh hive init`, `bh hq init`, and the legacy hub now init straight onto the shared server —
across all three `bd init` paths (furnished, bootstrap-from-`refs/dolt/data`, and
zero-footprint). Two facts are persisted rather than left to an environment variable:
`dolt_mode: "server"` in the hive's `.beads/metadata.json`, and `dolt.shared-server: true` in
its `.beads/config.yaml`.

Zero-footprint stays zero-footprint — server mode adds no tracked artifact, no extra commit.
Nothing about publishing changes either: issue history still travels as `refs/dolt/data` on
each repo's own git remote. Storage mode is a *local engine* choice, not a change of where your
data lives for anyone else.

### 2. Existing hives stay embedded until you say otherwise

The idempotent "`.beads/` already exists" skip in onboarding returns before any server-mode
wiring runs, so re-running `bh hive init` on an embedded hive leaves it embedded. This is
deliberate: moving a store is `bh hive migrate-storage`'s job and never a side effect of
re-running onboarding.

You can therefore run a mixed fleet indefinitely. `bh doctor`'s store-engine section reports
each hive's mode, and — for hives on server mode only — probes the endpoint, because server
mode is the first storage mode that can be *down*
([dolt-store-engine-liveness-adr.md](design/dolt-store-engine-liveness-adr.md)).

### 3. Prerequisites

- **Beads v1.3.0-rc.1 or newer**, which supports shared-server mode and embeds the Dolt
  v2.2.0-era transport fix. The managed Nix path pins that RC exactly. The same build
  requirement as [§11 below](#11-requirement-your-bd-build-must-embed-dolt--220) applies here
  for the same reason, and more sharply: migration moves whole stores.
- **Free disk during the move, not after it.** Migration takes a verified Dolt-native backup
  *before* it touches anything, restores that into the server-side database, and moves the old
  store aside rather than deleting it — so mid-run a hive briefly occupies roughly **3×** its
  store size (old store + backup + new server-side copy). HQ at 1.6 GB is the one to plan for.
- **`bh setup check` green**, so you are not diagnosing an unrelated dependency mid-migration.

### 4. The migration, end to end

`bh hive migrate-storage` is the verb. It is *not* `bh hive migrate` — that one is the
`ws`→`bh` rename (markers, hooks, skills), a completely different axis that happens to share a
word.

```sh
bh hive migrate-storage --dry-run              # whole fleet: sizes, target paths, nothing changes
bh hive migrate-storage beadhive --dry-run     # one hive
bh hive migrate-storage beadhive --confirm     # apply, one hive — start here
bh hive migrate-storage --confirm              # apply, whole fleet (Factory HQ last)
```

A real run refuses without `--confirm`. Per hive it does: **verified backup → migrate →
verify → report**, and it stops at the first step that cannot be verified:

- **Backup, two levels, both verified before anything destructive.** A portable JSONL export
  (line count cross-checked against `bd status`'s own issue count) plus a Dolt-native backup
  taken *over the connection* — the only format that restores into a *different* engine mode.
  A tarball of `embeddeddolt/` is not that format; nothing in a running SQL server reads a
  directory by that name. If either level cannot be verified, the hive is not migrated.
- **Verify means readable AND complete**, not "the command exited 0": the store re-opens, the
  issue count matches pre-migration exactly, a schema version is recorded, and the persisted
  `dolt_mode` really says `server`. An engine/metadata disagreement is a **failed** migration,
  not a warning — that silent drift is the specific failure this verb exists to prevent.
- **One migrator per hive**, enforced by a lock file (stale locks are reclaimed, not waited on
  forever). Two concurrent migrators against one remote-backed database fork the schema
  silently and unrecoverably.
- **Fleet-wide runs are resumable and per-hive isolated.** Outcomes are recorded in
  `~/.beadhive/storage-migrate-state.json` as each hive finishes, so a killed run picks up
  where it stopped rather than at hive 1, and one failure never strands the other 21. Factory
  HQ goes last, on purpose: it is the largest store and the hardest case.

Afterwards, each hive's database lives under `~/.beads/shared-server/dolt/<database>` (honoring
bd's own `BEADS_SHARED_SERVER_DIR` if you set it). Confirm with `bh doctor` or
`bh hive ready`.

### 5. Rollback, precisely

Two different one-way-ness questions get confused here, so state them separately:

- **Storage-mode migration is REVERSIBLE.** embedded → shared and back is `bd backup` /
  `bd backup restore`, full Dolt commit history preserved in both directions. The verb prints
  this before it does any real work.
- **bd-binary schema migration is NOT.** Beads v1.3.0-rc.1 upgrades this hive's schema one way
  from v62 through v66. Designate one migrator for each remote-backed store: it must run the RC
  first, publish the resulting Dolt state, and every other host must update before opening that
  store. Do not run any older Beads binary after the v62→v66 migration: releases through v1.2.2
  understand only schema v53 and cannot safely reopen it. This is orthogonal to storage mode —
  it closes when the designated migrator upgrades `bd`, not when it moves the store.

### 6. BEHAVIOR CHANGE: `backup.enabled` is set true on every migrated hive

`bd`'s auto-backup defaults **on** in embedded mode when a git remote exists and **off** in
server mode (upstream's reasoning: many clients on one shared server each registering a
same-named backup remote and full-syncing would be a self-amplifying storm). Migrating without
correcting that would quietly leave a hive less durable than it was found, so the verb sets
`backup.enabled: true` explicitly as part of migrating — including on hives it finds already
migrated, which is how a partially-applied earlier run gets healed.

**Policy update (`bh-eu0oi`, 2026-09-27):** that behavior was reversed after per-worktree backup
stores caused large duplicate copies and blocked safe worktree pruning. Current onboarding and
storage migration do not enable scheduled backups. An existing explicit `backup.enabled: true`
is preserved; a new shared-server hive remains at bd's OFF default. The migration's one-time
verified safety backup is unchanged.

### 7. What it buys, and what the numbers actually say

All measured on one machine, 2026-08-03. Quote them with their caveats or not at all:

| | embedded | shared |
|---|---|---|
| 5 readers + 1 writer, median of 3 (`bh-u562.1`) | 3.456 s | **0.720 s** |
| engine-open + query, A/B on identical cloned data ([`bh-00cq`](spikes/bh-00cq-external-dolt-sql-server.md)) | 187 ms | **55 ms** (−71%) |
| cold 306 MB clone from the production remote (`bh-00cq`) | hung past 240 s | 10 s |
| fleet disk, 22 hives × `.beads/embeddeddolt` | ~2.8 GB | reclaimable (§8) |

**The caveats, from the spike that produced the numbers:**

- The 187 ms → 55 ms A/B used a **300 MB store — the least favourable case for server mode**,
  not the best. It is engine-open + query with `bd`'s own 63 ms process spawn subtracted; raw
  wall-clock improvements were 48–69% by verb. Embedded latency tracks store size while a
  server holds the store open, so larger stores should gain more — but *that is an inference
  from two runs, not a measurement*.
- **Concurrency is unmeasured** in that A/B. The 5-readers-1-writer row above is a separate
  measurement, and the fleet-wide resource argument (19 embedded engines vs one server under
  simultaneous load) remains untested at fleet scale.
- The ~2.8 GB is this operator's fleet — 22 hives, worst offenders 864 MB (beadhive), 825 MB
  (workspace), 343 MB (homelab), and HQ alone at 1.6 GB. Yours will differ. `--dry-run` reports
  your real per-hive sizes before you commit to anything.
- Bootstrap cost is a wash (11 s embedded vs 9 s server for the same payload). Server mode
  costs nothing on first sync.

### 8. What's safe to delete — and why the disk isn't free yet

**Migrating does not reclaim a byte.** The old store is *moved aside*, never deleted:
`.beads/embeddeddolt/` becomes `.beads/embeddeddolt.pre-migrate-<UTC timestamp>/`. That is
deliberate — it keeps the pre-migration store recoverable by hand, and it keeps
"is this hive embedded?" answering correctly for everything else in `bh`. It also means the
~2.8 GB is still on disk until you remove those directories yourself.

Once `bh doctor` reports the hive on server mode and you have used it for long enough to trust
it:

```sh
du -sh <hive>/.beads/embeddeddolt.pre-migrate-*      # look before you delete
rm -rf <hive>/.beads/embeddeddolt.pre-migrate-*      # this is where the disk actually comes back
```

> **On a furnished hive, check `git status` first.** `.beads/.gitignore` ignores
> `embeddeddolt/` by exact name, so the moved-aside `embeddeddolt.pre-migrate-*` is **not**
> covered by it and will show up as untracked files in a hive whose `.beads/` is tracked. Do
> not commit it. (Zero-footprint hives are unaffected — all of `.beads/` is excluded there.)

Also safe to prune, once you are done migrating:

- **`~/.beadhive/storage-migrate-backups/<hive>/<timestamp>/`** — the pre-migration backups
  (JSONL export + Dolt-native). These are your rollback material for §5; keep them until you
  are certain, then delete them. They are artifacts, never the only copy of anything.
- **`~/.beadhive/storage-migrate-state.json`** — the resume ledger. Deleting it only makes the
  next fleet run re-examine every hive (each is idempotent, so that is safe, just slower).
- **`~/.beadhive/storage-migrate-locks/`** — per-hive lock files, reclaimed automatically when
  stale. Remove only if no migration is running.

Nothing in §"What's safe to delete, and what must never be copied" for 0.7.0 changes:
`~/.beadhive/host.yaml` is still machine-local forever, and `~/.beadhive/hq/` is still durable.

## 0.6.0 → 0.7.0 — Factory HQ, the multi-host model, and the fleet/host config split

0.7.0's real subject is **multi-host**. Before this release, "the machine `bh` runs on" was
implicit and untracked; every host quietly assumed it was the only one. 0.7.0 makes the host a
first-class concept with its own identity, its own manifest, and a write fence — and Factory HQ
graduates from a local cache into the fleet's durable, remote-backed store. None of that is
visible from a version number, and it changes what's on disk enough that this is a guide, not a
changelog entry.

> **Breaking change, read this first:** `bh backup <dest>` is now `bh backup export [dest]`.
> See [§6](#6-breaking-change-bh-backup-is-now-bh-backup-export) below — this is the one thing
> in 0.7.0 that breaks existing muscle memory or a script.

**If you only ever run `bh` on one machine and never touch `bh hq`/`bh host`,** almost none of
this changes your day-to-day: `guard_primary` (§1) only refuses a write when a host *lease*
exists naming another host, and a factory that has never adopted anything has no lease at all —
single-host stays single-host, functionally unchanged. What still applies to you regardless:
the `bh backup export` rename (§6), and the fact that your first `bh hq init` will write real
disk (§5) the moment you decide to stand one up.

### 1. The multi-host model

A **host** is one `~/.beadhive` config home (usually one machine). 0.7.0 gives it four new
pieces of state, all covered in depth by
[`docs/design/multi-host-model-adr.md`](design/multi-host-model-adr.md):

- **Host identity — `~/.beadhive/host.yaml`.** A `host_id` (UUID) plus a cosmetic `label`,
  minted once by `bh config init` and **never regenerated, never synced, never copied to
  another host** — it is this machine's identity, not fleet truth. A hostname rename never
  changes it.
- **Per-host manifests in HQ — `hosts/<host_id>.yaml`.** Once a host registers
  (`bh host init` / `bh host provision`), its role (`executor` / `transient` /
  `viewer`) and identity mechanism are recorded in Factory HQ, so `bh host list` can render the
  whole fleet's roster from one place.
- **The host lease + epoch fence.** Exclusive-write arbitration for a hive between hosts, split
  across two refs: a **lease** at `refs/bh/lease/<prefix>` in HQ (who *should* be primary —
  schedule, TTL, `bh host list`), and a **fence** at `refs/bh/epoch` beside the hive's own data
  (who *may* write — enforced atomically with the push). You manage this indirectly through
  `bh host adopt <hive>` / `bh host release <hive>` / `bh host packup`; you never touch either
  ref by hand.
- **`guard_primary()`.** The check every write verb (`bh work assign|claim|start|submit|merge`,
  `bh plan file`) now runs before mutating a hive: refuse if this host isn't the hive's leased
  primary. Reads (`ready`, `list`, `show`, `brief`, `sync`) are never gated. Critically: **an
  absent lease means "unconfigured", not "someone else's"** — until you `adopt` a hive from
  some host, every host may write it, exactly like before 0.7.0.

The practical upshot: multi-host is opt-in. Nothing forces exclusivity onto a single-machine
setup; it switches on the moment a *second* host adopts a hive you also work on.

### 2. Factory HQ

Factory HQ (`~/.beadhive/hq/`) is the fleet's durable, remote-backed store — full details in
[HQ.md](HQ.md). The verbs that matter for an upgrade:

| Command | Does |
|---|---|
| `bh hq init [--create] [--dry-run]` | Stand up (or idempotently re-wire) HQ: `bd`-init the local store (first run only), take a verified pre-push backup, scaffold `fleet.yaml`/`workspace.toml`/`hosts/`, then wire + push the remote. `--create` makes the remote as a private, empty repo when it doesn't exist yet. |
| `bh hq clone` | Bootstrap a **second** host with no local HQ: clone `main`, hydrate bead state from `refs/dolt/data`, register the `local/factory/hq` identity. Refuses if `~/.beadhive/hq` already exists — never clobbers. |
| `bh hq push [--dry-run]` | Git mode commits/publishes tracked HQ config and HQ-owned Beads; SQL mode publishes only HQ Beads state. It never refreshes the hub aggregate; run `bh sync` for that. |
| `bh hq status` | Read-only ahead/behind for both halves against the wired remote. |
| `bh hq restore [--list] [--from DIR] [--level auto\|tar\|jsonl] [--dry-run] [--confirm]` | Restore HQ from a pre-push backup — `--level tar` replaces the Dolt store, `--level jsonl` upserts the portable export (works even with no readable store). |

The first `bh hq init` on a real fleet is not a cheap operation — see §5 before you run it.

### 3. The fleet/host config split

A flat `~/.beadhive/config.yaml` — the only shape that existed before 0.7.0 — mixed fleet-wide
truth (`orgs`, `dimensions`, `work.validate_cmd`, `managed_repos`, …) with host-local truth
(`worktrees.path`, `otel.*`, `work.identity`, `hq.remote`, …) in one file. 0.7.0 splits that
partition into two files that `config.load()` deep-merges at read time: `fleet.yaml` (inside
`~/.beadhive/hq/`, identical on every host) as the base, with the host's own reduced
`config.yaml` merged over it. Which key belongs to which side is *data*
(`config_partition.py`'s `FLEET_PREFIXES`/`HOST_PREFIXES`), not a branch you have to reason
about by hand. Full schema in
[CONFIGURATION.md — Fleet + host config](CONFIGURATION.md#fleet-host).

**This matters for exactly one host, and it's a one-time step:**

- **The host that FOUNDS a fleet** (the first one to run `bh hq init`) must also run
  `bh config split` — see [the walkthrough](#the-upgrade-end-to-end) below. `bh hq init` writes
  `fleet.yaml` from a
  snapshot of your *current* resolved config, but it never rewrites your own `config.yaml` to
  drop the now-duplicated fleet keys; left alone, the next `config.load()` sees the same
  fleet-classified keys on both sides and raises a `ConfigError` naming every offending key.
  `bh config split` is what actually reduces your file, and it's idempotent — a config with
  nothing fleet-shaped left in it is a clean no-op on re-run.
- **A host JOINING an existing fleet needs no manual split at all.** `bh hq clone` — and
  `bh host provision`, which calls it — reconciles the host config automatically: it drops the
  stale fleet-shaped keys your local template left behind (never merging them anywhere; the
  freshly cloned `fleet.yaml` is authoritative) and **reports what it dropped**. This is the
  direction that matters for almost every upgrading host, and it needs zero operator action.

Don't run `bh config split` on a joining host "just in case" — the two directions move keys
opposite ways (split *publishes* your leaves into `fleet.yaml`; clone's reconciliation *drops*
your stale copies), and a joining host has nothing of its own worth publishing.

### 4. Host lifecycle

New verbs to adopt/decommission a host, and — the part that's easy to get wrong — **two of them
sound almost identical and are not**:

| Verb | Scope | Does |
|---|---|---|
| `bh host provision --role <role> [--auto] [--dry-run]` | new host | The whole adoption path in one idempotent, resumable call: `config init` → `git workspace update` → resolve `hq.remote` → `hq clone` (auto-reconciling, per §3) → `host init` → per-hive `bead sync` → fix `.beads` permissions → verify. Every step probes before acting, so re-running against partial state is always safe. **Prerequisite:** `bh setup check` — see §10. |
| `bh host init --role <role>` | this host | Mint/write just this host's own manifest into HQ (`hosts/<host_id>.yaml`). What `provision` calls internally as one of its steps. |
| `bh host lease adopt <hive> [--force]` | this host, one hive | Become primary for `<hive>`: fence its remote, then lease it in HQ. |
| `bh host lease release <hive>` | this host, one hive | Yield this host's lease for `<hive>` (a tombstone; the epoch survives). |
| `bh host lease release --all` | this host, every hive | Release every lease this host currently holds — the "I'm switching machines" ritual. |
| **`bh host retire [--dry-run] [--backup] [--confirm] [--purge]`** | **HOST-LOCAL** | Decommission **THIS host only**: one SAFE/NEEDS_BACKUP/BLOCKED verdict across every hive, worktree, held lease, and HQ, then release leases → sync+push every hive → reclaim local clones/worktrees → deregister this host's manifest → push HQ. **Never touches `managed_repos`/fleet registration** — the fleet still has every hive after this runs, just not on this machine. |
| **`bh host rm <host_id> [--dry-run] [--confirm] [--force]`** | **FLEET-WIDE** roster entry | Unregister an *orphaned* manifest from HQ (e.g. a wiped-and-rebuilt host whose old entry never self-cleared). Registry-only — no clone, worktree, or history is touched. Requires `--confirm`; `--force` additionally bypasses the live-lease and recent-last-seen guards. |

And the hive-level counterparts — where the scope distinction is the single easiest thing to
get catastrophically wrong, because the verb names are so close:

| Verb | Scope | Does |
|---|---|---|
| **`bh hive rm <id>`** | **FLEET-WIDE** | Unregister a hive from `managed_repos` — shared fleet truth, so **every host loses this hive**, not just this one. Registry-only; leaves `.beads`/the clone intact. |
| **`bh hive retire <id> [--dry-run] [--backup] [--confirm] [--purge]`** | **FLEET-WIDE** | Guarded teardown: assess → backup-or-consent → worktree teardown → soft-archive the clone → unregister. The unregister step is the same fleet-wide `managed_repos` drop as `rm`, even though the clone/worktree teardown only touches this host. |
| **`bh hive reclaim <id> [--dry-run] [--backup] [--confirm] [--purge]`** | **HOST-LOCAL** | The *host-local-only* twin of `retire`: identical assess → backup-or-consent → worktree teardown → soft-archive, but **never unregisters** — `managed_repos` (and every other host's copy) is left untouched, so the hive stays registered for the fleet. Use this when only this host no longer wants a local copy. |

If you want to stop working on a hive from *this laptop* but keep it live for the rest of the
fleet, that's `bh hive reclaim` or `bh host retire` — never `bh hive rm`/`bh hive retire`, which
take it away from everyone.

### 5. Backups and disk

0.7.0 starts writing backups nobody asked for by name — they're a safety net, but they consume
real disk, and nothing pruned them before this release. Three independent roots, each with a
different owner and retention policy (full design:
[backup-retention-boundary-adr.md](design/backup-retention-boundary-adr.md)):

| # | Root | Written by | When | Default retention |
|---|---|---|---|---|
| 1 | `~/.beadhive/hq-backups/<date>/` | `bh hq init`, before HQ's first remote push | once, per remote-wiring event | keep newest **5** dated dirs (`backup.hq_keep`), auto-pruned right after each new verified backup |
| 2 | `<hive>/.beads/backup/` | `bd`'s own Dolt-native backup | periodic, `backup.interval` (bd's own timer) | rotate past **500 MB** (`backup.hive_cap_mb`), keep **3** generations (`backup.hive_rotate_keep`) — operator-invoked, not automatic |
| 3 | `~/.beadhive/backups/<provider>/<org>/<repo>/` | `bh backup export` | manual, ad hoc | overwritten each run — no history to prune |

**A real first `bh hq init` wrote 744 MB** (a 743,894,216-byte Dolt tarball plus a 5,504,318-byte
JSONL export) into `hq-backups/` on one run, with no pruning at the time. On a real, already
populated fleet, `bh backup usage` reported **1.4 GB across 21 backup roots** — mostly root #2,
which grows continuously with every commit until you rotate it. Don't let a full volume be how
you find out:

```sh
bh backup usage              # size + policy for all three roots, this host
bh backup usage --json       # machine-readable
bh backup reclaim --dry-run  # preview what a reclaim would free, all roots
bh backup reclaim --root cache --dry-run   # show SUPERSEDED vs RETAINED per-hive caches
bh backup reclaim --root cache --confirm   # remove only caches superseded by local .beads
bh backup reclaim --root hq --confirm            # prune HQ's dated dirs to backup.hq_keep
bh backup reclaim --root hive --confirm          # rotate the CURRENT hive's bd backup once over cap
bh config set backup.hq_keep 3                   # keep fewer HQ snapshots
bh config set backup.hive_cap_mb 200             # rotate a hive's bd backup sooner
```

`bh backup reclaim --root hive` needs `--confirm` to actually mutate (it rotates bd's live
backup destination via bd's own sanctioned lifecycle verbs, never chunk-store surgery) —
`--dry-run` previews with zero writes either way.

### 6. BREAKING CHANGE: `bh backup` is now `bh backup export`

Pre-0.7.0, `bh backup [dest]` was a bare command that wrote the JSONL interchange mirror.
0.7.0 needed `usage`/`reclaim` as proper subcommands of the same top-level name, and a
positional-argument-vs-subcommand parse conflict makes a hybrid "bare action, but also a
subcommand group" shape actively ambiguous — so `bh backup` is now a **group**, and the old
bare invocation is a subcommand:

```sh
# before 0.7.0
bh backup                 # exported the JSONL mirror
bh backup ./some/dest     # exported to an explicit destination

# 0.7.0 and later
bh backup export                 # same behavior, new name
bh backup export ./some/dest     # same behavior, new name
```

**If you have a script, alias, or cron job that calls bare `bh backup`, it now does nothing
useful** (`bh backup` with no subcommand just prints help) — update it to `bh backup export`.
This is pre-1.0 (`major_version_zero = true`), so a CLI rename is a MINOR version bump per this
project's own versioning convention, not something a major-version gate would have caught.

### 7. BREAKING CHANGE: `bh hive rm` now requires `--confirm`

`bh hive rm` drops the hive from `managed_repos` — **fleet truth**, so every host loses it, not
just the one running the command (see §4). Before 0.7.0 it took no flags at all and performed
that drop immediately: it printed a warning naming the consequence on the line *before* the
mutation, which announced the outcome at the moment you could no longer prevent it.

It now matches every sibling destructive verb:

```sh
bh hive rm <id> --dry-run    # preview; changes nothing
bh hive rm <id> --confirm    # perform the fleet-wide unregister
bh hive rm <id>              # refuses, exit 1
```

This closes an asymmetry rather than inventing a rule: `bh hive retire` runs the *identical*
`registry.unregister` as its final step and has always gated it behind `--confirm`. The same
fleet-wide drop was protected in one verb and unprotected in the other.

Both the refusal and `--dry-run` print the `bh hive add` invocation that would restore the
entry. `rm` is recoverable — but only while you still know the provider/org/repo/prefix/kind,
which is exactly what unregistering takes away.

**If you have a script calling `bh hive rm`, add `--confirm`.**

### 8. BREAKING CHANGE: `bh host` verb renames

Two renames land together, both inside the `bh host` group that is itself new in 0.7.0 — so
these only affect you if you were tracking pre-release builds.

**`bh host remove` is now `bh host rm`, and requires `--confirm`.**

`rm` is the decided spelling for "unregister one entity" across the whole CLI
(`bh hive rm`, `bh worktree rm` — see the verb model in
[`design/cli-mcp-naming-conventions-adr.md`](design/cli-mcp-naming-conventions-adr.md) §5b-i);
`remove` shipped against that convention and is corrected before it sets. It also picks up the
same `--dry-run` / `--confirm` gate every other teardown verb carries, and the old `--yes`
(self-removal) folds into `--confirm` — the two flags asked the same question at different
scopes:

```sh
bh host rm <host_id> --dry-run    # preview the plan; changes nothing
bh host rm <host_id> --confirm    # perform the fleet-wide unregister
bh host rm <host_id>              # refuses, exit 1
```

`--force` is unchanged and still separate: it bypasses the live-lease and recent-last-seen
guards, and `--confirm` never implies it.

**The lease verbs moved under `bh host lease`, and `packup` is gone.**

| Before | Now |
|---|---|
| `bh host adopt <hive>` | `bh host lease adopt <hive>` |
| `bh host release <hive>` | `bh host lease release <hive>` |
| `bh host packup` | `bh host lease release --all` |

Their object is a *lease* — a renewable, time-bounded claim over a hive — not the host. Flat,
`bh host release` sat next to `bh host retire` and read as its milder synonym when one is
reversible and the other terminal, and `bh host adopt <hive>` took a hive argument in a group
where every other verb takes a `host_id`. `packup` disappears because fan-out is spelled
`--all`, never its own verb name.

**All three old spellings keep working as hidden aliases**, so scripts do not break; they are
just off `--help`. Prefer the new forms in anything you write from here.

### 9. BEHAVIOR CHANGE: onboarding no longer installs the pre-push fence hook

`bh hive init` / `bh hive onboard` used to furnish a `pre-push` git hook in every hive — the
multi-host fence's early refusal. **They no longer install anything.** It is now opt-in:

```sh
bh hive hook install [HIVE_ID]     # install the fence shim for one hive
bh hive hook pre-push [HIVE_ID]    # the hook contract itself, for your own dispatcher
```

Two reasons, and the second is the one that matters.

**It is not enforcement.** Current bd forces `core.hooksPath=/dev/null` in its Dolt transport,
so this hook does not run for `bd dolt push`. Managed publication now reserves
`refs/bh/epoch` by remote CAS before bd and verifies the same ticket afterward. That rejects a
stale host before data, but it is not atomic with bd's opaque push; `bh doctor` exposes the
CAS→push window and raw-bd bypass. The hook remains only a compatibility/diagnostic entrypoint.

**The old default was already failing quietly.** The installer is deliberately
non-destructive: finding a `pre-push` it did not write, it skips and reports
`"skipped (custom hook present)"`. So every hive whose operator used *any* hook manager already
had no fence, while appearing to have one. Opt-in replaces a silent partial default with an
explicit choice.

If you want the early refusal, run `bh hive hook install` per hive. **Re-run it after the
first `bd dolt push`** on a fresh hive: with bd's embedded engine the push that carries
`refs/dolt/data` originates in a transport repo bd creates lazily, so before that there is
nothing to install into (the verb says so rather than failing).

Already-installed hooks keep working untouched — they are simply never created or refreshed
again unless you ask. If you use a hook manager (lefthook, husky, pre-commit), prefer wiring
`bh hive hook pre-push` as a job instead; it reads git's ref list on stdin and exits non-zero
to refuse, so any dispatcher can call it. See
[`design/hooks-as-functionality-adr.md`](design/hooks-as-functionality-adr.md).

### 10. `bh setup check` — a prerequisite `bh host provision` doesn't announce

Nearly every `bh` verb is gated behind a passing post-install dependency cache (`setup`,
`config`, and `doctor` are the only exemptions) — on a fresh host that has never run the check,
`bh host provision`, the recommended entry point for adopting a new host, refuses before doing
anything:

```text
✗ `bh host` requires setup — run `bh setup check` first.
  Skip with BH_SKIP_SETUP_CHECK=1 (debug bypass).
```

Running `bh setup check` once clears it (cached from then on), so it's a required first step,
not a dead end — but neither `provision`'s own 8-step plan (which starts one step later, at
`config init`) nor most written adoption sequences say so up front. This was found the hard way
during the 0.7.0 release-readiness pass and is tracked as a documentation/UX gap
([bh-1kzc](design/0.7.0-release-readiness.md#gap-1--bh-setup-check-is-an-undocumented-prerequisite),
still open) — until it's fixed in the tool itself, treat it as step 0 of every sequence below.

### 11. REQUIREMENT: your `bd` build must embed dolt >= 2.2.0

**This is the one to read if you run more than one host.** 0.7.0's multi-host model syncs bead
data with `bd dolt pull`. On a `bd` whose embedded dolt predates **v2.2.0**, that pull can hang
**indefinitely** on a large store — upstream [beads#4770](https://github.com/gastownhall/beads/issues/4770),
a quadratic `git cat-file` read. Measured here: 170s then killed, versus 3.2s on a fixed build.

**Every tagged `bd` release through v1.2.2 is affected.** Those releases embed the pre-v2.2.0
Dolt transport code. Beads v1.3.0-rc.1 is the first release candidate with the fixed
v2.2.0-era dependency and is the Nix-pinned build. Do not substitute the ordinary stable
package for it: it cannot open a schema migrated from v62 through v66.

**Upgrading the standalone `dolt` CLI does not help.** dolt is statically compiled into `bd`
(a ~137 MB binary) and the CLI is never spawned. Verified by upgrading dolt 2.1.10 → 2.2.2 and
retesting: still hung.

Two escapes:

```sh
# 1. Beads v1.3.0-rc.1 (the managed Nix path pins this release candidate)
nix profile add github:beadhive/beadhive/latest#default

# 2. run bd against an external dolt sql-server >= 2.2.0
bd init --server --server-host 127.0.0.1 --server-port <port>
```

The second is the more durable answer: `bd` issues fetch/pull as `CALL DOLT_FETCH(...)` over
the database connection, so the **server's** dolt does the transport work — which takes the
dolt version out of `bd`'s release cadence entirely. Verified at the dolt layer: dolt 2.2.2
fetched the same remote in **11 seconds** where an affected embedded build hung past 240s.

`bh setup check` now warns when it detects an affected `bd`. It is a **warning, not a gate** —
the hang needs a large store to bite, so a small or new hive may never hit it, and blocking
setup over a probabilistic issue would be worse than the issue.

### The upgrade, end to end

**Once, on every host**, after installing the new `bh` version:

```sh
bh setup check     # clears the post-install dependency gate (§10) — do this first, always
bh config init     # idempotent: never touches an existing config.yaml, mints host.yaml if absent
```

**Then, exactly one path per host** — pick based on whether this host is starting a fleet or
joining one that already exists.

**Path A — this host FOUNDS the fleet** (you have no `hq.remote` anywhere yet, and no other
host has stood one up):

```sh
bh hq init --create          # stand up HQ; --create makes a private empty remote if needed
bh config split               # reduce this host's config.yaml now that fleet.yaml is real (§3)
bh host provision --role executor --auto   # finishes registration; earlier steps
                                                    # (config init, hq clone) are already done
                                                    # and report `skipped`, not repeated
```

**Path B — this host JOINS a fleet another host already founded** (`hq.remote` resolves to
someone else's HQ):

```sh
bh host provision --role executor --auto   # or --role transient for an intermittently-used
                                          # laptop; no manual `bh config split` needed (§3) —
                                          # `hq clone`'s own reconciliation handles it and
                                          # reports what it dropped
```

Either path, finish by checking you actually landed usable: `bh host provision`'s own final
step is a verify gate, but you can re-run it any time — `bh doctor` also folds in a fleet-health
section.

### What's safe to delete, and what must never be copied

0.7.0 doesn't change the answer for most of `~/.beadhive/` — the full durable / regenerable /
machine-local / artifact classification for every entry already lives in
[beadhive-home-layout-contract.md](design/beadhive-home-layout-contract.md); don't re-derive it
by hand. The two facts from that contract worth repeating here, because they're exactly what
0.7.0 makes newly relevant:

- **`~/.beadhive/host.yaml` is machine-local, by design, forever.** Minted once, never
  regenerated, and explicitly never synced or templated. **Do not copy it to another host, put
  it in dotfiles sync, or restore it from another machine's backup** — doing so would make two
  hosts share one `host_id`, which breaks the fencing/identity model `guard_primary` and the
  per-host HQ manifest both key off. If a host is wiped and rebuilt, it gets a *new* `host.yaml`
  (and its old HQ manifest becomes exactly the kind of orphan `bh host remove` exists to clean
  up).
- **`~/.beadhive/hq-backups/` and `<hive>/.beads/backup/` are artifacts, not sources of truth.**
  Safe to prune with `bh backup reclaim` (§5); never the *only* copy of anything durable.
  `~/.beadhive/hq/` itself, by contrast, **is** durable — a fresh host obtains it via
  `bh hq clone`, never by hand-copying another host's directory.

## See also

- [DOLT.md](DOLT.md) — the store engine vs the optional central server, and how to tell which
  mode a hive is on.
- [design/dolt-server-mode-adr.md](design/dolt-server-mode-adr.md) — why shared server, why not
  embedded or bd's owned mode, and the migration/rollback path.
- [design/dolt-store-engine-liveness-adr.md](design/dolt-store-engine-liveness-adr.md) — what
  `bh` does when the store engine is down.
- [spikes/bh-00cq-external-dolt-sql-server.md](spikes/bh-00cq-external-dolt-sql-server.md) — the
  transport and latency measurements, with their method and caveats.
- [HQ.md](HQ.md) — Factory HQ in full: layout, naming, hub-vs-HQ, `push`/`status` mechanics.
- [CONFIGURATION.md — Fleet + host config](CONFIGURATION.md#fleet-host) — the full partition
  schema, `--scope fleet|host`, and `bh config split`'s exact behavior.
- [design/multi-host-model-adr.md](design/multi-host-model-adr.md) — the decision record for
  the host/lease/epoch model, including the amendment that split the lease from the fence.
- [design/backup-retention-boundary-adr.md](design/backup-retention-boundary-adr.md) — why
  three backup mechanisms exist and the retention policy behind each.
- [design/beadhive-home-layout-contract.md](design/beadhive-home-layout-contract.md) — every
  `~/.beadhive/` entry classified durable / regenerable / machine-local / artifact.
- [design/0.7.0-release-readiness.md](design/0.7.0-release-readiness.md) — the verified pass
  this guide's upgrade sequence is drawn from.
- [CHANGELOG.md](../CHANGELOG.md) — the mechanical, per-commit record.
