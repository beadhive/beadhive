# Spike bh-hsus.2 — Does ONE declarative table reproduce all seven dependency registries?

**Bead:** `bh-hsus.2` · **Seat:** `dev/table` · **Type:** proof code + characterization test (see note)
**Feeds decision on:** `bh-hsus.3` (land the table), `bh-hsus.4` (git-workspace becomes a dep),
`bh-hsus.5` (split installs-it from runs-a-seat), `bh-hsus.6` (`bh dep` CLI), `bh-hsus.7` (the ADR).

> Departs from [`TEMPLATE.md`](TEMPLATE.md)'s "no product code" rule **because the bead asks it
> to**: bh-hsus.2's own description is "write `deps.py` … as a THROWAWAY proof" and its acceptance
> requires "a characterization test [that] asserts derived membership equals today's literals".
> The proof is therefore executable — `src/beadhive/deps.py` with **zero callers**, plus
> `tests/test_deps_characterization.py`. bh-hsus.3 then wires it up. Everything else about the
> template's shape is kept.

## Question

bh carries seven overlapping registries of "external things bh depends on", plus an eighth
hand-mirrored in `flake.nix` comments. Does **one** declarative table reproduce every one of them
as a filter — and specifically, do these five derivations hold?

```text
setup.PROBE_TABLE     = [d for d in DEPS if d.required == "always"]
setup.RUNTIME_PROBES  = group "store-runtime", selector dolt.backend
harness.HARNESSES     = [d for d in DEPS if d.install and d.install.cmd]
role.KNOWN_HARNESSES  = [d for d in DEPS if d.runs_seats]
credential probes     = [d for d in DEPS if d.auth]
```

…with `required` carrying **exactly two** values (`"always"` and `"group:<name>"`) and nothing left
over. Plus four blocking questions the later children cannot start without:

1. Does `codex` accept an `--agent`-equivalent flag — i.e. can it run a seat? (`opencode` takes
   `--agent <seat>`.) This decides whether `codex` is a legal value of the `agent` selector.
2. Does nixpkgs carry `claude-code` and/or `codex`? If yes, `install.cmd` could be `None` on the
   Linux plane and the flake supply both. `nix` is not on the operator's Mac — a test-bed question.
3. Does `git-workspace` move out of `plugins.registry()` without disturbing the onboard / retire /
   hive-ready / worktree loops?
4. Does `flake.nix` derive its package list, or stay hand-mirrored? Recommend one.

Explicitly **not** asking: whether required-vs-optional should be a type boundary (the operator cut
that on 2026-08-05 and it is treated as settled), nor whether to build a plugin system (no).

## Method

1. **Read every registry at `main` (6fac43f)** and recorded its literal value inline in
   `tests/test_deps_characterization.py`, so the proof pins *both* sides — the derivation and the
   registry — rather than going tautological once bh-hsus.3 makes the registry a derivation.
2. **Wrote `src/beadhive/deps.py`** with `Dep` / `Auth` / `Install` / `Group` / `DEPS` / `GROUPS` /
   `is_required` / `present` / `satisfied`, imported by nothing, and ran the derivations against the
   live registries (28 assertions, `uv run pytest tests/test_deps_characterization.py`).
3. **Q1 — codex, empirically**, against the installed `codex-cli 0.146.0` (`/opt/homebrew/bin/codex`,
   the same binary and version [`bh-a7so.8`](bh-a7so.8-codex-empirical.md) used): full `codex --help`
   and `codex exec --help`, a direct `codex --agent` invocation, `codex features list`, and a
   flag-inventory sweep of `codex completion bash` (6,116 lines) for anything matching `*agent*`.
4. **Q2 — nixpkgs, on the Linux test-bed** (`beadhive-factory`, Debian 13, `x86_64-linux`,
   `nix` from the multi-user daemon install). Evaluated against the **exact rev `flake.lock` pins**
   (`NixOS/nixpkgs e72e4f299401a3689d4b3d5fc6496b11db7064eb`) so the answer is about the flake we
   ship, not "some nixpkgs": `nix eval` for `.version` / `.meta.license` / `.meta.platforms` /
   `.meta.unfree`, then `nix build --dry-run` with and without `NIXPKGS_ALLOW_UNFREE=1`.
   This question **cannot** be answered on the operator's Mac — `nix` is not installed there.
5. **Q3 — by experiment, not by reading.** Removed `gitworkspace_plugin.PLUGIN` from
   `plugins.registry()` and ran the whole suite, recording exactly which tests broke; established a
   clean baseline first (`just check` → 3492 passed) and separately confirmed which failures were
   pre-existing `-m integration` cases. Reverted.
6. **Q4 — by experiment, on the test-bed.** Built two throwaway flakes to test whether deriving is
   even *possible* before arguing about whether it is *wise*: Route A (`builtins.fromJSON
   (builtins.readFile ./deps.json)` under pure flake eval) and Route B (import-from-derivation —
   run `python3` at eval time).

## Evidence

### 1. All five derivations hold, exactly

`tests/test_deps_characterization.py`: **28 passed, 1 skipped** (the skip is Evidence 5).
Ten rows, and every registry falls out as a filter with the recorded literal reproduced
element-for-element **and in order**:

| Registry | Derivation | Members |
|---|---|---|
| `setup.PROBE_TABLE` | `d.required == "always"` | git-workspace, gh, bd, dolt |
| `setup.RUNTIME_PROBES` | group `store-runtime` | colima, docker, podman |
| `harness.HARNESSES` | `d.install and d.install.cmd` | claude, codex |
| `role.KNOWN_HARNESSES` / `config.KNOWN_HARNESSES` | `d.runs_seats` | claude, opencode |
| credential probes | `d.auth` | gh, claude, codex |

`harness.HARNESSES` is reproduced **field-for-field**, not just by name — `package`, `license`,
`version_env`, `proprietary` all survive the move onto `Install`, so bh-hsus.5 relocates the licence
stance rather than re-deciding it.

### 2. `required` has two values, and they partition the table with nothing left over

Four rows `"always"`, three `"group:store-runtime"`, three `"group:agent"` — ten rows, no row in two
groups, no row in zero. `is_required()` is two branches:

```python
def is_required(dep, cfg=None) -> bool:
    if dep.required == ALWAYS:
        return True
    return GROUPS[dep.group].select(cfg) == dep.name
```

**`backend: jsonl` falls out with no special case**, which was the stated signal the shape is right:
`store_runtime_selection` returns `"jsonl"`, that matches no member's `name`, and nothing in the
group is required. `backend: none` behaves identically. Neither string appears anywhere in
`is_required`.

The two "at-least-one-of" groups really are the same mode — the only difference between them is
which config value the selector reads (`dolt.backend` vs `config.harness_name()`).

### 3. **Q1 — NO. codex 0.146.0 cannot run a seat.**

```text
$ codex --agent developer --help
error: unexpected argument '--agent' found

  tip: to pass '--agent' as a value, use '-- --agent'

Usage: codex [OPTIONS] [PROMPT]
```

A full flag sweep of `codex completion bash` (6,116 lines — every flag of every subcommand) matches
exactly **one** `*agent*` flag: `--use-agent-identity-auth`, which belongs to `remote-control` /
`app-server` alongside `--listen --remote --environment-id --name` — a daemon *auth-identity* flag,
not a seat selector. Neither `codex --help` nor `codex exec --help` has any seat/persona/agent
option. Compare `opencode --help`: `--agent   agent to use   [string]`, plus a whole
`opencode agent` subcommand for managing them.

**The closest analogue is not close enough.** `-p/--profile <CONFIG_PROFILE_V2>` "layer[s]
`$CODEX_HOME/<name>.config.toml` on top of the base user config" — TOML config layering (model,
sandbox mode, approval policy), as [`bh-a7so.8`](bh-a7so.8-codex-empirical.md) Evidence 4 exercised
in detail. It cannot carry an agent *definition* the way `claude --agent <plugin>:<seat>` does, and
`bh role <seat>`'s whole contract is "exec the seat with the bundled agent def". `codex features
list` shows `multi_agent  stable  true`, but that is in-session sub-agent spawning; it surfaces no
CLI flag (the sweep above is exhaustive).

So `runs_seats=False` for codex, and **codex must not become a legal value of the `agent`
selector**. Today `config_schema` types the field `Literal["claude", "opencode"]`, which already
excludes it — that exclusion is *correct*, not an oversight, and the table now says why.

### 4. **Q2 — YES to both, but the conclusion the bead hypothesised does NOT follow.**

At the exact rev `flake.lock` pins, on `x86_64-linux`:

```text
PRESENT claude-code version=2.1.220 license=unfree unfree=true
  platforms=["aarch64-darwin","aarch64-linux","x86_64-linux"]
PRESENT codex      version=0.146.0 license=Apache-2.0 unfree=false
  platforms=[… "aarch64-darwin","aarch64-linux","x86_64-linux" …]
ABSENT  codex-cli
ABSENT  openai-codex
```

Both exist, both cover every system the flake declares. But **`claude-code` is unfree**, and that is
load-bearing:

```text
$ nix build --no-link --dry-run <pinned>#claude-code
… Package 'claude-code' has an unfree license, refusing to evaluate …
  { nixpkgs.config.allowUnfree = true; }  …

$ NIXPKGS_ALLOW_UNFREE=1 nix build --no-link --dry-run --impure <pinned>#claude-code
… (evaluates and resolves the full closure) …

$ nix build --no-link --dry-run <pinned>#codex
these 5 paths will be fetched (141.5 MiB download, 491.3 MiB unpacked):
  … codex-0.146.0 …
```

Adding `pkgs.claude-code` to `toolchainFor` would make **every** `nix develop` / `nix build` /
`install.sh` run fail for anyone who has not opted into unfree — including `nix flake check` — and
the escape (`--impure` + `NIXPKGS_ALLOW_UNFREE=1`) is exactly the "you accept these terms yourself"
posture `harness.py` already implements deliberately (bh-pc2a.36: Claude Code is proprietary; baking
it in would make an image publisher a redistributor). nixpkgs' own `unfree=true` is the same fact
stated by a second, independent source. `codex` carries none of this — Apache-2.0, free, and
`harness.py` already notes it "declares Apache-2.0 and stays baked".

So: `install.cmd` is **not** `None` on the Linux plane. `npm install -g` stays the harness install
route (it is the one route that works on every plane, including macOS where there is no flake at
all), and the flake stays the *infra* toolchain.

### 5. Credential probes are pinned but not yet live-guarded — named, not hidden

`harness_auth.py` exists only on `wt/bead/epic/bh-q160` (`f46ef9b`); main has never seen it. The
derivation is therefore asserted against the recorded literal `["gh", "claude", "codex"]`, with a
second assertion behind `pytest.importorskip("beadhive.harness_auth")` that compares against the
live `harness_auth.PROBES` — it **skips on main and arms itself the moment bh-q160 merges**. The
`Auth` rows carry that module's own data unchanged (`GH_TOKEN`/`GITHUB_TOKEN` + `gh auth login
--web`; `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` + `claude setup-token`; `OPENAI_API_KEY` +
`codex login`).

### 6. **Q3 — YES for the lifecycle loops; NO for two other consumers.**

Removing `gitworkspace_plugin.PLUGIN` from `plugins.registry()` and running the suite breaks
**exactly six** tests, none of them a lifecycle loop:

| Failing test | What it observes |
|---|---|
| `test_gitworkspace_plugin.py::test_registry_includes_git_workspace_then_orca` | registry membership |
| `test_hitch_plugin.py::test_registry_includes_hitch_last` | registry order |
| `test_gitworkspace_plugin.py::test_plugin_groups_cmd_lists_repo_groups` | `bh plugin git-workspace groups` |
| `test_gitworkspace_plugin.py::test_plugin_groups_cmd_empty_message` | `bh plugin git-workspace groups` |
| `test_gitworkspace_plugin.py::test_plugin_tree_help_lists_git_workspace` | `bh plugin --help` |
| `test_gitworkspace_plugin.py::test_hive_ready_plugin_checks_includes_git_workspace_line` | `bh hive ready` line |

Zero failures in `onboard.py`, `retire.py` or `worktree.py` — as predicted, because each of those
loops filters on a hook git-workspace does not implement (`onboard.py:1390` `on_onboard is not
None`; `retire.py:340` `on_retire is None`; `worktree.py:489/516/556` `wt_create`/`wt_remove`). The
eight further failures in that run (`test_agf_modalities`, `test_host_fence_int`,
`test_agf_remote_sandbox`) are pre-existing `-m integration` cases, confirmed failing at baseline
with `plugins.py` reverted, and excluded from `just check` (`FAST := "not integration"`).

So the move is cheap **but not free**, and the two live consumers are:

- **`cli.py:106`** mounts every registered plugin's sub-app. Dropping the registration deletes the
  `bh plugin git-workspace groups` command outright — a user-visible CLI surface.
- **`hive_ready.py:129`** emits one readiness line per plugin. Dropping it deletes the
  `git-workspace` line from `bh hive ready`.

Worth recording alongside: `gitworkspace.py` itself (the pure module) is *already* consumed directly
by `config.py`, `doctor.py`, `gitauth.py`, `hive.py`, `hq.py` and `host_provision.py` — git-workspace
is a first-class dependency in every way except its type. Only the sub-app and the readiness line
ride the plugin seam, which is precisely the required/optional contradiction the epic names
(`gitworkspace.enabled()` defaults to **false** while `PROBE_TABLE` requires the binary
**unconditionally**).

### 7. **Q4 — deriving is possible (both ways), and still not worth it.**

Tested rather than assumed, on the test-bed:

```text
### Route A — builtins.readFile ./deps.json under pure flake eval:
["git-workspace","gh","bd","dolt"]

### Route B — import-from-derivation (run python3 at eval time):
building '/nix/store/…-deps-json.drv'...
["bd"]
```

Both work. So this is a cost question, not a feasibility one:

- **Route A** needs a generated `deps.json` committed next to `flake.nix` — which replaces "flake
  drifts from `deps.py`" with "`deps.json` drifts from `deps.py`", *plus* a codegen step and a CI
  gate to catch it. You have not removed a hand-mirrored artifact; you have added one.
- **Route B** makes every `nix eval` / `nix develop` build a derivation and run Python before it can
  even *evaluate*, and import-from-derivation is routinely disabled in stricter evaluators.
- Either way the **name→attribute map stays hand-written**: `bd` is not `pkgs.bd` but a
  `beadsHead pkgs` override carrying its own `rev`/`hash`/`vendorHash`, and `toolchainFor` also
  supplies `git` and `uv`, which are not probe-table rows at all. Automation would cover 3 of 6
  lines.
- The list changes roughly never, and the failure mode of drift is *loud and immediate*: a missing
  always-required dep makes `bh setup check` print `✗ missing: <name>` at provisioning time on the
  very host being provisioned.

### 8. Both platforms run, and they exercise DIFFERENT branches of the same code

Five of six defects in the session that produced this epic were invisible on macOS, so
`bh setup check` was compared byte-for-byte on both — main (`6fac43f`) vs the batch branch, stdout
*and* stderr, from scratch worktrees with a scratch `BH_HOME`:

| | macOS (aarch64-darwin) | test-bed (`beadhive-factory`, x86_64-linux, `nix develop`) |
|---|---|---|
| `dolt.backend` | `colima` | absent → `jsonl` |
| store-runtime group | selects `colima` — **5** rows probed | selects nothing — **4** rows probed |
| stdout / stderr | byte-identical | byte-identical |
| exit | 0 | 0 |

That is the useful difference, not an inconvenience: macOS exercises the group-selects-a-member
branch of `is_required`/`probe_tools`, and Linux exercises the group-selects-nothing branch, which
is precisely the `jsonl`-falls-out-with-no-special-case claim. Both are byte-identical across the
change.

Suite, same selection as `just check` (`-m "not integration"`), run on the test-bed against **both**
worktrees so the Linux failure set is attributed rather than guessed at:

```text
before (main 6fac43f):        18 failed, 3474 passed, 7 skipped
after  (wt/batch/bh-hsus):    18 failed, 3506 passed, 8 skipped   (+32 = the new tests)
failures only in AFTER:       (none)
failures only in BEFORE:      (none)
```

**Zero regressions**; the 18 are identical, pre-existing, and environmental — 12 in
`test_osv_license_gate.py` (no `osv-scanner` on that host) and 6 where `bd init` fails standing up a
scratch hub/hq store under that host's nix-supplied bd HEAD build. All 18 pass on macOS, so they are
a test-bed environment gap on `main`, out of scope here and reported rather than fixed.

## Verdict — **GO**

The shape holds as specified. All five derivations reproduce today's literals element-for-element;
`required` has two values covering ten rows with nothing left over; `is_required()` is two branches;
`backend: jsonl` selects nothing with no special case in the code. `setup.probe_one()` remains the
single detection mechanism (`deps.present` delegates to it), and stages 1 and 2 stay separate, so
the in-image manifest path's zero-subprocess contract is untouched.

Two findings **sharpen** the design rather than breaking it:

- **Q1's NO is the load-bearing one.** `codex` is a declared member of the `agent` group that config
  can never select — the identical shape to `dolt.backend: jsonl` selecting no runtime, and it needs
  no special case either. The three-way disagreement the word "harness" was hiding is now
  expressible in the type: `{installable} ∩ {runs_seats} == {claude}`, `{installable} \ {runs_seats}
  == {codex}`, `{runs_seats} \ {installable} == {opencode}` — asserted directly.
- **Q2's YES does not license the conclusion attached to it.** nixpkgs has both, but `claude-code` is
  unfree and would break pure evaluation of the flake for everyone. `install.cmd` stays.

Residue is named, not hidden — three items, each with a guard test where a guard is possible:

1. `config_schema`'s `Literal["claude", "opencode"]` (top-level and per-hive). A pydantic `Literal`
   cannot be built from a runtime list without losing static typing, so it stays hand-written;
   `test_residue_config_schema_literal_mirrors_the_seat_runners` fails the moment it and
   `runs_seats` disagree.
2. `hitch_plugin._HITCH_TARGETS`. Its **values** are hitch's own vocabulary (`claude` →
   `claude-code`) and cannot be derived; its **keys** are bh's and are guarded.
3. `hitch_plugin` / `orca`'s own `shutil.which()` calls. These are **not** a second dep-detection
   mechanism to fold in: hitch's binary name is a *config value* (`config.hitch_command(cfg)`), so
   it cannot be a static table row at all. It is a per-plugin readiness probe — the
   required/optional type boundary doing exactly its job.

## Recommendation

1. **bh-hsus.3: land `deps.py` as written**, derive `setup.PROBE_TABLE` / `setup.RUNTIME_PROBES`
   from it, and replace `doctor.py:1205`'s hand-written `probe_one("bd", "bd", ["bd", "--version"])`
   with a table lookup. No behaviour change; keep the characterization test as the permanent gate.
2. **bh-hsus.4 must budget for two consumers, not zero** (Evidence 6). Moving git-workspace out of
   `plugins.registry()` deletes `bh plugin git-workspace groups` and its `bh hive ready` line. The
   epic's own rule — "`bh plugin <name>` stays a mount point and only rows with a sub-app get one" —
   points at the answer: give the **dep** table the ability to contribute a sub-app and a readiness
   line, which git-workspace is the only row that needs (it is the one dep with a real
   `bh plugin`-shaped surface). Do not dual-register it as both a dep and a plugin; that
   re-introduces the `required` vs `enabled` competition the type boundary exists to end.
3. **bh-hsus.5: derive `role.KNOWN_HARNESSES` from `runs_seats`** so `bh role --harness codex` is
   rejected *for the right reason* — codex cannot exec a seat (Evidence 3) — rather than by an
   unrelated hand-written tuple that happens to agree. Keep `codex` in the `agent` group; keep it out
   of the `config_schema` `Literal`. Re-run Evidence 3's probe against a newer codex before
   assuming this stays true — it is a version fact, not a design fact.
4. **Q4: stay hand-mirrored, and close the drift with a test rather than codegen.** Replace
   `flake.nix`'s four `# PROBE_TABLE` comments (which will name a derivation, not a literal, once
   bh-hsus.3 lands) with one pointer to `deps.py` and the exact predicate, and add a pure-Python
   test that parses `toolchainFor` and asserts every always-required dep appears. That is the gate
   the comments were pretending to be, at ~15 lines and no `nix` dependency in the test suite.
5. **Do not add a `requires` edge for bd-requires-dolt** (unchanged from the bead's own design):
   both are `"always"` today, nothing needs the edge, and the group mechanism accommodates a chain
   when a second beads backend actually lands.
6. **`codex` in the flake is now a real option** (Apache-2.0, free, every declared system) if a
   headless local-install host should be able to run codex non-interactively. Out of scope here —
   noted for bh-q160, not acted on.
