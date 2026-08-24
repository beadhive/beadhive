# Beadhive stream operator entities v1 contract

**Status:** normative addendum to
[`beadhive-stream-v1-contract.md`](beadhive-stream-v1-contract.md)  
**Machine-readable shape:**
[`../schemas/beadhive-stream-operator-entities-v1.schema.json`](../schemas/beadhive-stream-operator-entities-v1.schema.json)

This addendum defines the four operator-semantic entities carried beside `StreamIssue`.
It is intentionally backend-neutral: provenance tells the polling adapter how to project bd
data, but neither the records nor their consumers expose bd commands or storage details.

## 1. Frame integration

Every `snapshot` carries all four arrays, even when they are empty:

```json
{
  "work_dependencies": [],
  "gate_requests": [],
  "epic_schedules": [],
  "assignments": []
}
```

Every `delta` carries all eight arrays, even when they are empty:

```json
{
  "work_dependencies_changed": [],
  "work_dependencies_removed": [],
  "gate_requests_changed": [],
  "gate_requests_removed": [],
  "epic_schedules_changed": [],
  "epic_schedules_removed": [],
  "assignments_changed": [],
  "assignments_removed": []
}
```

`*_changed` contains full replacement records, not field patches. `*_removed` contains only
stable entity IDs. All record arrays and removal arrays sort ascending by entity `id`; IDs
are unique within each array.

A `resync` carries none of these arrays. As required by the base contract, it is immediately
followed by a full snapshot that rebuilds all issue and operator state together.

The extension does not create a second clock. Operator records contain neither `revision` nor
`as_of`: the one enclosing frame `revision` and `as_of` apply atomically to `issues` and all
operator arrays. The provider's content revision includes every operator array plus the base
issue and partial-state fields. A change to any of them changes the enclosing revision.

## 2. Stable IDs

Consumers treat every ID as opaque. Providers derive IDs identically across implementations:

1. Encode the record's natural-key tuple as a compact JSON array: UTF-8, no whitespace,
   non-ASCII characters unescaped except where JSON requires escaping.
2. Compute the lowercase hexadecimal SHA-256 digest of those bytes.
3. Prepend the entity prefix below.

| Entity | Prefix | Natural-key JSON array |
| --- | --- | --- |
| `WorkDependency` | `work-dependency:sha256:` | `[hive, issue_id, depends_on_id, type]` |
| `Assignment` | `assignment:sha256:` | `[hive, issue_id]` |
| `GateRequest` | `gate-request:sha256:` | `[hive, gate_id]` |
| `EpicSchedule` | `epic-schedule:sha256:` | `[hive, epic_id]` |

The full ID is the prefix followed by exactly 64 lowercase hexadecimal characters.

## 3. `WorkDependency`

Exact record shape:

```json
{
  "id": "work-dependency:sha256:<digest>",
  "hive": "beadhive",
  "issue_id": "bh-child",
  "depends_on_id": "bh-prerequisite",
  "type": "blocks",
  "created_at": "2026-08-24T12:00:00Z",
  "created_by": "operator@example.invalid"
}
```

Provenance and rules:

- One entity is projected for each direct dependency object on an exported issue.
- `hive` is the registry-owned repository slug already used by `StreamIssue.hive` for the
  scope-qualified source issue (for example, `beadhive`). It is not an owner/repository path
  such as `github/beadhive/beadhive`.
- `issue_id`, `depends_on_id`, and `type` map directly from the dependency object.
- `created_at` and `created_by` map directly when supplied; either is `null` when the backend
  does not expose it.
- Open-ended dependency metadata is not projected.
- The provider obtains dependency-capable export-shaped reads. It must not reconstruct these
  records with list plus per-issue or batched-show fan-out.
- A deleted dependency appears in `work_dependencies_removed`; changes to non-key fields
  appear as a full record in `work_dependencies_changed`.
- If dependency-capable export is unavailable, the frame remains usable but is partial; the
  adapter does not silently claim an authoritative empty dependency collection.

## 4. `Assignment`

Exact record shape:

```json
{
  "id": "assignment:sha256:<digest>",
  "hive": "beadhive",
  "issue_id": "bh-child",
  "seat": "dev/operator-entity-contract"
}
```

One entity exists for each issue with a non-empty assignee. `seat` is the assignee value
verbatim: the provider does not parse it, infer a role, or enforce a naming convention.
Changing an assignee emits the same stable ID in `assignments_changed`; clearing it emits the
ID in `assignments_removed`.

## 5. `GateRequest`

Exact record shape:

```json
{
  "id": "gate-request:sha256:<digest>",
  "hive": "beadhive",
  "gate_id": "bh-gate",
  "blocks": ["bh-target"],
  "gate_type": "human",
  "gate_kind": "review",
  "status": "open",
  "reason": "bh:review abc1234",
  "opened_at": "2026-08-24T12:00:00Z",
  "resolved_at": null
}
```

### 5.1 Provenance

- The provider performs one scope-level gate listing, including open and resolved gates, at
  the refresh's single `as_of` instant. It does not fan out one gate read per issue or hive.
- A source issue is a gate when its raw `issue_type` is `gate`.
- `gate_id` is the source issue ID; `gate_type` is its raw `await_type` value, or `null` when
  that value is absent.
- `opened_at` is the source `created_at`; `resolved_at` is `closed_at` and may be `null`.
- `reason` prefers the raw reason field. If absent, it is the text following the first
  case-insensitive `Reason:` marker in the description. If neither exists it is `""`.
- `status` is `open` only when the raw status is `open`; every other raw status is
  `resolved`.
- `blocks` is the lexically sorted set of reverse-derived targets whose dependency edge is
  `type == "blocks"` and `depends_on_id == gate_id`. It may be empty: gate creation can exist
  even when a backend refuses that edge.

The raw aggregate gate rows available today do not carry trustworthy source-repository
identity. Hive identity is therefore resolved fail-closed:

1. A hive-scoped read uses its already-resolved registry repo slug.
2. At hub or factory scope, collect the distinct `hive` values from the reverse
   `WorkDependency` records used to derive `blocks`. Exactly one value identifies the gate.
3. An aggregate gate with zero or conflicting candidate hives is omitted, while the enclosing
   frame remains usable with `partial: true` and
   `partial_reason: "gate_hive_identity_unavailable"`.

The provider never infers a hive from a gate ID or description. Consequently `blocks: []` is
valid only when hive identity is independently known (for example, a hive-scoped read). An
unlinked aggregate gate is omitted and marked partial, not serialized with a guessed `hive`.

### 5.2 `gate_kind` classification

The provider calls the shared `work_logic._gate_kind` classifier with the complete gate row;
it does not reimplement marker recognition. Its result maps to the public enum as follows:

1. `review` remains `review`. The current marker is exactly `bh:review <sha>` after a
   case-insensitive
   `Reason:` prefix, where `<sha>` is 7–40 hexadecimal characters. The legacy bare
   `review <sha>` marker remains recognized for existing gates.
2. `security` remains `security`.
3. `kickoff` remains `kickoff`.
4. `release-hold` maps explicitly to `other` in v1.
5. `other` remains `other`.

Unknown gate forms are never dropped merely because their kind is unknown, and are never
mislabeled as review/security/kickoff. The hive-identity rule above is the separate condition
that can make an aggregate gate unprojectable.

### 5.3 Resolved-gate retention

An open gate is always present. A resolved gate is present exactly while
`as_of - 24 hours <= resolved_at <= as_of`. At the first refresh after it ages past that
window, its ID appears in `gate_requests_removed`. This bounded retention makes recently
resolved operator actions observable without keeping an unbounded gate history.

A resolved gate without a parseable `resolved_at` is omitted and the enclosing frame is
partial with a reason such as `gate_resolution_timestamp_unavailable`. It is not treated as
open and is not retained forever.

## 6. `EpicSchedule`

Exact record shape:

```json
{
  "id": "epic-schedule:sha256:<digest>",
  "hive": "beadhive",
  "epic_id": "bh-epic",
  "groups": [
    {
      "kind": "planner",
      "batch": "frontend",
      "issue_ids": ["bh-ui-a", "bh-ui-b"]
    },
    {
      "kind": "chain",
      "batch": null,
      "issue_ids": ["bh-model", "bh-adapter"]
    }
  ],
  "singletons": ["bh-docs"],
  "coordinators": ["bh-child-epic"]
}
```

### 6.1 Candidates and scheduler authority

One `EpicSchedule` exists for every epic in scope, including a closed epic. Candidate children
are direct parent-child members whose status is not `closed`; `open`, `blocked`, and
`in_progress` children all remain candidates. Closed children are excluded. Child epics sort
into `coordinators` and never into a leaf group or singleton.

The provider calls the pure existing `schedule.plan_schedule` implementation with candidates
sorted by ID for deterministic ties. It passes
`max_size=max(1, len(non_closed_direct_leaves))`: a deterministic, non-restrictive projection
cap owned by this contract, not the operator's current dispatch configuration. It leaves
`force_single_group` false and `merged_groups` unset. It does not call
`work.schedule_payload`, read model or schedule configuration, inspect git merged-group state,
or duplicate the scheduler algorithm. The wire mapping is:

- scheduler `planner` group -> `kind: "planner"`, with `batch` equal to the label after the
  `batch:` prefix;
- scheduler `chain` group -> `kind: "chain"`, with `batch: null` and dependency-order
  members;
- scheduler singletons -> `singletons`;
- scheduler child-epic coordinators -> `coordinators`.

There is no `fanout` group kind: fanout is how singleton work may execute, not a dependency
group. There is no `collapsed` group kind either. Operator-forced collapse, model
availability/routing, release deferrals, and prose scheduling reasons are runtime policy, not
facts derived from bead state, and therefore are not projected.

### 6.2 Ordering and no-batch behavior

Planner groups sort first by `batch`; chain groups follow, sorted by their first member ID.
Planner group members and `singletons` sort lexically. Chain members preserve dependency
order. `coordinators` sort lexically.

Without planner batch labels, independent children are singletons and a private linear
dependency DAG may become a `chain`. An epic with no active children still has an
`EpicSchedule` whose `groups`, `singletons`, and `coordinators` are all empty; the record is
never omitted merely because the schedule has no work.

An epic leaving the stream's scope emits `epic_schedules_removed`. Ordinary candidate,
grouping, or ordering changes emit a full record in `epic_schedules_changed`.

## 7. Base-contract invariants

This addendum preserves all base stream invariants:

- every session starts with a full snapshot, including `--since` sessions;
- the snapshot's `as_of` is the high-water mark for the one coalesced scope refresh;
- unavailable subdomains produce explicit partial state rather than false empty truth;
- resync is followed by a full replacement snapshot;
- stdout contains flushed NDJSON frames only and diagnostics use stderr; and
- the stream command remains the process boundary and cleans up its complete backend process
  tree on normal exit, interruption, backend failure, and broken pipe.

## 8. Required feature demonstration

The bh-5wpb6 feature demo is a deterministic, bounded invocation of the real
`bh stream --format ndjson` command through the bd-backed polling provider; a seeded
throwaway hive is acceptable. The demo must prove all of the following:

1. Every stdout line parses as JSON, and the first line is a full snapshot with all four
   operator arrays.
2. The initial state includes at least one dependency, assignment, classified gate, and an
   epic schedule with no planner batch label.
3. After mutating source state, a later delta has a new enclosing revision and contains at
   least one operator `*_changed` record and one operator `*_removed` ID.
4. The bounded stream terminates cleanly, diagnostics remain on stderr, and no descendant
   backend process survives.
5. The same fixture assertions run against the polling provider's contract surface and a
   test-double provider, so future provider substitution is executable. Test-double-only
   rendering is not a sufficient feature demo.
