# Graph-derived attest keys

Beadhive can split a hive's validation gate into named **attest keys** and ask a build graph
which keys a change can affect. A green key may carry from one tree to another only when an
impact receipt proves its complete input closure unchanged. This is a per-key transfer, never a
claim that the new tree as a whole is green.

The default remains `native-full`: every change invalidates every key. Selective validation is
an opt-in optimization, and uncertainty always costs more validation rather than less.

## This repository's key catalog

The live catalog is configured under `work.attest` in the fleet configuration. Keep it aligned
with the recipes in `justfile`; `just check-attest-catalog` enforces that partition.

| Key | Opaque command | Pants selector | Covers |
|---|---|---|---|
| `docs` | `just attest-docs` | `attest:docs` | Markdown lint |
| `unit` | `just attest-unit` | `attest:unit` | Ruff and licence policy |
| `stateful` | `just attest-stateful` | `attest:stateful` | Proven Pants tests plus the residual native fast suite |
| `integration` | `just attest-integration` | `attest:integration` | Landing integration tests |
| `architecture-contracts` | `just attest-architecture-contracts` | `attest:architecture-contracts` | Architecture, transport, wire, and proof contracts |
| `package` | `just attest-package` | `attest:package` | Pants package attestation |
| `demos` | `just attest-demos` | `attest:demos` | Local-loop and live-ingress operator demos |

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
native residual from a clean checkout. `just pants-ci-benchmark-check` verifies the raw sample
counts and percentile claims in `docs/proof/bh-t8t7r-ci-benchmark.json`. That proof retains the
former selectorless floor as historical before evidence; there is no after-floor command because
the floor was removed rather than made smaller.

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

Pants is the implemented backend. It obtains affected targets with
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
   `native-full` and records the reason.
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

For a one-off diagnostic, request the consumer's `--full` mode where exposed. To roll back a
hive, set `work.attest.impact.backend: native-full` (or remove the impact block). This retains
the catalog but invalidates every key, reproducing the conservative all-key behavior. Removing
the catalog restores the original single `validate_cmd` path. No ledger migration is needed.

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
