# Dependency taxonomy ADR — one table, and why `harness` stopped being a noun

**Status:** accepted · **Date:** 2026-08-05 ·
**Supersedes:** nothing · **Reverses:** one accepted criterion of `bh-q160.3` (Decision 6) ·
**Amends:** `deployment-isolation-direction-adr.md` not at all — Decision 5 of that ADR is an
*input* here (the table must not branch on plane, so plane-specific routes are prose).

Records the verdict `bh-ckqt` asked for, after the `bh-hsus` molecule (one spike and five
implementation children) built it. `bh-ckqt` wanted a written answer to one question — does
`harness` stay a top-level noun, become a subtype, or keep its verb while the data underneath is
reconciled — "with the reasoning, not just the choice."

**This document contradicts the epic that spawned it in three places, and says so each time.** The
epic counted the registries wrong, claimed `required` had two values when it needed three, and
listed two reasons `required: bool` was insufficient when there were four. Those corrections came
out of the work, not out of review, and the design is better for them. An ADR that repeated the
plan instead of the outcome would be worth nothing.

---

## Context

`bh` depends on external tools it does not ship: `git-workspace`, `gh`, `bd`, `dolt`, a container
runtime, and an agent harness. Each of those facts had grown its own list. Nothing reconciled
them, and the compiler could not tell you when you forgot one.

`bh-q160.3` made the problem concrete rather than theoretical. It built `bh harness auth`, which
had to probe `gh` — and `gh` is not a harness. It needs a credential, it runs no seat, `bh` has no
install route for it, and it is required unconditionally. The verb was named after one kind of
member of the set it operated on, and so was the module (`harness_auth.py`). That single
compromise is what turned a tidiness complaint into a bead.

The word "harness" was already naming two disjoint sets whose intersection is `{claude}`:

- `codex` can be installed and authenticated, but `bh role --harness codex` was **rejected**.
- `opencode` can run a seat, but `bh` can neither install nor authenticate it.

So `kind == harness` was never one axis. It was *`bh` installs it* **and** *it runs a seat*, two
facts that had been silently disagreeing for as long as both lists existed.

### The count was wrong

The epic asserted seven registries in code plus an eighth hand-mirrored in `flake.nix` comments.
There were **eight** in code. `config.KNOWN_HARNESSES` was a fourth hand-mirrored harness tuple —
alongside `harness.HARNESSES`, `role.KNOWN_HARNESSES`, and `config_schema`'s `Literal` — and the
epic's own list omitted it. It is a small error with a large moral: the epic was an argument that
nobody can keep parallel lists in their head, written by somebody who could not keep the list of
lists in their head.

---

## Decision 1 — `harness` is a filter over the table, not a top-level noun

**A verb must not be named after one kind of member of the set it operates on.** That is the whole
argument, and `gh` is the proof: `bh harness auth gh` was a sentence that asserted something false
in order to reach a tool the operator genuinely needed to authenticate.

`harness` becomes a column value — `kind == "harness"` — over one declarative table
(`src/beadhive/deps.py`). Verbs that operate **across** the table are top-level and take the name
as an **argument**:

```text
bh dep list [--kind harness|infra] [--missing]
bh dep show <name>
bh dep install <name>
bh dep auth [<name>] [--check]
```

`bh plugin <name>` is unchanged and keeps its three genuinely optional integrations (orca,
observaloop, hitch). It is a **mount point** for a tool's own sub-app, not a verb over a set, which
is why it did not absorb the dependency table: `bh plugin gh` would be an empty namespace, and an
empty namespace invites the wrapper that `credentials.py`'s own docstring forbids.

`bh harness list|install|auth` survive as **thin aliases** — `bh-q160.3`'s acceptance and the
documented adoption sequences name them, so removing them would break written instructions for no
gain. Each alias is a single call into `dep_cli`, not a second implementation, so the alias and the
canonical verb cannot drift apart; the `harness` group is hidden from the help panels so the help
lists one surface rather than two. `harness.ls()` is gone: `bh dep list` prints the same install
notes, licence and proprietary footer for whatever rows it shows, so `bh-pc2a.36`'s guarantee that
the user is *told* what they are accepting survives the move.

Against `bh-ckqt`'s three candidate shapes, this is **B** (rename the surface, keep the old verb as
an alias) with `harness` demoted to a `kind`. It is not **C**: nothing became a subtype of plugin.
Decision 2 says why.

---

## Decision 2 — required vs optional is a type boundary, and `plugin` is not the unifying noun

`Plugin` is a strict superset of `Dep`. It has `cli`, `readiness`, `binary`, `version_cmd` — every
field a dep needs — plus lifecycle hooks and `enabled`. The cheap-looking move is therefore to keep
one record with a mode flag and let `harness` and `dep` both be plugins. It was rejected.

**`enabled` and `required` are different questions that had been competing for one row.**
`required` asks *must this host have this tool for this version of `bh` to work*. `enabled` asks
*does the operator want this integration switched on*. Merging the two types means the merged row
carries both fields anyway and something has to arbitrate when they disagree. Keeping them on
separate types means each field lives on exactly the type that can answer it: `deps.py` has no
`enabled` field and `plugins.py` has no `required` field, so the disagreement is not expressible.

**git-workspace is the case that decides it.** It answered both questions at once, in opposite
directions, and had done so for a long time (Decision 4). Under a merged `plugin` noun that
contradiction would have been perfectly *representable* — a row with `required=True, enabled=False`
type-checks fine. The type boundary is what makes it a compile-time impossibility rather than a
runtime surprise, and that is the entire value being bought.

The cost is real and worth stating: `cli` and `readiness` genuinely belong to both types, so they
are declared twice. That duplication is the price of the boundary, not an oversight.

---

## Decision 3 — `required` has THREE values, and the third had to be said out loud

The epic claimed two: `"always"` and `"group:<name>"`. Shipped: **three**.

```text
always        unconditional                       git-workspace, gh, bd, dolt
group:<name>  a config value SELECTS the member   store-runtime, agent
never         no configuration, now or ever       codex
```

**The genuine simplification stands.** Four distinct requirement situations existed in the tree —
unconditional, conditional on config (the container runtime, per `dolt.backend`), one-of-a-group
(the agent harness), and never — and they collapse to three values because *conditional on config*
and *one-of-a-group* turned out to be the **same mode**: a group whose selector is a config value.
`dolt.backend` selects `colima|docker|podman`; `config.harness_name()` selects `claude|opencode`.
`is_required()` is three branches, not a predicate DSL, and a future multi-backend `beads` gains a
`beads-backend` group the same way.

The signal that the group shape is right is that `dolt.backend: jsonl` needs **no special case**:
it selects nothing, matches no member's name, and nothing in that group is required. Neither
`"jsonl"` nor `"none"` appears anywhere in `is_required`.

**Why `never` had to become an explicit value rather than hide as unreachable membership.** In the
spike, `codex` was a *member* of the `agent` group whose selector could never name it —
`config_schema` types the field `Literal["claude", "opencode"]`. So `is_required(codex)` was False
**by construction**, not merely False today, and group membership was quietly acting as a third
value meaning "never required" while the model had no honest way to say that.

This is *not* the same shape as `dolt.backend: jsonl`, and the spike's first draft claimed it was;
that claim is retracted in the spike itself. The two are duals with the hole on opposite sides:

| | `dolt.backend: jsonl` | `codex` in group `agent` |
|---|---|---|
| What is out of place | a selector **value** with no member | a **member** with no selector value |
| Members reachable by *some* config | all three | two of three — codex by none |
| What the group does for it | models "none required" — its empty case | nothing |
| `is_required` over the whole selector range | False *for this value* | False *for every value* |

What codex's membership actually encoded was a **category** — "this is an agent harness" — which
`kind="harness"` already carries. A duplicated classification, sitting in the one field whose job
is requirement, asserting something false: that some configuration could make codex required.

`bh-hsus.5` resolved the fork by admitting the value (`required="never"`). The alternative was to
drop codex from the group and leave `required` with nothing to say about it — which is sharper as a
question and worse as an answer, because it needs a new `required`-adjacent type for a row nothing
requires, landing codex on the wrong side of this ADR's own Dep/Plugin boundary without codex being
a plugin. `kind="harness"` still carries codex's category; `group_members("agent")` now returns
only the two rows a config can actually select; and `required` states only genuine requirement
facts.

The honest cost: **`never` weakens the type boundary Decision 2 draws.** A `Dep` was defined as
"required for this version of `bh`", and there is now one row that is a dep and is never required.
The boundary has three cases in it, and codex is the only occupant of the third. That is a real
concession, not a footnote, and it is preferred to a predicate whose falsity was structural and
invisible.

### `required: bool` was insufficient on FOUR counts, not two

`bh-ckqt` asked for a `required: bool` to be "proven insufficient by two existing cases." Four were
found, and each breaks it differently:

1. **The container runtime** — config-conditional. `RUNTIME_PROBES` existed as a separate list
   precisely because a bool could not express "only when `dolt.backend` says so."
2. **The agent group** — config-selected. `claude` XOR `opencode`, chosen by
   `config.harness_name()`.
3. **git-workspace** — required *and* optional simultaneously. `gitworkspace.enabled()` defaulted
   **False** while `setup.PROBE_TABLE` required the binary **unconditionally**.
4. **codex** — never required, and still a real dep: a documented install route and a live auth
   probe.

Case 3 is the one that could not be papered over with a richer enum, because it was not one field
with too few values — it was two mechanisms with opposite defaults. Case 4 is the one nobody had
articulated at all.

---

## Decision 4 — git-workspace is a dep, not a plugin

**The evidence is a defaults contradiction that had been live for months.**
`gitworkspace.enabled()` returned False by default; `setup.PROBE_TABLE` required the
`git-workspace` binary unconditionally. `bh` refused to start without a tool whose integration was
off by default. Two mechanisms, opposite defaults, same tool.

It is resolved on the side the gate already enforces. `git-workspace` moves out of
`plugins.registry()` and into `DEPS` with `required=ALWAYS`. `git_workspace.enabled` is **deleted
outright, not deprecated**: every reader (`registry.py`, `route.py`, `hq.py`, `host_provision.py`,
`doctor.py`) now calls straight through and degrades to "nothing configured" instead of gating on a
flag, and `config.orca_enabled()` drops its AND-gate on it because there is nothing left to test.

Corroborating evidence, which is why this was not a close call: `gitworkspace.py` was **already**
consumed directly by `config.py`, `doctor.py`, `gitauth.py`, `hive.py`, `hq.py` and
`host_provision.py`. It was a first-class dependency in every way except its type. Only two things
rode the plugin seam, and the spike measured them by experiment rather than by reading — removing
the registration broke exactly six tests, none of them a lifecycle loop:

- `bh plugin git-workspace groups`, a user-visible CLI surface.
- the `git-workspace` line in `bh hive ready`.

Both survive the move: `gitworkspace_plugin.py` keeps its sub-app and its readiness probe, now
mounted by `cli.py` and called by `hive_ready.py` explicitly instead of through the generic plugin
loop. It is deliberately **not** dual-registered as both a dep and a plugin — that would
re-introduce the `required`-vs-`enabled` competition the type boundary exists to end.

`plugins.registry()` keeps orca, observaloop and hitch: genuinely optional integrations, each gated
on its own `enabled` flag.

---

## Decision 5 — eight registries derive from one table; two stay separate, with reasons

| Registry (before) | Members | What it became |
|---|---|---|
| `harness.HARNESSES` | claude, codex | derived — `deps.has_install_route()` |
| `harness_auth.PROBES` | gh, claude, codex | **deleted** — `auth` is a column; set derives from `deps.authenticated_deps()`; module renamed `credentials.py` |
| `setup.PROBE_TABLE` | git-workspace, gh, bd, dolt | derived — `deps.always_required()`, same triples, same order |
| `setup.RUNTIME_PROBES` | colima, docker, podman | derived — `deps.group_members("store-runtime")`, selector `dolt.backend` |
| `role.KNOWN_HARNESSES` | claude, opencode | derived — `deps.seat_runners()` (`d.runs_seats`) |
| `config.KNOWN_HARNESSES` | claude, opencode | derived — same call. **The one the epic's count missed** |
| `config_schema` `Literal[…]` (2 sites) | claude, opencode | **stays hand-written**, drift-gated |
| `hitch_plugin._readiness`'s `shutil.which()` | — | **stays**, and is not a dep probe |
| `flake.nix`'s four `# PROBE_TABLE` comments | git-workspace, gh, bd, dolt | comments deleted; one pointer to `deps.py` plus a real drift test |

`doctor.py`'s hand-written `probe_one("bd", "bd", ["bd", "--version"])` is a tenth site and became
a table lookup; `doctor._backend_tag` now **is** the store-runtime group's selector rather than a
second copy of it, so the cache's `backend` field and the runtime choice can no longer disagree.

**Two stay separate, and the reasons are different.**

`config_schema`'s `Literal["claude", "opencode"]` cannot be generated: a pydantic `Literal` built
from a runtime list loses static typing, which is the only reason to have it. The honest answer is
a gate, not generation —
`test_residue_config_schema_literal_mirrors_the_seat_runners` fails the moment it and `runs_seats`
disagree.

`hitch_plugin._readiness`'s own `shutil.which()` is **not** a second detection mechanism to fold
in. Hitch's binary name is a *config value* (`config.hitch_command(cfg)`), so it cannot be a static
table row at all. That is the required/optional boundary doing its job, and
`test_residue_plugin_readiness_probes_are_not_dep_probes` pins it as a decision rather than an
omission.

**The flake stays hand-mirrored on measured grounds, not by default.** The spike built both
derivation routes on the Linux test-bed and both work — `builtins.fromJSON (builtins.readFile
./deps.json)` under pure flake eval, and import-from-derivation running `python3` at eval time. So
it is a cost question, and the cost loses: Route A swaps a hand-mirrored flake for a hand-mirrored
JSON *plus* codegen *plus* a CI gate; Route B makes every `nix eval` build a derivation before it
can evaluate; and either way the name→attribute map stays manual (`bd` is a `beadsHead` override
with its own rev/hash/vendorHash, and `toolchainFor` also supplies `git` and `uv`, which are not
probe rows). The four `# PROBE_TABLE` comments are replaced by one pointer to `deps.py` and
`tests/test_flake_toolchain.py`, which is the gate the comments were only pretending to be.

**Detection stays one mechanism and two stages.** `deps.present()` delegates to
`setup.probe_one()` rather than adding a second `shutil.which()`. Stage 1 (`present`, "is it
here") and stage 2 (the auth probe, "is it usable") stay **separate gates** even though the table
is one, because `bh setup check`'s in-image manifest path is contractually zero-subprocess
(`test_setup_manifest.py`) and every auth probe shells out. `satisfied(dep) = present AND (no auth
OR authenticated)`, with the stage-2 answer passed *in* rather than probed, so nothing `setup
check` can reach will shell out.

`bh setup check` is otherwise untouched: still the presence gate, stage 1 only, same output. It
reads its rows from the table instead of owning them.

---

## Decision 6 — a codex-only host fails the gate, reversing `bh-q160.3`

`bh-q160.3` shipped `--check` with a pairwise OR over `(claude, codex)`, on an accepted criterion
that read: *a codex-only host is legitimate and must not fail the gate.*

**That acceptance rested on a false premise.** `bh role --harness codex` is rejected — codex 0.146.0
has no `--agent` equivalent, verified empirically (`codex --agent X` exits `unexpected argument`,
and a sweep of all 6,116 lines of `codex completion bash` finds one `*agent*` flag, a daemon
auth-identity flag on `remote-control`). So a codex-only host **passed a gate it then failed at**,
which is strictly worse than failing at the gate: the gate exists to tell an unattended install
that the host is not ready, and it was telling it the opposite.

Reversed with operator approval (2026-08-05). Under the selector model the OR does not move — it
**disappears**: every row required under `cfg` that carries a credential must have it, and
`required_deps()` selects claude XOR opencode. `bh-hsus.5` had already made codex `required="never"`
and removed it from the `agent` group, so the table already said codex could never satisfy the
gate; this made the gate agree with the table. Net effect: a codex-only host now either fails
`--check` or launches a seat, never both.

Secret hygiene carried over verbatim through the rename and the registry deletion: probes report
that a variable is **set**, never its contents, and the macOS Keychain query still omits `-w` so
`security` cannot print the secret even by accident. The planted-token test survives unchanged in
substance, and a new test pins the absent `-w` directly rather than by inspection.

---

## Finding — the same defect four times: correct, and aimed at the wrong fix

This is the finding most likely to be useful to somebody who was not here. The `bh-pc2a.33` failure
mode — **a true statement that points at the wrong fix** — appeared four times in one molecule, and
every occurrence traces to two lists disagreeing about the same set of tools:

1. **`missing_hint()` told the operator to run `bh harness install codex`** — a command that exits
   1 (`bh-hsus.1`). `HARNESSES` said codex was a harness `bh` installs; `install()` read
   `install.cmd` and said it was not.
2. **`harness.HARNESSES` derived as `d.install and d.install.cmd`**, which silently collapsed to
   `{claude}` the moment codex got `cmd=None` (`bh-hsus.3`). Fixed by splitting the predicate:
   `has_install_route()` = {claude, codex} (`bh` knows how it arrives) against `installable()` =
   {claude} (`bh` will run it), a strict subset.
3. **`role.py:215`'s missing-binary guard skipped itself** for exactly the harnesses absent from
   the *other* registry (`bh-hsus.5`). It keyed off `HARNESSES` (install-route rows), so an absent
   `opencode` fell straight through into the exec and produced a bare `opencode: command not
   found` — the precise failure that guard's own docstring says it exists to prevent, reproduced
   one call site over.
4. **`bh role --harness codex` said "unknown harness"** about a harness `bh` documents, installs
   and authenticates (`bh-hsus.6`). "Unknown" sends the operator off to check their spelling. The
   refusal now distinguishes the two cases from the table itself — a name in `deps.harnesses()`
   "cannot run a seat", anything else is genuinely unknown — and labels the list it offers for what
   it is: harnesses that can run a seat.

Three of the four were live in shipped code. The fourth (2) never had a caller: it landed in the
spike's zero-caller `deps.py` and the characterization test caught it when `bh-hsus.1` invalidated
it in parallel. That is one data point in favour of pinning both sides of a derivation, and it is
one data point, not a trend.

**This is the strongest single argument for the table.** None of these four is a hard bug. Each is
one list being asked a question a *different* list was the authority on, and each produced output
that was true, confident, and useless. A taxonomy does not prevent bugs; it prevents this specific
class of them, by making the second list stop existing.

---

## What was deliberately NOT built

- **No plugin system.** No dynamic loading, no registry protocol, no third-party extension points.
  Ten rows in a hand-written tuple, exactly like `plugins.registry()`. `bh-ckqt` said not to and
  nothing found argues otherwise.
- **No installer resolver.** The table does not branch on plane. Where a route differs by
  macOS/Linux/Nix that lives in `install.note` as **prose** — the direct consequence of
  `deployment-isolation-direction-adr.md` Decision 5, which split the toolchain by plane. `codex`
  carries `cmd=None` plus three routes as text; nothing dispatches on `sys.platform`.
- **No `requires` edge.** `bd` requires `dolt`, and no edge was added: both are `always` today,
  nothing needs it, and the group mechanism accommodates a chain when a second beads backend
  actually lands.
- **No `AUTH_PROBES` constant.** A named module-level container enumerating the gated rows would
  be `PROBES` again under a nicer name. The characterization test asserts on the module
  **namespace**: no module-level container may enumerate the gated rows.
- **No codegen** for `flake.nix` or for the pydantic `Literal` — drift tests instead, for the
  reasons in Decision 5.
- **No new detection mechanism.** `setup.probe_one()` was prior art in the tree and stayed the one
  way a dependency is detected.

---

## Sequencing — why this molecule went before the rest of `bh-q160`

`harness_auth.py` existed **only** on `wt/bead/epic/bh-q160`; `main` had never seen it. `bh-hsus.6`
had to rename that file and delete its `PROBES` dict, so it could not run against `main`.

The epic's own design note said this molecule "lands on that epic branch or waits for it to merge."
It did neither at first: `bh work start` forked the container off `main`, because that is what
`integration_base` is for a top-level epic. That was reconciled mid-flight by **merging
`wt/bead/epic/bh-q160` into this molecule's container** (`e15e2ce`) — a true merge, chosen
deliberately over a cherry-pick, so that `bh-q160.2` and `bh-q160.3` become **ancestors** here and
there is nothing to conflict when `bh-q160` later lands on `main`. A cherry-pick would have
duplicated the same file content across two lineages and guaranteed a conflict at exactly that
point. One conflict arose, in `cli.py`: both sides had added a command decorator immediately before
`harness_install` — `bh-q160.3` the new `harness auth` verb, `bh-hsus.1` a rewritten `install` help
string. Both were kept.

**Why this molecule went first**, with `bh-q160`'s remaining children blocked behind it:
`bh-q160.3` is what produced the defect, and every further `bh-q160` child would have built more
surface on the same wrong nouns. The taxonomy also *reverses* one of `bh-q160.3`'s accepted
behaviours (Decision 6), and reversing an acceptance is cheapest before more work depends on it.
Operator-approved sequencing, 2026-08-05.

---

## Where the molecule corrected itself

Stated plainly, because the final state was not the plan:

1. **The two-value `required` model was the spike's headline claim, and it did not survive.** The
   partition was true as literally asserted and the model was still wrong, because membership was
   doing unstated duty as a third value. Three values (Decision 3).
2. **The registry count was seven and is eight** (nine counting the flake comments the epic already
   counted separately). `config.KNOWN_HARNESSES` was missed.
3. **`required: bool` was insufficient on four counts, not the two `bh-ckqt` asked to be proven.**
4. **The `harness.HARNESSES` predicate changed under the spike's feet.** `d.install and
   d.install.cmd` was correct at `main` and wrong the moment `bh-hsus.1` gave codex `cmd=None`.
   The replacement is a gain, not a patch: two questions that had been one set by accident became
   two predicates.
5. **The spike's Q2 conclusion was replaced while its evidence stood.** It read "`install.cmd`
   stays; `npm install -g` remains the one route that works on every plane." npm was never a route
   that works — it looked universal because npm is universally *present*, which is not the same
   property, and alongside a native install it leaves a second copy on `PATH` whose precedence is
   shell ordering. Read correctly, nixpkgs' `claude-code` being **unfree** (so a pure `nix build`
   refuses) is a second independent argument *for* the native bootstrap, not for npm.
6. **The container was forked off the wrong base** and was fixed by merging, not by restarting
   (above).

Every one of these is recorded in place — amended, not edited away — in
`docs/spikes/bh-hsus.2-dependency-table.md` and in the commit bodies, so the record stays auditable.

---

## `bh-ckqt`'s acceptance criteria, one by one

| Criterion | Status |
|---|---|
| A written verdict on `harness`'s status, with the reasoning | This document. Answer: shape **B** — surface renamed, `bh harness *` kept as thin aliases, `harness` demoted to `kind`. Not shape C: nothing became a subtype of plugin (Decisions 1, 2) |
| The four registries derive from one source, or record why they stay separate | **Satisfied differently.** There were not four — there were eight, plus the flake comments. Six derive; `harness_auth.PROBES` did not derive, it was **deleted**; two stay separate with recorded reasons (Decision 5) |
| `gh` is infrastructure-that-needs-auth, reachable by the auth verb without being called a harness | Held. `kind="infra"`, `runs_seats=False`, `auth=Auth(…)`, reachable as `bh dep auth gh`. The module is `credentials.py` — named for what it probes, not for who owns the credential |
| The model expresses unconditional, conditional-on-config, and one-of-a-group; `required: bool` proven insufficient by two cases | **Satisfied differently.** All three are expressed, but conditional-on-config and one-of-a-group are the **same mode**, so three situations need two values, not three. And `required: bool` fails on **four** counts, not two (Decision 3) |
| No dynamic loading, no registry protocol, no third-party extension points | Held, and the list of things not built is above |
| Any rename keeps `bh harness auth` working | Held. `bh harness list\|install\|auth` are single-call aliases into `dep_cli`, so they cannot drift from the canonical verbs |

`bh-ckqt` also asked, in its design section, that `setup.probe_one()` stay the single detection
mechanism "whatever the taxonomy above it looks like." It did.

---

## Consequences

- **Adding a dependency is now one edit.** A row in `DEPS` with the right columns, and every
  registry that mentions it follows. Forgetting a list is no longer possible for six of the eight;
  for the other two it is a test failure rather than a silent disagreement.
- **`bh` gains a noun.** `bh dep` is the canonical surface and `bh harness` is an alias, so three
  verbs have two spellings. The aliases are one line each and hidden from help, but they are still
  two names for one thing and will need a deprecation decision eventually.
- **A codex-only host now fails `bh dep auth --check`.** This is a deliberate reduction in what
  passes the gate, and it will surface as a regression to anyone who had one working (Decision 6).
- **`git_workspace.enabled` is gone, not deprecated.** A persisted config carrying it is migrated —
  the key is deleted on the next real CLI invocation — and `validate_config()` downgrades a stray
  one to a non-blocking warning in the meantime. Five docs that instructed operators to set it were
  updated to say what is true now.
- **The Dep/Plugin boundary has a third case.** `required="never"` means a row can be a dep and
  never be required. One row occupies it (Decision 3) and a second would deserve scrutiny.
- **`deps.present()` shells out.** Delegating to `probe_one()` buys "one detection mechanism" at
  the price of running the tool's version command to answer "is it on `PATH`". That is fine for
  `setup check`, which wanted the version anyway, and it is a real cost for any future caller that
  only wants presence.

---

## Limitations

1. **Q1's answer is a version fact, not a design fact.** "codex cannot run a seat" was measured
   against codex **0.146.0**. Re-run the probe before relying on it — if a later codex grows an
   `--agent` equivalent, `runs_seats=True` and `required="group:agent"` are both back on the table,
   and Decision 6's reversal would need revisiting with it.
2. **The full gate never ran on this molecule.** `just check-all` was red on `main` — eight
   integration tests failing, and the gate the justfile says to wire at main-merge points was not
   wired (`bh-dfz2`, since fixed: the eight are green and lefthook's `pre-push` now runs
   `check-all` on any push that updates `main`). Everything here was validated by `just check`,
   which excludes `-m integration`, so the integration surface of *this* change was unproven at
   the time it landed — the next `main` push is the first run that covers it.
3. **The Linux verification is uneven across the molecule.** `bh-hsus.1` verified the native
   bootstrap on the test-bed and `bh-hsus.2` recorded a byte-for-byte `bh setup check` comparison
   there. `bh-hsus.4`–`.6` were verified on macOS only; the spike's stated gate for skipping a
   test-bed re-run is "the change touched detection or `PATH` logic", and those three changed
   derivation, ownership and messaging rather than probing. That reasoning is sound and it is still
   a gap, given that five of six defects in the session that produced `bh-ckqt` were invisible on
   macOS.
4. **`harness.installed_path()` is a second `shutil.which()` in the tree.** It answers "*where*"
   rather than "*is it here*" — `probe_one` returns no path — so it is not a duplicate detection
   mechanism so much as a second caller of the same stdlib function for a different output. It
   pre-dates this molecule and was not folded in. Naming it here so the "one detection mechanism"
   claim is read with the right scope.
5. **The table is hand-written and always will be.** Nothing about this design scales past a few
   dozen rows, and nothing about it should. It is a taxonomy over known tools.
6. **Two mirrors survive**, closed by drift tests rather than by construction (Decision 5). A test
   is weaker than a compiler; it is what is available for a pydantic `Literal` and a Nix
   expression.

---

## Relationship to other beads

| bead | relationship |
|---|---|
| `bh-ckqt` | the design bead this ADR answers — its first acceptance criterion is this document |
| `bh-hsus` | the implementation molecule: `.1` install route, `.2` spike, `.3` land the table, `.4` git-workspace, `.5` split the registries, `.6` the `bh dep` surface, `.7` this record |
| `bh-q160` | its container is an **ancestor** of this molecule (Sequencing); `bh-q160.3`'s codex-only acceptance is reversed by Decision 6; its remaining children were blocked behind this work |
| `bh-lecz` | pre-existing — `auth --check` passes on an **expired** credential, because presence is not validity. The taxonomy gives it a home as stage 2 of `satisfied()`, which makes it a small fix rather than a design question |
| `bh-h5if` | discovered by `bh-hsus.1` — the container's `bh-harness` volume mounts `~/.claude` while the native installer writes `~/.local`, so an installed harness does not survive a container restart |
| `bh-c3nf` | discovered driving this molecule — batch-path defects in `bh work`: a stray per-bead worktree from `resume`, `bh work check` validating the wrong tree and seeding a **false green** into the ledger, and `merge --group` closing only one member |
| `bh-dfz2` | discovered here — `just check-all` red on `main`, and the gate not wired (Limitation 2) |
| `bh-pc2a.33` | the failure mode the Finding section names four fresh instances of |
| `bh-pc2a.36` | the proprietary-harness stance — its licence text and its "the user is told what they are accepting" guarantee moved onto the table and into `bh dep list`, and were re-decided by nobody |
| `deployment-isolation-direction-adr.md` | its Decision 5 (toolchain split by plane) is why this table does not branch on plane |
