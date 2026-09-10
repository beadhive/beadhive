# Selective-CI operational report

Evidence date: 2026-09-10 UTC

## Current disposition

- Certified closures: **0**
- Uncertified closures: **24**
- Production selective routes: **0**

No production selective-CI savings are claimed. Focused developer timings and
exact-tree receipt reuse answer different questions and are not extrapolated into this
report. Both promoted boundaries therefore retain unavailable measurements until
qualifying production samples exist.

## Production measurements

### `commit`

- Qualifying samples: 0
- Before wall time: unavailable
- Before compute time: unavailable
- Before queue delay: unavailable
- After wall time: unavailable
- After compute time: unavailable
- After queue delay: unavailable
- Full-suite frequency: unavailable
- Miss rate: unavailable
- Flake rate: unavailable
- Measured savings: unavailable

### `main-integration`

- Qualifying samples: 0
- Before wall time: unavailable
- Before compute time: unavailable
- Before queue delay: unavailable
- After wall time: unavailable
- After compute time: unavailable
- After queue delay: unavailable
- Full-suite frequency: unavailable
- Miss rate: unavailable
- Flake rate: unavailable
- Measured savings: unavailable

`unavailable` means there are zero qualifying observations, not zero cost or zero
failures. The current policy routes all real work through a full gate because no
closure is eligible.

## Closure inventory

Every row uses `just check` on uncertainty. A row can be reconsidered only after all
listed triggers are cleared and fresh certification plus shadow evidence is checked.

### `adapters`

- Kind: adapter
- State: uncertified
- Evidence date: 2026-09-10
- Owner: adapters closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `config.fragments`

- Kind: contract
- State: uncertified
- Evidence date: 2026-09-10
- Owner: config.fragments closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `config.pure`

- Kind: contract
- State: uncertified
- Evidence date: 2026-09-10
- Owner: config.pure closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `config.store`

- Kind: contract
- State: uncertified
- Evidence date: 2026-09-10
- Owner: config.store closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `contract.agent-launch`

- Kind: contract
- State: uncertified
- Evidence date: 2026-09-10
- Owner: contract.agent-launch closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `contracts`

- Kind: contract
- State: uncertified
- Evidence date: 2026-09-10
- Owner: contracts closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `integration`

- Kind: integration
- State: uncertified
- Evidence date: 2026-09-10
- Owner: integration closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `kernel`

- Kind: kernel
- State: uncertified
- Evidence date: 2026-09-10
- Owner: kernel closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `kernel.lifecycle`

- Kind: kernel
- State: uncertified
- Evidence date: 2026-09-10
- Owner: kernel.lifecycle closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `kernel.plugins`

- Kind: kernel
- State: uncertified
- Evidence date: 2026-09-10
- Owner: kernel.plugins closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `kernel.telemetry`

- Kind: kernel
- State: uncertified
- Evidence date: 2026-09-10
- Owner: kernel.telemetry closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.agents`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.agents closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.config`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.config closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.hives`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.hives closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.planning`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.planning closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.state`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.state closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.work`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.work closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.worktrees`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.worktrees closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `plugin.herdr`

- Kind: plugin
- State: uncertified
- Evidence date: 2026-09-10
- Owner: plugin.herdr closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `plugin.hitch`

- Kind: plugin
- State: uncertified
- Evidence date: 2026-09-10
- Owner: plugin.hitch closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `plugin.observaloop`

- Kind: plugin
- State: uncertified
- Evidence date: 2026-09-10
- Owner: plugin.observaloop closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `plugin.orca`

- Kind: plugin
- State: uncertified
- Evidence date: 2026-09-10
- Owner: plugin.orca closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `plugin.repowise`

- Kind: plugin
- State: uncertified
- Evidence date: 2026-09-10
- Owner: plugin.repowise closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `system-smoke`

- Kind: system
- State: uncertified
- Evidence date: 2026-09-10
- Owner: system-smoke closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

## Registration and invalidation

A new module or plugin cannot register without a present closure and a conformance
declaration. The checked closure row is that declaration: it names ownership, source
scope, direct tests, shared-contract tests, and reverse-dependent tests.
`just test-closure-check` discovers registrations and rejects missing or incomplete
rows.

| Trigger | Automatic fallback | Action |
| --- | --- | --- |
| `boundary-change` | `just check` | recertify before selective use |
| `coverage-staleness` | `just check` | recertify before selective use |
| `escaped-regression` | `just check` | recertify before selective use |
| `schema-change` | `just check` | recertify before selective use |
| `tool-version-change` | `just check` | recertify before selective use |

## Retained safety boundaries

The complete `just check-all` gate is mandatory for these boundaries:

- `child-epic-finish`
- `final-workstream-review`
- `final-workstream-submit`
- `leaf-merge`
- `release`
- `scheduled`

Commit and main-integration routing may use a closure only after certification;
every uncertainty uses `just check`. Changing
`selective_ci.mode` from `certified` to `full` is the one-value rollback.
