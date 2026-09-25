# ADR: Beads API-first parallel replacement

- **Status:** Accepted
- **Date:** 2026-09-25
- **Decision owner:** Beadhive maintainers
- **Related work:** `bh-ie41e`, `bh-97fo0`, `bh-bwnys`, `bh-sy36q`, `bh-dw3e`, `bh-xkg4u`

## Context

Beadhive currently reaches Beads primarily by constructing `bd` command lines and parsing
their output. That boundary has accumulated command-specific wrappers, broad fakes, and
stateful tests which reproduce parts of Beads instead of testing Beadhive's own orchestration.
The proposed repository-port refactor would improve isolation, but it would also preserve a
large provider-neutral domain and an in-memory implementation of behavior already owned by
Beads.

Beads v1.3.0 provides a daemon HTTP API described by an OpenAPI document. The same official
Beads runtime can use embedded or external Dolt storage, and the forthcoming team deployment is
expected to expose the same API family. This lets Beadhive make the supported Beads service its
primary work-state dependency instead of abstracting over hypothetical stores.

Worktree execution is different: native Git, Herdr, and Worktrunk are genuine alternative
implementations. Beadhive must retain its identity, naming, and safety policy while allowing a
selected provider to perform the mechanics.

## Decision

### 1. Treat the official Beads API as the work-state platform

Beadhive will generate a Python client from an exact, checksummed Beads v1.3.0 OpenAPI artifact.
Generated request, response, and error types are the default boundary for supported operations.
We will not create a generic bead-store provider hierarchy for `bd`, `br`, `bw`, `nodb`, or an
in-memory store.

One small handwritten `BeadsSession` layer may own endpoint selection, credentials, request
deadlines, context/capability negotiation, error normalization, and optional supervision of a
local `bd serve` process. It must not reproduce Beads lifecycle rules or maintain a second data
model.

HTTP writes are adopted operation by operation. An operation moves only after its audit actor,
hook/event behavior, concurrency semantics, and ambiguous-failure reconciliation are understood.
CLI compatibility remains an explicit, named path for unsupported, administrative, recovery, or
temporarily non-equivalent operations. It is not a transparent retry after an ambiguous HTTP
write. Destructive API operations such as delete and sweep remain denied unless separately
designed and approved.

Embedded Dolt, external Dolt, and team-server deployment are Beads topologies selected through
endpoint configuration and advertised capabilities, not separate Beadhive storage providers.

### 2. Build the replacement beside the legacy implementation

The API-first path will be implemented from scratch in two independent uv-workspace packages:

1. `beadhive-beads-client`: the pinned OpenAPI artifact, reproducible generator configuration,
   generated SDK, and a minimal `BeadsSession` composition layer.
2. `beadhive-core`: thin Beadhive command handlers and policy that depend on the generated
   client, plus the small operational capability contracts that Beadhive genuinely owns.

The installed `beadhive` distribution remains the compatibility shell and composition root. It
continues to own the public CLI, configuration discovery, operator output, validation execution,
Git safety, daemon entry points, and compatibility-only administration. New work and planning
commands are implemented and tested in `beadhive-core`, then selected at the top-level command
composition boundary. A temporary per-command compatibility switch may select legacy behavior
during rollout.

This is a replacement, not a permanent parallel architecture. Each cutover bead must name the
legacy modules, argv wrappers, fakes, and tests it deletes. The old path is retained only for
operations explicitly classified as CLI compatibility.

### 3. Test Beadhive policy without emulating Beads

Unit tests use the generated client's transport seam, typed response builders, and small recorded
fixtures. They assert Beadhive routing, policy, safety, and error presentation. They do not
implement an `InMemoryBeadRepository` or simulate the Beads state machine.

A small conformance suite runs against a real Beads v1.3 service to prove generated-client drift,
authentication/context behavior, selected mutations, concurrency, and the explicit CLI
compatibility boundary. Beads' own storage and lifecycle behavior is not exhaustively retested in
Beadhive.

### 4. Keep plugin contracts operational and narrow

Plugins integrate through a short list of typed capability slots where alternatives actually
exist. The first such slot is worktree management. Beadhive core computes durable bead identity,
branch intent, preferred path/name, and safety constraints. A selected `WorktreeManager` performs
`create`, `open`, `inspect`, `remove`, and `prune` to the extent its declared capabilities allow.
Native Git is the built-in implementation; Herdr and Worktrunk may supply adapters.

Provider selection is explicit and capability-checked. Lifecycle observations such as
`worktree_created` or `worktree_removed` are separate observers; they are not mixed into the
provider fallback chain. Broad generic event buses and speculative provider interfaces are out of
scope.

The exact worktree contract will be selected by a bounded spike because Worktrunk's template-based
paths may not satisfy Beadhive's current exact-path convention without an adapter or a deliberate
naming-policy adjustment.

## Parallel-replacement boundary

The following can be built and validated without importing the legacy root application:

- generated Beads v1.3 client and session composition;
- work reads, lifecycle mutations, relationship operations, filing, ready/scheduling queries,
  and capability/error handling supported by the API;
- thin work/planning command handlers and their policy tests;
- plugin capability selection and the worktree-manager request/result contract;
- native worktree provider behavior and provider contract tests.

The following remain in the compatibility shell initially and are adapted into the new core:

- Typer command names/options and operator-facing output contracts;
- config, actor/identity, hive discovery, and credential resolution;
- validation/check execution, submission, review evidence, and merge serialization;
- Git remote/branch safety and durable Beadhive worktree naming policy;
- backup, migration, repair, federation, and other Beads administration;
- host daemon, MCP, telemetry, and unrelated hive/factory functionality.

This makes a large cutover practical for bead-facing `work` and `plan` commands, but it is not a
rewrite of the entire Beadhive product. The cutover seam is top-level command composition, not a
flag deep inside every old function.

## Superseded decisions

- The read-only/journal-first scope in `docs/spikes/bh-ie41e.5-bd-serve-adoption-decision.md` is
  superseded by API-first reads and selectively proven writes. Its measured evidence, security
  constraints, and failure taxonomy remain inputs.
- The general repository-port family and deterministic in-memory repository proposed by
  `bh-bwnys` and `bh-sy36q` are superseded by the parallel replacement described here.
- The alternative bead-engine direction in `bh-dw3e` is superseded. Useful canonical-artifact,
  credential, and worktree-safety tasks remain valid.
- The rule in `docs/design/herdr-integration-adr.md` that Beadhive must always execute worktree
  mechanics itself is superseded. Beadhive retains policy and identity ownership while the
  selected provider may execute those mechanics. The ADR's durable identity and safe-adoption
  constraints remain in force.

## Consequences

- The new path has a small dependency graph and fast package-local tests.
- The migration is organized by vertical command cohorts and top-level cutovers, not by deeply
  replacing persistence calls throughout legacy modules.
- Generated code can be regenerated and diffed; handwritten translations are limited to policy
  the API does not express.
- Some CLI compatibility will remain while Beads HTTP mutation parity matures.
- A command cannot be cut over until its user-visible behavior and safety invariants are captured
  at the shell boundary.
- Success is measured partly by deleted legacy code and deleted state-emulating tests, not only by
  new coverage.

## References

- Beads v1.3.0 release and HTTP API notes: <https://github.com/gastownhall/beads/releases>
- Beads storage topologies: <https://github.com/gastownhall/beads/blob/main/README.md>
- Herdr CLI worktree operations:
  <https://github.com/herdrdev/herdr/blob/master/docs/next/website/src/content/docs/cli-reference.mdx>
- Worktrunk: <https://github.com/max-sixty/worktrunk>
- Worktrunk custom-path limitation: <https://github.com/max-sixty/worktrunk/issues/1982>
