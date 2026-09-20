# `bh-28emy.1`: Beads 1.3.0 and Dolt 2.3.5 toolchain pin

## Artifact and installation decision

The operator directed this bead to use the official release artifacts rather than compile either
tool. `flake.nix` therefore packages immutable GitHub release archives with `stdenvNoCC` and an
independent fixed-output hash for every supported system. This replaces the original
source-plus-vendor-hash wording: there is no vendored source closure or compiler in these
derivations. The recorded release commits remain provenance metadata:

- Beads 1.3.0: `f45b249ce6b40ba62aecc03949e6371e8f7c79d8`
- Dolt 2.3.5: `ad65af6cc937d10fa3c88e2041fed4325968b581`

`nix flake check --no-build`, `nix build .#beads --no-link`, and
`nix build .#default --no-link` all passed. The focused drift contract passed 9 tests and checks
the versions, release URLs, per-platform hashes, renamed derivations, absence of the RC/2.3.1
pins, and container metadata.

## Before and after

The managed profile was installed with the existing `nix profile install` path. On this frame,
the machine named `beadhive-factory` is also "this host"; there is one install plane, not a second
machine to mutate.

| | profile generation 2 (before) | profile generation 3 (after) |
|---|---|---|
| profile target | `/nix/store/v4gsr384jm3rd65hirnqfgc9q30nz9nl-profile` | `/nix/store/7ymxvgx0wdvkcn82csg1rp02nwcfnnyl-profile` |
| `bd` target | `/nix/store/caavdh2rbhzk4p0kx2yqwhq4zjc0ymsw-beads-1.3.0-rc.1/bin/bd` | `/nix/store/b7lc4xl35b2sl6h9pg492gh634pdxafy-beads-1.3.0/bin/bd` |
| `bd version` | `1.3.0-rc.1 (dev)` | `1.3.0 (f45b249ce: HEAD@f45b249ce6b4)` |
| `dolt` target | `/nix/store/bpldh49d2c4yzv45cdrbgkvqqqx396p9-dolt-2.3.1/bin/dolt` | `/nix/store/3gsing0yw4rqfmh5hlz0lm99jqwysd7d-dolt-2.3.5/bin/dolt` |
| `dolt version` | `2.3.1` | `2.3.5` |

Generation 3 was installed before any production-hive operation. `bh-qn9zt` subsequently repaired
the frame command environment, after which both an ordinary fresh login command and this bare
non-login transient service resolved the generation-3 binaries:

```sh
systemd-run --user --wait --pipe --collect \
  --unit=bh-28emy-1-service-probe \
  /path/to/beadhive/scripts/probe-frame-toolchain-path.sh
```

The service exited `0/SUCCESS` as uid `8335`, HOME `/home/bees`. Its PATH began
`/home/bees/.local/bin:/home/bees/.nix-profile/bin:/nix/var/nix/profiles/default/bin`; the local
compatibility links resolved into the same generation-3 store paths. It reported Beads 1.3.0,
Dolt 2.3.5, and Nix 2.35.1, with no RC/dev or alternate binary in the resolved path.

## Migration boundary

No Dolt server was started, no migration command was run, and no production hive was successfully
opened with these binaries. The live shared-server data directory was not used by any build or
probe. The explicit production remote/ref inventory and designated-migrator gate remain owned by
`bh-28emy.2`; only after that bead completes may the final binary open and migrate production.
