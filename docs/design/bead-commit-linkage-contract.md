# Bead↔commit linkage contract — `git.commits` (bh-1b0rc.1)

> Status: **decided.** This is the storage contract for durable bead-to-commit linkage: the
> exact metadata key, the value shape, and the write semantics every producer must follow.
>
> Fix the shape before anything writes it. It is cheap to decide now and expensive to change
> once a backfill has written it across ~1,262 issues.
>
> **Why this epic exists:** commit-message correlation alone is not sufficient. See
> [`bead-mention-correlation-yield-adr.md`](bead-mention-correlation-yield-adr.md) for the
> decision and
> [`../spikes/bh-rwryq.3-correlation-yield.md`](../spikes/bh-rwryq.3-correlation-yield.md) for the
> measurement. The short version: the *matcher* is sound (0.0% post-tightening false
> positives in all three hives), but *coverage* fails in one hive of three for a reason no
> matcher can fix — history that predates that hive adopting beads. Durable linkage recorded at
> the moment work lands is the fix for that residual; regex-over-commit-messages demotes to a
> one-time bootstrap.

## The key

A single **flat** metadata key literally named:

```text
git.commits
```

The dot is **part of the key's literal string name**. This is *not* a nested JSON path — it does
**not** mean `metadata` → `git` → `commits`. The stored shape is:

```json
{ "git.commits": "[\"<sha>\", \"<sha>\"]" }
```

### Never write the nested shape

```json
{ "git": { "commits": ["<sha>"] } }
```

That shape is **incompatible and non-canonical** for this purpose and must not appear anywhere in
this codebase for bead↔commit linkage. It is not a stylistic preference: the flat and nested forms
are two genuinely different, *colliding* representations that `bd` will happily hold side by side
with no relationship between them.

### Why flat — the empirical `bd` behavior

Verified directly against a throwaway `bd` store (bd 1.1.0), not assumed. The two `bd update`
metadata flags do materially different things:

|invocation|resulting `metadata`|
|---|---|
|`bd update <id> --set-metadata 'git.commits=foo'`|`{"git.commits": "foo"}` — flat key, value stored **verbatim as a string**, never JSON-parsed|
|`bd update <id> --metadata '{"git":{"commits":[…]}}'`|`{"git": {"commits": […]}}` — a **nested** object under top-level key `"git"`|

Three properties of that second (whole-blob) flag decide the question:

1. **It merges shallowly, at the top level only.** Other existing top-level keys survive a blob
   write — a pre-existing `"team": "platform"` and a pre-existing flat `"git.commits"` were both
   still present afterwards.
2. **The `"git"` key itself is replaced wholesale, not deep-merged.** Writing
   `{"git":{"commits":[…]}}` over an existing `{"git":{"commits":[…],"note":"keepme"}}`
   **destroyed `git.note`** — silently. Any future sibling key under `"git"` is therefore
   collateral damage of every linkage write.
3. **The two forms collide rather than alias.** After setting both, `metadata` held
   `"git.commits": "…"` (flat) *and* `"git": {"commits": […]}` (nested) simultaneously, and
   unrelated. Reproduced directly; there is no reconciliation between them.

`bd update --set-metadata` / `--unset-metadata` is the only practical CLI surface for a **human**
to correct one field by hand. Composing a full `--metadata` JSON blob by hand requires reading the
current value first regardless, and — per property 2 — risks clobbering sibling keys under
`"git"` if any ever exist. The operator correction path (bh-1b0rc.4) is scoped to exactly
`--set-metadata` / `--unset-metadata`.

So the flat key wins: it is what `--set-metadata` / `--unset-metadata` actually operate on
cleanly, with no nested-vs-flat collision risk. `--unset-metadata 'git.commits'` removes the flat
key and leaves any unrelated top-level keys untouched (also verified).

## The value

A **JSON array of full 40-character commit SHAs**, itself **serialized as a JSON string**.

```json
{ "git.commits": "[\"9f8a1c…40 chars\", \"2b7e04…40 chars\"]" }
```

- **Full SHAs, never abbreviated.** Abbreviations collide as history grows, and the storage cost
  is irrelevant at this volume.
- **Serialized as a string** because `--set-metadata`'s value is *always* a raw string — there is
  no way to hand it a native JSON array (property 1 of the table above). This is a constraint of
  the write surface, not a preference.
- **Consumers must `json.loads()` the value.**
- **A missing key, or a value that fails to parse, is an empty list (`[]`) — never an error.**
  Treating unparseable linkage as fatal would let one malformed bead break a whole overlay render
  or backfill run.
- **Order is append-only, oldest-observed-first.** New SHAs go on the end. Do not reorder, and do
  not dedupe-and-resort existing entries.

## Accumulate, never overwrite

A bead can be closed by more than one commit (a fix, plus a follow-up, plus a merge). Every writer
— `bh work submit`, `bh work merge`, the backfill — follows this algorithm:

1. **Read** the bead's current `metadata["git.commits"]` (via `bh bd show <id> --json`),
   JSON-parse it, defaulting to `[]` if absent or unparseable.
2. **Diff** — compute which of the new SHA(s) are not already present.
3. **If none are new, skip the write entirely.** Do not call `bd update` at all.
4. **Otherwise append only the new SHA(s)** — preserving existing order, new ones at the end —
   JSON-encode the full list, and write it back:

   ```sh
   bd update <id> --set-metadata 'git.commits=<json>'
   ```

### The idempotency rule

**Writing an already-present SHA is a no-op, not a duplicate entry.** This is what makes both the
live write and the backfill safe to re-run, and it is what lets bh-1b0rc.3 claim "re-running
produces zero diffs" as a property of the writer's own construction rather than an aspiration.

Step 3 — skipping the `bd update` call outright — is load-bearing for that guarantee and is
**not** merely an optimization:

> **Note on `updated_at`.** It would be reasonable to assume a no-op `--set-metadata` still bumps
> the bead's `updated_at`, making every re-run look like a diff. Measured against bd 1.1.0, it
> does **not**: re-writing a byte-identical value left `updated_at` frozen, while a changed value
> moved it (controlled for, with a sleep between writes, so the two cases are distinguishable).
> `bd` detects the unchanged write itself.
>
> Step 3 stays mandatory anyway. That no-op detection is an undocumented implementation detail of
> one `bd` version, not part of its contract, and the zero-diff guarantee should not rest on it.
> Skipping the call makes the guarantee true by construction at our layer, and avoids a pointless
> store round-trip per bead on every backfill re-run.

### Known limitation: read-then-write, not compare-and-swap

Steps 1–4 are a read-then-write. Two writers racing on the *same* bead's `git.commits` could lose
an append. This is accepted rather than solved, because concurrent writers to one bead's linkage
are not expected: `submit` and `merge` are actor-scoped to the bead's own owner, and merges are
already serialized by the hive's single merge slot.

## Not gated on the bv plugin flag

**This write is deliberately NOT gated on the bv integration flag.** An earlier revision of the
roadmap coupled the two; that coupling is dropped.

`bv` structurally cannot consume this correlation, on two independent grounds established by the
correlation-yield spike:

1. Its **co-commit method reads a beads JSONL that git never tracks** — `bd`/Dolt does not commit
   it, so the input bv would correlate against is simply absent from the repo.
2. Its **`ExplicitMatcher` patterns all require a numeric ID suffix** (`PROJECT-123`-style), which
   beadhive's base36-ish `bh-xxxxx` IDs never have.

Gating a `bd` metadata write behind a `bv` feature toggle would therefore make it wait on a tool
that structurally cannot read its own output. bv plugin registration proceeds on its own schedule
(bh-60s.1), and bh-1b0rc.5 carries the upstream ask for a pattern-flag fix — **nothing in this
epic blocks on that ask landing.**

## Written by / consumed by

|bead|role|
|---|---|
|bh-1b0rc.2|**Writer.** `bh work submit` records the SHA(s) it produced; `bh work merge` records the merge commit onto the bead(s) it merged. A metadata write failure is surfaced as a warning, never fatal — linkage must not block work landing.|
|bh-1b0rc.3|**Writer.** The idempotent full-history backfill, for beads that closed before the write verbs shipped. Consumes the canonical matcher in [`../../scripts/bead_commit_correlation.py`](../../scripts/bead_commit_correlation.py) rather than reimplementing a pattern.|
|bh-1b0rc.4|**Operator path.** Manual correction — see the section below.|

Future consumers (a bv correlation method, an overlay UI) read the key per **The value** above:
`json.loads()`, and treat missing/unparseable as `[]`.

## Operator correction path

See bh-1b0rc.4.

## References

- [`bead-mention-correlation-yield-adr.md`](bead-mention-correlation-yield-adr.md) — the NO-GO
  decision on commit-message correlation as an unconditional basis, and why durable linkage is the
  prerequisite for the blanket case.
- [`../spikes/bh-rwryq.3-correlation-yield.md`](../spikes/bh-rwryq.3-correlation-yield.md) — the
  evidence record: per-hive yield numbers, the false-positive token list, and the canonical
  matcher pattern.
- [`../../scripts/bead_commit_correlation.py`](../../scripts/bead_commit_correlation.py) — the
  canonical matcher (`extract_candidates()` / `resolve_candidates()`), consumed by the backfill.
