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
>
> **Amended after rebase onto `wt/bead/epic/bh-hsus` (2026-08-05).** This spike was researched
> and written against `main` (`6fac43f`). `bh-hsus.1` was reviewed and merged into the container
> branch in parallel and rewrote `harness.py` underneath it: `Harness(package=…, proprietary=…)`
> became `Harness(name, binary, license, install: InstallRoute, version_env)`, claude moved off
> `npm install -g` onto its native bootstrap (`curl -fsSL https://claude.ai/install.sh | bash`),
> and codex got `cmd=None`. Two things here were stale and are amended below, each marked
> **AMENDED**: the `harness.HARNESSES` derivation (Evidence 1) and **Q2's conclusion** (Evidence
> 4). The Q2 *evidence* is unchanged and now supports the native route more strongly than it
> supported npm. Q1, Q3, Q4 and everything else are untouched. A new section, **Q1 follow-on**,
> answers a question the review raised against Evidence 3.

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
| `harness.HARNESSES` | **AMENDED** `d.install is not None` | claude, codex |
| `role.KNOWN_HARNESSES` / `config.KNOWN_HARNESSES` | `d.runs_seats` | claude, opencode |
| credential probes | `d.auth` | gh, claude, codex |

**AMENDED — the `harness.HARNESSES` predicate.** As researched it was `d.install and
d.install.cmd`, which was correct while both harnesses installed via npm. After bh-hsus.1, codex
carries `cmd=None` (a route bh documents but does not drive), so that predicate yields `{claude}`
and no longer reproduces the registry. The membership is unchanged; the predicate is now
`d.install is not None`. The split is a **gain**, not a patch: "bh knows how this arrives" and "bh
will run it" were the same set by accident and are now two — `deps.has_install_route()` = {claude,
codex} against `deps.installable()` = {claude}, `installs < routed` strictly. Conflating them is
precisely the bug bh-hsus.1's own review caught in `missing_hint()` (routing to
`bh harness install codex`, a command that exits 1).

`harness.HARNESSES` is reproduced **field-for-field**, not just by name — `name`, `binary`,
`license`, `version_env` and the whole `InstallRoute` (`cmd` argv, the 150-char remedy `note`,
`proprietary`) are asserted byte-for-byte, so bh-hsus.5 relocates the licence stance rather than
re-deciding it. `package` is gone from both sides: nothing installs by package name any more.

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

### 3a. **Q1 follow-on — is codex's `agent` group membership coherent, or decoration?**

Raised in review against this spike's own claim that codex-in-the-`agent`-group is "the same shape
as `dolt.backend: jsonl`". **It is not the same shape, and the membership is decoration.** Stated
plainly because `bh-hsus.5` inherits the answer; the behaviour is unchanged here.

**The two cases are duals, not instances of one case.** Group membership is the *left* side of the
relation and selector range is the *right* side, and each case has a hole on a different side:

| | `dolt.backend: jsonl` | `codex` in group `agent` |
|---|---|---|
| What is out of place | a selector **value** with no member | a **member** with no selector value |
| Members reachable by *some* config | all 3 (colima, docker, podman) | 2 of 3 — codex by none |
| What the group does for it | models "none required" — the group's **empty case** | nothing |
| `is_required` under the whole selector range | False *for this value* | False *for every value* |

`jsonl` is the group's empty case and the group still does real work for every one of its rows —
that is why it needs no special case, and that finding (Evidence 2) stands. codex is the opposite:
it is unrequirable **by construction**, not merely unrequired today. No config that `config_schema`
admits can reach it, so `is_required(codex)` is a constant-False predicate wearing a conditional
one's clothes. `tests/test_deps_characterization.py::test_codex_membership_is_not_the_jsonl_case`
pins exactly this — it quantifies over the selector's whole legal range on both sides rather than
over a few hand-picked configs.

**So what does `required="group:agent"` encode for codex?** Not a requirement — a **category**
("this is an agent harness"). And that category is already carried by `kind="harness"`. It is a
duplicated classification sitting in the one field whose job is requirement, and it states
something false: that some configuration could make codex required.

**Why it is nonetheless harmless today, and why the auth probe is not evidence against this.**
Nothing branches on it. Every derivation reads `install`, `runs_seats` or `auth`; **none reads
`required`**. In particular the credential probe is coherent on its own terms: `auth` is a stage-2
property of a tool that is *present* ("if you have this, is it usable?"), orthogonal to whether bh
*requires* it — the same reason `bh harness list` reports on a codex bh will never install. Reading
codex's auth probe as implying codex is required gets the dependency backwards.

**The cost is to the model's own headline claim.** "`required` has exactly two values and they
partition the table" is true as literally asserted, but group membership is quietly serving as a
third value meaning *never required* — and the two-value model has no honest way to say that.

**For `bh-hsus.5` — the fork, not decided here.** Either (a) admit an explicit never-required value
and re-prove the partition with three values, which costs the headline claim; or (b) drop codex
from the `agent` group and let `kind="harness"` carry its category alone, which preserves two
values but must answer what `required` then says for a row nothing requires. (b) is the sharper
question, because a row that no version or configuration of bh ever requires is, by this epic's
*own* type boundary ("a `Dep` is required for this version of bh; a `plugins.Plugin` is an optional
integration"), sitting on the wrong side of it — and codex is not a plugin either. Do not read that
as a recommendation to make it one; read it as: the type boundary has a third case in it that the
epic has not yet named, and codex is the row that exposes it.

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

**AMENDED — the conclusion. The evidence above stands unchanged; what it licenses does not.**

As written the conclusion read: *"`install.cmd` stays; `npm install -g` remains the one route that
works on every plane."* The first half survives — claude keeps a bh-driven `install.cmd`, and the
flake stays the *infra* toolchain, carrying no harness. The second half is **wrong**, and this
spike's own evidence is why.

npm was never a route that "works". bh-hsus.1 measured it on the Linux test-bed: an `npm install
-g` next to a native install produces a **second copy** of claude, and which one answers on PATH is
down to shell ordering. It looked universal because npm is universally *present*, which is not the
same property.

Read against that, the unfree finding is not an argument for npm — it is the second independent
argument **for the native bootstrap**:

| Route | macOS | Linux | Nix plane | Opt-in required |
|---|---|---|---|---|
| `curl -fsSL https://claude.ai/install.sh \| bash` | yes | yes | yes | none |
| `nixpkgs#claude-code` | n/a (no flake) | flake only | yes | **`allowUnfree`** |
| `npm install -g @anthropic-ai/claude-code` | shadows the native install | shadows the native install | shadows it | none |

The native installer is the only row that is available on every plane *and* needs no unfree opt-in
*and* leaves exactly one binary on PATH. nixpkgs' `unfree=true` says out loud what
`install.sh` handles implicitly — that taking Claude Code is the user's own acceptance of
Anthropic's terms — so declining to put it in `toolchainFor` and pointing at the vendor's own
installer are the *same* decision, not competing ones.

Net for Q2: nixpkgs carries both, **`claude-code` must not enter `toolchainFor`** (unfree would
break pure evaluation for everyone), `install.cmd` stays non-`None` for claude — as the **native
bootstrap**, not npm — and codex's `cmd` is `None` because its three routes are plane-specific and
this table does not branch on plane.

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

### 8a. Re-verified after the rebase, against the CONTAINER base

The run above compared against `main` (`6fac43f`). After rebasing onto `wt/bead/epic/bh-hsus`
(`51fae7e`, bh-hsus.1 merged) the comparison was redone on macOS with the *container* as `before`,
from two detached scratch worktrees, **both branches of the group exercised**:

| | scratch `BH_HOME` | `BH_HOME` seeded with the real `config.yaml` |
|---|---|---|
| `dolt.backend` | absent → `jsonl` | `colima` |
| store-runtime group | selects nothing — **4** rows probed | selects `colima` — **5** rows probed |
| stdout | byte-identical | byte-identical |
| stderr | byte-identical (empty) | 191 bytes both; identical after normalising the one structlog `"timestamp"` — a wall-clock, the only differing field on the only line |
| exit | 0 | 0 |

Seeding a scratch `BH_HOME` rather than using the real one is what let the colima branch run
without writing the operator's `setup-state.json` (confirmed untouched).

**Linux was NOT re-run, deliberately.** The gate for a re-run is "the rebase changed detection or
PATH logic", and it changed neither: the rebase delta is confined to `deps.py`'s `install` /
`license` / `version_env` fields on the two *harness* rows and the predicates over them.
`bh setup check` probes `always_required()` + `group_members("store-runtime")` and never touches an
`agent`-group row; `probe_one` and `shutil.which` are untouched on both sides. The platform-specific
risk in this change was always the group-selects-nothing branch, and the scratch-`BH_HOME` column
above exercises exactly that branch on macOS. The npm→native move that *is* Linux-sensitive belongs
to bh-hsus.1, which verified it on the test-bed and is the `before` side here.

## Verdict — **GO**

The shape holds as specified. All five derivations reproduce today's literals element-for-element;
`required` has two values covering ten rows with nothing left over; `is_required()` is two branches;
`backend: jsonl` selects nothing with no special case in the code. `setup.probe_one()` remains the
single detection mechanism (`deps.present` delegates to it), and stages 1 and 2 stay separate, so
the in-image manifest path's zero-subprocess contract is untouched.

Two findings **sharpen** the design rather than breaking it:

- **Q1's NO is the load-bearing one.** codex cannot exec a seat, so it must not become a legal value
  of the `agent` selector. The disagreement the word "harness" was hiding is now expressible in the
  type — and after bh-hsus.1 it is a **three**-way split, not two: `{has_install_route} == {claude,
  codex}`, `{runs_seats} == {claude, opencode}`, `{installable} == {claude}`, with
  `{installable} ⊊ {has_install_route}`. All asserted directly.
- **Q2's YES does not license the conclusion attached to it.** nixpkgs has both, but `claude-code` is
  unfree and would break pure evaluation of the flake for everyone. `install.cmd` stays — **as the
  native bootstrap, not npm** (AMENDED, Evidence 4).

One claim in the first draft is **RETRACTED**, and its retraction is the material change on rebase:

- codex's `agent` group membership is **NOT** "the identical shape to `dolt.backend: jsonl`". jsonl
  is a selector value outside the member set with every member still reachable; codex is a member
  outside the selector's range, reachable by nothing. The membership is **decoration** — a category
  `kind="harness"` already carries, sitting in the field that means requirement. Harmless today (no
  derivation reads `required`), unchanged here, and `bh-hsus.5`'s to resolve. Full answer and the
  fork it faces: **Evidence 3a**.

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

1. **bh-hsus.3: land `deps.py`**, derive `setup.PROBE_TABLE` / `setup.RUNTIME_PROBES` from it, and
   replace `doctor.py:1205`'s hand-written `probe_one("bd", "bd", ["bd", "--version"])` with a table
   lookup. No behaviour change; keep the characterization test as the permanent gate. **AMENDED —
   not "as written":** the `Install` record is replaced by `InstallRoute`, field-for-field with
   bh-hsus.1's, and `package` plus every npm reference is dropped from the table. The mirror of
   `harness.HARNESSES`'s values is deliberate and gated — `deps` sits on `setup`'s import-cheap
   start-up path and `harness` imports `typer`, so it restates rather than imports, exactly the
   posture Q4 chose for `flake.nix`. bh-hsus.5 collapses the mirror.
2. **bh-hsus.4 must budget for two consumers, not zero** (Evidence 6). Moving git-workspace out of
   `plugins.registry()` deletes `bh plugin git-workspace groups` and its `bh hive ready` line. The
   epic's own rule — "`bh plugin <name>` stays a mount point and only rows with a sub-app get one" —
   points at the answer: give the **dep** table the ability to contribute a sub-app and a readiness
   line, which git-workspace is the only row that needs (it is the one dep with a real
   `bh plugin`-shaped surface). Do not dual-register it as both a dep and a plugin; that
   re-introduces the `required` vs `enabled` competition the type boundary exists to end.
3. **bh-hsus.5: derive `role.KNOWN_HARNESSES` from `runs_seats`** so `bh role --harness codex` is
   rejected *for the right reason* — codex cannot exec a seat (Evidence 3) — rather than by an
   unrelated hand-written tuple that happens to agree. Keep it out of the `config_schema` `Literal`.
   Re-run Evidence 3's probe against a newer codex before assuming this stays true — it is a version
   fact, not a design fact. **AMENDED — "keep `codex` in the `agent` group" is withdrawn as a
   recommendation and handed over as a decision** (Evidence 3a): the membership is decoration, and
   .5 picks between admitting a never-required value (costing the two-value headline) and dropping
   codex from the group (needing an answer for what `required` then says). Also inherit the split
   `has_install_route` vs `installable`: whatever .5 does about membership, `bh dep install` must
   read the narrower set or it will offer codex a command that exits 1.
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
