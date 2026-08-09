# Publish boundary for bead data ADR (bh-7jm7v)

> Status: **bh-7jm7v.1 decided and verified; siblings pending.** This ADR belongs to epic
> bh-7jm7v ("Publish boundary for bead data") — a POLICY epic. Nothing here ships a publish
> pipeline; it specifies decisions with structural teeth that a future publish step (the
> eventual beadhive.ai integration, not built in this repo) must respect. Each child bead owns
> one section and amends this file when it lands:
>
> - **bh-7jm7v.1 (this section): the exact `bd export` flag set for a public snapshot.** Decided
>   below.
> - bh-7jm7v.2: which bead FIELDS appear in a public snapshot (the record subset — separate
>   axis from which RECORDS are exported, which is this section's concern).
> - bh-7jm7v.3: structural guarantee that the publish path can't reach the HQ-wide loader
>   (`beadhive-data/src/bead-graph.ts`'s `scope=hq` mode) — single-hive scoping enforced by the
>   absence of a code path, not by convention.
> - bh-7jm7v.4: `schema_version` on the published payload.
>
> This section does not pre-write any of .2/.3/.4's content.

## bh-7jm7v.1 — the exact `bd export` flag set for a public snapshot

### The question

Section 09 of the Bead Graph x Git History Overlay proposal flagged that whatever flags a
public publish step passes to `bd export` need to be specified explicitly. `bd export --help`
says the default invocation already excludes both infrastructure beads (agents/roles/messages)
and memories (`bd remember` content) — so getting the safe behavior doesn't strictly require
passing any flag at all. That is exactly the trap: **relying on a default that happens to be
safe today is not the same as a decision that stays safe when the code calling `bd export` is
rewritten by someone who doesn't know that history.** This section names the invocation
explicitly, including which flags must never be added, so the contract survives a rewrite.

### The decision

The public-snapshot invocation, run from inside the target hive (so `bd` resolves that hive's
own local `.beads` database — no `-r <hive>` / bh-level `-a`/`--all` fan-out to other
registered hives):

```sh
bh bd export -o <dest>/issues.jsonl
```

(equivalently, wherever `bh` isn't in the loop — e.g. a non-`bh` publish step running the
upstream binary directly — the same contract is: `bd export -o <dest>/issues.jsonl`, `bd`
being invoked once, `-C`'d or `cwd`'d into exactly the one hive being published, never a
multi-hive or HQ-scoped invocation.)

**Must NOT be present, ever:** `--all`, `--include-memories`, `--include-infra`. Any one of
these re-admits infra beads and/or memories; `--all` re-admits both plus bd's ephemeral wisps
table (see Verification). A publish step's test suite should assert these three flags are
absent from the constructed command line, not merely that the output looks clean on one run.

**Decided against, with reasons (not required, not forbidden — see below):** `--scrub`,
`--exclude-owner`.

### Why explicit flags, not defaults — this is not hypothetical in this repo

`bd export`'s own default already excludes memories and infra beads. The risk is that a
DIFFERENT code path — one not written yet, in a different repo, possibly a different language
— reuses a nearby example that isn't the safe one. This is not a hypothetical: **this repo
already contains both shapes side by side.**

| call site | invocation | flags | why |
|---|---|---|---|
| `hub.py` (`_sync_hive`, `HQ` fleet aggregation) | `engine.export_jsonl()` → `bd -C <hive> export -o <path>` | none | hub aggregation reads only what a hive is willing to publish into the fleet view |
| `hq.py` / `storage_migrate.py` (pre-op integrity check) | same `export_jsonl()` helper | none | verifying issue counts before a migration/push, not a full-fidelity backup |
| `cli.py:2591` (`bh backup export`, backup ADR root #3) | `run(["bd", "export", "-o", ..., "--all"])` | **`--all`** | this one is deliberately full-fidelity — an operator's own portable copy of their own data (`docs/design/backup-retention-boundary-adr.md`, root #3) |

Three call sites use the safe default; one — `bh backup export`, the closest existing example
of "export this hive's JSONL to a file for someone to look at" — deliberately uses `--all`,
correctly, for its own purpose. A public-snapshot implementation is more likely to be adapted
from the operator-facing `bh backup export` shape (same verb, same "one file out") than from
the internal fleet-sync helper, and copying that one wholesale carries `--all` straight into a
publish path where it does not belong. Naming the exact flags — including the negative list —
closes that specific, demonstrated failure mode instead of a hypothetical one.

### Flags decided against

**`--scrub` ("exclude test/pollution records") — decided NOT to use, with evidence.**
Ran it against this hive and diffed the 7 records it drops against a default export
(command + result in Verification below). All 7 are real, current bug/task beads about test
flakiness in this codebase (e.g. `bh-712wt`: *"test_mcp_install::test_install_success is flaky
under xdist..."*, `bh-myp0`: *"Test suite scans the operator's real $GIT_WORKSPACE — hermeticity
leak..."*) — none are pollution or synthetic fixtures. `--scrub`'s definition of "test/pollution"
appears to key on text matching (titles starting with `test_`/`Test`) rather than any actual
provenance/synthetic marker, so on this hive's real backlog it produces false positives: real
engineering content silently dropped from a snapshot that's supposed to be complete. Using it
would make the public snapshot quietly incomplete with no signal to the reader that anything
was withheld. Not used.

**`--exclude-owner` — decided NOT to use by default, left as an available lever.**
This hive has no test/bot/throwaway identity in its owner set today (`created_by` is `Brian
Cripe` or a real dispatcher/developer agent identity throughout; `.beads/config.yaml` sets no
`export.exclude_owners`, and none of the flags exercised in Verification needed it). There's
nothing to exclude right now, and baking in a speculative identity list would be curation
without a present need. If a hive later develops a genuine throwaway/sandbox identity whose
issues shouldn't appear publicly, `--exclude-owner <identity>` (repeatable) is the documented
mechanism to add explicitly at that point — this decision doesn't rule it out, it just doesn't
invoke it pre-emptively.

### Verification — run against this hive's real data

All commands below were run from this repo's worktree (`bh-7jm7v.1`'s own worktree, itself a
clone of the `beadhive/beadhive` hive) against the hive's real live Dolt-backed database, not a
fixture. Working set was returned to its pre-verification state after each mutating step (a
throwaway memory and a throwaway `message`-type bead were created, checked, and deleted).

**1. Baseline — default vs. every re-admitting flag, before any test record existed:**

```sh
$ bh bd export -o default.jsonl
Exported 2392 issues to default.jsonl
$ bh bd export --all -o all.jsonl
Exported 2392 issues to all.jsonl
$ bh bd export --include-memories -o mem.jsonl
Exported 2392 issues to mem.jsonl
$ bh bd export --include-infra -o infra.jsonl
Exported 2392 issues to infra.jsonl
$ diff default.jsonl all.jsonl && echo IDENTICAL
IDENTICAL
```

Same count, byte-identical — this hive has no memory/infra records to leak *today*, so this
alone doesn't prove the filters work. It only proves nothing is currently at risk. Tested the
filters directly next.

**2. Memory exclusion, positive-control:**

```sh
$ bh bd remember "ADR verification throwaway memory for bh-7jm7v.1 — safe to delete" \
    --key adr-verify-throwaway
Remembered [adr-verify-throwaway]: ...
$ bh bd export -o default2.jsonl && bh bd export --include-memories -o mem2.jsonl
Exported 2392 issues to default2.jsonl
Exported 2392 issues and 1 memories to mem2.jsonl
$ grep -c adr-verify-throwaway default2.jsonl mem2.jsonl
default2.jsonl:0
mem2.jsonl:1
$ bh bd forget adr-verify-throwaway
Forgot [adr-verify-throwaway]: ...
```

Default export: **0** hits for the memory. `--include-memories`: **1** hit. Filter confirmed
live, then the memory was removed (`bd memories` reports "No memories stored" again).

**3. Infra-bead exclusion, positive-control:**

```sh
$ bh bd create "ADR verification throwaway message-type issue for bh-7jm7v.1 — safe to delete" \
    -t message --json
{"id": "bh-wisp-p7v", "issue_type": "message", "ephemeral": true, ...}
$ bh bd promote bh-wisp-p7v --reason "ADR verification: promote to test --include-infra, then delete"
✓ Promoted bh-wisp-p7v to permanent bead
$ bh bd export -o default4.jsonl && bh bd export --include-infra -o infra4.jsonl
Exported 2392 issues to default4.jsonl
Exported 2393 issues to infra4.jsonl
$ grep -c bh-wisp-p7v default4.jsonl infra4.jsonl
default4.jsonl:0
infra4.jsonl:1
$ bh bd delete bh-wisp-p7v --force
✓ Deleted bh-wisp-p7v
```

Default export: **0** hits for the `message`-type bead. `--include-infra`: **1** hit. Filter
confirmed live on a real (non-fixture) infra-typed record, then the record was deleted.

Side finding worth recording: while the bead was still an ephemeral wisp (bd's separate,
`dolt_ignored` wisps table — `bd create -t message` lands there by default), `--include-infra`
alone did **not** surface it; only `--all` did (2393 either way once promoted to permanent, but
2392/2392/**2393** — default/`--include-infra`/`--all` — while still a wisp). `--all` reaches a
third table `--include-infra`/`--include-memories` individually do not. This reinforces keeping
`--all` off the forbidden-flags list's redundant twin: it is strictly wider than the sum of the
other two, not equal to it.

**4. `--scrub`'s false positives (the 7 records cited above):**

```sh
$ bh bd export -o default.jsonl && bh bd export --scrub -o scrub.jsonl
Exported 2392 issues to default.jsonl
Exported 2385 issues to scrub.jsonl
```

7 records dropped (`bh-712wt`, `bh-c42i.2`, `bh-go6i`, `bh-jksq.6`, `bh-jrk5g`, `bh-myp0`,
`bh-ts2yp`) — inspected each via `bh bd show <id> --json`; all real, current, non-pollution
bug/task beads.

**5. Final state check — hive returned to its pre-verification baseline:**

```sh
$ bh bd memories
No memories stored. Use 'bd remember "insight"' to add one.
$ bh bd export -o final.jsonl && diff default.jsonl final.jsonl && echo IDENTICAL
Exported 2392 issues to final.jsonl
IDENTICAL
```

## bh-7jm7v.3 — single-hive scoping, enforced by the absence of a code path

### Scope translation: what "the HQ-wide loader" is *in this repo*

This bead's acceptance criteria were written against a sibling repo's shape ("no import or
call path reaching `bead-graph.ts`'s HQ-wide loader"). `bead-graph.ts` lives in
`beadhive-ui`/`beadhive-data` and does not exist here; nothing in this section touches that
repo. The property it names, however, is repo-independent — *the publish path must not be able
to address more than the one hive being published* — and this repo has its own, larger version
of exactly the machinery that criterion is about:

| this repo | what it is |
|---|---|
| `src/beadhive/hub.py` | "one aggregated beads DB (under `$BH_HOME`) holding a cross-hive view of every registered hive"; `bh hub <bd cmd>` queries it. The direct analog of the TS-side HQ-wide loader. |
| `src/beadhive/hub_bulk.py` | bulk operations over that same aggregate |
| `src/beadhive/hq.py`, `src/beadhive/hq_restore.py` | Factory HQ — the durable fleet-wide aggregation store |
| `bd.passthrough()` → `route.targets()` / `route.fan_out()` | the `-a`/`--all` and `-r`/`--hive` machinery behind the `bh bd` passthrough, which runs ONE `bd` subcommand across MANY hives |

So "single-hive scoping, structurally" here means: the publish path has no import or call path
to `hub` / `hub_bulk` / `hq` / `hq_restore`, and does not reach for the cross-hive fan-out.

### Why a module was created rather than only a test

The bead's own design note prefers "a module boundary that makes the Hub loader unreachable
from the publish package", with "a test that asserts the absence of the call path" as the
fallback. Neither was available off the shelf, because **there is no publish code in this repo
at all** — nothing publishes, and this epic explicitly does not build a publish pipeline. There
was therefore nothing to split apart and nothing whose call graph a test could walk.

The choice taken is the module boundary (the bead's preferred form), sized down to what is
honestly true today: `src/beadhive/publish_export.py` is a **boundary scaffold** — one
sanctioned function wrapping bh-7jm7v.1's decided invocation, **not wired into any CLI command
and not called from anywhere**. Its docstring says so. That is the point of it: it fixes the
shape the eventual publish step must be written into, so that step is a call rather than a
fresh, unguarded `bd export` someone adapts from `bh backup export` (the `--all` hazard
bh-7jm7v.1 documented). The fallback — a test asserting properties of call sites that exist for
other reasons (`hub._sync_hive`, `storage_migrate`, `cli`'s `backup export`) — would have
guarded *those* modules' scoping, which is not the thing at risk: they are supposed to be
Hub-capable. Guarding them would have been the decorative version.

What was deliberately **not** built, since the module could have grown into it: no service, no
scheduler, no beadhive.ai integration, no CLI verb, no caller. If a future bead wires a real
publish step, it calls `export_public_snapshot()`; the guard below then applies to real code
without being rewritten.

### The guard

`tests/test_publish_boundary.py`. Five checks, each a pure function over its input so it can be
run against both the real code and a widened variant (see the next section).

1. **Transitive import closure** (the structural guarantee). A BFS over the package's static
   import graph from `publish_export`, collecting every `beadhive.*` sibling reachable at any
   depth, must contain none of `hub` / `hub_bulk` / `hq` / `hq_restore`. The walker uses
   `ast.walk`, so an import **deferred inside a function body** counts exactly like a
   module-level one — this package uses lazy imports to break real cycles (`bd.run` imports
   `engine` that way), so a lazy `from . import hub` is the most plausible regression, not a
   contrived one. All five import spellings are handled and pinned by a test
   (`from . import x`, `from .x import y`, `from beadhive import x`, `from beadhive.x import y`,
   `import beadhive.x`).
2. **Direct-reference ban.** `publish_export`'s own AST must not import `route`/`registry` or
   any aggregate module, must not reference `passthrough` / `fan_out` / `targets`, and must not
   name `importlib` / `__import__` / `exec` / `eval` / `compile`. Docstring text is exempted so
   that *describing* the boundary is never mistaken for crossing it (this file's own module
   docstring names `route.fan_out`; a guard that punished the explanation would push authors
   toward deleting it).
3. **Constructed argv.** `public_snapshot_argv()` is pure and returns exactly
   `["export", "-o", "<dest>/issues.jsonl"]` — bh-7jm7v.1's invocation, with no conditional
   flag anywhere that could grow an `--all`. Asserted by equality *and* against that section's
   negative list (`--all`, `--include-memories`, `--include-infra`) plus the routing flags
   (`-a`, `-r`, `--hive`, `--global`). This is the "assert the flags are absent from the
   constructed command line, not merely that one run's output looks clean" that bh-7jm7v.1
   asked a publish step's test suite for.
4. **Pinned signature.** `export_public_snapshot(hive_root, dest_dir)` — any added or renamed
   parameter fails. There is no `hive=` / `scope=` / `all=` parameter, *not even one defaulting
   safely*: a parameter that exists is a parameter a caller can pass. `hive_root` is a
   filesystem path ("the checkout you are standing in"), never a hive name — a name would be a
   registry lookup, which is the `-r <hive>` shape.
5. **Runtime refusal.** The one way a path-shaped argument could still address the aggregate is
   by pointing at it, so `hive_root` is refused when it resolves inside `config.home()`
   (`$BH_HOME`), `hub_dir()`, `hq_dir()` or `cache_dir()` — the last because a cache clone is
   *another* hive's data sitting on this machine. `$BH_HOME` is checked as well as the three
   stores under it so a future aggregate store is covered without editing this list; the three
   are checked individually because each is separately overridable by env and can sit outside
   `$BH_HOME`. A directory with no `.beads/` is also refused.

**Where the boundary is drawn, and the one place it is weaker than it sounds.** `route` and
`registry` are reachable transitively from `publish_export`, because it calls `bd.run()` — the
package's shared bd-invocation helper, which emits `bd -C <hive_root> …` (one hive, named by
path) but sits in a module that also hosts the fan-out entry point. A *transitive* ban on
`route`/`registry` is therefore unachievable without duplicating the bd seam inside the publish
module, which would trade a real, tested invocation path for a hand-rolled one — a worse
outcome than the thing it would buy. So the ban on those two is by **direct reference**
(check 2) while the aggregate modules are banned **transitively** (check 1). Stating that split
is the point: the guard claims exactly what it enforces. The aggregate stores are the payload —
reaching `hub` is what publishes a private hive's beads — and nothing reaches them.

### How the guard was verified to fire

A guard that names machinery nobody calls is dischargeable by doing nothing. This one was
proven to fail on the widening it exists to prevent, two ways.

**A. Anti-vacuity is itself enforced in CI**, not just performed once here. The test file
carries positive controls that would break if the walker ever went blind:

- the same walker, run over this package's genuinely Hub-capable modules, must FIND them —
  `cli → hub`, `cli → hq`, `cli → hub_bulk`, `hq → hub`,
  `storage_migrate → hq` — and every hop of each reported chain is re-verified against the
  source, so it cannot pass by inventing a path;
- a synthetic package proving the walk is transitive *and* sees an import nested inside a
  function inside a function (`entry → middle → hub`);
- the real `publish_export.py` source, re-parsed with `from . import hub` spliced in as the
  last statement of `export_public_snapshot`, must produce a violation — plus the same for all
  five module-level spellings, for `route.fan_out(...)` / `bd.passthrough(...)` /
  `importlib.import_module(...)` call injections, for each forbidden flag, and for each
  signature drift.

**B. Manually widened on a scratch branch** (`scratch/widen-publish-boundary`, deleted after —
this is the bead's "attempt the widening and confirm the check fires" criterion), reverting
between each:

*Widening 1 — a deferred `from . import hub` inside `export_public_snapshot`, followed by a
`hub.sync` reference.* `uv run pytest tests/test_publish_boundary.py` → **2 failed, 38 passed**:

```text
FAILED test_publish_export_cannot_reach_any_aggregate_module
E  AssertionError: publish_export can now reach a cross-hive module — a public snapshot could
   span private hives: {'hq': 'publish_export -> hub -> hive -> onboard -> storage_migrate -> hq',
   'hub': 'publish_export -> hub', 'hub_bulk': 'publish_export -> hub -> hub_bulk'}.
   See docs/design/publish-boundary-adr.md before widening this.
FAILED test_publish_export_makes_no_direct_routing_reference
E  AssertionError: assert ['imports beadhive.hub'] == []
```

Note the blast radius the message reports: one import of `hub` also drags in `hq` and
`hub_bulk` five hops away, through `hive → onboard → storage_migrate`. That chain is why a
convention ("don't call the Hub loader") would not have held — the widening does not have to
name `hq` to reach it.

*Widening 2 — `--all` added to the constructed argv* (the exact bh-7jm7v.1 hazard):
**2 failed, 38 passed**, `test_public_snapshot_argv_is_exactly_the_decided_invocation` →
`At index 1 diff: '--all' != '-o'`, and `test_export_public_snapshot_runs_the_decided_invocation`
on the same argv. (The equality assertion trips first; the flag-list assertion on the same argv
is proven separately by the parametrized anti-vacuity case for each of the seven flags.)

*Widening 3 — a `hive: str = ""` parameter added* (the innocuous-looking one, safely defaulted):
**1 failed, 39 passed** —
`signature drifted: ('hive_root', 'dest_dir', 'hive') != ('hive_root', 'dest_dir')`.

Reverted, scratch branch deleted, suite green (40 passed).
