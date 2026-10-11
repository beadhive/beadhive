# ADR: HQ storage port, unknown-outcome contract, Dolt-only capabilities and test strategy

- **Status:** Accepted (operator sign-off on the hypothesis licence, 2026-10-11)
- **Date:** 2026-10-11
- **Decision bead:** `bh-rlphq`. **Epic:** `bh-xxkf7` (HQ storage port with a
  contract-verified in-memory fake and a Dolt adapter package).
- **Inputs:** the 2026-10-11 storage-seam survey recorded in `bh-xxkf7`;
  [git-hq-authority.md](git-hq-authority.md);
  [dolt-first-hq-config-cutover-adr.md](dolt-first-hq-config-cutover-adr.md);
  [hive-writer-partitioning-adr.md](hive-writer-partitioning-adr.md);
  [frame-dolt-server-hq-mode-adr.md](frame-dolt-server-hq-mode-adr.md);
  [hq-trust-profiles-and-capabilities-adr.md](hq-trust-profiles-and-capabilities-adr.md);
  the superseded [bead-backend-abstraction.md](bead-backend-abstraction.md);
  [package-class-library-vs-plugin-adr.md](package-class-library-vs-plugin-adr.md) and
  `scripts/check_package_imports.py`.
- **Supersedes:** nothing. Every invariant in the input ADRs stands. This record changes where
  storage code lives and how it is tested, not what the fence, placement or authority protocols
  guarantee.

## Context

HQ, fence and frame code reach durable storage directly. The survey counted about 250 raw SQL
`execute` sites. The largest are `hq_sql_runtime` (50), `hq_sql_config` (49),
`hq_sql_receiver` (40), `hq_sql_operator` (29) and `hq_sql_placement` (12), followed by
`hq_sql_session*`, `fence_data` (22), `fence_orphan` (12), `fence_audit` (10), `fence_cutover`
(7) and `hive_forward` (16). The survey also found:

- about 55 lines that call Dolt procedures or system tables (`DOLT_HASHOF`, `DOLT_COMMIT`,
  `DOLT_MERGE`, `DOLT_RESET`, `dolt_log`, `dolt_history_*`, `dolt_commit_ancestors`,
  `DOLT_PREVIEW_MERGE_CONFLICTS_SUMMARY`);
- about 15 `bd` or `dolt` subprocess sites;
- about 40 git ref and remote calls.

The partial ports that exist are bypassed or leak storage detail:

- `HqControlPlane` (`src/beadhive/hq_control_plane.py`) is a facade with `GitControlPlane` and
  `SqlControlPlane` implementations. Below it, every `hq_sql_*` class owns its SQL strings over
  a PyMySQL connection opened by `hq_sql_transport.connect`.
- `FenceData`, `PlacementAuthority`, `EpochRef` and `BumpReclaim` (`src/beadhive/writer_adopt.py`)
  are real ports, but `FenceData.commit_bump(statements: Sequence[str], message)` passes SQL
  text through the port, and `BumpReclaim.plan` returns statements that ride in the bump.
- Test fakes work at the SQL-string level (`FakeConnection` / `FakeCursor` in
  `tests/test_hq_sql_placement.py`). They are brittle, and there is no in-memory store with
  real semantics.
- Session staleness is computed from Dolt's `UTC_TIMESTAMP(6)`, in a trigger
  (`hq_sql_session._stamp_trigger`) and in `TIMESTAMPDIFF` reads (`hq_sql_session`,
  `failover_observer.sql_session_staleness`). No in-process tool can fake that clock, so
  staleness tests either wait on real time or do not exist.

On fence- or HQ-touching changes the integration segment takes 12.7 minutes of wall time and is
bound by Dolt slots. Of the 14 most expensive test files (about 6,500 of 8,487 test-seconds),
about 1,750 s are storage-adapter contracts that must stay on real Dolt. About 2,375 s are
protocol behaviour (fencing decisions, epoch, lease and placement logic, refusal paths,
failover ordering) that could run against a faithful fake. About 2,400 s are genuine
end-to-end smoke tests.

### Scope

This port covers **HQ storage and fence storage only**:

- **HQ storage:** the central HQ records. That means placement, failover policy, authority
  grants and renewals, config publications, inbox, receipt and report rows, and frame sessions.
- **Fence storage:** the fence tables inside each hive's Dolt data (`bh_writer`,
  `bh_epoch_live`, `bh_write_mark`, `bh_local_ident` and the `bh_*` guard triggers), plus the
  legacy `refs/bh/epoch` ref on the hive remote.

It is **not** a bead-data abstraction. Beads and Dolt are endpoint topologies of the official
Beads API, not Beadhive storage providers
([beads-api-first-parallel-replacement-adr.md](beads-api-first-parallel-replacement-adr.md)
superseded [bead-backend-abstraction.md](bead-backend-abstraction.md)). Issue rows keep moving
only through supported Beads operations. The one exception is the failover reclaim (see
[Epoch and fence bump](#epoch-and-fence-bump)): condition 7 of the partitioning ADR requires it
to share the bump commit, so the port carries it as a typed set of bead ids, never as SQL.

## Decision

### 1. Shape: a backend-neutral port, a fake, and a Dolt adapter package

We adopt the survey's hybrid option C:

| Package | Import name | Holds |
|---|---|---|
| `packages/beadhive-hq-storage` | `beadhive_hq_storage` | The port protocols, the value and outcome types, the error types, the in-memory fake with an injected clock, and the reusable contract scenarios and state machine. |
| `packages/beadhive-hq-storage-dolt` | `beadhive_hq_storage_dolt` | The Dolt adapter for the generic port and the optional Dolt-only capability interfaces. All SQL strings, Dolt procedure calls and error-code classification live here. |

Root `src/beadhive` keeps the domain logic (signatures, eligibility, adopt ordering, failover
policy, reclaim planning, receiver validation) and the composition that binds an adapter. A git
adapter for the generic HQ port is out of scope, but nothing in the port may preclude it (see
[section 5](#5-git-adapter-compatibility)).

The port is a set of small protocols, not one wide interface. A consumer depends only on the
protocols it uses, and a fake or adapter can satisfy them one at a time.

### 2. Operations

All types below live in `beadhive_hq_storage`. The signatures are normative for `bh-9fg5k`;
parameter names may be refined there, but the outcome types and their meaning may not.

#### Common types

```python
Revision = NewType("Revision", str)        # opaque, adapter-minted; compare by equality only
OperationId = NewType("OperationId", str)  # caller-minted canonical UUID text, one per intent

class Absent:                              # expectation "the record must not exist yet"
    ...
ABSENT: Final[Absent]

JsonValue = None | bool | int | float | str | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
Body = Mapping[str, JsonValue]             # canonical JSON; adapter enforces a size cap

class RecordKind(StrEnum):                 # closed; a new kind is a port change + contract cases
    PLACEMENT = "placement"                # hq_live_hive_leases | refs/bh/lease/<prefix>
    FAILOVER_POLICY = "failover_policy"    # hq_live_failover_policy
    AUTHORITY = "authority"                # frame grant / renewal / lifecycle record

class StreamKind(StrEnum):                 # append-only rows
    INBOX = "inbox"                        # per-principal signed proposals and beats
    RECEIPT = "receipt"                    # trusted first-seen receipts and floors
    REPORT = "report"                      # frame reports and evidence

class LivenessKind(StrEnum):
    SESSION = "session"                    # frame_<principal>_<epoch>_session
    EVIDENCE = "evidence"                  # measured evidence rows

@dataclass(frozen=True)
class Versioned(Generic[T]):
    key: str
    revision: Revision
    body: T
```

#### Outcome types

A mutation never reports "probably". It returns exactly one of three outcomes. A transport
failure that happens **before** anything is sent raises an exception instead, because then the
mutation is known not to have happened.

```python
@dataclass(frozen=True)
class Applied:
    revision: Revision                     # the new revision (or head) the store issued
    operation: OperationId

@dataclass(frozen=True)
class Conflict:
    current: Versioned[Body] | None        # what the store holds now; None when absent

@dataclass(frozen=True)
class Unknown:
    operation: OperationId
    expected: Revision | Absent            # the ORIGINAL expectation, never a refreshed one
    reason: Literal["deadline", "transport", "commit"]

Outcome = Applied | Conflict | Unknown
```

`Unknown.reason` keeps the three causes `HqLeaseUnknown` already distinguishes. `"deadline"`
means the operation budget ran out while awaiting the result. `"transport"` means the result
read failed after the request may have committed. `"commit"` means the commit acknowledgment
itself was lost.

#### Errors

```python
class StorageError(Exception): ...                   # base; message never carries a secret
class StorageUnavailable(StorageError): ...          # nothing was sent; definitely not applied
class NotProvisioned(StorageError): ...              # schema/row/table the operation needs is absent
class CapabilityUnsupported(StorageError): ...       # adapter refuses an optional capability
class InvalidRequest(StorageError): ...              # malformed key/body/id; refused before I/O
class IntegrityViolation(StorageError): ...          # store state breaks a port invariant
```

`IntegrityViolation` covers cases such as a second `bh_writer` row or a regressed epoch. It
fails closed and is never retried.

A serialization failure at commit (MySQL `1213`) or `rowcount 0` on a CAS is a `Conflict`, not
an error. This is condition 5 of the partitioning ADR. The Dolt adapter classifies driver errors
by their DB-API `args[0]` code and never imports PyMySQL to do it.

#### Versioned records (compare-and-set)

```python
class VersionedRecords(Protocol):
    def read(self, kind: RecordKind, key: str) -> Versioned[Body] | None: ...
    def read_many(
        self, kind: RecordKind, keys: Collection[str] | None = None
    ) -> Sequence[Versioned[Body]]: ...
    def compare_and_set(
        self,
        kind: RecordKind,
        key: str,
        body: Body,
        *,
        expected: Revision | Absent,
        operation: OperationId,
    ) -> Outcome: ...
    def lookup(self, kind: RecordKind, key: str, operation: OperationId) -> LookupOutcome: ...
```

These rules are part of the contract:

- **Every successful CAS issues a fresh revision.** The adapter mints it and the caller never
  computes one (condition 5; a write that kept its token could commit silently over a
  competing CAS). The Dolt adapter keeps the receiver-compatible
  `sha256(record || uuid)` 64-hex format for `PLACEMENT`, so the trusted receiver and directors
  can keep taking over each other's rows.
- **Wire quirks belong to the adapter.** Today the placement cause rides in the `request_id`
  column as `cause_token`, and the director witness rides in `request_sha256` as
  `placement_witness`. These become fields the adapter encodes and decodes. The port body
  carries `cause` as a plain value.
- **Some kinds are UPDATE-only.** `PLACEMENT` CAS from `ABSENT` raises `NotProvisioned`, because
  operator provisioning seeds a never-placed hive and the director credential never inserts.
  The fake models the same rule, so a protocol test cannot depend on behaviour real Dolt
  refuses.

`PlacementAuthority` (read plus CAS of `(frame, epoch)`) is a typed view over `PLACEMENT`. See
[section 6](#6-mapping-of-the-existing-ports).

#### Unknown outcome and its lookup

```python
@dataclass(frozen=True)
class Confirmed:
    operation: OperationId
    revision: Revision                     # where the witness is visible
    recorded_at: datetime                  # store-clock stamp of the confirmed write

@dataclass(frozen=True)
class NotConfirmed:
    operation: OperationId

LookupOutcome = Confirmed | NotConfirmed
```

Every mutation carries a caller-minted, immutable `OperationId`, and the store persists it as
the mutation's **witness**:

| Mutation | Where the witness lives |
|---|---|
| Placement row | its request id column |
| Config publication | its publication UUID |
| Hive-fence bump | its `adopt-<epoch>` sentinel mark (condition 1) |
| Appended row | its natural key, for example `(frame, incarnation, sequence)` |

The contract has four rules:

1. On `Unknown`, the caller calls `lookup` with the **same** `OperationId`. `Confirmed` means
   the mutation is applied. Its revision may already be superseded by a later writer, so it is
   not a snapshot.
2. `NotConfirmed` means only "no confirmed outcome is visible now". It is **not** proof that the
   mutation did not apply. On a dolt-ignored live table such as placement, a later CAS overwrites
   the witness, so an applied-then-superseded write also reads as `NotConfirmed`.
3. `NotConfirmed` never authorizes reissuing the same mutation against the original
   expectation, and never authorizes minting a fresh `OperationId` for the same intent against a
   refreshed parent. A consumer may start a **new decision** from a fresh read, as today's
   `PlacementUnknown` ("read back, never re-issue") already does.
4. Retry policy belongs to the consumer, not the port. The strictest policy is recorded here so
   that `bh-x7xtc` preserves it: config publication follows `recover_publication`. Absence never
   authorizes a new UUID or a refreshed-parent retry, and initial seeding into an empty schema
   stays a separate `bh-4shu3` contract.

The fake must be able to produce `Unknown` on demand, both with the write applied and with it
not applied, so that both branches of every consumer are testable without real Dolt.

#### Append-only rows with retention

```python
class AppendLog(Protocol):
    def append(
        self, stream: StreamKind, key: str, body: Body, *, operation: OperationId
    ) -> Outcome: ...                      # idempotent: same key+operation -> same Applied
    def scan(
        self, stream: StreamKind, *, key: str | None = None,
        after: Revision | None = None, limit: int,
    ) -> Sequence[Versioned[Body]]: ...
    def prune(
        self, stream: StreamKind, *, older_than: timedelta, operation: OperationId
    ) -> PruneReport: ...                  # age measured by the STORE clock
```

The store stamps the time an entry was recorded. The caller cannot supply it ("runtime keys
cannot manufacture receipt timestamps", [git-hq-authority.md](git-hq-authority.md)). A second
append of the same key with a different body is a `Conflict`; this is how replay is detected.

#### Liveness rows, `now()` and staleness

```python
class StorageClock(Protocol):
    def now(self) -> datetime: ...         # timezone-aware UTC

class Liveness(Protocol):
    def now(self) -> datetime: ...         # the store's own clock
    def renew(self, kind: LivenessKind, key: str, body: Body | None = None) -> datetime: ...
    def ages(
        self, kind: LivenessKind, keys: Collection[str] | None = None
    ) -> Mapping[str, timedelta]: ...      # computed in ONE read against the store clock
    def stamped_at(self, kind: LivenessKind, key: str) -> datetime | None: ...
```

See [section 3](#3-now-and-staleness-ownership) for who owns the clock.

#### Committed history (config publication, read at revision, sync)

```python
@dataclass(frozen=True)
class ChangeSet:
    documents: Sequence[tuple[str, bytes]] # ordered canonical raw documents
    tables: frozenset[str]                 # explicit staging allowlist, never "add everything"

@dataclass(frozen=True)
class ConsistencyToken:                    # maps onto HqConsistencyToken
    backend_identity: str
    generation: str
    revision: Revision

@dataclass(frozen=True)
class SyncReport:
    head: Revision
    ahead: int
    behind: int
    diverged: bool

class CommittedHistory(Protocol):
    def head(self) -> Revision: ...        # replaces SELECT DOLT_HASHOF('HEAD')
    def read_at(self, revision: Revision) -> Sequence[tuple[str, bytes]]: ...
    def consistent_read(self) -> AbstractContextManager[ConsistencyToken]: ...
    def publish(
        self, changes: ChangeSet, *, expected_head: Revision | Absent,
        operation: OperationId, author: str, message: str,
    ) -> Outcome: ...                      # Applied.revision is the exact committed hash
    def lookup_publication(
        self, operation: OperationId, *, expected_head: Revision
    ) -> LookupOutcome: ...
    def fetch(self) -> SyncReport: ...
    def push(self) -> Outcome: ...         # non-fast-forward -> Conflict
```

`consistent_read` exists for the combined config and authority read
(`load_config_authority_snapshot`). An adapter that cannot provide one atomic read raises
`CapabilityUnsupported`; it never joins independent reads. This preserves the current Git
binding's explicit refusal.

#### Epoch and fence bump

```python
@dataclass(frozen=True)
class WriterRow:          frame: str; epoch: int; revision: str = ""
@dataclass(frozen=True)
class PlacementView:      frame: str; epoch: int; token: Revision
@dataclass(frozen=True)
class RefView:            frame: str; epoch: int; sha: Revision
@dataclass(frozen=True)
class EpochFloor:
    history_max: int                       # highest bh_writer.epoch ever committed on main
    writer: int | None                     # live bh_writer.epoch (None: not cut over)
@dataclass(frozen=True)
class ReclaimSet:
    dead_frame: str
    bead_ids: frozenset[str]               # in_progress beads to revert inside the bump
@dataclass(frozen=True)
class BumpRequest:
    frame: str; epoch: int; revision: str  # adopt-<epoch> sentinel is implied
    operation: OperationId
    reclaim: ReclaimSet | None = None
@dataclass(frozen=True)
class GuardReport:        expected: int; present: int; missing: tuple[str, ...]

class HiveFence(Protocol):
    def sync_to_remote(self) -> SyncReport: ...            # StorageUnavailable when unreachable
    def writer(self) -> WriterRow | None: ...
    def remote_writer(self) -> WriterRow | None: ...
    def epoch_floor(self) -> EpochFloor: ...
    def guard_conformance(self) -> GuardReport: ...
    def commit_bump(self, bump: BumpRequest) -> Revision: ...  # ONE local commit
    def push(self) -> Outcome: ...                         # non-fast-forward -> Conflict
    def lookup_bump(self, frame: str, epoch: int) -> LookupOutcome: ...

class EpochRefStore(Protocol):                             # legacy refs/bh/epoch
    def read(self) -> RefView | None: ...
    def compare_and_set(self, frame: str, epoch: int, *, expected: RefView) -> Outcome: ...

class PlacementStore(Protocol):                            # typed view over PLACEMENT
    def read(self, prefix: str) -> PlacementView | None: ...
    def compare_and_set(
        self, prefix: str, frame: str, epoch: int, *,
        expected: PlacementView, cause: str, operation: OperationId,
    ) -> Outcome: ...
```

The epoch to bump to stays a domain decision in `writer_adopt`: the maximum of
`EpochFloor.history_max`, `EpochFloor.writer`, `refs/bh/epoch` and placement, plus one. The
port exposes the reads; it does not choose the epoch.

`commit_bump` writes the `bh_writer` singleton, the `adopt-<epoch>` sentinel and, when present,
the `ReclaimSet` reverts, all as **one** commit (conditions 1, 2 and 7). Then `push` publishes
it, and `lookup_bump` finds the sentinel on the remote head. `HiveFence` has exactly one
production adapter, Beads' Dolt endpoint (embedded or server), plus the fake. It is generic only
so that adopt and guard logic can be tested on the fake. Its signatures still contain no SQL.

#### Capability access

```python
class CapabilityHost(Protocol):
    def capability(self, kind: type[C]) -> C | None: ...
```

An adapter returns `None` for any capability it lacks. A consumer that requires a capability
raises `CapabilityUnsupported` itself. The generic protocols never include Dolt-only operations.

### 3. `now()` and staleness ownership

- **The adapter owns the clock that staleness is measured by.** `Liveness.now()`,
  `Liveness.ages()`, `AppendLog.prune(older_than=...)` and every recorded-at stamp use the
  store's clock, never the caller's. This keeps today's guarantee that a frame cannot freshen
  its own session: the `renewed_at` trigger stamps `UTC_TIMESTAMP(6)` on the server.
- **The Dolt adapter uses server time.** `now()` reads `UTC_TIMESTAMP(6)`. `ages()` computes
  `TIMESTAMPDIFF(MICROSECOND, renewed_at, UTC_TIMESTAMP(6))` inside one statement, so
  client/server skew never enters the result. The adapter keeps the existing Dolt 2.3.5
  workaround for fractional-second interval arithmetic (`hq_sql_session.py`). The trigger stays:
  the stamp is applied by the server even for a principal that writes the table directly.
- **The fake owns an injected clock.** `InMemoryHqStorage(clock: StorageClock)` stamps renewals
  and appends from `clock.now()`. `tests/harness/clock.FakeClock` satisfies `StorageClock`
  structurally, as does any `StateClock`. The port package defines its own protocol and never
  imports `tests` or `beadhive`. Tests advance the clock to cross `failover_after`,
  heartbeat-TTL and retention thresholds deterministically, with no sleeps (see
  `docs/TEST_PROCESS_POLICY.md`).
- **Thresholds stay with the domain.** The port returns ages, and the consumer compares them
  with policy (`failover_after`, lease TTL plus skew, retention margin). Policy values remain
  where they live today (`hq_live_failover_policy`, `LivenessPolicy`, `hq.sql.inbox_retention_s`).
- **A git adapter has no server clock.** Its store clock is the independent operator observer's
  receipt clock (durable first-seen receipts, [git-hq-authority.md](git-hq-authority.md)). The
  port allows this because `now()` belongs to the adapter.

### 4. Generic versus Dolt-only capabilities

| Need | Where | Why |
|---|---|---|
| Versioned record read and CAS (placement, failover policy, authority grant and renewal) | generic `VersionedRecords` | Git has an exact-sha equivalent (`--force-with-lease`, signed atomic updates). |
| Unknown outcome and lookup by witness | generic | Git has immutable witness refs; Dolt has request id and publication UUID columns. |
| Append-only inbox, receipt, report rows with store-clock retention | generic `AppendLog` | Git keeps receipts in the authority commit. |
| Liveness renew and ages | generic `Liveness` | Each adapter's clock is described in section 3. |
| Head probe, publish change set, read at revision, fetch and push with ahead/behind/diverged | generic `CommittedHistory` | Git: commits and the `bh-config` head; Dolt: `DOLT_COMMIT` and `DOLT_HASHOF`. |
| Consistent combined read | generic, may raise `CapabilityUnsupported` | Git refuses it today, by design. |
| Epoch floor, writer row, bump, sentinel lookup | generic `HiveFence` | Production is Dolt only; the fake carries the protocol tests. |
| `refs/bh/epoch` read and CAS | generic `EpochRefStore` | Implemented over `gitref` in root; frozen as a floor at Φ4 and removed by D2. |
| Commit-graph walks (`dolt_log`, `dolt_commit_ancestors`), `dolt_history_*` reads for the I3 late-write check | Dolt-only `DoltHistory` | `fence_audit`. |
| Merge with conflict preview, orphan divert and merge, `DOLT_RESET --hard` | Dolt-only `DoltMerge` | `fence_orphan`, `fence_cutover`, the server-mode reset. |
| MySQL users and grants, table-scoped forwarder grants, session table and account provisioning, trigger and grant conformance | Dolt-only `DoltAccounts` | `hive_forward`, `hq_sql_session_provision`, `check_grants`, `check_triggers`. |
| Frame-private data ref `refs/dolt/frame/<id>` | Dolt-only `DoltFramePrivateRefs`, **provisional** | See below. |

`DoltHistory.table_history` accepts only a closed `FenceTable` enum (`bh_writer`,
`bh_write_mark`, `bh_epoch_live`). The capability cannot be used to read bead history, which
keeps the bead-data boundary mechanical rather than a matter of convention.

**`refs/dolt/frame/<id>` stays provisional.** The ref exists only in spike documents
(`bh-jbb6r` E8, `bh-sieai`) and as the `open` write policy's mechanism in the trust-profiles ADR
(section 4). No product code pushes it yet, and the spike found `DOLT_FETCH` through a
`--ref` remote needs a trailing-slash workaround. The capability is therefore declared but not
implemented, has no contract cases, and its shape may change without an ADR amendment. No bead
in `bh-xxkf7` owns it; the trust-profiles implementation molecule decides it when it builds
`open`.

### 5. Git adapter compatibility

The generic port must stay implementable over the protected Git HQ in
[git-hq-authority.md](git-hq-authority.md). Its constraints become port rules:

- **Mode defaults to Git; unsupported modes fail explicitly.** Root composition selects an
  adapter from the existing `hq.mode` and `hq.sql.enabled` switches. A failed SQL adapter never
  falls back to Git (frame-dolt-server-hq-mode ADR).
- **Exact expected revisions.** `Revision` is opaque and compared only by equality, so a git
  commit or object sha works as well as a Dolt hash or the receiver's 64-hex token. No port
  operation takes "latest" as an expectation.
- **Signed atomic updates.** Signing is not a port concern. Callers sign bodies in root, and a
  git adapter signs commits with the key bound at construction. Nothing in the port forces
  unsigned writes. A CAS that touches head and witness together (`bh-config` plus
  `config-witness`) is one `publish` or `compare_and_set` call, so the adapter can make it one
  atomic push.
- **No Dolt-only operation is required by a generic consumer.** Each consumer in `bh-3kl1k`'s
  set reaches Dolt-only behaviour through `capability(...)`, and on Git it gets
  `CapabilityUnsupported`.
- **Witness and generation.** `ConsistencyToken` carries `backend_identity` and `generation`,
  matching `HqConsistencyToken`. A git adapter fills them from the recovery generation.

### 6. Mapping of the existing ports

| Existing seam | Decision | Detail |
|---|---|---|
| `HqControlPlane` (`GitControlPlane`, `SqlControlPlane`) | **Wrap** | It stays the application-level facade: signatures, eligibility, admission, lifecycle, git broker custody. `SqlControlPlane` is rebuilt over the port protocols instead of `hq_sql_*` SQL. `GitControlPlane` is unchanged in this epic. Port `Unknown` maps to `HqLeaseUnknown` and `PublicationUnknown`; `ConsistencyToken` maps to `HqConsistencyToken`; `ConfigAuthoritySnapshot` stays a root domain type. |
| `FenceData` | **Replace** | `HiveFence` replaces it, and `commit_bump(statements, message)` becomes `commit_bump(BumpRequest)` (`bh-jhgum`). `FenceNode` becomes the root composition of the Dolt adapter over the existing `DoltCliEngine` / `BdServerEngine`. `fence_data_port`'s resolver keeps its contract and answers a `HiveFence` or `None`. |
| `PlacementAuthority` | **Absorb** | It is exactly `PlacementStore`. `writer_adopt` imports the port protocol; the old name stays as an alias until `bh-sqfvq` lands and is then removed. `PlacementLost` is raised by the domain on `Conflict`. |
| `EpochRef` | **Absorb** | `EpochRefStore`, with its git implementation over `gitref` kept in root. `EpochRefLost` is raised by the domain on `Conflict`. |
| `BumpReclaim` | **Wrap** | The reclaim planning stays in root (`failover_reclaim`), but `plan()` returns a `ReclaimSet` instead of SQL statements. `after_landed` and `with_kind` are unchanged. |
| `WriterRow`, `PlacementView`, `RefView` | **Move** | These are pure records, so they move into the port package. `writer_adopt` re-exports them. |
| `hq_sql_transport.connect` | **Keep in root, inject** | See section 7. |

### 7. Package and import plan

Both packages are **library** packages under
[package-class-library-vs-plugin-adr.md](package-class-library-vs-plugin-adr.md). They carry no
`plugin.json`, and `scripts/check_package_imports.py` classifies them from that alone. These
rules follow:

- **`beadhive-hq-storage` imports nothing from `beadhive`** and has no third-party runtime
  dependency (stdlib only). Its `__init__` `__all__` lists the protocols, types, errors and
  `InMemoryHqStorage` / `InMemoryHiveFence` / `InMemoryEpochRef`. The contract scenarios live in
  a `beadhive_hq_storage.contract` submodule with its own `__all__`. It imports `pytest` and
  `hypothesis` only inside the functions that build fixtures and the state machine, never at
  module scope. Neither becomes a runtime dependency (see [section 8](#8-test-strategy) and the
  [licence decision](#licence-decision-hypothesis)).
- **`beadhive-hq-storage-dolt` imports `beadhive_hq_storage` only through its `__all__`** and
  nothing from `beadhive`. It never imports PyMySQL. It takes a DB-API connection factory
  `Callable[[Role], Connection]` for HQ and a statement-engine protocol for hive fences, both
  injected by root. It owns every SQL string, Dolt procedure call and error-code
  classification for the migrated consumers.
- **Transport stays in root.** `hq_sql_transport.connect` depends on root's
  `HqSqlConnection` settings model and the fnox `SecretBroker`. Root composition passes a
  factory that closes over it, so the bounded deadline, no reconnect and verified TLS behaviour
  are unchanged and PyMySQL stays lazily imported in root. Whether transport later moves into
  the Dolt package is open (owner `bh-br1cd`).
- **Domain types.** Pure fence records move into the port package (section 6). Placement
  bodies are canonical JSON with a five-field `lease` map, so root keeps `HostLease` and its
  validation and converts at the edge. Settings models (`HqSqlConnection`, `LivenessPolicy`)
  stay in root. A type is duplicated in the port package only deliberately, with a comment
  naming the root original.
- **Root imports both packages through `__all__`.** Root tests may not patch either package's
  private names; they substitute the fake at the port boundary instead (bh-qdezo.9 rule).
- **Gate wiring** (`bh-wnhkn`) mirrors `beadhive-bd-cli`:
  - a new attest key `hq-storage`;
  - an `hq-storage-check` recipe (lint, format, hermetic `uv sync --package`, both packages'
    tests) and `attest-hq-storage`;
  - entries in `check-all-native` and `check-all-pants`;
  - exclusion from the shared `packages-check` lists (justfile around lines 814 to 837);
  - a `scripts/check_attest_catalog.py` entry;
  - workspace dependency-group and wheel-vendoring entries, so the published `bh` wheel carries
    both import packages, as it does for `beadhive-core`.

### 8. Test strategy

**Contract suite.** `beadhive_hq_storage.contract` defines the scenarios once, as functions of
a store factory. They cover:

- CAS fresh-revision, conflict and `ABSENT` rules, including UPDATE-only kinds;
- idempotent appends and replay conflicts;
- every `Unknown` branch with `lookup`;
- publication exact-hash readback and `lookup_publication`;
- bump atomicity with sentinel and reclaim;
- sync and push conflict reporting;
- `CapabilityUnsupported` for missing capabilities.

The suite runs in the `tests/contracts/` pattern, with one test body parametrized over an
`adapter` fixture with the ids `fake` and `dolt`:

- `packages/beadhive-hq-storage/tests`: the `fake` parameter only, fully hermetic.
- `packages/beadhive-hq-storage-dolt/tests`: the parametrized `[fake, dolt]` suite. The `dolt`
  parameter self-skips without a `dolt` binary, like `beadhive-bd-cli`'s real tests, and takes
  a Dolt slot. It reuses the rewindable shared clusters from `bh-ouo62` and resets between
  cases instead of provisioning a server per test.
- `tests/contracts/test_hq_storage_composition.py` in root: a thin check that root composition
  binds adapters satisfying the protocols. It does not run the full suite a third time.

Staleness cases use the clock only on the fake. On Dolt they assert ordering, not elapsed time:
`stamped_at` lies between `now()` read before and after `renew`, and ages are non-negative. No
contract case sleeps or depends on wall-clock timing.

**Differential state machine.** A hypothesis `RuleBasedStateMachine`
(`beadhive_hq_storage.contract.build_state_machine(make_a, make_b)`) drives the fake and the
Dolt adapter with the same command sequence. The commands are CAS with a fresh or stale
expectation, append, prune, renew, publish, bump, push, and injected unknown outcomes. After
every step it compares observable state: outcome kinds, record bodies, the revision-equality
pattern (not raw revision text, which differs by design), witness lookups and head movement.
Clock-dependent fields are projected out. The `advance_clock` rule exists only when both sides
are fakes, which also gives a fast fake-against-fake self-check of the machine.

**Profiles.** These are registered in the packages' `conftest.py`:

| Profile | Where | Settings |
|---|---|---|
| `gate` | every `hq-storage-check` and integration run | small budget (about 25 examples, 20 steps), `derandomize=True`, `database=None`, `deadline=None`, `suppress_health_check=[too_slow]`; for the Dolt-backed machine, `phases` exclude shrinking |
| `nightly` | the scheduled lane | about 10 to 20 times the budget, `derandomize=True`, shrinking enabled |
| `dev` | local default | hypothesis defaults, local example database allowed |

Derandomized runs make every gate result reproducible from the tree alone. Exploring new
sequences is the nightly lane's job, by budget, not by seed churn.

**Risk: shrinking against real Dolt is slow.** Every replayed step is a real SQL round trip,
plus a reset per example, and a shrink can replay hundreds of sequences. Mitigations:

- the gate skips the shrink phase on the Dolt-backed machine and reports the unshrunk failing
  sequence and seed;
- the nightly profile shrinks, under the watchdog's existing timeout;
- each minimal failing sequence is checked in as an explicit regression case in the contract
  suite, so it then runs on both adapters as an ordinary fast test;
- the machine runs on the integration lane under the existing `BH_DOLT_SLOTS` throttle (never
  raised), never in the unit lane.

If nightly shrink time is still prohibitive, `bh-br1cd` may cap the per-run wall budget. It may
not weaken the comparison.

**Moving protocol tests.** `bh-utvyj` rewrites the roughly 2,375 test-seconds of protocol
behaviour to drive the product modules over the fake. The roughly 1,750 s of storage-adapter
contracts fold into the Dolt package's suite (`bh-br1cd`), and the end-to-end smokes stay
where they are. `bh-l2m59` measures the result against the 12.7-minute baseline.

**Rejected alternatives:**

- **A fake MySQL wire server (`mysql-mimic`), or SQLite / DuckDB stand-ins.** These would
  re-implement Dolt SQL semantics (cell-wise merge, `1213` at commit, `dolt_ignore`, triggers)
  and drift from them. The fake lives at the Python port level instead.
- **schemathesis.** It generates tests from OpenAPI or GraphQL schemas against an HTTP service.
  This port is an in-process Python interface with no HTTP surface, so schemathesis has nothing
  to drive here. Its possible use for Beadhive's OpenAPI and GraphQL interfaces is tracked
  separately in decision bead `bh-hjx6d`.

### Licence decision: hypothesis

- **Decision:** adopt `hypothesis` (MPL-2.0) as a **dev-only** dependency, used for the
  port's contract state machine and property tests.
- **Operator sign-off:** Brian Cripe, 2026-10-11, recorded as a comment on `bh-rlphq`:
  *"hypothesis (MPL-2.0) is approved as a dev-only dependency for the port contract suite /
  RuleBasedStateMachine."*
- **Why it fits policy.** MPL-2.0 is file-level weak copyleft. Using the library unmodified
  places no obligation on Beadhive's own files, and `MPL-2.0` is already in the license gate's
  allowlist (`license_allow` in the justfile).
- **How dev-only is enforced.** `hypothesis` goes only into the `dev` dependency group, never
  into `[project].dependencies` or an extra of root or either package. The SBOM is exported
  with `--no-dev`, so it never enters `bom.json` or the shipped wheel. The lazy imports
  described in section 7 keep the packages importable without it.
- **Who adds it.** `bh-9fg5k`, together with the first test that uses it, pinned in `uv.lock`.

## Consequences

**Positive:**

- Protocol logic (adopt ordering, placement CAS loss and unknown handling, failover timing,
  retention, publication recovery) becomes testable in-process against a fake whose
  faithfulness is checked mechanically. The survey estimates about 2,375 to 80 test-seconds,
  and about 3 to 3.5 minutes off fence-touching integration runs.
- Session-staleness tests become clock-controllable for the first time.
- SQL and Dolt procedure text concentrates in one package with one owner. `FenceData`'s SQL
  leak and the SQL-string fakes go away.
- A git adapter for the generic port remains possible, and Dolt-only operations are visible as
  such at every call site.

**Negative:**

- There are two more packages, one more attest key and more gate wiring to maintain.
- Ports must move with their consumers. During migration some modules temporarily have both a
  port path and a legacy SQL path; each migration bead removes its own legacy path.
- The fake is a second implementation that can drift. The contract suite and the state machine
  are the only defence, so a port change without matching contract cases is a review refusal.
- Real-Dolt shrinking cost, as described in section 8.
- Wire-format compatibility with 0.22.x readers and the trusted receiver (the token format and
  the cause and witness columns) is now the Dolt adapter's responsibility. It must be covered
  by that package's tests, because the fake cannot prove it.

## Open questions and owners

| Question | Owner |
|---|---|
| Package layout, attest key, justfile and catalog wiring, wheel vendoring | `bh-wnhkn` |
| Final protocol names; whether `ChangeSet` carries table names or a typed publication; the `RecordKind` set beyond the three initial kinds | `bh-9fg5k` |
| `hypothesis` pin, profile budgets and the fake-against-fake self-check | `bh-9fg5k` |
| Whether `hq_sql_transport` moves into the Dolt package; real-Dolt shrink budget cap; folding the Dolt-semantics tests in | `bh-br1cd` |
| Placement cause and witness encoding in the adapter; removing the `PlacementAuthority` alias | `bh-sqfvq` |
| `ReclaimSet` sufficiency for every reclaim the M3 policy plans; `lookup_bump` on embedded and server engines | `bh-jhgum` |
| Mapping `PublicationUnknown` and `recover_publication` onto `Unknown` / `lookup_publication` with no behaviour change | `bh-x7xtc` |
| Session, receiver, operator and runtime migration; staleness through `Liveness.ages` | `bh-xnn5p` |
| `DoltHistory`, `DoltMerge` and `DoltAccounts` final shapes for cutover, orphan, audit and `hive_forward` | `bh-3kl1k` |
| Which protocol tests move to the fake, and which stay end-to-end | `bh-utvyj` |
| Measured saving against the 12.7-minute baseline | `bh-l2m59` |
| `refs/dolt/frame/<id>` (`DoltFramePrivateRefs`) | none in `bh-xxkf7`; the trust-profiles `open` policy work |
| A git adapter for the generic HQ port | none in `bh-xxkf7`; filed when Git HQ needs it |

### Settled by `bh-9fg5k`

The port, fakes and contract suite are in `packages/beadhive-hq-storage`. Its README maps
each operation to the contract scenarios that cover it.

- **Protocol names** are as written in section 2. The implementation refines them in these
  ways:
  - `AppendLog.lookup_append(stream, key, operation)` is new. It resolves an `Unknown` append
    by the append's natural key.
  - `HiveFence.lookup_bump(frame, epoch, *, operation)` now takes the caller's
    `OperationId`, so the outcome can carry it. An adapter only has to find the
    `adopt-<epoch>` sentinel; it never has to persist the id.
  - `lookup_publication` also accepts `ABSENT` as its expected head.
  - `head()` returns a `Revision`. Every store starts with a provisioning commit, and a store
    with no head raises `NotProvisioned`.
  - `fetch()` only reports; it never moves the local head. `sync_to_remote()` reports the
    ahead and behind counts it saw before it reset.
  - Both `push()` methods mint their own operation id. On `Unknown`, `expected` is the remote
    head before the push.
  - `PruneReport` holds `stream`, `removed`, `retained` and `operation`.
  - `PlacementView.token` is a `Revision`.
- **`ChangeSet` carries table names**, not a typed publication. `tables` is the explicit
  staging allowlist. Each document is named `<table>/<name>`, and its table must be in that
  allowlist. Reusing a publication's `OperationId` is an `InvalidRequest`.
- **The `RecordKind` set stays at three kinds.** Adding a kind needs a port change plus
  contract cases.
- **Append replay is strict.** An append is idempotent only when the key, the operation and
  the body all match. Any other second append of the same key, even with an identical body,
  is a `Conflict`.
- **Fence bump.** A `commit_bump` at or below `max(history_max, writer)` raises
  `IntegrityViolation`. The `ReclaimSet` revert applies only to beads still `in_progress`,
  the same guard `reclaim_statements` uses.
- **hypothesis:** `hypothesis>=6.100`, dev group only, locked at 6.169.0 in both `uv.lock`
  and the Pants resolve.
- **Profile budgets:**

  | Profile | Examples | Steps | Notes |
  |---|---|---|---|
  | `gate` | 25 | 20 | Loaded when `BH_HYPOTHESIS_PROFILE` is unset, so an unconfigured run is deterministic |
  | `nightly` | 500 | 40 | Shrinking on |
  | `dev` | hypothesis defaults | hypothesis defaults | Opt-in |

- **`build_state_machine(a, b)` takes two `ContractAdapter` values**, not bare factories.
- **Fake-against-fake self-check.** The gate runs the machine fake against fake, with
  `advance_clock` enabled. It also runs a mutant that reuses CAS revisions, and the machine
  must reject that mutant.
