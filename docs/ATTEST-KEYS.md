# Graph-derived attest keys

Beadhive can split a hive's validation gate into named **attest keys** and ask a build graph
which keys a change can affect. A green key may carry from one tree to another only when an
impact receipt proves its complete input closure unchanged. This is a per-key transfer, never a
claim that the new tree as a whole is green.

The default remains `native-full`: every change invalidates every key. Selective validation is
an opt-in optimization, and uncertainty always costs more validation rather than less.

## This repository's key catalog

The key catalog is configured under `work.attest` in the fleet configuration. The keys partition
`just check-all-native`, the push gate, and run only the native framework; Pants steps live only
in the optional `just check-all-pants` profile (whether Pants stays is bh-ahm6x).
`just check-attest-catalog` checks that partition and both explicit native and Pants recipe
graphs. The live native profile uses the path backend and keeps the catalog active; uncertainty
falls back to the complete key set.

| Key | Opaque command | Pants selector | Covers |
|---|---|---|---|
| `docs` | `just attest-docs` | `attest:docs` | Markdown lint |
| `unit` | `just attest-unit` | `attest:unit` | Ruff and licence policy |
| `stateful` | `just attest-stateful` | `attest:stateful` | Native stateful suite (hermetic fence) |
| `root-composition` | `just attest-root-composition` | native paths | Root tests that compose workspace package APIs, plus `tests/contracts` |
| `integration` | `just attest-integration` | `attest:integration` | Landing integration tests |
| `architecture-contracts` | `just attest-architecture-contracts` | `attest:architecture-contracts` | Architecture, transport, wire, and proof contracts |
| `packages` | `just attest-packages` | `attest:packages` | Ruff and sandboxed tests for every other `packages/*` distribution |
| `bd-cli` | `just attest-bd-cli` | `attest:bd-cli` | Ruff and sandboxed tests for `packages/beadhive-bd-cli` only |
| `demos` | `just attest-demos` | `attest:demos` | Local-loop and live-ingress operator demos |

`packages` owns the template, Beads client, core, plugins, and worktrees distributions. The
template in `packages/_template` gives each package recursive source, resource, and test
targets tagged `attest:packages`; `just pkg <name> check` validates one package during
development, while `just packages-check` is the whole-tree recipe called by
`just attest-packages`. Copying the template to a new package and running `uv lock` includes it
in the workspace and Pants graph. The native catalog lists current package directories
explicitly so a new package remains uncovered until its validation ownership is reviewed. Every
package test runs in its own Pants sandbox. A package
backend is selected through plugin manifest discovery and a lazy bootstrap binding; the attest
key itself does not import or register backend implementations.

`beadhive-bd-cli` has its own dedicated `bd-cli` key instead (bh-vq34o): it implements
`beadhive-core`'s ports and changes far more often than the rest of `packages/*`, so folding it
into the shared `packages` key would invalidate every other distribution's proof on every touch.
Its targets carry `attest:bd-cli` rather than `attest:packages`, and `just bd-cli-check` — not
`just packages-check` — is its whole-package recipe, called by `just attest-bd-cli`. The native
`bd-cli` selector also includes `beadhive-core`, `beadhive-beads-client`, `pyproject.toml`, and
`uv.lock`, because its proof depends on those package APIs and the resolved dependency graph.

Both native package keys select `justfile` and `scripts/*`. Those paths cover the recipe bodies,
the hermetic fence, watchdog, pytest report wrapper, and package test-path discovery instead of
maintaining a second fragile list of runner helpers. Both also select `pyproject.toml` and
`uv.lock` for Ruff, workspace, build, and resolution inputs. Direct `beadhive-bd-cli` paths remain
excluded from `packages`, while its core and Beads-client dependency inputs select both keys.
Every package sync and build is package scoped: `packages` validates the template, core, plugins,
and worktrees; its Beads-client leaf owns that distribution; and `bd-cli` owns its distribution.

The native `@root-composition` selector expands to workspace distributions consumed by the root
project. Consumption is derived from direct and optional project requirements, every dependency
group (including included groups), and workspace package source roots vendored into the root
wheel. A package-only change selects its package key and `root-composition`; it does not select
the complete stateful, integration, or demo keys. `scripts/root_composition_tests.py` registers
the root tests for each consumed package and always includes `tests/contracts`. The ordinary and
full native profiles run `stateful-native` and `root-composition-native` as disjoint pytest file
partitions, so this optimization removes work only from selective package gates. A root source
change selects both partitions and the other root keys. An unknown path or an impact backend
error still falls back to every key.

`root-composition` also owns `root-workspace-check`: the Pants package tests/build and the root
release smoke artifact. Pants directly imports the root distribution, while the root wheel vendors
five workspace libraries including bd-cli, so those operations cannot belong to either isolated
package key. The full native and Pants gates list this leaf exactly once. Its native selector
includes `src/*`, `README.md`, `scripts/*`, root metadata, every derived consumed package, and
`docs/design/*.md`; the last pattern covers the compatibility ledger read by a contract test.

The key command receives no changed-path or package argument. It therefore runs the deduplicated
union of every registered package's root tests, rather than claiming per-package command
narrowing. On the measured bh-3quwp command implementation, that conservative union is 27 files
and 700 items: the root pytest leaf took 34.740 seconds, while the complete key took 49.530 seconds
for 734 items after its 34-test Pants/root-artifact leaf. The impact backend requires every derived
root workspace dependency to have a `PACKAGE_TESTS` entry; a missing entry makes resolution fail
closed to all keys instead of silently omitting an unknown root test closure. It also scans direct
imports in root tests and requires each package consumer to appear in that mapping or the
registered contract set.

The final combined v0.20.0 wave tree (`7f9d4ba6`, tree `89cc1ed8`) also measured the full root
path with its required cross-blocker fixes. Its six selected keys passed in 1,131.720 seconds:
`unit` 1.690, `stateful` 461.010, `root-composition` 51.370, `integration` 375.750,
`architecture-contracts` 83.490, and `demos` 158.410 seconds. The stateful partition collected
8,857 items and passed 8,846 with 11 skipped; root composition passed 699 of 700 root items with
one skipped, 34 Pants tests, and the release smoke. Against the 939.531-second five-key pre-split
sample, adding root composition makes the new six-key total 192.189 seconds (20.46%) higher. The
corresponding five after keys total 1,080.350 seconds, 140.819 seconds (14.99%) higher. The
package-only `packages` plus `root-composition` sample remains 73.750 seconds, a 91.6% reduction
from its 881.544-second pre-split selection.

The Pants proven-tests manifest remains an optional Pants-profile concern. Native architecture
and lifecycle gates do not require an edit to that manifest when a new package test is added.
`scripts/native_package_tests.py` discovers native package test roots directly from the workspace;
its behavioral contract executes a newly created package test while both Pants manifests remain
byte-identical. Because the recipe passes its output through shell command substitution, discovery
rejects path names containing shell metacharacters or whitespace before emitting the raw argv.
The root-composition renderer applies the same validation to registered root tests and discovered
contract tests before emitting raw paths or `--ignore` arguments. Its standalone validation
recipe runs before either consumer performs command substitution, so an invalid render cannot
degrade into pytest's default full-tree collection.

Keys are policy, not test-framework plugins. `cmd` is an opaque string that Beadhive executes
verbatim. A key is required unless configured with `policy: optional`. An optional key may be
absent or return the explicit unknown exit code 75 without blocking, but a real nonzero result
always blocks.

Execution state is a separate axis from evidence policy. Set `enabled: false` only with a
non-blank `disabled_reason`; the key is then visibly skipped on every selective run and is
absent from impact resolution, aggregate proof, and the carry ledger. `bh config validate` and
`bh doctor` warn while the deviation remains configured. An optional timezone-aware
`disabled_until` makes a temporary exception fail closed: at or after that instant the key runs
again even if `enabled` was not restored. For example:

```yaml
- name: stateful
  cmd: just attest-stateful
  policy: required
  enabled: false
  disabled_reason: "temporary capacity incident bh-example"
  disabled_until: "2026-09-22T00:00:00Z"
  selectors: {pants: "attest:stateful"}
```

Re-enable by setting `enabled: true`; retained reason/expiry metadata is inert, so toggling the
single execution flag restores the enabled behavior without changing the key's command, policy,
or selectors.

There is no selectorless attest key. `require-bd` observes host `PATH`, but that does not make it
globally relevant: it guards `attest-integration`, whose pytest selection can otherwise skip
vacuously when `bd` is absent. The demos perform their own executable preflight and fail rather
than skip. Their application and fixture inputs are declared in `scripts/BUILD`; code and config
owners select `attest:demos`, and the demos run hermetically.

The stateful command is itself a checked partition. Tests carrying `pants:proven` run as
individual sandboxed Pants processes and are omitted from the native pytest collection; every
other test defaults to native. `scripts/pants_ci.py verify` proves the sets are disjoint and
exhaustive, rejects a missing source or BUILD override, and is part of both architecture gates.
Developer checks query changed targets and transitive dependents from the integration merge
base. A proven-only impact runs only those Pants tests; affected unproven tests run the native
residual, while global inputs and uncertain or unowned executable changes fail closed to both
complete partitions. Submit/merge attestation always runs the complete proven closure plus the
native residual from a clean checkout. `just pants-ci-benchmark-check PATH` verifies an explicitly
supplied runtime benchmark report. Benchmark samples remain local validation artifacts.

## Receipt and backend contract

Core code depends on one build-system-neutral port:

```text
ImpactResolver.resolve(repo, base_rev, head_rev, keys) -> ImpactReceipt
```

An `ImpactReceipt` records the backend and version; base and head trees; changed and unowned
paths; global inputs hit; invalidated and unaffected keys; evidence mapping keys to backend unit
identifiers; a fallback reason; elapsed time; and a content digest. The digest is stored with
each carried verdict.

A backend must determine:

1. which unit owns every changed path, including deletion ownership from the base tree;
2. the transitive dependents of those owners; and
3. which keys select those affected units.

Pants is the implemented backend, supplied by the `beadhive-pants` plugin's `build.impact`
capability. Its separate `build.verify` capability supplies build graph health diagnostics.
Core imports their public contracts and selects their runtime bindings at bootstrap; it has no
static dependency on the package implementation. The impact backend obtains affected targets with
`pants --changed-since=<base> --changed-dependents=transitive peek`, maps target tags named
`attest:<key>` to keys, and trusts test targets only after they have passed in the Pants sandbox
with declared inputs. The sandbox-proof manifest is `scripts/pants_proven_tests.json`.

The same targets also carry exactly one change-category dimension:

- `category:code` for application and executable support code;
- `category:test-only` for tests and fixtures;
- `category:build-system` for BUILD files, locks, Pants/toolchain configuration, and build tools;
- `category:docs` for documentation, including Markdown at the repository root; and
- `category:config` for hive, automation, deployment, and tool configuration.

These are graph tags, not path rules in Beadhive. The backend reads them from the same `peek`
rows used for ownership and `attest:*` selection. A changed owner without exactly one known
category makes the resolver fail closed to the full key set. A build-system owner also forces the
full set because it changes the graph oracle itself. For a changeset spanning categories, Pants'
affected units naturally select the union of their keys. In particular, config selects `demos`,
while docs-only selects `docs`; no category intersection or conflict rule exists.

The same port admits other build graphs without changing gate consumers:

- Turborepo can run `turbo run <tasks> --filter=...[<base>] --dry=json`, map affected task names
  to keys, treat `globalDependencies` as global inputs, and require enforced task `inputs` before
  declaring a key proven.
- Bazel can intersect `rdeps(//..., set(<changed files>))` with
  `attr(tags, "attest:<key>", //...)`, treat workspace/module files, `.bazelrc`, and toolchains
  as global inputs, and use its sandboxed actions as proof of declared inputs.

These are mapping sketches, not claims that Turborepo or Bazel adapters are implemented.

## Fail-closed rules

Every backend follows the same rules:

1. An unowned changed path invalidates every key. A missing owner never means safe.
2. A changed global build input invalidates every key. Pants global inputs include
   `pants.toml`, every `BUILD` and lock file, `pyproject.toml`, `uv.lock`, `justfile`,
   `.mise.toml`, and `scripts/hermetic.sh`.
3. A resolver error, timeout, unavailable backend, or backend-version mismatch falls back to
   `native-full` and records the reason. This `on_unresolved: fallback` policy is the normative
   production mode. Developers diagnosing selection may opt one hive into `strict`, which exits
   76 with the reason before any key runs instead of paying for the conservative all-key run.
4. An owner with no category tag, an unknown category, or more than one category fails closed to
   every key.
5. A selected unit that has not been sandbox-proven makes its key depend on every change. A key
   without a selector does too.
6. A command that reads Git metadata or other state outside the graph never carries.

Only a current green verdict can be a carry source. Carries do not chain, do not refresh the
source verdict's age, and never transfer red or unknown results.

## Operation and rollback

Enable Pants impact resolution for one hive:

```yaml
work:
  attest:
    impact:
      backend: pants
      on_unresolved: fallback  # production default; use strict only to diagnose selection
    keys:
      - name: docs
        cmd: just attest-docs
        selectors: {pants: "attest:docs"}
      - name: demos
        cmd: just attest-demos
        selectors: {pants: "attest:demos"}
```

Normal `bh work check`, `submit`, `review --run`, `merge`, `finish`, and
`bh release attest --if-needed` consumers then resolve keys and print whether each key ran,
carried (including source tree and receipt digest), or remained unknown.

Batch lifecycle boundaries use the same proof model. `bh work submit --group` resolves the
shared batch branch once and records a green verdict for each key it actually runs. On an
unchanged branch, `bh work merge --group` reuses those exact-tree, exact-command verdicts and
runs only keys whose proof is missing or invalidated. After the batch merge, the molecule tree
can reuse the same key proof when its content is byte-identical. A rebase, command change,
expired verdict, red result, or unknown result forces that key to run again.

The fallback and bypass contracts remain unchanged:

- With no key catalog, group submit and merge run the phase's monolithic `validate_cmd`; merge
  keeps its existing exact-tree reuse behavior.
- An enabled validation bypass audits one bypass of the monolithic phase boundary. It does not
  invoke key selection and creates no green key proof.
- Disabled or policy-skipped keys create no proof. A red, unknown, or interrupted key also
  cannot satisfy a later boundary.
- A successful configured full gate expands into key proof only at the documented full-gate
  phases (`molecule`, `merge-main`, `push-main`, and `postland`) and only when the command is the
  exact configured partition. Ordinary submit and merge commands never broaden proof beyond
  the keys they actually ran.

For a one-off diagnostic, request the consumer's `--full` mode where exposed. To roll back a
hive, set `work.attest.impact.backend: native-full` (or remove the impact block). This retains
the catalog but invalidates every key, reproducing the conservative all-key behavior. Removing
the catalog restores the original single `validate_cmd` path. No ledger migration is needed.

`work.attest.impact.on_unresolved` layers per hive over the global setting like `backend` and
`timeout_seconds`. Its accepted values are `fallback` and `strict`; an invalid hand-edited value
degrades to `fallback` at runtime and is reported by `bh config validate`. Strict mode is a
deliberate development-only exception to the Attested Green invariant that impact uncertainty
may only invalidate more work: it makes unresolved selection an explicit error so developers can
observe and repair it. Exit 76 is distinct from key UNKNOWN (75) and from the release/decision
0–3 vocabulary.

## Docs-only timing example

The motivating change, bh-hhpuo, modified two Markdown files and no code. On 2026-09-19 its
pre-selective landing ran `just check-all` twice, taking **more than 8 minutes per run**. The
equivalent commands used at the release and molecule boundaries were:

```sh
/usr/bin/time -f 'elapsed=%e s' just attest
/usr/bin/time -f 'elapsed=%e s' bh work finish <epic>
```

That historical before result is preserved as a lower bound because the original run recorded
“8+ minutes”, not sub-second precision. For a docs-only selective run, Pants resolves the owned
doc and its proven dependents, runs only `docs`, and carries `demos` and every other eligible
unaffected key. The bh-1j3ei rollout records the directly observed after timings at
the two real boundaries rather than extrapolating them: the `.10` submit output is the docs-only
attest sample, and the epic's `finish` output is the land sample. Both outputs must name the
selected keys and receipt states; a fallback makes the sample a full-gate result and must not be
reported as a selective speedup.

As a component measurement on the `.10` docs-only tree, the selected docs command took **6.749
seconds** on a warm checkout:

```sh
TIMEFORMAT='elapsed=%R s'; time env -u FORCE_COLOR just lint-md
```

This is the key's execution time, not an invented `just attest` or `bh work finish` total; those
boundary measurements also include graph resolution, clean-checkout setup, and ledger work.

The first selective `.10` submit attempt took **547.701 seconds**. Under the pre-split catalog it
ran `always-run`, `docs`, and `stateful` green, but correctly refused to submit because four
required keys had no qualifying source verdict to carry. That is bootstrap evidence, not a
successful after result: seed current base verdicts before using it in a speedup comparison.

For future comparisons, start from a tree with current green verdicts, make only an owned docs
change, run the two commands above with a warm dependency cache, and retain both wall time and
the per-key output. Cache state, base/head revisions, backend version, and any fallback reason
belong with the measurement.
