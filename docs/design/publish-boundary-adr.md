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
