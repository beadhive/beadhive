# Beadhive wire schemas

This directory is the versioned, language-neutral release point for Beadhive wire contracts.
Consumers pin a release from `index.json`; they do not resolve schemas from a hosted registry at
runtime. Stable `urn:beadhive:wire-schema:*` identifiers are independent of repository paths.

Published release directories are immutable. A compatible change within major version 1 is
published in a new semver directory and added to `index.json`. A breaking change requires a new
major. `just wire-schema-compat` compares the candidate tree with the target branch (override it
with `BH_WIRE_SCHEMA_BASE_REF`) and enforces both directions of compatibility. Optional object
properties may be added; removing or retyping properties, changing requiredness, or changing the
members of an enum marked `x-beadhive-closed-union` is rejected within a major.

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
non-interactive guard, while MCP projections never prompt. Re-render it with
`uv run python scripts/render_operation_catalog.py`; catalog drift is checked in tests.

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

Release 1.5.0 publishes the current operation catalog without rewriting any earlier v1 release.
It adds the Beads schema capture/check operations and their CLI parent paths, and records the
pre-existing guarded interactive workspace-init offer on `bh doctor`. That metadata correction
is explicitly limited to the historic non-interactive doctor projection; operation and parent
identities remain immutable within the major under the catalog compatibility gate.
