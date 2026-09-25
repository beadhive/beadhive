# Official contracts and semantic telemetry v1.0.0

This release makes the package-owned contract bundle and the semantic telemetry envelope an
official supported surface. The bundle is local, language-neutral, checksum verified, and does
not require a hosted registry or generated clients. The exact bytes ship under
`beadhive/schemas/contracts/v1.0.0`; `inventory.json` is the entry point.

## Pre-publication snapshot correction

The host snapshot and its Gateway projection were corrected before any supported consumer or
Factory release received this bundle. The canonical v1 seed is now the compact
`beadhive.snapshot-summary/v1` shape: a deterministic, byte-bounded work-item summary list with
truthful coverage and hive-wide invalidation events. The earlier checked rich snapshot draft was
never shipped, so the reviewed publication decision replaced that draft in the v1 baseline and
release bundle instead of creating a misleading alternate major or compatibility bridge. This
is a one-time pre-publication correction; the immutability and compatibility rules below apply
to this corrected handoff.

## Supported artifacts

The inventory is authoritative for the 23 artifacts in 17 families: configuration, plugin
manifests and fragments, operations, CLI and MCP projections, host OpenAPI, gateway contracts,
lifecycle events, the telemetry envelope, seats, launch and workspace values, and the
prepare/commit/abort and receipt families. Each row names its canonical source owner, stable ID,
major version, compatibility policy, conformance case, package-relative path, and SHA-256.

The older `docs/schemas/wire` releases remain immutable supported snapshots. This release does
not delete or silently supersede an artifact that was not promoted into the package bundle. The
generated [compatibility report](../proof/official-v1-contract-compatibility.json) accounts for
every manifest-declared historical artifact, classifies exact and compatible matches, and records
policy divergences that remain pinned to their historical release.

## Version negotiation

Consumers must pin the package version, `release_version` from `inventory.json`, and the stable
artifact ID. A v1 reader accepts only the contract major it implements and verifies the inventory
checksum before parsing an artifact. Optional additive fields must be ignored unless their
artifact policy says otherwise. A consumer must not select a contract merely from the highest
number it happens to find or resolve a stable URN without the containing release context.

CLI, MCP, and HTTP consumers negotiate at their existing transport boundaries: CLI JSON and MCP
clients use the catalog's declared version/projection, while HTTP clients use the OpenAPI and
gateway contract version fields. There is no runtime registry lookup and no implicit downgrade.

## Deprecation

Within v1, schemas and OpenAPI evolve additively and catalogs follow their declared append-only
policy. An artifact identity, required field, closed-union member set, operation identity, access
requirement, or projection privilege cannot be changed in place. Deprecation starts with an
additive replacement plus release-note notice; removal waits for a new major. Historical wire
artifacts stay available for consumers pinned to those immutable bundles.

## Redaction

Telemetry carries finite event names, phases, outcomes, and attribute keys. Secret material,
credentials, tokens, arbitrary paths, task text, event content, and unbounded identifiers are not
metric labels. Correlation and instance identifiers may be attached to trace/event context but
are excluded from metric dimensions. Adapter and exporter errors are sanitized and are
observational only: telemetry failure cannot change an operation or lifecycle outcome.

## Consumer upgrades

Before upgrading, compare the installed inventory checksum set with the version under test, run
the supplied conformance cases, and evaluate the generated compatibility report for every
artifact ID the consumer uses. Upgrade decoders before enabling newly added fields or members.
Keep the previous package/wire snapshot available for rollback. Plugin consumers must use the
manifest and plugin-fragment schemas together; they must not infer credentials, commands, or
capability ownership from telemetry.

## Operational validation

The release boundary is verified without external services or collector deployment:

- clean-install/package proof builds a wheel offline, unpacks it, and verifies every packaged
  artifact byte against `inventory.json`;
- generation and evidence checks are deterministic and read-only under `--check`;
- CLI, MCP, and API runtime drift checks compare the installed artifacts with their canonical
  transport/OpenAPI owners;
- telemetry-disabled checks prove the SDK and flush hook are untouched when disabled;
- dead-collector checks prove one finite CLI/stdio flush budget and a non-fatal timeout outcome;
- the full correctness gate is `bh work check`, whose tree-keyed receipt binds all checks to the
  exact release commit. Generated documentation never embeds a self-referential commit hash.

The focused release matrix is:

```console
uv run pytest -q tests/test_contract_release_evidence.py
uv run pytest -q tests/test_otel.py::test_disabled_by_default_no_op \
  tests/test_otel.py::test_disabled_does_not_touch_otel \
  tests/test_otel.py::test_cli_flush_semantics_share_one_budget_and_report_dead_collector_timeout
uv run python scripts/generate_contract_release.py --check
uv run python scripts/generate_contract_release_evidence.py --check
```

The lifecycle submit/check receipt is the source of truth for the final full-gate result at the
candidate tree. No document should claim that result before that receipt exists.

## Maintainer procedure

1. Change the canonical domain/transport owner; do not hand-edit a generated artifact.
2. Run both release generators with `--write`, inspect the inventory and compatibility report,
   and explain every `policy-divergence-retained` row before publication.
3. Add conformance coverage and consumer guidance for each additive field/member.
4. Run the focused matrix, then the full lifecycle check at the clean signed candidate tree.
5. Advance the immutable published baseline only as a separately reviewed publication decision.
6. For a breaking change, create a new major directory, stable IDs, negotiation guidance, and a
   migration/deprecation window; never rewrite an already published directory.

## Plugin-author procedure

1. Treat the plugin manifest and the named plugin-config fragment as the public authoring
   contracts; keep capability, lifecycle, configuration, and security declarations explicit.
2. Add optional fields or new capabilities only with conformance cases and backward-compatible
   defaults. Do not tighten access, privilege, requiredness, or a closed union inside v1.
3. Test against the oldest supported v1 bundle and the candidate, then publish the minimum
   supported package/contract release in plugin upgrade notes.
4. Request a new contract major when compatibility policy rejects the intended change.
