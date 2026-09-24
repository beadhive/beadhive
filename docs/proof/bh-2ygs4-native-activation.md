# Native validation switch: exact-tree evidence

Activation status: prepared, not applied. A scoped live-fleet write was rejected pending
explicit operator approval; the current hive validation settings remain unchanged.

The native full profile passed on clean commit
`6fe0a96189e3148cbc023818277765162314accd`, tree
`3f679b9f35b9bee6d0a670d6f84e22d4517a9fd3`. The command was
`just check-all-native`; wall time was 778 seconds. The run did not execute Pants.

| Phase | Collection | Result |
| --- | ---: | --- |
| Raw core pytest (`not integration and not pants_profile`) | 9,348 | 9,336 passed; 12 skipped |
| Raw real-`bd` integration pytest | 68 | 66 passed; 2 skipped |
| Workspace package pytest | 36 | 36 passed |

The total test collection was 9,417. The core and integration selections had no overlap;
their union omitted exactly one test, the explicitly marked recursive Pants PEX packaging
test. All four `packages/*/tests/test_*.py` paths appeared in the package collection.
The workspace build produced both sdist and wheel for each of `beadhive`,
`beadhive-package-template`, and `beadhive-pants` (six artifacts). The local-loop and live
ingress demos completed. The full run log at `/tmp/bh-2ygs4-native-full-final.log` has SHA-256
`08b8d4049880fe19f0aa3975a81cf8044d92d0bc457e7ed91f70ce66e96dd1e1`.

Focused structural/drift preservation on the same code tree ran 17 tests successfully:
`tests/test_check_attest_catalog.py`, `tests/test_native_validation_graph.py`,
`tests/test_workspace_native_gate.py`, the Pants-full activation assertion in
`tests/test_pants_activation.py`, and the pure Pants build-closure assertion in
`tests/test_beadhive_pants_artifacts.py`. The catalog requires both explicit Pants
recipes, the Pants-only PEX proof, and native exclusion of that proof. `pants.toml`, BUILD
files, the `beadhive-pants` package, lockfiles, and historical proof manifests remain tracked.
No Pants full comparison was run; that work is deferred to `bh-ahm6x`.

The fleet profile transition is the exact patch in `bh-2ygs4-native-fleet.patch`.
The inverse switch from native to explicit Pants commands is
`bh-2ygs4-pants-profile.patch`. The latter was dry-run and applied in an isolated temporary
copy, then byte-compared to the expected Pants profile. It did not execute Pants. Applying
the native patch to the live fleet requires approval because it replaces the eight required
Pants attest keys with the native single-command path; the prepared patch does not alter
other hive entries or fleet settings.

The ledger hashes the literal command string with SHA-256, truncated to 16 hex characters.
The two profiles therefore have distinct fast and full keys:

| Gate | Command | Ledger command hash |
| --- | --- | --- |
| Native fast | `just check-native` | `c3ede8e648f0acbd` |
| Pants fast | `just check-pants` | `a35b67d3bacd92d9` |
| Native full | `just check-all-native` | `0d8cb31504a0f045` |
| Pants full | `just check-all-pants` | `664f76f442359c76` |

The measured managed Pants cache baseline was 27,469,893,903 bytes at
`/home/bees/.cache/beadhive/pants`; the separate legacy cache was 741,129,382 bytes at
`/home/bees/.cache/pants`. Neither cache is deleted by the switch.
