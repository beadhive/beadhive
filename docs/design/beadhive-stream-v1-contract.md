# Beadhive stream v1 contract — backend-neutral bead-state frames (bh-jksq.1)

> Status: **decided.** This is the wire/type contract for `bh stream` v1: canonical scopes,
> the backend-neutral consumer-facing state shape, the snapshot/delta/resync frame types,
> opaque-revision semantics, NDJSON framing, and stdout/stderr rules. It is filed before any
> of it is built — bh-jksq.2 (the port), bh-jksq.3 (the polling adapter), bh-jksq.4 (the CLI),
> and bh-jksq.5/bh-jksq.6 (shutdown + tests) implement against this document; they do not
> renegotiate its shapes in passing. Fix the shape before anything reads it, the same
> principle [`bead-commit-linkage-contract.md`](bead-commit-linkage-contract.md) applies to
> metadata keys.
>
> **Epic:** bh-jksq ("Stream backend-neutral Beadhive state"). **Sibling addendum epic:**
> bh-5wpb6 projects richer operator entities (`WorkDependency`, `GateRequest`, `EpicSchedule`,
> `Assignment`) onto this same transport without touching the plumbing defined here — see
> [§8](#8-extension-policy-what-bh-5wpb6-layers-on-top).

## 0. Non-goals (inherited from the epic, restated so this doc is self-contained)

bh-jksq's description fixes these out of scope for v1, and every section below is written
consistently with that boundary:

- **No topic filters.** A stream is scoped (`factory` | `hub` | `hive`), not further filtered
  by label/status/type. A snapshot for a scope is the *whole* known corpus at that scope.
- **No durable replay.** There is no on-disk/server-side log of past revisions. A revision's
  validity window is bounded by the adapter process that produced it, not preserved across
  adapter restarts. §5 spells out what that means for `--since`.
- **No MCP streaming.** `bh stream` is a CLI/subprocess contract (NDJSON over stdio). An MCP
  transport for the same frames is a later, separate surface, not this contract.
- **No direct database backend.** v1's only adapter is bd-backed (bh-jksq.3). This contract is
  written so a future adapter (event log, direct DB, remote service — per bh-jksq.2's
  acceptance criteria) can be swapped in without a consumer-visible change, but v1 ships one.

## 1. The boundary this contract does not cross: bead state vs. dispatch execution

**Read this before anything else in this document, and before touching bh-jksq.2/.3/.4.** A
second, unrelated JSONL event stream already exists in this tree
(`src/beadhive/dispatch_log.py`, one aggregate sink per hive, written by every dispatcher loop:
`seat_spawned`, `seat_harvested`, `seat_cancelled`, `dispatch_cause_recorded`, `dispatch_pass`
records carrying `pid`, `pgid`, `session_id`, cancel rungs). It is easy to mistake for a
prototype of this one, or to assume the two should eventually merge. They must not, and the
epic's own notes (2026-08-11) are explicit that whoever writes this contract states the
boundary here so the next reader does not rediscover it as if it were an oversight.

The line is drawn by **[`work-runtime-tiers-adr.md`](work-runtime-tiers-adr.md) Decision 1**
("beads owns lifecycle state; the runtime owns process scheduling only... no runtime-only
lifecycle state. A runtime MAY keep a richer *execution* record... but is NEVER authoritative
about whether a bead is claimed, blocked, approved, or done") and sharpened to a *zero carve-out*
by **[`loop-ownership-and-execution-memory-adr.md`](loop-ownership-and-execution-memory-adr.md)
Decision 2** ("v1 persists nothing outside beads" — the dispatch sink's `pid`/`pgid`/cancel-rung
detail is exactly the permitted-but-bounded "richer execution record", held in process/on a
host-local file, never promoted to lifecycle truth).

Two streams, two different questions, and this contract is one side of that line:

| | **This stream** (`bh stream`) | **The dispatch sink** (`bh host dispatch logs`) |
|---|---|---|
| Answers | "What is true about the work?" | "What did one dispatcher loop DO on this host?" |
| Scope | backend-neutral bead state (§3) | one host's process-scheduling events |
| Backend | `bd` today, swappable (bh-jksq.2) | `beadhive.log` file sink, not swappable — it's *the* execution record |
| Carries | issue id, status, priority, labels, dependencies | pid, pgid, session_id, cancel rung, exit code |
| Persistence | opaque revisions, no durable replay (§0) | append-only JSONL, host-local, not git-synced |
| Authoritative for lifecycle? | **yes** — it is beads, adapted | **no**, by construction (ADR Decision 1) |

Consequences this contract commits to:

1. **The dispatch sink is never a backend for this stream.** It is host-local and deliberately
   not git-synced — one hive's sink says nothing about the same hive on another host, which is
   the opposite of what a backend-neutral bead-state stream needs to be true.
2. **A future consumer correlating the two is expected**, not accidental — the obvious one is a
   supervisor watching several dispatchers (the epic notes name the deferred director loop,
   `loop-ownership-and-execution-memory-adr.md` Decision 4, as the likely first such consumer).
   The join key is the bead id. `StreamIssue.id` (§4) MUST stay spelling-compatible with the
   `bead` field the dispatch sink already writes on every record (`seat_spawned.bead`,
   `seat_harvested.bead`, ...) — same bd issue id string, no reformatting on either side.
3. **This stream is for observers, never for driving a loop.** `work-runtime-tiers-adr.md`
   Decision 1 already rejected a bespoke mailbox/event bus for dispatch wake-up in favor of
   `bd gate`'s poll-based waiter list ("a mailbox with a slower doorbell"). A loop that took its
   next action from `bh stream` instead of from `bd`/beads directly would put lifecycle truth in
   the transport — exactly the invariant Decision 1 protects. Nothing in this contract is a gate,
   a lease, or a wake-up signal; it is a read-only projection.

## 2. House conventions this contract reuses rather than reinvents

- **Envelope shape.** `beadhive/jsonout.py`'s `--json` convention (flat top-level
  `schema_version: int`, additive fields keep the version, a removed/retyped/re-meant field
  bumps it) is reused verbatim for frames — see §6. bd's own `--json` output already uses this
  shape (`engine.py`'s recorded observations of `bd dolt status --json`); copying it means one
  rule for reading a `bh` payload and a `bd` payload.
- **Seam shape.** The port bh-jksq.2 builds is modeled on `engine.py`'s `Engine` `Protocol` +
  frozen-dataclass-result pattern (itself modeled on `dolt.py`'s container-backend dispatch): a
  thin selectable implementation, not a plugin framework. This contract fixes the *wire/type*
  shapes that protocol must produce; it does not design the protocol's method signatures
  (bh-jksq.2's job).
- **Scope vocabulary.** `factory` / `hub` / `hive` are the same three terms `docs/OVERVIEW.md`
  already defines (§3 below quotes that mapping rather than inventing new names).

## 3. Canonical scopes

Exactly three, matching the mental model in `docs/OVERVIEW.md` — a stream instance is bound to
**one** scope for its lifetime (`bh stream --scope factory|hub|hive`, bh-jksq.4); there is no
mid-stream rescoping and no multi-scope multiplexing in v1 (consistent with "no topic filters").

| Scope | What it is | Backing store today |
|---|---|---|
| `hive` | One repo's own beads DB — the state a `bd` command run in that repo's worktree would see. | that hive's own Dolt store |
| `hub` | The personal cross-hive aggregate at `~/.beadhive/hub/`, built by `bh sync` — every hive one operator's workspace knows about. | the hub's aggregated export |
| `factory` | The shared Factory HQ aggregate at `~/.beadhive/hq/` — the durable control-plane store multiple operators/agents share, when registered. | HQ's aggregated export |

`hub` and `factory` share the same aggregation shape and therefore the same `StreamIssue`
record (§4) — only the scope tag and which hives are included differ; a consumer does not
need a second type to read a `factory` stream versus a `hub` stream.

## 4. Backend-neutral state shape: `StreamIssue`

v1 ships **exactly one** entity type. It is a deliberately curated projection of a bd issue —
not `bd show --json`'s own record, and not the JSONL export record verbatim. Fields absent
below are absent on purpose (see the exclusions list): the instruction this contract exists to
satisfy is "bd records and event shapes must not become the public contract."

```jsonc
{
  "id": "bh-jksq.1",              // bd issue id — stable identity, join key with the dispatch
                                   // sink's "bead" field (§1.2). Never re-derive a hive from
                                   // this by parsing the prefix — read `hive` below instead.
  "hive": "beadhive",             // owning hive's slug, from the registry, not string-parsed
                                   // out of `id`. Always this stream's own hive at hive scope.
  "issue_type": "task",           // bd's issue_type passthrough: task | bug | epic | gate | ...
  "status": "in_progress",        // bd's status passthrough: open | in_progress | blocked |
                                   // closed | ... (bd's own vocabulary, not remapped — see
                                   // the exclusions note on why passthrough is fine here but
                                   // not everywhere)
  "priority": "P1",
  "title": "Define Beadhive stream v1 contract",
  "labels": ["architecture", "streaming"],
  "assignee": "dev/contract-1",   // seat string, or null
  "parent_id": "bh-jksq",         // epic/parent id, or null
  "dependencies": [
    { "issue_id": "bh-jksq.1", "depends_on_id": "bh-96ew5", "type": "blocks" }
  ],
  "updated_at": "2026-08-22T00:00:00Z"
}
```

`dependencies` edges are trimmed to `{issue_id, depends_on_id, type}` — a subset of the export
record's `{issue_id, depends_on_id, type, created_at, created_by, metadata}` shape
(bh-jksq.3's notes) — deliberately, so that bh-5wpb6.2's `WorkDependency` projection is an
**additive** field restoration on the same edge shape, not a rename or a new read path.

### What is excluded from `StreamIssue`, and why

- **Raw bd `metadata`.** bd's `metadata` is an open-ended bag of backend-specific keys —
  `git.commits` (see `bead-commit-linkage-contract.md`) is one example already in the corpus,
  and nothing stops a future key being added that only makes sense in bd's own storage model.
  Passing `metadata` through verbatim would make bd's internal key conventions part of the
  public contract, which is exactly what this contract exists to prevent. A specific, curated
  key surfaces as its own typed field (as `dependencies` already does) or as a sibling entity
  (§8) — never as an opaque pass-through bag.
- **Lease/heartbeat detail, Dolt refs, gate resolution mechanics.** These are bd-internal
  execution detail, not "what is true about the work" at the level a stream consumer asks it.
  A gate's *presence and kind* is exactly the kind of fact bh-5wpb6.1's `GateRequest` will
  surface as its own entity (mapped from bd's gate type + reason-marker convention, per that
  epic's design notes — deliberately not decided in this document).
- **Description/body text.** Not needed by any named v1 consumer (CLI, the operator-console
  spike that motivated bh-5wpb6); adding it later is additive under §6's versioning rule, so
  there is no cost to deferring it.

`status`, `priority`, `issue_type` are kept as bd's own passthrough vocabulary rather than
remapped to a new enum: they are already small, stable, closed vocabularies bd defines and
`bh` already surfaces unchanged elsewhere (e.g. `bh work list --status`), so remapping them
here would be renaming for no consumer benefit — the "don't leak bd" instruction is about
shape and internal mechanics (the exclusions above), not about refusing every field bd also
happens to use that name for.

## 5. Frames: snapshot, delta, resync

Every frame shares one envelope (§6 gives the exact wire form). Three frame kinds:

### `snapshot` — the full known state of the scope

- Carries `issues: StreamIssue[]` — **every** issue currently known at this scope (no topic
  filter, §0).
- **Always the first frame of a stream session**, before any `delta` or `resync` — including a
  session opened with `--since`. This is the snapshot-first ordering the epic's acceptance
  criteria require. A consumer must be able to build correct state from a `snapshot` alone,
  never needing a control frame or delta to arrive first. §7 defines how the adapter can honor
  `--since` without weakening this invariant.
- `reason: "initial" | "resync"` — `"initial"` for the first snapshot of a fresh session,
  `"resync"` for a snapshot sent to recover from a `resync` control frame (§5, next). A
  consumer that doesn't care about the distinction can treat both identically ("replace all
  local state with this").

### `delta` — issues that changed since a prior revision

- Carries `since_revision` (the opaque revision this delta is relative to — echoes back what
  the consumer last applied, or the snapshot's revision on the first delta of a session),
  `changed: StreamIssue[]` (full replacement records, **not** field-level patches — matching
  bh-jksq.3's "emit changed snapshots" adapter design, so the wire shape doesn't presuppose a
  diffing capability an adapter may not have), and `removed: string[]` (issue ids that have
  left the scope's known set entirely).
- `removed` is for **identity leaving the scope** — deleted, or a hive de-registered from
  `hub`/`factory` — never for an issue closing. Closing a bead is an ordinary `changed` record
  with `status: "closed"`. A consumer must not infer "done" from `removed`.
- Only ever follows a `snapshot` (or another `delta`) within the same session; never the first
  frame.

### `resync` — a control frame: discard state, a fresh snapshot is coming

- Carries no `issues` payload. Fields: `reason: "unknown_revision" | "scope_mismatch" |
  "adapter_error"`.
  - `unknown_revision` — a revision that was valid earlier in this session is no longer
    recognized (for example, an adapter restarted and lost its in-memory history).
  - `scope_mismatch` — the adapter's active continuity token no longer belongs to this scope or
    adapter instance. Defensive: opaque revisions are never valid across scopes or adapters
    (§7), so the stream recovers instead of risking a delta against the wrong state.
  - `adapter_error` — the adapter hit an error it cannot guarantee left its delta series
    coherent (mid-refresh crash-and-restart of internal cache state) and is resetting rather
    than risk emitting a wrong `changed`/`removed` pair.
- A `resync` frame is **always immediately followed by a `snapshot`** frame with
  `reason: "resync"` in the same stream — it is not a terminal frame and not an error; the
  stream does not exit because of it.
- A `resync` is **mid-session recovery only**. It can appear only after the session's initial
  `snapshot`; startup revision misses are handled by the snapshot-first `--since` rule in §7,
  not by putting `resync` before the first snapshot.
- **Why a separate control frame instead of folding this into `snapshot.reason`:** a consumer
  wants to be able to show "resyncing…" the instant the decision is made, not after a
  potentially large snapshot has finished transmitting and parsing. Separating the
  control-plane signal ("state is being reset, more is coming") from the data-plane payload
  ("here is the new state") lets a consumer react to the first without buffering the second.

## 6. Wire form: NDJSON envelope

One JSON object per line, UTF-8, LF-terminated, no pretty-printing — `bh stream ... --format
ndjson` per bh-jksq.4. Every frame is:

```jsonc
{
  "schema_version": 1,
  "frame": "snapshot" | "delta" | "resync",   // NOT "type" — bd already overloads "type" for
                                               // issue_type and gate type; a third meaning of
                                               // "type" in the same ecosystem is a trap. This
                                               // is a deliberate naming call, flagged here.
  "scope": "factory" | "hub" | "hive",
  "revision": "<opaque>",                     // absent on `resync` (there is nothing valid to
                                               // hand back yet — the next frame, a `snapshot`,
                                               // carries the new one)
  "as_of": "2026-08-22T00:00:00Z",            // RFC3339 — see §7's freshness note; NOT part of
                                               // the opaque revision, always consumer-readable
  "partial": false,
  "partial_reason": null,                     // set + `partial: true` together, see §7
  // ...frame-specific fields from §5 (issues / since_revision+changed+removed / reason)
}
```

Following `jsonout.py`'s rule verbatim: adding a field to any frame is **not** a
`schema_version` bump (consumers ignore unknown keys); removing, retyping, or re-meaning one
is. §8 is the extension point this rule exists to support.

`schema_version` is scoped to this contract (the `bh stream` frame contract), the same way
`SETUP_CHECK_SCHEMA` and `DOCTOR_SCHEMA` are each their own number in `jsonout.py` — there is
no shared global version across unrelated `bh` payloads.

## 7. Opaque revisions and resync triggering

- **A revision is an opaque string.** Its format is entirely adapter-defined (a monotonic
  counter, a content hash, a Dolt commit hash, a timestamp+nonce — whatever the backend behind
  bh-jksq.2's port finds cheapest to produce). A consumer MUST treat it as an inert token: pass
  it back verbatim as `--since`, never parse it, never compare it lexically or numerically,
  never assume any two revisions from different scopes or different adapter instances are
  comparable. This opacity is precisely what lets bh-jksq.2's port be implemented by bd
  polling today and something else later with zero consumer-visible change.
- **Scoped, not global.** Revisions are ordered only within one `(scope, adapter instance)`
  pair. A `hub` revision says nothing about a `hive` revision even for the same underlying
  data, and a revision from one adapter process is not valid against a different adapter
  process (including a restarted one) — hence `resync`'s `scope_mismatch` reason.
- **`--since <revision>` never changes snapshot-first startup ordering.** If the adapter still
  retains a complete state for that revision, the first frame is that full state as a
  `snapshot` with `reason: "initial"` and the recognized revision; coherent `delta` frames may
  then catch it up to the current revision. If the revision is unknown or belongs to another
  scope/adapter instance, the first frame is instead the current full state as a `snapshot`
  with `reason: "initial"`. There is no leading `resync`: a new session has emitted no state for
  the consumer to discard. The adapter MAY explain the ignored startup hint on stderr (§9), but
  an expired `--since` is not an error and never weakens the rule that every session starts with
  a snapshot.
- **No durable replay means a current initial snapshot is the common reconnect case, not an
  edge case** (§0). This is a deliberate, dated scope decision, not a placeholder for a missing
  feature: v1 does not build a replay log, so a consumer that expects "reconnect and get exactly
  what I missed" as the *typical* path is designing against a v2 that does not exist yet. Build
  reconnect UX around "usually starts from current state, occasionally starts from the retained
  `--since` state and catches up via delta." `resync` is reserved for continuity lost after that
  initial snapshot.
- **Freshness/staleness is `as_of`, not the revision.** bh-jksq.3's notes measured a real
  failure mode: an export-shaped cache can be legitimately hours stale with nothing in the read
  path noticing. `as_of` (§6) is the RFC3339 timestamp of when the underlying data was known
  accurate — for a polling/cache-backed adapter this can be well behind wall-clock "now", and a
  consumer computing staleness compares `as_of` to the current time itself; the contract does
  not invent a `stale: bool` threshold because "how stale is too stale" is a consumer policy
  question, not a wire fact. `as_of` travels on every frame (including `resync`, where it is
  the time of the decision to resync) precisely because it must never require decoding the
  opaque `revision` to learn how fresh a frame is.
- **Degraded data:** `partial: true` + `partial_reason: <string>` on a `snapshot` or `delta`
  signals the adapter served the frame anyway despite a subsystem it would normally consult
  being unavailable — directly per bh-jksq.3's note that a slow/failed precondition (e.g.
  `bh doctor`) must degrade the frame, never silently suppress the snapshot. `partial_reason`
  is a short machine-readable string (e.g. `"registry_unavailable"`); anything more detailed
  belongs on stderr (§9), not in the frame.

## 8. Extension policy: what bh-5wpb6 layers on top

This contract intentionally ships **one** entity type (`StreamIssue`) and leaves room for
richer operator-semantic entities without redesigning the transport:

- New entity types (`GateRequest`, `EpicSchedule`, `WorkDependency`, `Assignment` — the four
  bh-5wpb6 scopes in, per its design notes) arrive as **additional sibling top-level arrays**
  on the same `snapshot`/`delta` frames (e.g. a future `"gates": [...]"` beside `"issues":
  [...]"`), each gated behind its own presence — a consumer reading only `issues` today is
  unaffected by a new array appearing, per §6's additive-field rule.
- `dependencies` embedded on `StreamIssue` (§4) is deliberately shaped so `WorkDependency` can
  restore its trimmed fields (`created_at`, `created_by`) additively rather than as a rename —
  see bh-5wpb6's own note that this is "very close to a direct field mapping."
- This document does **not** decide `GateRequest.gateKind`'s bd-gate-type-vs-reason-marker
  mapping or `EpicSchedule.groups`' batch-label-vs-DAG derivation — those are bh-5wpb6.1's job,
  explicitly, per that bead's own scoping. What this contract commits to is only that neither
  question requires touching §5's frame kinds, §6's envelope, or §7's revision semantics to
  answer.

## 9. stdout / stderr rules

- **stdout carries frames and nothing else.** One NDJSON line per frame (§6), flushed after
  each write so a slow consumer isn't stalled behind stdio buffering, no banners, no progress
  text, no blank lines. A consumer reading `bh stream`'s stdout can `json.loads` every line
  unconditionally.
- **stderr carries every diagnostic.** Startup/shutdown logging, adapter warnings, retries,
  the free-text detail behind a `partial_reason` code, and any backend error that does not
  itself rise to a `resync` frame. This is what lets `bh stream ... --format ndjson |
  jq ...` work without stderr noise corrupting the pipe.
- **Exit codes.** `0` = clean shutdown — the consumer closed its read end, or a bounded run
  completed; a `resync` frame is a normal, recoverable, expected-in-operation event and never
  causes a non-zero exit on its own. Non-zero = the stream hit a backend/adapter failure it
  could not recover from even via resync. Making shutdown leave no orphaned `bd` process for
  either exit path is bh-jksq.5's job (timeout/cancellation/SIGTERM/broken-pipe termination of
  the whole descendant tree) — this contract states the exit-code convention that bead's
  implementation must honor, it does not itself guarantee process-tree cleanup.
- Consequence for consumers: `bh stream` is the **only** process boundary a consumer needs —
  the epic's acceptance criterion "consumers can receive initial state plus changes without
  spawning bd directly" holds because nothing above ever asks a consumer to reach past `bh
  stream` into `bd`.

## 10. Open judgment calls, for the next reader

Recorded here rather than left implicit, per this doc's own house style:

1. **`removed` semantics** (identity leaving the scope, not "closed") was a judgment call —
   the alternative (a `removed` list also covering closes) was rejected because it would force
   every consumer to hold onto closed-issue state indefinitely to avoid mis-reading a close as
   a deletion.
2. **`resync` as a distinct control frame** rather than a `snapshot.reason` value was a
   judgment call, reasoned through in §5; the alternative is strictly simpler on the wire and
   worth revisiting if bh-jksq.6's contract tests find the two-frame handshake awkward to test
   against.
3. **`frame` instead of `type`** as the discriminator key name was a judgment call made to
   avoid a third meaning of "type" in an ecosystem that already has `issue_type` and bd gate
   `type`; flagged in §6 inline as well.
4. **No `metadata` pass-through** (§4) is the strictest reading of "bd records... must not
   become the public contract." If a concrete consumer need for an unmodeled bd metadata key
   shows up before bh-5wpb6 lands, adding it as its own named field is additive (§6) and does
   not require revisiting this document's structure.
