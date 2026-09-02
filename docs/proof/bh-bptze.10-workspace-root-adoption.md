# bh-bptze.10 workspace-root adoption

## Provenance and boundary

This reconciliation merges accepted source tip
`832f29b96ee513cc5d554296035eda9009f433be` (tree
`cf8113f2081b6e8d0cdb4f54116ad9d58460066e`) into capability baseline
`0d6ec0006836b2906a6103fc7683604c33626cae` (tree
`c43ad3893d029eec00f2173cb95536f04afe2116`). The source remains a merge parent, so its seven
Good-signed commits retain their original identities.

The selected boundary is workspace-root policy: one resolver owns root precedence and ownership
classification; the config module owns the typed persisted contract; filesystem seeding and
operator presentation remain outward adapters. Its modularization score is 12/14: cohesion 2,
coupling 1, side-effect ownership 2, port narrowness 2, replaceability 1, dependency direction 2,
and test-closure reduction 2. The remaining coupling is the deliberately retained flat-package
compatibility surface. This bead adopts the contract for later hives/worktrees extraction; it does
not claim that those modules are already complete.

North-star dependency direction:

- canonical config contracts define `git_workspace.mode` and `git_workspace.root`;
- `identity.workspace_root` and `identity.workspace_mode` own policy and depend lazily on the
  stable config facade through `FacadeBinding`, without creating an import cycle;
- `gitworkspace` owns concrete seed-file I/O;
- CLI, doctor, and readiness adapters consume those policies and render or request effects; and
- existing `beadhive.config` / `beadhive.config_schema` imports and monkeypatch points remain
  compatible.

## Conflict disposition by symbol

| Conflict candidate | Resolution |
| --- | --- |
| `config.py` | Retained the current facade. Added candidate-root derivation as `config_services.managed_repo_path`, exported through that facade. |
| `config_schema.py` | Retained the compatibility facade. Added mode/root fields and the external-only validator to canonical `modules/config/contracts.py`. |
| `identity.py` | Adopted env > config > guarded default resolution, `<BH_HOME>/ws`, populated-legacy scoping, blank-env handling, and ownership classification; used the current lazy config binding to preserve the import graph. |
| `gitworkspace.py` | Retained current explicit-path > workspace-root > HQ source precedence; added idempotent `is_seeded` / `ensure_seeded` without overwriting existing TOML. |
| `gitworkspace_plugin.py` | Retained the required-dependency, non-plugin surface; internal roots report missing seed setup, while external roots retain the unset-`GIT_WORKSPACE` stale warning. |
| `doctor.py` | Retained the current structured payload, render, timing, and JSON paths; added pure ownership/seed fields and an interactive-consent-only seed offer. |
| `cli.py` | Clean merge: `config init` seeds only a resolved internal root through the shared writer and leaves external roots untouched. |
| operation/private-path policy | Declared the doctor prompt at its CLI projection while keeping the MCP resource pure and non-interactive; recorded candidate-root `.git` inspection as an exact workspace-discovery exception. |
| `config.example.yaml` | Retained current canonical-schema and required-dependency language; added mode/root ownership documentation without restoring `enabled`. |
| config/doctor/git-workspace tests | Retained current suites and added the accepted precedence, validation, seed, readiness, doctor, and compatibility cases. Removed only obsolete assertions about the deleted `git_workspace.enabled` plugin gate. |

## Behavior and exclusion ledger

Preserved behavior:

- a nonblank `$GIT_WORKSPACE` wins before config is loaded;
- `mode: internal` resolves exactly `<BH_HOME>/ws`;
- `mode: external` uses an explicit root or the legacy `~/workspace` default;
- root with no mode remains an explicit external root;
- `mode: internal` plus root fails at the canonical config boundary and names `$BH_HOME`;
- a populated legacy root is retained, but registrations whose clone is absent from that
  candidate root cannot trigger the guard;
- internal seeding is idempotent and never overwrites a bare or split workspace TOML;
- external setup and doctor paths write nothing; and
- doctor only offers the internal seed after explicit interactive consent.

`bh-cgcg.3` remains excluded. There is no migration command, move/copy/rename operation, upgrade
hook, or automatic relocation in this tree. The only new write creates an internal root and a
minimal seed file after `bh config init` or explicit doctor consent.

## Validation evidence

- Starting revision and tree matched the required baseline exactly; the worktree was clean.
- Pre-merge focused config/doctor/git-workspace/config-module/hive/worktree baseline: 426 passed.
- Adopted focused config/root/seed/init/readiness/doctor tests: 238 passed.
- Broader config/identity/setup/hive/worktree/closure run: 650 passed and three checked generated
  artifacts reported expected drift; after regeneration, the three proof groups passed (9 tests).
- Post-boundary focused tests including generated artifacts: 247 passed.
- The first configured full run reached all 7,495 tests and reported seven owned reconciliation
  regressions: one prompt-catalog declaration, one exact private-path ownership entry, four
  child-environment legacy-default expectations, and one checked evidence count. After
  fix-forward, the complete affected policy/environment/evidence files passed (55 tests).
- Ruff lint and format checks passed.
- Import-boundary check passed: 232 files, 2,287 edges, 6 owned legacy cycles / 278 cycle edges.
- Closure registry passed: 18 present, 5 absent.
- Wire-schema compatibility passed for the initial release baseline.

One earlier sandbox-only attempt failed all 238 selected tests during fixture setup because local
socket creation was denied; no product test body ran. The permission-correct rerun above is the
classified result. The pre-existing `.2` baseline remains the exact 11 nodeids recorded in its
preserved `lastfailed` cache; this reconciliation does not modify or reclassify them.
