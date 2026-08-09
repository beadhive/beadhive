# Publish boundary for bead data ADR (bh-7jm7v)

> Status: **bh-7jm7v.1 and bh-7jm7v.2 decided and verified; .3/.4 pending.** This ADR belongs to
> epic bh-7jm7v ("Publish boundary for bead data") — a POLICY epic. Nothing here ships a publish
> pipeline; it specifies decisions with structural teeth that a future publish step (the
> eventual beadhive.ai integration, not built in this repo) must respect. Each child bead owns
> one section and amends this file when it lands:
>
> - **bh-7jm7v.1: the exact `bd export` flag set for a public snapshot.** Decided below.
> - **bh-7jm7v.2: which bead FIELDS appear in a public snapshot** (the record subset — separate
>   axis from which RECORDS are exported, which is .1's concern). Decided below.
> - bh-7jm7v.3: structural guarantee that the publish path can't reach the HQ-wide loader
>   (`beadhive-data/src/bead-graph.ts`'s `scope=hq` mode) — single-hive scoping enforced by the
>   absence of a code path, not by convention.
> - bh-7jm7v.4: `schema_version` on the published payload.
>
> Neither decided section pre-writes any of .3/.4's content.

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

## bh-7jm7v.2 — the publishable FIELD subset of a bead record

### The question

The section above decided WHICH RECORDS leave the hive. This one decides, given a record is in
that export, WHICH OF ITS FIELDS are publishable. Separate axis, same artifact — together they
are the whole boundary.

Section 07 promises the detail panel shows the *complete underlying record*. That is right for
an internal viewer. Pointed at a public snapshot, "complete" needs a stated edge, because the
record carries more than the work: it carries the machinery that produced the work, one raw
email address, a free-form metadata dict, and ~3.4 MB of free text nobody reviewed for
publication.

### Where this is enforced: a post-export filter, not a flag

`bd export` has **no field-selection flag**. Its complete flag set is `--all`,
`--include-infra`, `--include-memories`, `--scrub`, `--exclude-owner`, `-o`, `--verbose` —
every one of them selects *records*, none selects *fields*. With .1 fixing the invocation to
exactly `bh bd export -o <dest>/issues.jsonl`, it follows that this boundary **cannot** be
enforced at the export call. The enforcement point is a filter sitting between `bd export`'s
output and publication, and the publish step must have no path that copies a raw
`issues.jsonl` to a public location. No such filter exists yet and this section does not build
one; it specifies what one must do, precisely enough to be implemented from the table alone.

### Three principles that decide every row

**P1 — the structure of the work publishes; the state of the machinery driving it does not.**
A bead's shape, graph, timing and content are the artifact. Lease timers, gate modes, edge
provenance and the exporter's own wire discriminator are how the factory ran, not what it
produced.

**P2 — never publish a field whose presence or absence makes another published field
misleading.** Both directions. It keeps `close_reason` in (a bead closed `orphaned by bounce —
cleared on resume` is not the same as one closed `merged`, and `status` alone cannot say so),
and it takes the derived counts out (see below).

**P3 — allow-list by key, and fail closed.** A key nobody has decided about is withheld. This
is .1's failure mode one level down: relying on a shape that happens to be safe today is not a
decision that survives a schema addition. `bd`'s export schema can grow at any upstream
release, and a filter that copies unknown keys inherits whatever the next one holds.

Two cross-cutting rules follow: **Rule I** (identity shape) and **Rule F** (free text), each
stated in its own section below.

### The allow-list — top level

Enumerated against the real export from this hive (`bh bd export`, 2026-08-09): **N = 2394
records, 31 distinct top-level keys observed**. The table is normative. A key absent from it is
withheld by P3 — including any key a future `bd` adds.

|field|publish?|reason|
|---|---|---|
|`id`|**yes**|The join key. Every edge, every link and the graph itself are meaningless without it.|
|`title`|**yes** (Rule F)|The primary display value.|
|`description`|**yes** (Rule F)|Section 07's promise. The largest field: 2383 records, 1.60 MB.|
|`design`|**yes** (Rule F)|The reasoning behind the work — the thing a public bead graph exists to show.|
|`acceptance_criteria`|**yes** (Rule F)|What "done" meant. Reads as a public contract; 809 records, no measured risk hits.|
|`notes`|**yes** (Rule F)|Execution findings. Highest risk *density* of any field — see Rule F.|
|`close_reason`|**yes** (Rule F)|P2: qualifies `status`. `merged` vs `orphaned by bounce — cleared on resume` vs `Refiled via bh plan…` are different outcomes that `status: closed` flattens.|
|`status`|**yes**|Bounded enum, no free content.|
|`priority`|**yes**|Bounded integer.|
|`issue_type`|**yes**|Bounded enum, and .1's record filter is defined in terms of it.|
|`labels`|**yes**|The viewer's faceting axis and this hive's whole taxonomy (153 distinct values across 45 namespaces). See the residual note under Rule F: a label is the one *structured* field an author can put arbitrary text into.|
|`created_at`|**yes**|Work-graph time.|
|`updated_at`|**yes**|Work-graph time.|
|`started_at`|**yes**|Work-graph time.|
|`closed_at`|**yes**|Work-graph time. These four expose working cadence, which the public commit timestamps on the same remote already expose at finer resolution.|
|`defer_until`|**yes**|P2: qualifies `status`. A deferred bead published as plain `open` misrepresents the backlog.|
|`mol_type`|**yes**|P1-structure: the shape of the work graph (`swarm`, 73 records), which a graph viewer legitimately renders. Bounded enum.|
|`dependencies`|**yes**, sub-filtered|The graph. Element-level table below.|
|`comments`|**yes**, sub-filtered (Rule F)|`bd export` inlines full comment **text**, not just a count — 23 comments across 17 records, 36 KB. Element-level table below.|
|`external_ref`|**conditional**|Publish **only when the value resolves to an `id` present in the same snapshot**; otherwise omit the key. Measured: 6 records carry it, 5 resolve in-export, and 1 (`bh-8g9d` → `homelab:hl-hkd.4`) is a `<hive>:<id>` pointer at a *different, unpublished hive*. That is a cross-hive reference in a **structured** field — the same leak class Rule F handles for prose, and it would have ridden along silently.|
|`metadata`|**key allow-list**|Publish the single key `git.commits` and nothing else; omit the dict when that key is absent. See both sections below.|
|`assignee`|**yes** (Rule I)|Seat / display name. Who did the work is the substance of an agentic factory, and every seat name in this corpus is already a public git **author** on this remote.|
|`created_by`|**yes** (Rule I)|Same.|
|`owner`|**no**|It is a raw email address in 1698 of 1698 records it appears in, and the **only** email-shaped value anywhere in the corpus. Zero display value (constant per hive — the repo already states who owns it). Withheld by Rule I, which drops it by shape rather than by name so the boundary survives a hive whose `created_by` is an email too.|
|`await_type`|**no**|P1-machinery: the mode of the kickoff/review gate (`human`, 468 records), not a property of the work. Already implied by the published `kickoff:` / `review:` labels.|
|`lease_expires_at`|**no**|P1-machinery: live claim-lease state. Meaningless in a snapshot, and it is the one field that signals an agent is running *right now* on a specific bead.|
|`heartbeat_at`|**no**|Same.|
|`_type`|**no**|`bd export`'s own wire discriminator (constant `issue` across all 2394 records). Republishing it couples the public payload to the exporter's format, which is exactly the coupling the published payload should not have.|
|`dependency_count`|**no**|Derived, **and it does not mean what the published array shows**. Measured: it counts outgoing `blocks` edges only (2394/2394 exact), while `dependencies[]` carries six edge types — so the two disagree in 1393 of 2394 records (e.g. `bh-7jm7v.2`: count `1`, array `2`). Published side by side it renders an actively wrong panel (P2). Nothing is lost: it is exactly `[edges where issue_id == self and type == "blocks"] \| length` over the published edge set.|
|`dependent_count`|**no**|Same, inbound: exactly `[edges where depends_on_id == self and type == "blocks"] \| length` graph-wide (2394/2394 exact). The published edge set is closed — 0 dangling targets across 2453 edges — so the reconstruction is total.|
|`comment_count`|**no**|Derived and exactly redundant: equals `comments \| length` in 2394/2394 records. Dropped with its two siblings so the rule is one rule ("derived counts are the consumer's job") rather than three exceptions.|

**23 of 31 top-level keys publish. 8 are withheld.**

### The allow-list — `dependencies[]` elements

2453 edges, 6 keys each.

|field|publish?|reason|
|---|---|---|
|`issue_id`|**yes**|Edge source.|
|`depends_on_id`|**yes**|Edge target.|
|`type`|**yes**|Edge kind (`blocks`, `parent-child`, `discovered-from`, `relates-to`, `related`, `supersedes`) — the semantics the graph is drawn from.|
|`created_by`|**no**|P1-machinery: edge provenance. Who typed a dependency is not the dependency.|
|`created_at`|**no**|Same.|
|`metadata`|**no**|An unbounded free-form dict on every edge, closed by P3. It is `{}` in all 2453 edges today, so denying it costs exactly nothing now and closes the same hole the top-level `metadata` rule closes.|

### The allow-list — `comments[]` elements

23 comments across 17 records; `comments` is present precisely when `comment_count > 0`.

|field|publish?|reason|
|---|---|---|
|`id`|**yes**|Opaque surrogate key; a stable anchor for deep links into a comment.|
|`issue_id`|**yes**|Join key back to the bead.|
|`author`|**yes** (Rule I)|Seat / display name, same treatment as `created_by`.|
|`created_at`|**yes**|Work-graph time.|
|`text`|**yes** (Rule F)|Full comment body. This is the least-reviewed prose in the corpus: agent-authored review notes averaging ~1.6 KB, written mid-execution for an audience of one.|

### The allow-list — `metadata` keys

|key|publish?|reason|
|---|---|---|
|`git.commits`|**yes**|Decided explicitly in the next section.|
|*anything else*|**no**|P3. `metadata` is written by `bd update --set-metadata`, which stores **any** key with its value verbatim — it is a free-form dict, not a schema. Today it holds exactly one distinct key across 460 records; the allow-list is by key so that stays true regardless of what a future tool stashes there.|

### `git.commits` — decided explicitly: IN

`git.commits` is the flat metadata key from
[`bead-commit-linkage-contract.md`](bead-commit-linkage-contract.md) (epic bh-1b0rc): a
JSON-encoded array of full 40-character commit SHAs, present on 460 records, 966 (bead, SHA)
pairs, 643 distinct SHAs. It arrived via a backfill, and the risk with a field that arrives by
backfill is that it rides into a public payload unexamined. Decided on its own merits:

**The endpoints are already public, and so is the association.** Both halves matter, and the
second is the one worth checking rather than assuming.

- *Endpoints:* all 643 distinct SHAs resolve in this repo; 636 are reachable from
  `origin/main`, and the other 7 are on local `main` merely not yet pushed. Zero are orphaned.
- *Association:* the concern is that linking SHA → bead correlates public commits into a work
  narrative the commits alone do not tell. Measured, it does not: **966 of 966 pairs have the
  bead id verbatim in the commit message.** That figure is partly tautological (this corpus is
  backfill-derived, and the backfill matched on commit messages), so the independent evidence
  is what settles it: `bh work merge` writes the bead id into the **merge bubble subject**
  itself — 192 commits on `origin/main` read `chore(merge): bead <id>` — and it writes that
  subject from the bead, with no dependence on any developer's own subject lines. The
  bead↔commit correlation is therefore already published, structurally, by the merge verb.
  Listing it in the record restates a public fact in a machine-readable place.

**The forward-looking case was checked too, not assumed.** The live writer
(`work.py:_record_submit_commits`) records every commit in `base..branch`, and the submit
cleanliness guard (`work_logic.py:22`) requires a conventional subject but **not** a bead id in
it. So a future entry *can* link a commit whose own message never names the bead — the 100%
figure is a property of today's corpus, not a guarantee. It does not change the decision: both
endpoints remain public, and the merge bubble still names the bead. Under Rule F the same
precondition applies to this field as to prose — a hive that must not publish free text must
not publish its commit linkage either, for the same reason.

**Integrity caveat, not a confidentiality one.** `git.commits` is append-only and never
rewritten (contract: *"accumulate, never overwrite"*), while a bead branch can be rewritten by
`bh work refine` after a changes-requested bounce, or abandoned outright. Both leave recorded
SHAs that will never appear on any public ref. Measured today: **0 of 643** are orphaned. But
the mechanism exists, so a consumer must treat a published SHA as a **link hint, not a
resolvable reference**, and the publish step must not fail — or drop the record — when a SHA
does not resolve.

### Rule I — identity fields publish by shape, not by name

**Any identity-bearing value is published only in display-name / seat form. A value matching an
email-address shape is omitted.** Applies to `assignee`, `created_by`, `comments[].author` and
`owner` alike, and is a predicate a filter evaluates per value:

```text
if value matches ^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$  →  omit the key
```

Measured on this corpus: `owner` is email-shaped in all 1698 records that have it; `created_by`
(2393), `assignee` (428), `dependencies[].created_by` (2453) and `comments[].author` (23) carry
**zero** email-shaped values — they are `Brian Cripe` and agent seats like `dev/dev1`,
`disp/ladder`.

Why by shape rather than "withhold `owner`": in *this* hive the field is harmless — its value
is the same address that authors all 1065 commits already on the public remote, so publishing
it would be zero new exposure. That is precisely the kind of happens-to-be-safe fact .1 refuses
to build on. In a hive where a contributor filed beads but never committed, or where
`created_by` is an email rather than a name, a name-based rule publishes an address that is
*not* already public. The shape predicate holds in both hives; the name-based one does not.

### Rule F — free text: IN, but conditionally, and the condition has teeth

Free text is the largest part of the record and the reason a public bead graph is worth looking
at. Withholding it would leave ids, labels and edges — a snapshot nobody would read, and a
direct contradiction of section 07. So: **`title`, `description`, `design`,
`acceptance_criteria`, `notes`, `close_reason` and `comments[].text` are in the publishable
subset** — under one precondition, and with one named residual risk.

#### What is actually in it (measured, not assumed)

Scanned all 3.44 MB of free text in the export for machine-detectable risk shapes:

|field|records|bytes|private/CGNAT IPv4|home paths|foreign GH orgs|other-hive names|
|---|---|---|---|---|---|---|
|`title`|2394|133 KB|0|0|0|19 in 15 recs|
|`description`|2383|1.60 MB|10 in 6 recs|16 in 14 recs|125 in 68 recs|240 in 119 recs|
|`design`|493|542 KB|3 in 3 recs|3 in 3 recs|10 in 6 recs|43 in 31 recs|
|`acceptance_criteria`|809|370 KB|0|0|0|20 in 18 recs|
|`notes`|336|626 KB|6 in 3 recs|17 in 12 recs|7 in 5 recs|88 in 31 recs|
|`close_reason`|840|81 KB|0|0|0|14 in 9 recs|
|`comments[].text`|17|36 KB|0|0|0|10 in 4 recs|

Credential-shaped strings (`ghp_…`, `sk-…`, `AKIA…`): **0 occurrences.**

The concrete cases behind those numbers:

- **`bh-4o07n`** (a real closed bug) explains a cross-hive sync defect by naming eight other
  hives on this operator's machine — `homelab`, `bc-workspace`, `ah`, `obs`, `bh-infra`,
  `bh-cp`, `agf`, `dxnvh` — quoting a filesystem path under `~/.beadhive/cache/github/…` and a
  project UUID. The export is structurally scoped to one hive; its prose is not.
- **Private network topology.** Free text carries RFC1918 and Tailscale-CGNAT addresses
  (`10.10.10.80`, `10.10.10.130`, `10.10.10.138`, `100.116.151.118`, `100.93.155.55`) from
  multi-host work. These appear **0 times** in `origin/main`'s tracked tree and **0 times** in
  `git log -S` across all history — they exist *only* in bead prose.
- **Scale.** Across the corpus: `briancripe` 173 mentions / 68 beads, `homelab` 85 / 39,
  `bh-infra` 64 / 24, `bc-workspace` 29 / 10, `gastownhall` 26 / 14, `dxnvh` 24 / 9.

#### Rule F1 — the precondition (binding)

**Free-text fields are publishable only from a hive whose bead data is already published on the
same public remote as its code.** Mechanically checkable, one command:

```sh
git ls-remote <origin> refs/dolt/data   # must return a ref, on a remote that is public
```

For this hive it does: `d3b4e84…  refs/dolt/data` on `git@github.com:beadhive/beadhive.git`.
A hive that fails the check publishes the **structured** subset only — ids, enums, timestamps,
labels, edges — and omits every field marked "Rule F" plus `metadata.git.commits`. Fail closed:
an unreachable or ambiguous remote is a failure, not a pass.

This is the epic's own premise made into a predicate rather than an assumption. It is what
stops the scope creep the epic exists to stop — a publish path widening from one hive whose
prose is already public to hives whose prose is not — at the field level, where the widening
would actually do damage.

#### Why not a scrubber

The obvious alternative is to publish free text through a content filter that strips or drops
matching records. Rejected, on this ADR's own evidence: `--scrub` (section .1) dropped 7 real
engineering beads as "pollution" on a text heuristic, silently, with no signal to the reader.
The scan above reproduces the same failure one level down — 125 of the "foreign GitHub org"
hits are benign public upstreams (`dolthub`, `steveyegge`, `BloopAI`), and `claude-plugin`,
`beadhive-ui` and `homebrew-tap` are this operator's own public repos. A pattern filter tuned
to catch the 19 genuine private-network occurrences would silently mangle hundreds of correct
sentences. **A snapshot that is quietly wrong is worse than one that is complete or absent.**

#### Residual risk (accepted, named, with a recommendation)

Rule F1 governs *whether* a hive's prose publishes. It does not govern *what* an author put in
that prose. That residual is real and is **accepted rather than solved here**, because it is a
content-authoring concern, not a schema one — no field-subset decision can fix a sentence.
Specifically, for a hive that passes F1:

1. Bead prose can name, characterise, and expose the network topology of **other** hives,
   including private ones, even though the export is structurally single-hive (bh-7jm7v.3
   closes the *structural* path to other hives; it cannot close the prose one).
2. "Already public" is not "already discoverable". This hive's prose is public only via
   `refs/dolt/data` — a ref a normal `git clone` does not fetch and no search engine indexes.
   A published snapshot makes the same bytes indexed and greppable. That is a genuine change in
   exposure even though it is not a change in *permission*, and it should not be argued away.

**Recommended, not decided here** (each needs its own bead; none blocks this decision):

- An authoring norm — free-text fields describe *this* hive's work; refer to other hives by
  role ("another hive on the same host"), not by name, and never record host addresses or
  operator paths in a bead.
- A pre-publish **report**, never a silent filter: emit the counts in the table above per
  snapshot, hard-fail only on credential shapes (measured 0 today, so the gate is free to adopt
  and any future hit is a true regression), and require an explicit acknowledgement for
  private/CGNAT IPv4 and home-path hits rather than dropping them.
- `labels` is the one *structured* field an author can put arbitrary text into. Today's 153
  values are a clean controlled vocabulary and the `org:` / `repo:` / `provider:` triple is
  uniformly `beadhive` / `beadhive` / `github`, so nothing is withheld — but a label carrying a
  private org or repo name would publish under this decision, and the norm above should cover
  labels too.

### Enforceable form

The tables above are normative. This filter is a **verification artifact**, not the publish
step — it exists to prove the tables are unambiguous enough to implement mechanically, and it
was run against the real export to produce the numbers in the next section.

```jq
# Reference enforcement of the bh-7jm7v.2 allow-list, over `bd export`'s JSONL.
# Usage: jq -s -c -f filter.jq issues.jsonl
def email: test("^[^@[:space:]]+@[^@[:space:]]+\\.[A-Za-z]{2,}$");   # Rule I
def ident: if type == "string" and email then empty else . end;
. as $all
| ([$all[].id] | map({(.): true}) | add) as $ids
| $all
| map(
    { id, title, description, design, acceptance_criteria, notes, close_reason,
      status, priority, issue_type, labels,
      created_at, updated_at, started_at, closed_at, defer_until, mol_type }
    + (.assignee   // null | if . == null then {} else (ident | {assignee: .}) end)
    + (.created_by // null | if . == null then {} else (ident | {created_by: .}) end)
    + (if .dependencies
       then {dependencies: (.dependencies | map({issue_id, depends_on_id, type}))}
       else {} end)
    + (if .comments
       then {comments: (.comments | map({ id, issue_id, created_at, text }
              + (.author // null | if . == null then {} else (ident | {author: .}) end)))}
       else {} end)
    + (if (.metadata // {})["git.commits"]
       then {metadata: {"git.commits": .metadata["git.commits"]}}
       else {} end)
    + (if (.external_ref // null) != null and ($ids[.external_ref] // false)
       then {external_ref} else {} end)
    | with_entries(select(.value != null))
  )
| .[]
```

The construction is what makes it fail closed: every emitted key is **named**, so a key `bd`
adds tomorrow is absent from the output until someone amends the table (P3). A filter written
as a *deny*-list would publish it silently. Under Rule F1 a hive that fails the precondition
runs the same filter with the seven Rule-F keys and `metadata` struck from the object
constructor.

### Verification — run against this hive's real data

Source: `bh bd export -o issues.jsonl` (.1's exact invocation) from this bead's worktree
against the live Dolt-backed store, 2026-08-09. **2394 records, 4.86 MB.** Read-only
throughout; nothing was mutated and no filter was installed anywhere.

**1. Field census** — 31 distinct top-level keys, 6 per `dependencies[]` element (2453 edges),
5 per `comments[]` element (23 comments / 17 records), 1 distinct `metadata` key (460 records,
all `git.commits`). Every key in the tables above came from this census, not from a schema doc.

**2. Filter applied** — `jq -s -c -f filter.jq issues.jsonl` → **2394 records, 4.41 MB**
(-9.3%). Assertions, all confirmed on the output:

|assertion|result|
|---|---|
|record count unchanged (this is a field filter, not a record filter)|2394 → 2394|
|`owner`, `await_type`, `lease_expires_at`, `heartbeat_at`, `_type` present|**0 records each**|
|`dependency_count`, `dependent_count`, `comment_count` present|**0 records each**|
|surviving top-level keys|**23**|
|`dependencies[]` element keys|`issue_id`, `depends_on_id`, `type` only|
|`metadata` keys|`git.commits` only|
|email-shaped values anywhere in the output (Rule I)|**0**|
|`external_ref` survivors|**5** — the `homelab:hl-hkd.4` cross-hive pointer dropped|

**3. `git.commits` claims** — 966 (bead, SHA) pairs / 643 distinct; 0 non-40-char; 643/643
resolve as objects in this repo; 636 reachable from `origin/main`; 7 on local `main` unpushed;
**0 orphaned**. 966/966 pairs have the bead id verbatim in the commit message, and 192 commits
on `origin/main` carry a `chore(merge): bead <id>` subject written by the merge verb itself.

**4. Derived-count semantics** — `dependency_count` equals outgoing `blocks` edges in
**2394/2394** records and equals all outgoing edges in only 1001/2394; `dependent_count` equals
graph-wide inbound `blocks` edges in **2394/2394**. Dependency targets dangling outside the
export: **0 of 2453**, so both are exactly reconstructable from the published edge set.

**5. Rule F1 predicate** — `git ls-remote origin refs/dolt/data` on
`git@github.com:beadhive/beadhive.git` returns `d3b4e84…`; this hive passes, so its free text
publishes.

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
