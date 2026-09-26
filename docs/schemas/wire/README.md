# Beadhive wire schemas

This directory is the versioned, language-neutral release point for Beadhive wire contracts.
Consumers pin a release from `index.json`; they do not resolve schemas from a hosted registry at
runtime. Stable `urn:beadhive:wire-schema:*` identifiers are independent of repository paths.

## Supported releases and deprecation

Release 1.5.0 is the supported baseline. It was cut from the tree as it stood on 2026-09-26 and
carries the live operation catalog. Every release below it (1.0.0–1.4.0) is **deprecated**. Each
one has `"deprecated": true` in its `index.json` entry and in its `release.json`. The files stay
for history and are otherwise unchanged, but no test, script, or `just` recipe compares against
them. They carried unreconciled drift: verbs were appended in place to 1.2.0, while 1.3.0 and
1.4.0 shipped an older catalog. The operator dropped compatibility with them on 2026-09-26
because no consumer used them (bh-bwnys.5). Pin 1.5.0 or later.

Supported release directories (1.5.0 and later) are immutable. A compatible change within major
version 1 is published in a new semver directory and added to `index.json`. A breaking change
requires a new major. `just wire-schema-compat` compares the candidate tree with the target branch
(override it with `BH_WIRE_SCHEMA_BASE_REF`) and enforces both directions of compatibility.
Optional object properties may be added; removing or retyping properties, changing requiredness,
or changing the members of an enum marked `x-beadhive-closed-union` is rejected within a major.
Operation-catalog data is matched by operation name, so a new operation may land anywhere in the
name-sorted list. Removing, renaming, or changing a published operation fails.

## Add a CLI verb

The live operation catalog always tracks the release `index.json` names as `latest`
(`uv run python scripts/render_operation_catalog.py --check`, part of `just check-native`).
Adding a verb therefore publishes the next minor release:

1. Implement the Typer command and register its operation in the authoritative declaration,
   `beadhive.kernel.operations` (the `_CLI_ROWS` entry, plus any parent, MCP, or read-path rows
   it needs).
2. Run `just wire-publish`. It cuts the next minor release (for example 1.5.0 → 1.6.0) by copying
   the latest release directory, rendering the live catalog into it, bumping `release_version` in
   its `release.json` and `conformance.json`, and appending it to `index.json` as `latest`. It
   then regenerates everything derived from the catalog: the transport projection inventory, the
   package contract bundle, the contract compatibility report, and the closeout digests.
3. Commit the new release directory and the regenerated artifacts with the verb, then run
   `bh work check`.

`just wire-publish` is idempotent on a branch. A release the branch has already cut is absent
from the integration base's `index.json`, so re-running the command refreshes that unpublished
release in place instead of cutting another minor. When a rebase finds that the base published the
same version in the meantime, drop your release directory and its `index.json` entry, keep the
base's, and re-run `just wire-publish` to cut the next minor.

Changing an existing published operation shape is a breaking catalog change. After reviewing that
compatibility decision, run `uv run python scripts/publish_wire_release.py --major`, followed by
the remaining derived-artifact generators in `just wire-publish`. The new major carries the live
catalog while every earlier release directory remains unchanged.

Do not edit a published release by hand. `just wire-schema-compat` rejects any in-place change to
a 1.5.0-or-later release. It also rejects a new release that removes or changes an operation, an
artifact, or a schema constraint that the previous supported release published.

## Artifacts by release

The artifacts below were introduced in releases that are now deprecated. Release 1.5.0 carries
all of them forward; only its operation catalog was re-rendered from the live declaration.

The v1 command schemas describe the JSON emitted by `bh hive status --json`,
`bh hive survey --json`, `bh hive onboard --json`, and `bh hive ready --json`. They intentionally
do not redesign the established status/survey emitters. The lifecycle schemas preserve bh's own
human text alongside structured state. The control-plane schemas preserve the current
`@beadhive/factory-contract` field spellings and publish shared version-first decoder fixtures in
`conformance.json`.

Release 1.2.0 introduces `operation-catalog-v1.json`, generated from
`beadhive.operation_catalog`, the authoritative declaration beside the application/core layer.
The catalog instance (`urn:beadhive:wire-catalog:operations:1`) and its validation schema
(`urn:beadhive:wire-schema:operation-catalog:1`) are distinct manifest artifacts. The declaration
records operation shape and projection policy only: it contains no handler references and is never
a runtime service locator. Every CLI/MCP projection declares its granularity, progress transport,
and interactivity policy; prompt-capable CLI projections name both the live prompt seam and their
non-interactive guard, while MCP projections never prompt. Publish catalog changes with
`just wire-publish` (see [Add a CLI verb](#add-a-cli-verb)); catalog drift is checked in tests.

Release 1.3.0 introduces `plugin-manifest-v1.schema.json`, the language-neutral contract for
declarative plugin identity, compatibility, capability ownership, lifecycle observation,
configuration ownership, CLI presentation, and security prerequisites. Plugin manifests declare
credential references and executable requirements, never credential values or executable command
lines. The policy and discovery semantics are ratified in the
[Plugin kernel v1 ADR](../../design/plugin-kernel-v1-adr.md).

Release 1.4.0 registers `config-v1.schema.json`, deterministically generated from the canonical
models in `beadhive.modules.config.contracts`. Generate it with
`uv run python scripts/generate_config_schema.py`; check drift with the same command plus
`--check`. The existing `bh config schema --json` list remains the explicitly versioned legacy
row projection rather than changing shape in place.

Release 1.5.0 is the supported baseline described above. It adds no new artifacts; its catalog
is the live catalog at the time of the cut.
