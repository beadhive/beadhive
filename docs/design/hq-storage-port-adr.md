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
    def commit_bump(self, bump: BumpRequest) -> BumpOutcome: ...  # ONE local commit
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

### Settled by `bh-br1cd`

The Dolt adapter is in `packages/beadhive-hq-storage-dolt`. Its README maps each operation to
its Dolt statements, its `Applied` / `Conflict` / `Unknown` cases and the witness each lookup
finds.

- **`hq_sql_transport` stays in root.** It depends on root's `HqSqlConnection` settings and
  the fnox `SecretBroker`, so moving it would pull root settings into the package or duplicate
  them. `beadhive.hq_storage_composition` closes `hq_sql_transport.connect` into the adapter's
  `Callable[[Role], Connection]` factory, one binding per `Role` (`operator`,
  `placement_writer`, `publisher`, `frame`, `reader`). It also binds a hive's fence engine
  with `fence_schema`'s 44 guard triggers. Root's transport deadline error has no numeric code,
  so composition injects a predicate for it, and the adapter reports it as
  `Unknown(reason="deadline")`.
- **Real-Dolt shrink budget.** The gate runs fake against Dolt with the `gate` budget and
  `no_shrink_phases()`. That measured 164 s on a loaded host. Every other profile shrinks
  against Dolt, capped at 100 examples per run (`DOLT_MAX_EXAMPLES`). The cap bounds the budget
  only; the comparison is unchanged.
- **The Dolt-semantics tests are folded in.** `tests/test_hq_sql_dolt_semantics_int.py` moved
  to `packages/beadhive-hq-storage-dolt/tests/test_dolt_semantics_int.py`; no copy remains in
  `tests/`. This covers the bh-wtsrc grant, trigger, UTC and host-pin cases, the bh-sieai
  merge, cherry-pick and revert trigger contract, and the bh-uhx2r E1 and E2 cases. The test
  now owns its server through the package's `DoltSqlServer`, and inlines the one-table write
  guard because the package cannot import root's test harness. Two changes remove its waits:
  - the no-op-renewal case now backdates the row with its trigger dropped, instead of
    sleeping;
  - the renewal latency samples stay in the spike's evidence record and are no longer
    asserted.
  `just fence-canary` now includes the package's tests, so the canary cases still run on every
  pin bump.
- **Every mutation reaches one commit point,** even when the outcome is already a `Conflict`.
  A lost acknowledgment is therefore always `Unknown`, as on the fake.
- **Error classification by `args[0]`.**
  - Before the commit point, `1213` or `1205` is a `Conflict` and anything else is
    `StorageUnavailable`.
  - At the commit point, `1213` is a `Conflict`. Otherwise the reason is `"deadline"` for
    `TimeoutError`, `3024`, `1317` or root's predicate; `"transport"` for `2006`, `2013` or
    `2055`; and `"commit"` for `1180` and anything unclassified.
  - `prune` and `renew` have no `Unknown` outcome. A lost acknowledgment there raises
    `StorageError`.

#### Adapter-specific behaviour and deviations

- **Placement witness.** For a port-written row, `request_id` holds the `OperationId` (the
  section 2 witness table), so the placement cause rides in the body, not as root's
  `cause_token`. `request_sha256` is still root's `placement_witness` over the row, and root's
  `is_director_row` recognises the row (`tests/contracts/test_hq_storage_composition.py`).
  Encoding the receiver's `{"authority", "lease"}` carrier and keeping `cause_token`
  compatibility is `bh-sqfvq`'s. The adapter's `PlacementStore` already reads both body shapes.
- **Placement `recorded_at`.** The receiver-compatible placement row has no stamp column. A
  `Confirmed` placement lookup therefore carries the server clock at lookup time, which is an
  upper bound on the write.
- **Adapter-owned live tables.** Kinds other than placement, the three streams and liveness use
  adapter-owned `dolt_ignore`'d tables: `hq_live_records`, `hq_live_append` and
  `hq_live_liveness`. Each has a stamping trigger. Mapping them onto `hq_live_failover_policy`,
  the per-principal inbox and session tables, and `hq_live_receipts` belongs to the migration
  beads (`bh-sqfvq`, `bh-xnn5p`).
- **Triggers on ignored tables are never committed.** On Dolt 2.3.5,
  `information_schema.triggers` fails for a committed trigger whose table is ignored, because
  it resolves the table at `HEAD`. `DoltAccounts.triggers()` reads `dolt_schemas` instead.
- **`ChangeSet.tables` is a logical allowlist in Dolt.** Documents live in one committed
  `hq_storage_documents` table keyed by `<table>/<name>`. Every publication stages exactly
  `hq_storage_documents`, `hq_storage_publications` and `hq_storage_publication_meta`, and never
  `-A`. Mapping onto the `hq_config_*` tables is `bh-x7xtc`'s.
- **`HiveFence` takes its guard set from root.** The contract fence carries the fake's three
  guard names, so the machine compares equal conformance reports. A bump that fails mid-batch
  discards its partial working set with `DOLT_CHECKOUT` of the tables it touched, then raises
  `StorageUnavailable`.
- **`ReclaimSet` coverage.** A `ReclaimSet` reverts the lifecycle fields and clears
  `claim-frame`. The `recovery:resumable` label and the audit comment were left to `bh-jhgum`,
  which grew `ReclaimSet` to carry them (see below).
- **`EpochRefStore` is not Dolt's.** `refs/bh/epoch` is a git ref in root, so the Dolt fence
  harness pairs the real fence with `InMemoryEpochRef`.
- **The Dolt-only interfaces are first-cut shapes.** `DoltHistory`, `DoltMerge` and
  `DoltAccounts` are declared in the Dolt package, and `DoltHistory.table_history` accepts only
  the closed `FenceTable` enum. Their final shapes are `bh-3kl1k`'s.
- **Fix in the shared machine.** `build_state_machine` compared `SyncReport` values by raw head
  text, which can only match fake against fake. It now projects the head through the
  revision-equality tags, like every other revision.
- **Fault injection on Dolt is real.** The harness wraps the connections or the engine it gives
  the adapter. An armed fault fires at the named method's first commit point: the real commit
  runs first when `applied`, and otherwise never reaches the server.

No other divergence between the fake and Dolt was found. The contract suite passes on Dolt
with the two clock-controlled scenarios skipped.

### Settled by `bh-sqfvq`

`beadhive.hq_sql_placement` is the port's first production consumer. It holds no SQL string and
no Dolt procedure call. The director's reads and its placement CAS go through
`VersionedRecords` on `RecordKind.PLACEMENT`, over the Dolt adapter that
`beadhive.hq_storage_composition.placement_storage` composes from the `placement_writer`
binding. The unit tests run on `InMemoryHqStorage`.

- **Placement body and wire encoding.** A director write's port body is the receiver's carrier,
  `{"authority", "lease"}`, plus `"director": true` and an optional `cause`. The adapter
  (`beadhive_hq_storage_dolt._placement`) stores:
  - exactly `canonical({"authority", "lease"})` in `lease_json`;
  - the cause in `request_id` as root's `cause_token`, so 0.23.x frames still read it;
  - for a write with no cause, the `OperationId` in `request_id`;
  - root's `placement_witness` in `request_sha256`.

  Reading a canonical carrier row returns `director` (whether the witness verifies) and, on a
  director row only, the `cause`. Any other row decodes as plain JSON without `director`, which
  root refuses as `placement row carrier invalid`, as `parse_row` does for a raw row. Root and
  adapter encodings are pinned equal in `tests/contracts/test_hq_storage_composition.py`.
- **Deviation: the witness of a caused write.** The section 2 table puts the placement witness
  in the request id column. A caused write cannot do that, because `request_id` must stay the
  cause token. Its revision is bound to the `OperationId` instead: `operation_revision` is
  `sha256` over the prefix, `lease_json` and the operation. `lookup` confirms a row whose
  `request_id` is the operation, or whose revision is that operation's revision under a
  verifying witness. The revision keeps the receiver's 64-hex format and is fresh for every
  write, because an `OperationId` is minted once per intent (rule 3). Reusing one with an
  identical carrier on the same hive would repeat a revision; the port already forbids that.
- **`Unknown` handling.** `place_cas` looks an `Unknown` up by the write's `OperationId`:
  - `Confirmed` returns the placement as applied (before, every lost acknowledgment raised);
  - `NotConfirmed` or a failed lookup raises `PlacementUnknown`, which is never re-issued and
    never re-minted.

  `PlacementUnknown` now carries the `operation` and the `reason` instead of a candidate
  revision, because the adapter mints the revision.
- **Lost CAS reason.** The port reports `rowcount 0` and `1213` alike as `Conflict`, so
  `PlacementLost.reason` is `CAS_LOST` (`rowcount 0 or 1213 serialization failure`). The
  pre-CAS check still reports `the row moved since it was read`.
- **The principal check moved into the adapter.** `DoltHqStorage(principals=...)` checks
  `CURRENT_USER()`, `DATABASE()` and `ACTIVE_BRANCH()` at the start of every session for a role,
  and refuses a mismatch with `StorageUnavailable`. The director's survey and place, and the
  conformance check, still hide driver text.
- **Deviation: the transitional `DoltHqSessions` capability.** The director reads the signed
  authority (`hq_sql_runtime`), the failover policy (`failover_policy`) and session staleness
  (`failover_observer`) in the same transaction as the placement rows. A `failover_after` write
  shares the placement CAS's transaction. None of those three is on the port yet, so the Dolt
  adapter offers a Dolt-only `DoltHqSessions` capability:
  - `snapshot()` is one read-only transaction with port-shaped reads and the raw cursor;
  - `transact()` commits only when the CAS is `Applied`, so a co-written policy row never lands
    without it.

  This keeps the one-transaction survey and the atomic `failover_after`. On storage without the
  capability (the fake), a survey carries no failover policy, and `failover_after` and the
  policy verbs are refused. `bh-xnn5p` removes each cursor use as it migrates those consumers,
  and the capability goes with the last one.
- **Table mapping.** Placement stays on `hq_live_hive_leases`. `FAILOVER_POLICY` is **not**
  mapped onto `hq_live_failover_policy` here. That table holds one row per scope and setting,
  bounded by `hq_liveness_policy`, and is written inside the placement CAS's transaction, so one
  versioned record per key would change its shape. It moves with the failover policy in
  `bh-xnn5p`, and `hq_live_records` stays adapter-owned until then.
- **Seed and conformance SQL.** `seed_statement` renders the adapter's
  `placement_seed_statement`. `conformance` reads grants and triggers through
  `placement_conformance_rows` on the operator's cursor. It keeps
  `information_schema.triggers`, so it behaves exactly as before; `DoltAccounts.triggers()`
  reads `dolt_schemas`, and its final shape is `bh-3kl1k`'s.
- **Connections.** A director call now opens one connection per port operation (one survey
  session; then a read, a CAS and possibly a lookup), each under the binding's own operation
  deadline. Before, a call held one connection for all of it.
- **Not done here.** `writer_adopt.PlacementAuthority` is not yet an alias of `PlacementStore`:
  `writer_adopt` still declares its own read-and-CAS protocol, which `host_adopt` implements
  over the frame's raw-row read. Absorbing it means editing `writer_adopt`, which `bh-jhgum`
  is changing in parallel, so it is left to `bh-jhgum` or a follow-on. The frame side
  (`hq_control_plane.read_placement`) still parses the raw row of the runtime authority's
  composite read with `parse_row`; it moves with the runtime authority (`bh-xnn5p`).
- **Tests.** `tests/test_hq_sql_placement.py` drives the CAS, its `Conflict` and `Unknown`
  branches and the director over `InMemoryHqStorage`, and its `FakeConnection` /
  `FakeCursor` are gone. Assertions about SQL text, commit counts and closed connections moved
  to `packages/beadhive-hq-storage-dolt/tests/test_hq_storage_dolt_adapter.py` on real Dolt.
  `tests/test_failover_policy.py` drives `failover_after` through a fake `DoltHqSessions`. The
  integration tests compose the adapter as the director does, and their assertions are
  unchanged except that a racing loser's reason is now `CAS_LOST` or the pre-CAS `moved`.

### Settled by `bh-x7xtc`

`beadhive.hq_sql_config` publishes and recovers HQ configuration through the port. A
publication is one `CommittedHistory.publish` of a `ChangeSet` (`publish_documents`), and
`recover_publication` is decided by `lookup_publication` on the immutable publication UUID
(`confirmed_publication`). Neither the publication's writes nor its `DOLT_ADD` / `DOLT_COMMIT`
are in root any more. The history is the Dolt adapter's `DoltConfigHistory`, which
`beadhive.hq_storage_composition.config_history` composes from the `reader` and `publisher`
bindings. The unit tests run on `InMemoryHqStorage`.

- **Strict recovery, unchanged.** An `Unknown` from `publish` raises `PublicationUnknown`
  with the original publication UUID and parent, and now also the port's `reason`. Root never
  reissues it. `NotConfirmed` returns `None` from `recover_publication`, as absence always
  did. It never authorises a reissue, a new UUID or a refreshed-parent retry (section 2,
  rule 4). An `Applied` publication whose readback fails or differs is still
  `PublicationUnknown`.
- **Deviation: the config publication does not use `hq_storage_documents`.** The `hq_config_*`
  tables are a committed wire format. The runtime authority, frames, operator surfaces and
  0.22.x readers read them, and `bh-4shu3`'s seed writes them. So the adapter maps the port
  onto them, column for column, in a second `CommittedHistory` (`_config.py`):
  - `ChangeSet.tables` must be exactly `hq_config_meta`, `hq_config_documents` and
    `hq_config_publications`, and that set is the explicit `DOLT_ADD` list (never `-A`);
  - the documents are `hq_config_meta/schema_version` (the storage version, a domain choice
    made by root from beadyard identity), then `hq_config_documents/<path>` in publication
    order;
  - the adapter derives each document's ordinal, kind and `content_sha256`, the ordered
    digest, the next sequence and the witness row. The kinds and the digest restate root's
    `_kind` and `ordered_documents_digest`, and
    `tests/contracts/test_hq_storage_composition.py` pins root's change set, kinds and digest
    equal to the adapter's.
- **Deviation: what `Confirmed` carries for a config publication.** The config reader has no
  grant on `dolt_log`, `dolt_commit_ancestors` or `dolt_history_*`; only the publisher holds
  them, and only for the initial-seed recovery. So the adapter confirms the witness row
  `AS OF` the current head against the original parent, with `HAS_ANCESTOR`, which is exactly
  the check `recover_publication` always made. `Confirmed.revision` is that head, where the
  witness is visible, not the publication's own commit. `recorded_at` is the server clock at
  lookup, an upper bound on the write, as for placement. `Applied.revision` from `publish` is
  still the exact committed hash.
- **The parent check after commit.** The generic `publish` reads the new commit's first
  parent from `dolt_commit_ancestors`. The config publication cannot, for the same grant
  reason. Root's readback (`load_snapshot(revision=<commit>)`) and its floor's ancestry and
  witness checks verify the commit instead, as they always did.
- **The publisher preflight stays in root, in its own read transaction.** Identity and Dolt
  version, the exact expected head, a clean `dolt_status`, the table allowlist, the previous
  carrier (raw for repair), beadyard identity, version monotonicity and the host floor are
  domain checks, so they run in root before the port publish. Before, they shared the write's
  transaction. The port publish re-checks the head, the working tree and the allowlist inside
  its own transaction, and its sequence CAS still guards the meta singleton. A race lost
  between the two is therefore a `Conflict`.
- **Error mapping.** `Conflict` is `configuration publication CAS conflict` (`1213` and
  `1205` included, as before). A head that had already moved at preflight is still
  `expected configuration revision changed`. `NotProvisioned` and `IntegrityViolation` keep
  their message (`HQ config schema table allowlist changed`, `HQ config working tree is
  dirty`). `StorageUnavailable` and `InvalidRequest` (including a reused publication UUID,
  which `1062` reported before) are `configuration publication failed`.
- **Recovery's witness read.** After the lookup, root's reader still reads the floor-checked
  head and the witness row, for its sequence and digest. A witness row against another parent
  is still `committed publication identity or parent mismatch`. A head that does not descend
  from the parent is still `committed publication parent is not an ancestor`. A witness row
  the port did not confirm (it landed between the two reads) reads as `None`, never as a
  recovery and never as permission to reissue.
- **Connections and deadlines.** A publication now opens the preflight's publisher
  connection, the port publish's publisher connection (plus a reader connection to report a
  `Conflict`'s head), then the readback. Before, it opened one publisher connection and the
  readback. Each port connection runs under its binding's own `operation_timeout`. The
  readback now gets its own reader budget instead of the publisher's remainder, so a slow
  publish no longer turns a committed publication into `PublicationUnknown` by deadline
  alone. `recover_publication` opens one more reader connection, for the lookup.
- **Not done here.**
  - Initial seeding (`initialize_snapshot`, `recover_initial_snapshot`) stays root SQL. It is
    the separate `bh-4shu3` contract (section 2, rule 4), and its recovery needs the
    publisher's `dolt_history_*` grant.
  - The committed reads stay root SQL on a reader cursor: `load_snapshot`,
    `committed_snapshot_at`, `tolerated_bound_at`, `authority_binding`, the floor and the
    `inspect_*` views. `hq_sql_runtime`'s combined config-and-authority read hands its own
    cursor to `committed_snapshot_at`, so they move with the runtime authority and
    `consistent_read` (`bh-xnn5p`).
- **Tests.**
  - `tests/test_hq_sql_config_port.py` drives publication and recovery over
    `InMemoryHqStorage`, replacing only root's preflight and readback seams. It covers
    `Applied`, `Conflict`, `Unknown` (each reason, applied and not), `Confirmed`,
    `NotConfirmed` and the no-reissue rule.
  - The `hq_config_*` row encoding, the explicit `DOLT_ADD` list, the dirty-tree and
    allowlist refusals, and a lost `DOLT_COMMIT` acknowledgment resolved by lookup run on real
    Dolt in `packages/beadhive-hq-storage-dolt/tests/test_hq_storage_dolt_adapter.py`.
  - `tests/test_hq_sql_config_int.py` and the other config publication tests are unchanged
    and pass.

### Settled by `bh-jhgum`

`FenceData.commit_bump(statements, message)` is gone. `writer_adopt` step 2 builds a typed
`BumpRequest` and drives the fence only through `HiveFence`; no SQL text crosses the port.

- **`ReclaimSet` grows.** It now carries everything the failover reclaim wrote before the port:
  - `bead_ids`: the beads whose lifecycle fields revert and whose `claim-frame` label clears;
  - `resumable`: the subset whose `recovery:*` labels are replaced by `recovery:resumable`;
  - `notes`: one `ReclaimNote` per bead (`bead_id`, `comment_id`, `author`, `text`,
    `created_at`), written as that bead's audit comment.

  Moving the label and the comment out to root would split the reclaim across two commits, or
  push SQL back through the port. Either breaks condition 7 or the scope rule. Root keeps the
  domain: it decides the D5a rows, mints the comment ids, stamps the time and writes the audit
  text. The store only writes it. The label names are port constants
  (`CLAIM_FRAME_LABEL_PREFIX`, `RECOVERY_LABEL_PREFIX`, `RESUMABLE_LABEL`), and
  `tests/contracts/test_hq_storage_composition.py` pins them to root's `work_backup`
  dimensions.
- **Every reclaim write is guarded on `in_progress`.** This includes the label and comment
  writes, which root wrote unguarded before. The Dolt adapter runs them before the lifecycle
  `UPDATE` so the guard still sees the claim. For a plan computed on the head it commits on,
  the result is unchanged; a bead that moved on now gets no stray comment either. The fake
  models the same rule, and `bump_reclaim_marks_resumable_and_leaves_the_audit_note` covers it
  on both adapters. A malformed set (a resumable bead or a note outside `bead_ids`, a duplicate
  comment id, a naive `created_at`) is `InvalidRequest` (`bump_refuses_a_malformed_reclaim`).
- **`ReclaimSet` is sufficient for every reclaim the M3 policy plans.** Only the `rewound` and
  `resumable` rows write, and both map onto the fields above. Every other D5a row is untouched
  by design. `failover_reclaim.reclaim_statements` is replaced by `reclaim_set`, and
  `ReclaimPlan.statements` by `ReclaimPlan.reclaim`.
- **Guard and floor.** Step 2 reads `guard_conformance()` instead of a trigger count. Its
  `required_triggers` argument is gone, because the expected set is the adapter's (root binds
  `fence_schema.trigger_names()`). A bump at or below the floor raises `IntegrityViolation`
  from the store. Step 2 does not catch it: it fails closed and is never retried.
- **Push outcomes.** `Applied` lands the bump, and `Conflict` (a non-fast-forward) loops back to
  the data check, as before. `Unknown` is resolved with `lookup_bump` under the bump's own
  `OperationId`. `Confirmed` counts as landed. Anything else is `unreachable`, which surfaces
  as "adopt incomplete": the re-run resumes at the placed epoch and never re-issues the bump.
  `StorageUnavailable` from any fence read or push is also `unreachable`.
- **`lookup_bump` on both engines.** `test_fence_composed_int._take` asserts `Confirmed` (and
  `NotConfirmed` for another frame) after every product adopt. That runs over root's
  `DoltCliEngine` (embedded) and `BdServerEngine` (server). `test_writer_adopt_int` asserts the
  same on the Dolt CLI frame.
- **`FenceNode` is the root composition.** `FenceNode.fence` is the Dolt `HiveFence` over the
  node's own engine (`hq_storage_composition.dolt_hive_fence`). `FenceNode` satisfies
  `HiveFence` by delegating `sync_to_remote`, `guard_conformance`, `commit_bump`, `push` and
  `lookup_bump` to it, so `fence_data_port`'s resolver still answers a `HiveFence` or `None`.
  `writer`, `remote_writer` and `epoch_floor` stay one cheap local read each, because the
  guard reads `writer()` on every write. `SqlFailed` and `writer_adopt.DataUnreachable` are
  also `StorageUnavailable`, so a node's failures honour the port contract, and existing
  `except` clauses keep working.
- **Legacy node operations stay until `bh-3kl1k`.** The cutover and orphan paths still use
  `FenceNode.push_main()` (the old boolean push) and `history_max_epoch()`. `sync_to_remote()`
  now runs the adapter's sync and still raises `DataUnreachable` with the engine's message.
- **Moved records.** `WriterRow` and `RefView` are the port's, re-exported by `writer_adopt`.
  `PlacementView` stays root's: its CAS token has a default that `doctor` relies on, and it is
  `PlacementStore`'s. It moves when `PlacementAuthority` is absorbed into `PlacementStore`.
- **`PlacementAuthority` is not absorbed here either.** Its `read()` / `cas()` shape differs
  from `PlacementStore.read(prefix)` / `compare_and_set(..., cause, operation)`, and absorbing
  it means reworking `host_adopt`'s `_LeasePlacement` and `_DirectorPlacement`. That is
  placement work, so it is left to a follow-on bead.

**Fake-ready protocol tests for `bh-utvyj`.** These suites drive root through the typed port but
still use hand-written port doubles. They can move onto `InMemoryHiveFence` without changing
what they assert:

- `tests/test_writer_adopt.py`: the order, crash, idempotence and loss cases, the typed bump,
  the floor and the unknown push;
- `tests/test_host_adopt_coexistence.py`: the `Data` and `ReclaimingData` doubles;
- `tests/test_guard_writer_gate.py`: it only needs `writer()`.

`tests/test_failover_reclaim.py` executes the Dolt adapter's statements on SQLite. It becomes
fake-ready once `InMemoryHiveFence` models `ReclaimData` reads (claims and policy rows) as
well. The integration suites (`test_writer_adopt_int`, `test_failover_reclaim_int`,
`test_fence_composed_int`, `test_fence_orphan_int`, `test_fence_cutover_int`,
`test_epoch_fencing_int`) stay on real Dolt.

### Settled by `bh-xxkf7.6`

`HiveFence.commit_bump` returns `BumpOutcome = Applied | Unknown`, not a bare `Revision`.
Before, the Dolt adapter read `DOLT_HASHOF('HEAD')` after the engine's commit and turned a
failure of that read into `StorageUnavailable`. That error means nothing was sent, yet the bump
had committed, and a caller that retried would hit `IntegrityViolation` on the epoch floor.

- **Why an outcome type, not an infallible head read.** The other option was to take the hash
  from the commit itself. `SqlConnectionFenceEngine` could, because `CALL DOLT_COMMIT` returns
  the hash as a row, but root's engines cannot do it reliably. `DoltCliEngine.commit` shells to
  `dolt add` and `dolt commit`, and `BdServerEngine.commit` to `bd dolt commit`, whose output
  is human text with no stable hash field. Both return only a `bool`. A raise from either
  commit can also follow a landed commit, for example a timeout after Dolt wrote it. A hash
  from the commit would not cover that case; an outcome type covers both.
- **The shape.** `Applied(revision, bump.operation)` is the new local head. `Unknown(bump.operation,
  expected, reason)` means the bump's commit may or may not exist. `expected` is the local head
  read *before* anything was sent. `reason` is `unknown_reason` of the commit's error, or
  `"transport"` / `"deadline"` for the head read after it. There is no `Conflict`, because a
  bump is local. Refusals (`InvalidRequest`, `NotProvisioned`, `IntegrityViolation` at the
  floor, or a commit that changed nothing) still raise. A failed statement batch still raises
  `StorageUnavailable`, because nothing was committed and the partial working set is
  discarded.
- **Resolution: never re-issued.** An `Unknown` bump is resolved by pushing, then calling
  `lookup_bump(frame, epoch, operation=bump.operation)`. If the commit exists, the push
  publishes it. If it does not, the push publishes nothing new and the lookup reads
  `NotConfirmed`. `writer_adopt.run_step2` does exactly this. After an `Unknown` commit, an
  `Applied` or `Unknown` push goes to `lookup_bump`: `Confirmed` is landed, anything else is
  `unreachable`. A `Conflict` push loops back to the data check as before, and that round's
  sync resets the local branch. No other caller of `commit_bump` exists (`FenceNode` only
  delegates).
- **Both adapters model it.** The fake's `FaultPlan` now arms `Unknown` on `commit_bump`, with
  the commit applied or not. The Dolt contract harness fires the same fault at the engine
  commit. Scenario `bump_unknown_resolves_through_lookup_bump` runs on both adapters, and
  `test_a_failed_post_commit_head_read_in_commit_bump_is_unknown_then_confirmed` loses the
  real post-commit `DOLT_HASHOF('HEAD')` read on a `dolt sql-server`.

### Settled by `bh-xnn5p`

`hq_sql_runtime`, `hq_sql_receiver`, `hq_sql_operator`, `hq_sql_session`,
`hq_sql_session_provision`, `failover_observer`, `failover_policy` and `director_failover` hold no
SQL string and no Dolt procedure call. The transitional `DoltHqSessions` capability is gone. The
director's survey and its `failover_after` write no longer need it.

- **The failover policy is `RecordKind.FAILOVER_POLICY`.** Each scope (a hive prefix or `*`) is
  one record, and its body is `{setting: seconds}`. `failover_policy.read_policy`,
  `policy_write`, `plan_changes` and `apply_changes` take the port's `VersionedRecords`.
  Thresholds and validation stay in root. The bounds' session TTL comes from the committed
  liveness policy (`bounds_from`). The unit tests run on `InMemoryHqStorage`.
- **Deviation: the policy mapping has a content revision and no witness.** `DoltHqStorage(...,
  runtime_tables=True)` maps the kind onto `hq_live_failover_policy`, column for column, with the
  same `DELETE` and upsert statements root issued before. That table has no revision or witness
  column. The director's grant (`check_grants`) allows writes to that table and placement only,
  so the revision cannot live in a side table. The revision is therefore the digest of the
  scope's rows, and the CAS compares it inside its transaction. It follows that:
  - identical content repeats a revision, so the "fresh revision" rule does not hold for this
    mapping;
  - `lookup` is always `NotConfirmed`.

  Both are safe. A competing write with the same content changes nothing, and `NotConfirmed`
  never authorises a retry (`FailoverPolicyUnknown`, "read it back"). Before the port the write
  had no expectation at all, so a concurrent change to the same scope is now a `Conflict`
  (`PlacementLost` with `CAS_LOST`) where it used to merge. The contract suite runs the
  adapter-owned default, and the mapping is proven on real Dolt in
  `test_hq_storage_dolt_adapter.py`. An unprovisioned table is `NotProvisioned`
  (`NO_SUCH_TABLE`, `1146`), which reads as "code defaults, not provisioned", as before.
- **Port addition: `AtomicRecords.compare_and_set_all`.** `failover_after` must never land
  without its placement CAS, and two generic CASes cannot guarantee that. The port gains an
  optional protocol with `RecordWrite` members:
  - every expectation holds, or nothing is written and the outcome is a `Conflict` naming the
    first failing member's current value;
  - `Applied.revision` is `writes[0]`'s, and every member carries the `OperationId`.

  Both adapters implement it. On Dolt it is one transaction, and a failed member rolls the
  transaction back instead of reaching the commit point. Three contract cases cover it:
  `atomic_cas_applies_every_write_or_none`, `atomic_cas_unknown_resolves_by_any_member` and
  `atomic_cas_refuses_malformed_writes_before_io`. `place_cas` resolves an `Unknown` through the
  placement's own witness. `Confirmed` means both records landed, and anything else is
  `PlacementUnknown`, never reissued. A store without the protocol refuses `failover_after`.
- **Session staleness is `Liveness.ages(LivenessKind.SESSION, keys)`.** A key is
  `"<principal>/<epoch>"` (`failover_observer.session_key`). With `runtime_tables`, the Dolt
  adapter maps `SESSION` and `EVIDENCE` onto the provisioned `frame_<principal>_<epoch>_session`
  and `_evidence` rows. Ages come from ONE `UNION ALL` statement of
  `TIMESTAMPDIFF(MICROSECOND, <stamp>, UTC_TIMESTAMP(6))`. Before, there was one statement per
  table, each with its own server instant. A renewal is the row's `UPDATE`. A missing table or
  row is `NotProvisioned`, never an `INSERT`, because frames hold `SELECT, UPDATE` only. A row
  that was never renewed has no age, so it is unobserved, as before.
  `failover_observer.session_staleness` replaces `sql_session_staleness`, and
  `director_failover.observe_sessions(liveness, registry, state)` replaces the cursor version.
  `test_director_failover.py` crosses `failover_after` by advancing the fake's clock, with no
  waiting.
- **Deviation: the survey is no longer one transaction.** Only the signed-authority read, the
  principal registry at its head and the liveness-policy bounds share one read-only runtime
  transaction (`DoltRuntimeReads.pinned()`). The placement rows, the failover policy and the
  session ages are separate port reads. Every decision taken from a survey is still guarded:
  `place` re-verifies the authority and CASes on the revision it read, and a moved row is
  `PlacementLost`. `place` likewise reads the placement row outside its authority transaction.
- **Eligibility thresholds are root's.** The one eligibility statement (`ELIGIBILITY_SQL`) still
  reads the grant and policy `AS OF` the head. It now returns `session_epoch_current` and
  `evidence_conformant` instead of the two freshness predicates. Root decides freshness with
  `hq_sql_session.within_ttl(age_us, ttl_s)`, the same whole-microsecond `<` comparison the
  statement made, so no result changes. The equality predicates (profile, release digest, lease
  holder) stay in SQL, under the columns' collation.
- **Deviation: the runtime authority, receiver, operator and session rows use a Dolt-only
  statement surface, not the generic port.** These modules verify signed, committed history:
  - `AS OF` authority and registry reads, `HAS_ANCESTOR`, `dolt_commit_ancestors`,
    `DOLT_DIFF_SUMMARY` and `dolt_schemas` (section 4: Dolt-only);
  - acceptances that write the receipt, floor, public observation and result, or the lease CAS
    and its result, in one commit;
  - an authority publication that `DOLT_COMMIT`s the row, the registry and the floor seed
    together.

  Mapping these onto `VersionedRecords` and `AppendLog` would split their atomicity. Instead,
  `beadhive_hq_storage_dolt.HqRuntimeStatements` names every statement exactly as root issued
  it, over root's own cursor, through `hq_storage_composition.runtime_statements`. Root keeps its
  connection, transaction boundaries, deadlines and verification, and its SQL-level test doubles
  keep matching unchanged. `DoltRuntimeReads` yields the same surface for the director's pinned
  read. The receiver's driver error codes come from the adapter's `error_code`, `CONFLICT_CODES`
  and `DUPLICATE_KEY`. Making these suites fake-ready is `bh-utvyj`'s, and the provisioning
  statements' final `DoltAccounts` shape is `bh-3kl1k`'s.
- **The committed config reads the runtime shares moved too.** `hq_sql_config._committed`,
  `_check_floor`, `committed_snapshot_at` and `tolerated_bound_at` read through the same surface
  (`config_meta_at`, `config_documents_at`, `config_witness_at`, `config_parent_at`,
  `has_ancestor`). They accept a cursor or the surface, so both root's own reader and the
  runtime's cross-database transaction use them.
- **Encoding equality.** `tests/contracts/test_hq_storage_composition.py` pins the adapter's
  table names, incarnation table names and keys, ignore pattern and stamping trigger equal to
  root's. It also pins the failover-policy statements to the text root issued before the port.
- **Not done here.** `hq_sql_runtime_schema` (operator DDL), the rest of `hq_sql_config`'s SQL
  (initial seeding, inspection, the publication preflight) and the frame's
  `SessionRenewer`/evidence connection stay root-owned. The renewer and the evidence publication
  run the adapter's statements on the frame's own connection, because the route resolution by
  `CURRENT_USER()` must share that session. `Liveness.renew` over the incarnation mapping is the
  same `UPDATE`.
