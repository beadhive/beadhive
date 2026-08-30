# ADR: Canonical operation catalog and wire-schema policy

> Status: **decided** (2026-08-30). Decision bead: `bh-j4gbx.1`.

## Context and premise

The operation catalog is canonical for exposed operation identity and shape. That is an operator
decision recorded on `bh-j4gbx`; this ADR accepts the premise and does not compare it with deriving
a catalog from the existing Typer or FastMCP declarations. Application handlers remain canonical
for behavior.

The immediate motivation is the surface audit's diagnosis:

> NO shared registry / codegen ties the two NAME sets together — drift is structural.

Today the CLI and MCP surfaces call the same core functions, but declare their names and shapes
separately. The [surface audit](cli-mcp-surface-audit.md) measures that drift. The
[naming ADR](cli-mcp-naming-conventions-adr.md) has already settled the names, flags, parameter
spelling, and projection rules. This ADR references those rules; it does not reopen them.

Two other constraints are already established:

- [`jsonout.py`](../../src/beadhive/jsonout.py) defines a flat, per-command
  `schema_version` + `command` envelope. Additive fields stay in the same command version;
  removing, retyping, or changing the meaning of a field requires a version bump.
- [`MODULES.md`](../MODULES.md#canonical-operation-and-transport-projection-model) places the
  declarative catalog and schema registry in the operation/schema kernel. A catalog binds known
  application handlers at a composition root; it is not a service locator or a new behavior
  layer.

The cross-repository consumer record `bh-app-tt7` supplies the concrete codec and union evidence:
`@beadhive/factory-contract` checks `schemaVersion` before touching its payload, while a widened
closed union can compile successfully and still break a consumer.

The four decisions below govern the rest of `bh-j4gbx`.

## Decision summary

| Question | Decision |
|---|---|
| Compatibility | Require **full compatibility** within a released major: both backward and forward compatibility. Breaking changes require a new major/version. |
| Closed unions | Treat every membership change to a closed union as breaking and reject it mechanically in the schema compatibility gate. |
| Catalog home | Keep the authoritative catalog in this repository's operation/schema kernel and publish it with the checked-in, language-neutral schema release. |
| Runtime codec | Put version-first decoding in a small hand-written runtime wrapper per target language, driven by generated validators and shared conformance fixtures. |
| CLI process model | Keep the CLI in-process over application handlers. Do not introduce HTTP or require a running server in this molecule. |

## 1. Full compatibility, including closed unions

Every change within one released schema major must be both:

- **backward compatible:** a candidate consumer can read every instance emitted under the prior
  release; and
- **forward compatible:** a consumer built against the prior release can safely read every
  instance the candidate producer may emit.

"Safely" includes the documented consumer rules, not merely whether a JSON parser returns a
value. Result-object consumers must ignore unknown object members, which keeps a genuinely
optional field addition compatible in both directions. A union is open only when its schema says
so and its runtime API exposes an unknown-value fallback. Otherwise it is closed.

Within a major, compatible changes include adding an optional result field, adding a new schema
artifact, and adding a new catalog operation that old catalog readers are required to ignore.
Breaking changes include removing or renaming a field, changing its type or meaning, changing
requiredness, changing an operation identity, and changing the member set of a closed union.

The closed-union rule is part of the mechanical compatibility gate, not a prose obligation and
not a separate source-code lint. Adding a member is additive for a producer but forward-breaking
for a consumer that was built against the closed set; removing a member is backward-breaking.
Therefore the gate must compare closed-union member sets in both directions and reject either
change unless the owning artifact's major/version changes. Its test suite must include an enum
widening fixture so a generic checker that only implements backward compatibility cannot silently
weaken the policy. If the selected compatibility library does not classify that fixture
correctly, the gate adapter must implement the member-set comparison itself.

Consumers should still render or log an unknown union member defensively rather than crash. That
defense protects diagnostics and inputs from a mismatched or nonconforming producer; it does not
make a closed-union change compatible or waive the producer's version bump.

This deliberately tightens the pre-canonical `@beadhive/factory-contract` convention recorded in
`bh-app-tt7`, which allowed producer-side union widening without a version bump while requiring
consumer fallbacks. Once that package is generated from the canonical release, this ADR's full
compatibility rule wins. Existing published v1 payloads and codecs are not rewritten in place.

The release manifest and each schema carry stable identifiers and explicit versions. A permitted
breaking change produces a new major artifact. For an existing `bh <command> --json` contract,
the command's top-level `schema_version` is bumped as well; it remains per-command rather than a
single global application version.

### Downstream assumptions

- `bh-j4gbx.2` may implement one full-compatibility gate against the previous released artifact.
  It must prove optional-field evolution succeeds and closed-union widening fails.
- `bh-j4gbx.3` may add fields under the existing `jsonout` envelope only when old consumers can
  ignore them and new consumers accept their absence. Its existing exit-code and human-output
  contracts remain independent compatibility surfaces.
- Catalog and surface generators may reject an incompatible change before generating code. They
  do not need a second naming or union policy invented per transport.

## 2. The catalog lives with Beadhive and is published as data

The authoritative catalog source lives in this repository, in the operation/schema kernel beside
the declarations that bind catalog entries to application handlers. Versioned, canonicalized
catalog output is checked in and released with the language-neutral artifacts under
`docs/schemas/`. The release manifest gives artifacts stable identifiers independent of their
repository paths.

This location follows ownership: Beadhive owns the operations, their current handlers, the CLI,
and the MCP projection. A separate catalog repository would split one atomic change across
repositories before the boundary has earned that cost. Putting the catalog in a Python package
does not make Python the interchange format: JSON Schema, the catalog artifact, compatibility
fixtures, and release metadata are plain data.

Consumers pin a released artifact or a generated language package. A Rust desktop application or
browser bundle does not import `beadhive` as a Python dependency, read the developer checkout at
runtime, or resolve schemas from a hosted registry. Distribution may later move without changing
artifact identifiers or catalog semantics.

The catalog is declarative input to build-time generators and registration adapters. It must not
dispatch operations, discover services, or move behavior out of application handlers.

### Downstream assumptions

- `bh-j4gbx.2` may establish the versioned release manifest and checked-in schema artifact in this
  repository; a hosted registry and runtime lookup are out of scope.
- `bh-j4gbx.4` may declare the catalog here, reference schemas by stable artifact identifier, and
  bind entries to existing handlers only at the composition boundary. It must not create a
  runtime service locator.
- Consumer-repository work may generate Rust or TypeScript from a pinned release without a Python
  dependency. Moving distribution later must require only changing how that release is fetched.

## 3. Version-first decoding is a runtime-wrapper responsibility

Schema generation supplies types and shape validators; it does not by itself guarantee decoding
order. Every supported target language therefore owns a small hand-written runtime wrapper around
its generated validator. The wrapper performs two passes:

1. parse only enough of the top-level value to read `schema_version` (or the established
   `schemaVersion` spelling of an existing external contract);
2. reject an absent or unsupported version with an error that names expected and received values,
   without reading any other field; then
3. only after the version matches, invoke the generated shape validator and map its result to the
   language's public types.

The canonical schema release includes language-neutral conformance fixtures. At minimum they
include a version-mismatch object whose remaining fields are invalid or hostile, proving that the
mismatch wins before payload validation, plus fixtures for valid data, invalid data, unknown
object members, and open- and closed-union behavior. Every runtime wrapper runs the same fixture
set.

`@beadhive/factory-contract/codec` remains the TypeScript reference behavior while its declarations
become a generated projection. It is not the canonical source of shapes. A generator may scaffold
wrapper code, but generated shape validation must stay behind the wrapper and must not make
version-first behavior generator-specific.

### Downstream assumptions

- `bh-j4gbx.2` must publish the version and codec-behavior fixtures with the schemas. It need not
  ship every language runtime.
- `bh-j4gbx.3` must keep the version discriminator at the top level of the existing envelope.
- Generated-client work in consumer repositories must provide one wrapper per language and prove
  it against the shared fixtures. Merely generating types is not a complete consumer contract.

## 4. The CLI remains in-process

The generated CLI registration calls the existing application handlers in-process. Catalog
projection changes how commands are declared, not where behavior runs. The schema layer plus
`bh <command> --json` is typed subprocess transport for consumers that already launch the CLI;
it does not require a service.

HTTP/OpenAPI remains outside `bh-j4gbx`. The molecule does not add a daemon, make CLI availability
depend on a server, or reopen the omitted tier 3. That preserves stage-0 consumers' ability to run
diagnostics and repository operations when no Beadhive service is available. An HTTP transport may
be proposed later if streaming, shared remote access, authentication, or an agent-harness use case
justifies its process and lifecycle costs; it would be another projection of the same catalog,
not the CLI's mandatory backend.

### Downstream assumptions

- `bh-j4gbx.5` may derive Typer registration directly over existing handlers and preserve current
  no-server behavior. The naming ADR's eight conventions are generation rules, unchanged here.
- `bh-j4gbx.6` may derive FastMCP registration as a separate transport projection. MCP-specific
  measurement, strict-read, privilege-allowlist, and composite-tool behavior stays in that
  adapter.
- No bead in this molecule needs an HTTP server, OpenAPI transport, network client, or server
  lifecycle to align the CLI and MCP surfaces.

## Consequences

- One repository can atomically change an operation, its schemas, catalog metadata, projections,
  fixtures, and compatibility evidence.
- The compatibility gate protects pinned cross-repository consumers, including the closed-union
  case that compiles successfully when enforced only in source code.
- Cross-language consumers share artifacts and behavioral fixtures while retaining idiomatic,
  deliberately small runtime wrappers.
- The catalog remains canonical without becoming a runtime dependency hub, and transport
  projection does not turn Beadhive into a required service.
- A breaking schema change is intentionally more explicit: it needs a major/version change and
  the corresponding consumer update rather than passing as an apparently additive producer edit.
