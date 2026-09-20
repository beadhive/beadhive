# `bh-ie41e.2`: typed one-hive `bd serve` adapter and failure taxonomy

## Question

Can Beadhive put a small typed client in front of one Beads 1.3.0 `bd serve` process without
weakening hive identity, schema, pagination, mutation, or failure semantics? Which failures may
fall back to the CLI, which must fail, and which leave a write indeterminate?

## Method

This is a research spike and changes no product code. I exercised the installed release binary
`bd version 1.3.0 (f45b249ce: HEAD@f45b249ce6b4)` against a new disposable proxied-server
workspace under `/tmp`. No managed hive database was used or mutated. The upstream source used
to interpret the observed wire responses was pinned at
`f45b249ce6b40ba62aecc03949e6371e8f7c79d8`; nothing was compiled.

The harness started loopback services on fixed disposable ports, negotiated context, walked a
two-page list, compared full and brief projections, created and conditionally updated isolated
rows, and injected:

- an invalid and an order-mismatched cursor;
- a stale revision and a revision with the wrong JSON type;
- missing/incorrect authentication and a refused `Host` header;
- an unsupported route;
- service process death and a client deadline;
- database process death while the HTTP process remained live; and
- `SIGTERM` shutdown.

The process and temporary databases were stopped and removed after the observations were
recorded. The only repository artifact is this document.

## Evidence

### 1. API and schema compatibility (highest impact)

The context handshake returned API `v0`, Beads `1.3.0`, backend `dolt`, HTTP schema version `1`,
the disposable repo/beads/database identity, a project UUID, and the 40 capabilities inventoried
by `bh-ie41e.1`. The HTTP `schema_version` is not the Dolt schema version and must never be used
as one.

The exact release source contains
`internal/httpapi/spec/openapi.v0.yaml`, 497,361 bytes with SHA-256
`9a33a349e5266bdba916246594a4fc457c63e39d7affb5acec9bd5b75acde4dd`. It declares 41
operations (including health and context). `project.enforce` is a negotiated middleware
capability rather than a separate operation. However, the installed release's help says the
wire contract is at `/v0`, while a real `GET /v0` returned:

```http
HTTP/1.1 404 Not Found
Content-Type: application/problem+json; charset=utf-8

{"code":"not_found","detail":"no such route on this server","status":404,"title":"Not Found",...}
```

Therefore runtime schema discovery does not work in this exact release. A generated client must
not be produced from memory, a nearby tag, or the JSON schema emitted by `bd schema` (which is
the issue-data schema, not this HTTP contract). The viable v1.3.0 boundary is a private client
generated or hand-written from a checked-in copy of this exact OpenAPI document, pinned by
release commit and digest. Startup must reject drift in API version or required response shape.

The project binding works and is worth using on every request. Sending an incorrect
`Bd-Project-Id` returned `400 invalid_argument`, `reason: project_mismatch`, and the server's
project ID; sending the negotiated ID succeeded. That turns accidental cross-hive reuse into a
typed refusal rather than a wrong-hive read or write.

The minimum negotiation sequence is:

1. `GET /healthz` proves only that an HTTP process answers.
2. Authenticate and `GET /v0/beads/context`; bind API version, Beads version, backend, repo root,
   beads directory, database, project ID, HTTP schema version, and capabilities to the client.
3. Require the operation-specific capability; never infer it from a neighbor.
4. Send the negotiated `Bd-Project-Id` on every operational request.
5. `GET /v0/beads/ready?limit=1` proves the database can serve a real query.

Context mismatch, authentication failure, and project mismatch are hard failures. They are not
reasons to run the same request through a less constrained transport.

### 2. Payload and downstream token reduction

A disposable issue carrying 20,000 bytes of free-form description/design/acceptance/notes was
listed once normally and once with `brief=true`:

| Projection | Response bytes |
|---|---:|
| Full | 20,340 |
| Brief | 275 |
| Saved | 20,065 (98.6%) |

At the intentionally rough four-bytes-per-token planning ratio, that is about 5,016 tokens
avoided for one synthetic text-heavy row. This does not predict the production corpus, but it
proves the reduction occurs before Beadhive parsing and agent serialization. The client should
make partiality explicit in its type because the wire response has no marker distinguishing an
omitted free-form field from an empty field. A consumer that needs those fields must fetch the
detail view.

List rows deliberately carry no `revision`; `GET /issues/{id}` does. That costs a detail read for
each row selected for guarded mutation, but prevents volatile revision tokens entering the
interchange row. The adapter must represent `IssueSummary` and `IssueDetail` as different types,
not default a missing list revision to `"0"` (which is a valid legacy token).

### 3. Fewer operations and atomic iteration speed

The persistent service removes process startup from each call, but the larger improvement is
where a server operation replaces a client-side composite:

| Candidate | Existing shape from `bh-ie41e.1` | Native shape | Iteration/correctness effect |
|---|---:|---:|---|
| issue guard/read | one `bd show` per guard | one `issues.get` | Same operation count; lower startup and typed detail. |
| work claim | `show`, `update --claim`, confirm `show` (3) | one `issues.claim` | Up to 67% fewer Beads calls; one transactional claim, subject to hook parity. |
| choose and claim | ready selection followed by claim | one `issues.claimNext` | Removes the selection/claim race as well as calls. |
| multi-field/label edit | one or more CLI field/label operations | one guarded `issues.update` | Title, fields, label add/remove/replace, metadata, and guards share one transaction. |
| create with graph edges | create followed by edge operations | one `issues.create` | Row, parent, dependencies, and waits-for either all land or none do. |
| group edits | per-member writes | one `issues.batchApply` | Fewer round trips with an all-or-nothing request; keep batch limits explicit. |

This ranking does **not** make writes the first adoption slice. HTTP mutations skip CLI hooks,
caller-supplied `actor` is provenance rather than authenticated identity, and Beadhive's gate,
state, merge-slot, swarm, sync, backup, and migration contracts remain absent. Reads are the
first safe slice. Atomic writes are a second slice only after parity tests and lifecycle
boundaries prove that skipping hooks is intentional.

### Wire fixtures

The exercised response shapes matched the pinned spec:

| Probe | Observed contract |
|---|---|
| context | `200 application/json`; identity plus 40 sorted capability strings |
| empty list/ready | `{"has_more":false,"items":[]}` |
| list page | `items`, `has_more:true`, opaque `next_cursor`; second page omitted the cursor and set `has_more:false` |
| invalid or wrong-order cursor | `400 invalid_cursor`; restart paging without the cursor |
| detail | complete issue plus decimal-string `revision` |
| guarded patch | `200 {issue,changed,revision}`; revision remained a decimal string and changed after the write |
| stale patch | `409 precondition_failed`, `param: expected_version`, echoed expected token; nothing written |
| numeric revision | `400 invalid_argument`, `param: expected_version`, `reason: invalid_value` |
| absent issue | `404 not_found` |
| unknown query parameter | `400 invalid_argument`, named parameter, `reason: unknown_parameter` |
| missing/wrong bearer token | `401 unauthenticated`, `WWW-Authenticate: Bearer`; no credential echo |
| refused host | `400 invalid_argument`, `param: Host` |
| project mismatch | `400 invalid_argument`, `reason: project_mismatch`, server project ID |
| database unavailable | `503 db_unavailable`, `Retry-After: 5` |
| unsupported `/v0/beads/gates` | `404 not_found`, `detail: no such route on this server` |

Every HTTP error observed was RFC 9457-style `application/problem+json` with `status`, `title`,
stable `code`, and `request_id`. Dispatch is on `code`, then the status class for unknown future
codes; prose in `detail` is diagnostic only.

### Pagination

Three same-second rows were traversed with `limit=2`: page one returned two rows and an opaque
v2 cursor; page two returned the remaining row with no repetition. Reusing that cursor under a
different sort returned `400 invalid_cursor`. The cursor pins an order and position, not a
snapshot, and does not encode filters. The client must therefore retain the complete immutable
query beside the cursor, repeat it byte-for-semantics on every page, and restart without a
cursor on `invalid_cursor`. A changed filter starts a new traversal.

An unlimited loopback list (`limit=0`) is valid, but the adapter should still impose an explicit
row/page budget. On non-loopback servers unlimited reads are refused. Cursor exhaustion is not
optional for any call replacing today's `--limit 0` behavior.

### Failure, retry, fallback, and idempotency taxonomy

| Failure | Read action | Mutation action | CLI fallback? |
|---|---|---|---|
| Connect refused before a connection | Retry within budget, then CLI if the negotiated operation permits it. | Safe to use CLI only when the transport proves no request was sent; otherwise classify as indeterminate. | Conditional. |
| Process dies / connection resets mid-response | Retry the idempotent read, then permitted CLI fallback. | **Indeterminate write**: reconcile by detail/event read before any replay. | No blind write fallback. |
| Client timeout | Cancel; retry a read within budget. The probe produced curl exit 28 and no HTTP body. | **Indeterminate write**, even with zero response bytes: the server may have committed. | No blind write fallback. |
| `503 busy` | Honor `Retry-After`; bounded retry. | Same, but a post-dispatch response still requires operation-specific idempotency. | Not by default. |
| `503 db_unavailable` | Honor `Retry-After`; surface outage after budget. CLI normally shares the same DB and is not a recovery path. | Do not replay. | No. |
| `400 invalid_cursor` | Restart the same traversal without a cursor. | N/A. | No. |
| `400 unknown_parameter` | Version-skew branch: remove only an optional understood optimization or use the operation's explicit CLI path. | Fail compatibility; do not silently weaken a write. | Read-only/explicit. |
| other `400` | Client/schema bug; fail loudly. | Final, nothing written. | No. |
| `401` or project/Host mismatch | Configuration or security failure. | Configuration or security failure. | No. |
| issue `404` | Final domain absence. | Final domain absence. | No. |
| route `404` despite negotiated capability | Contract violation; quarantine the client/server pair. | Contract violation. | No automatic replay. |
| `409 precondition_failed` | Re-read if the caller still wants the operation. | Re-read, re-decide, and compose a new guarded request; never retry the stale body. | No. |
| other known `409` domain refusal | Return typed refusal. | Return typed refusal; nothing written. | No. |
| unknown 4xx / 5xx | Unknown 4xx is a client/contract fault; unknown 5xx is a server fault with bounded retry policy. | Conservative indeterminate classification if dispatch/commit cannot be excluded. | No blind fallback. |

Idempotency is operation-specific:

- GETs are safe to retry, subject to page-query consistency.
- A same-value patch returns `200 changed:false`, but that does not make a timed-out patch safe to
  replay without reconciliation. A guarded replay after the first request committed generally
  sees a stale token and returns 409; re-reading distinguishes the resulting state.
- A create with a caller-chosen ID can reconcile an `already_exists` response against that row.
  A create with a server-minted ID must not be retried after an indeterminate outcome.
- Claim/release/close/reopen have their own replay fields and ownership rules; adopt each only
  after a contract test proves the exact same-actor and already-open/closed behavior Beadhive
  needs.
- Batch requests are all-or-nothing according to the pinned contract, but a lost response still
  requires reconciliation of every member before retry.

The adapter must not implement a broad "HTTP failed, run `bd`" catch block. Fallback is selected
before a mutation when negotiation says the operation is unsupported, or after a read failure
whose retry/fallback policy is explicit. Once a write may have reached the server, the result is
success, a typed final refusal, or indeterminate—never an automatic second transport.

### Readiness, connections, startup, and shutdown

Killing only the disposable Dolt child demonstrated the documented distinction:

```text
GET /healthz                         -> 200 {"status":"ok"}
GET /v0/beads/ready?limit=1         -> 503 db_unavailable, Retry-After: 5
```

The server logged `max_inflight=16`, `max_conns=64`, semaphore wait `10s`, request deadline
`1m`, pool maximum open connections `20`, and pool maximum idle connections `16`. One service
per hive therefore has meaningful database and process cost; a fleet adapter must budget and
measure it rather than starting a service per request or per agent. The benchmark bead owns the
fleet-shaped resource verdict.

A fixed port supplies single-bind exclusion; port `0` is appropriate only when the supervisor
captures stdout immediately and owns the child. The supervisor must start one service for one
negotiated hive context, wait for context plus database readiness, retain the process handle,
and invalidate the cached context on reconnect/restart.

Forced process death produced an immediate connection refusal on the next request. A separate
service paused with `SIGSTOP` caused a bounded 200 ms client timeout. `SIGTERM` logged
`shutdown_start drain_timeout=20s`, then `shutdown_complete`, and exited 0 even after the
database-outage probe. Beadhive should send `SIGTERM`, allow the advertised drain budget plus a
small supervisor margin, and only then escalate termination.

### Proposed private typed interface

The public boundary should expose domain-shaped results rather than raw HTTP or generic JSON:

```text
negotiate(ExpectedHive, RequiredContract) -> NegotiatedService
NegotiatedService.get_issue(IssueId) -> IssueDetail
NegotiatedService.list_issues(ListQuery, PageCursor?) -> Page[IssueSummary]
NegotiatedService.list_ready(ReadyQuery) -> ReadyPage
NegotiatedService.update_issue(IssueId, IssuePatch, ExpectedRevision, TrustedActor)
    -> MutationResult[IssueDetail]
NegotiatedService.claim_next(ClaimFilter, TrustedActor) -> MutationResult[IssueDetail?]
```

`IssueSummary` cannot supply a revision. `IssueDetail.revision`, `ExpectedRevision`, cursor,
project ID, capability name, and actor should be distinct opaque/string types so a generic
string cannot silently cross those boundaries. `TrustedActor` is derived from the Beadhive seat
inside the host daemon; downstream callers cannot provide arbitrary provenance.

The result algebra should be closed on the cases the caller must handle:

```text
Success(value)
DomainRefusal(code, typed fields, request_id)
IncompatibleServer(reason)
RetryableUnavailable(code, retry_after, request_id)
IndeterminateMutation(operation, issue_ids, request_id?)
TransportFailure(before_dispatch: bool)
```

The transport layer owns auth, `Bd-Project-Id`, request IDs, deadlines, bounded retries,
problem decoding, and telemetry. The domain adapter owns capability policy, pagination
exhaustion, partial/full projection types, CLI fallback selection, hook policy, and
reconciliation. This split keeps an HTTP library from deciding lifecycle semantics.

## Verdict (GO, narrow)

**GO** for a private, one-hive, read-first typed adapter backed by the exact pinned v1.3.0
contract. The service provides stable identity, narrow projections, typed problems, cursors,
revision guards, and genuinely atomic operations that can reduce tokens and later collapse
multi-call races.

**NO-GO** for generated-at-runtime clients, transparent wholesale subprocess replacement, or
automatic HTTP-to-CLI mutation fallback. `/v0` does not serve the advertised OpenAPI document,
HTTP writes skip hooks, actor is asserted provenance, and important Beadhive coordination
surfaces remain CLI-only.

## Recommendation

Proceed in this impact order:

1. Check in and digest-pin the exact v1.3.0 OpenAPI contract; generate/validate separate summary,
   detail, page, problem, and mutation-result models; add fixture tests for every response above.
2. Adopt negotiated `issues.get`, bounded `issues.list?brief=true`, ready/count, and dependency
   reads where the consumer needs only narrow typed fields. Measure serialized bytes and agent
   tokens in the fleet benchmark.
3. Add operation-level telemetry and explicit read-only CLI fallback. Record transport,
   capability, page count, bytes, fallback reason, request ID, and context identity.
4. Spike `issues.claimNext`, guarded `issues.update`, atomic create-with-edges, and batch apply as
   separate lifecycle changes. Each must prove hook, actor, event, replay, and reconciliation
   parity before replacing a CLI composite.
5. Keep gates, state dimensions, merge slots, swarms, sync/publication, backup, migration, and
   destructive maintenance on their existing explicit CLI/admin paths.

The quiesced benchmark should compare not only latency and process count, but also payload bytes,
pages, database connections, retries, subprocess fallbacks, and indeterminate outcomes. Atomic
operation count is the iteration-speed metric; raw HTTP latency alone is not the adoption case.
