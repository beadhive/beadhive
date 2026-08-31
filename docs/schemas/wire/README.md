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
