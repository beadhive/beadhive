# Validation profiles

This repository supports two complete validation profiles. The four explicit commands stay
stable when the primary changes:

| Profile | Fast command | Full command |
| --- | --- | --- |
| Native | `just check-native` | `just check-all-native` |
| Pants | `just check-pants` | `just check-all-pants` |

Native uses raw pytest for every core test and every `packages/*/tests` directory, then builds
all uv workspace distributions with Hatchling. The full command also runs lint, docs, licences,
architecture and wire contracts, real-`bd` integration, and both operator demos. Pants keeps
the proven test partition, graph and proof checks, package sandbox evidence, integration, and
demos. `just check-attest-catalog` rejects missing or partially wired steps in either graph.

`just check` and `just check-all` are convenience aliases for the selected primary. The live
hive configuration must name the corresponding **explicit** commands in `work.validate_cmd`
and `work.validate.molecule`, `merge-main`, `postland`, and `push-main`. The push hook's
`gate_cmd` in `scripts/main-push-gate.sh` must name the same full command as `push-main`.
Using explicit strings changes the ledger's command hash when the profile changes; an ambient
environment variable cannot make an old verdict count for a different gate.

The live hive's `work.attest.keys` catalog is the Pants selective profile. Native single-command
validation removes that catalog so ordinary validation executes `work.validate_cmd` directly.
Keep the Pants catalog available as a reversible profile; it is not a deletion of Pants package
code, `pants.toml`, BUILD files, locks, or proof manifests. A profile switch does not delete
Pants cache bytes. To recover, restore the Pants catalog and explicit Pants command strings in
the hive config, switch the two aliases and push hook to Pants, then run
`just check-attest-catalog` and `just check-all-pants` before relying on new verdicts.
