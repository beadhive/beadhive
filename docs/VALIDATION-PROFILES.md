# Validation profiles

This repository supports two complete validation profiles. The four explicit commands stay
stable when the primary changes:

| Profile | Fast command | Full command |
| --- | --- | --- |
| Native | `just check-native` | `just check-all-native` |
| Pants | `just check-pants` | `just check-all-pants` |

Native uses raw pytest for every core product test and every `packages/*/tests` directory, then builds
all uv workspace distributions with Hatchling. The full command also runs lint, docs, licences,
architecture and wire contracts, real-`bd` integration, and both operator demos. The one
`pants_profile` test recursively builds a Pants PEX, so it runs through `pants-artifact-check`
only in the Pants full profile. Pants also keeps the proven test partition, graph and proof
checks, package sandbox evidence, integration, and demos. `just check-attest-catalog` rejects
missing or partially wired steps in either graph.

Native is the selected primary: `just check` aliases `check-native`, and `just check-all`
aliases `check-all-native`. The beadhive hive configuration must name the corresponding
**explicit** commands in `work.validate_cmd` and `work.validate.submit`, `merge`, `union`,
`molecule`, `merge-main`, `postland`, and `push-main`. The push hook's
`gate_cmd` in `scripts/main-push-gate.sh` must name the same full command as `push-main`.
Using explicit strings changes the ledger's command hash when the profile changes; an ambient
environment variable cannot make an old verdict count for a different gate.

The Pants selective catalog is `work.attest.keys` in the fleet's beadhive entry. Native
single-command validation sets `keys: []`, so ordinary validation executes `work.validate_cmd`
directly. The remaining `attest.impact`, `semantic`, and `trivial` settings are inert without
keys. The active live strings are `just check-native` for ordinary phases and
`just check-all-native` for main-boundary phases; activation evidence is recorded in
`docs/proof/bh-2ygs4-native-activation.md`.

The exact fleet transition is recorded in
`docs/proof/bh-2ygs4-native-fleet.patch`. The reverse profile patch,
`docs/proof/bh-2ygs4-pants-profile.patch`, restores the saved Pants keys and explicitly sets
ordinary commands to `just check-pants` and main-boundary commands to `just check-all-pants`.
Apply it only against the matching active native fleet file, then switch the two aliases and
the push hook's `gate_cmd` to the same Pants profile in a reviewed tree. Run
`just check-attest-catalog` and `just check-all-pants` before relying on new Pants verdicts.
The full Pants comparison was deferred to `bh-ahm6x`; no Pants execution is required to
activate this native profile. Distinct command hashes ensure a native verdict is not reused as
a Pants verdict. The switch does not delete Pants package code,
`pants.toml`, BUILD files, locks, proof manifests, or any Pants cache bytes. Cache size may stay
large until an operator deliberately reclaims it.
