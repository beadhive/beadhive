# Complexity-first routing

Beadhive records the capability a bead requires independently from the model that happens to run
it. The stable contract is the ordered tier `SIMPLE < MEDIUM < COMPLEX < REASONING`; provider
catalogues, subscription entitlements, and harness aliases are resolved later, at dispatch time.

## Labels: capability, preference, and effort

Every routable epic, feature, task, bug, and chore carries exactly one canonical
`complexity:<TIER>` label. Gates, events, and internal molecule artifacts are excluded. A planner
may declare `complexity: REASONING` explicitly; otherwise `bh plan check` and `bh plan file`
classify stable type, title, description, design, and acceptance text before validation. UNKNOWN
text receives the required fallback `MEDIUM`, with fallback provenance shown in the plan report.

`model:<provider/model-name>` is optional and singular. It is only a preferred concrete model; it
never replaces the complexity requirement and it does not freeze the model catalogue. In loose
policy an unavailable preference may fall back with a warning. In strict policy it blocks launch
with remediation.

`size:<xs|s|m|l|xl>` answers a different question: expected implementation effort and automatic
collapse cost. A tiny task may require REASONING, and a large mechanical migration may be SIMPLE.
Scheduling groups by size budget but selects model capability from the maximum complexity tier.

## Routing configuration

```yaml
work:
  routing:
    policy: loose
    tiers:
      - model: openai/gpt-5-mini
        ceiling: MEDIUM
      - model: anthropic/claude-opus-4-1
        floor: COMPLEX
        endpoint: primary-gateway
```

Each entry contains `model` plus optional `floor`, `ceiling`, and `endpoint` only. Models use the
canonical `provider/model-name` form. Bounds are inclusive: an omitted floor means SIMPLE and an
omitted ceiling means REASONING. `endpoint` may be an HTTP(S) OpenAI-compatible/Bifrost endpoint
or a configured endpoint profile. Omitting it means the relevant developer or dispatcher harness
default; it does not imply a particular provider.

Credentials and TLS configuration live with the endpoint or harness account, never in a tier
entry. Legacy routing keys named `provider`, `launch_model`, or `access` are rejected.

## Availability and policy

For an endpoint route, Beadhive queries its `/v1/models` catalogue with a five-second timeout and
caches results for five minutes. A refresh failure may use a stale cache and reports that source.
Without a cache, configured routes remain weak explicit evidence; Beadhive does not claim that a
URL proves entitlement. An authoritative empty catalogue means no models are available.

For an omitted endpoint, the role/harness adapter uses its default account or subscription. Some
subscription CLIs cannot enumerate entitlements. In that case Beadhive trusts explicit configured
routes and labels the evidence `explicit_configuration`; operators should treat that as intent,
not proof. Availability is scoped by role and harness, so dispatcher access does not imply
developer access.

With no model preference, both policies choose the least-overpowered available model covering the
required tier. With a preference:

- `loose` warns and falls back by complexity when the preference is unavailable or out of range;
- `strict` refuses an unavailable or out-of-range preference, conflicting group preferences, or
  a missing capable route;
- a loose out-of-range fallback distinguishes overqualification/cost (required tier below the
  model floor) from underqualification/risk (required tier above its ceiling).

Every group, singleton, and nested coordinator decision reports `complexity`, `preferred_model`,
`selected_model`, `selection_reason`, `policy`, `availability_source`, `endpoint`, and `warnings`.
Blocked decisions also report `blocked` and `remediation`. The pre-existing `model` schedule field
temporarily aliases `selected_model`; downstream consumers should migrate to `selected_model`.
No public `launch_model` exists. Canonical identity remains in JSON, MCP, runtime envelopes, and
telemetry; a harness-specific token is produced only while constructing the final process argv.

## Migration and recovery

Always create and inspect a plan before changing a hive:

```bash
bh backfill-complexity --dry-run --plan complexity-plan.json --json
bh backfill-complexity --apply --plan complexity-plan.json \
  --pre-state complexity-pre-state.jsonl --audit complexity-audit.json
```

The plan is corpus- and content-hashed. Apply refuses drift, exports the complete pre-state before
the first mutation, preserves every `model:` label, and verifies exactly one valid complexity
label on every routable record afterward. A caught failure or interruption rolls mutations back.
The audit is written atomically as `state=applying` before mutation and checkpoints the current
and possibly uncertain bead around every write, so a hard termination has an explicit recovery
point. Retain the plan, JSONL pre-state, and audit together until verification succeeds.

After apply, require `state=applied`, no post-apply complexity errors, model preservation true,
and `second_dry_run_changes=0`. If the process died outside its rollback handler, stop writers,
inspect `recovery.uncertain_bead` and `next_index`, restore affected complexity labels from the
JSONL export, then rerun the same dry-run. Do not manufacture a new plan until the original
corpus is reconciled.

## Scorer status and operational ownership

The bundled Python scorer is a best-effort compatibility bridge, not a promise of permanent
reimplementation. Its Bifrost source pin, Apache-2.0 attribution, deviations, parity boundary,
and replacement choices are recorded in
[the upstream note](upstream/bifrost-complexity-scorer.md). Long term, prefer importing the
upstream Go package, compiling or deliberately forking a helper behind the classifier interface,
or calling a future gateway classification endpoint.

Planner and dispatcher skill guidance is maintained separately in the
[beadhive/claude-plugin](https://github.com/beadhive/claude-plugin) repository. Core behavior and
JSON/MCP compatibility remain owned and tested here; plugin prose must not become a second routing
implementation.
