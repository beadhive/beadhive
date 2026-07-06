# AGF factory roles — planes, seats, and the RBAC matrix

> **Status:** design / target-state. This is the canonical reference for AGF role, plane, and seat
> terminology and the least-privilege RBAC model. Reconciliation of the rest of the tree (docs,
> agent defs, source) to this document is tracked as a bead molecule — see the epic
> *"AGF factory roles: plane-aligned rename + resource-scoped RBAC matrix"*.

## Organizing principle

The factory (Beadery, driven by `bdry`/`ws`) runs AGF across **planes**, each staffed with
**inference seats** — agents that own a functional input→output and therefore need scoped
permissions to real resources (git, GitHub/Gitea, bd, CI gates, Head Office registry, keys).

**A seat's permissions derive from `(plane, function, resource scope, decision authority)`.**
"Sub-roles" are not name suffixes — they are the same function bound to a different *resource scope*
(which branch / which registry / which config). This is why there are no `-deep` seats: a
look-alike variant is one role parameterized by scope + capability, not a new name.

Scope note: this document covers **inference seats** only. Non-inference automation identities
(CI/IaC/gitops/CD *runner* service accounts) are out of scope as rows; the technologies a seat
touches are still a matrix column.

---

## 1. The planes

| Plane | Status | Owns (input → output) | Seats |
|---|---|---|---|
| **Control** | operational | workspace → governed + routed + configured + observed factory | supervisor · director · custodian · controller |
| **Planning** | operational | idea → gated molecule (epic + children + dep DAG) | planner · analyst |
| **Integration** | operational | kicked-off molecule → beads landed `--no-ff` on green line | dispatcher · developer · reviewer · merger |
| **Assurance** | proposed (cross-cutting gate layer) | change/release → security + policy verdict | warden (+ verifier as a *lens*) |
| **Release** | roadmap | green line → cut release (version + changelog + tag) | releaser |
| **Contribution** | roadmap | internal change → upstream PR over external rig | contributor |
| **Delivery / Deployment** | roadmap (named now) | release + IaC/gitops desired-state → reconciled system | operator |
| **Feedback / Operations** | speculative | prod telemetry/incidents → new beads into Planning | (unstaffed) |

**Assurance** is a *cross-cutting gate layer*, not a sequential plane — **warden** attaches gates at
pre-merge (Integration), pre-cut (Release), and pre-publish (Contribution). It deliberately breaks
the "one plane, one sequential handoff" tenet, and owns **security + policy only** — the
Contribution provenance scrub + human publish gate stay owned by the `contributor` seat.
**Delivery** *is* a proper sequential plane (`release → deploy → running`); it feeds **Feedback**,
which closes the loop back to Planning.

---

## 2. Seats by plane

### 2.1 Control plane — four seats over four conceptual resources

The control plane governs the *factory itself*. Its conceptual resources have different blast radii,
so they are separable authorities on a 3-level orchestration spine (**supervisor → director →
dispatcher**, where dispatcher lives one plane down in Integration):

| Conceptual resource | Seat | Identity | Decision authority | Flavor alias |
|---|---|---|---|---|
| The whole factory — cross-plane operations, policy, supervises the other control seats | **supervisor** | `super/` | ultimate / root | mayor · overseer |
| Intake + work routing (intake→plan→work) + interface to the per-rig dispatchers | **director** | `dir/` | high — routes/directs work across the fleet | — |
| Config + secrets + repo provisioning + resource cleanup | **custodian** | `cust/` | medium/mechanical — applies, doesn't decide | administrator · caretaker |
| Factory telemetry/efficiency — throughput, health, OTEL of the factory itself | **controller** | `ctrl/` | low — read-mostly, no mutation | the gauge |

**Distinctions:** *supervisor* governs and manages the control seats (org root); *director* is the
operations/traffic layer that routes work and talks to the per-rig *dispatchers* — it directs work,
holds no secrets, sets no policy; *custodian* is the only control seat touching **secret/key
material** (its own blast radius → its own identity) and does the mechanical commissioning;
*controller* only reads. Head Office registry (`~/.ws/config.yaml`) is partitioned: supervisor
writes policy, director writes fleet/`managed_repos` membership, custodian writes rig config,
controller reads.

**Collapse path:** a small/single-rig factory runs just the **supervisor**, absorbing the
director/custodian/controller scopes; split them into their own seats + identities as the factory
grows and the blast radii diverge. The full separation is designed here so the collapse is a
deliberate merge into the supervisor, not an accident.

### 2.2 Integration plane — dispatcher scoped by resource + mode; developer = leaf worker

The axis that matters: a **dispatcher** coordinates a *set* of beads to deliver an epic and lives on
**long-lived branches** (integration line, epic container, batch); a **developer** implements **one**
bead on an **ephemeral `bead/<id>`** branch. The collapsed epic worker is a *dispatcher* variant, not
a developer — matching existing seat-typing (epics resolve to the coordinator identity, bead work to
the worker identity).

**dispatcher** (`disp/`, was `coordinator`/`coord/`) — deliver an epic. One role; variant = **scope**
(which long-lived branch) × **mode** (how beads get done):

| Legacy name | Dispatcher variant | Scope (branch) | Implements? (Edit/Write) | Task |
|---|---|---|---|---|
| coordinator (root) | dispatcher @ main · **fanout** | integration main line | no | yes — fans out to developers |
| nested coordinator | dispatcher @ epic-container · **fanout** | epic container | no | yes |
| epic-coordinator | dispatcher @ batch · **collapsed** | shared batch branch | **yes** (in-line) | no |
| epic-coordinator-deep | dispatcher @ batch · **collapsed + escape** | batch branch | yes | yes (≤1 — kick one bead to a developer) |

- **mode = fanout vs collapsed:** fanout delegates each bead to a `developer` (dispatcher holds no
  Edit/Write); collapsed inlines the developer work on the shared batch branch (Edit/Write on).
  `work.dispatch.{mode,max_depth}` + ready-set shape select it.
- **`implement` (Edit/Write) and `sub-dispatch` (Task) are HARD ceilings** — presence/absence in the
  selected def. A few concrete dispatcher defs exist under the hood; the org model, docs, and
  identity see **one seat, `dispatcher` (`disp/`)**, with scope+mode as dispatch metadata.
- **`epic-coordinator`, `epic-coordinator-deep`, `foreman` are removed as names** — all fold into
  *dispatcher @ batch (collapsed)*; the "deep" escape valve is the `sub-dispatch:1` capability.

**developer** (`dev/`, was `crew/`) — implement one bead. Always ephemeral `wt/bead/<id>` →
`bead/<id>`, short-lived, leaf; never orchestrates. Merge path: `merge <bead>`. (A collapsed
dispatcher merges its set via `merge --group` → `finish`.)

**reviewer** (`rev/`) and **merger** (`merge/`) are unchanged in name.

### 2.3 Planning / Assurance / Release / Contribution / Delivery

| Seat | Identity | Plane | Note |
|---|---|---|---|
| **planner** | `plan/` | Planning | idea → gated molecule |
| **analyst** | `analyst/` | Planning | read-only research sub-agent for the planner |
| **warden** | `warden/` | Assurance | **security + policy only** gate (secret-scan, SBOM, policy-as-code); provenance stays with contributor |
| **verifier** *(lens, not a seat yet)* | `verify/` | Assurance | acceptance/e2e/QA — kept as a lens (developer-check + reviewer-demo + CI); promote to a seat only when e2e needs its own test-env identity |
| **releaser** *(roadmap)* | `release/` | Release | version + changelog + tag/release |
| **contributor** *(roadmap)* | `contrib/` | Contribution | name kept — mirrors upstream `CONTRIBUTORS`; the existing hard publish guard keys on `contrib/`; **owns the provenance scrub + human publish gate** |
| **operator** *(roadmap)* | `ops/` | Delivery | gitops reconcile + IaC apply + rollback (inference seat; runner identities out of scope) |

Gas-Town names (`polecat` / `overseer` / `the Refinery` / `the cartographer` / `the pit crew`) are
demoted to **optional, non-normative aliases** mapped onto the canonical names above.

---

## 3. Seat vs session (RBAC nuance)

A **seat** is an identity + permission archetype; a **session** is a running loop. **Any** session —
agent or human-supervised — MAY hold **multiple seats** (controller → director → merger; the way
collapsed dispatch already lets one loop work many beads). Least-privilege is preserved **per
action**: every `bdry` action re-stamps the acting identity via `--as <seat>/<name>`, so at any
instant the session wields exactly one seat's permissions, never the union. **Multi-seat session,
single-seat per action.**

Two *advisory* limits — guidance, **not framework-enforced**:

- **Cognitive load** — don't stack too many seats in one session; it confuses a less-capable agent.
  The ceiling scales with the session's reasoning capability, so it's advice, not a hard cap.
- **Conflict of interest** — for pairings that can rubber-stamp (developer + reviewer, developer +
  merger, author + approver of any gate) it is *advised* to split across sessions or use a
  human-supervised session. Advised, **not strictly required**; a rig may opt into a hard policy gate
  (the reviewer cross-seat knob) where it wants the guarantee.

  **The knob (`work.dispatch.reviewer_cross_seat`, bead .39):** `ws work approve` compares the
  approving identity against the bead's author *by person* (`dev/alice` and `rev/alice` are the same
  person in two hats). Default **`advise`** — a self-approval is allowed but **warns** (a visible
  notice plus a `reviewer_cross_seat_self_review` log event); **`hard`** — the self-approval is
  **blocked**, so a different seat/person must clear the review gate. It is deliberately advisory by
  default (least surprise); a rig sets `hard` per-rig or globally to enforce split review.

---

## 4. The RBAC matrix

Enforcement: `hard` = a CLI/guard gate (exists or proposed); `soft` = seat-typing + tool/model
scoping in the agent def.

| Seat | Plane | Identity | Resource scope | Decision | Chains from → to | Input → Output | Technologies | Permissions (least-privilege) | Enforcement |
|---|---|---|---|---|---|---|---|---|---|
| **supervisor** | Control | `super/` | whole factory + policy | ultimate/root | human → dir/cust/ctrl | factory state + policy intent → policy, operating decisions, launched control seats | Task, HQ registry (policy), `bdry` top-level | set policy, launch/oversee control seats, write HQ policy; **not** hold product keys, implement, merge, publish | soft (org root) |
| **director** | Control | `dir/` | intake + fleet routing | high | supervisor / intake → planners + dispatchers | intake + fleet state → routing (intake→plan→work), directed work | `bdry rig` (fleet), Task, HQ `managed_repos` | write fleet/`managed_repos`, route/direct work, launch dispatchers; **not** hold secrets, set policy, implement/merge | soft |
| **custodian** | Control | `cust/` | config + keys + provisioning | medium | director → serves all seats | config/commissioning intents → registered repos, applied config, keys, cleanup | `bdry config\|labels\|sync`, gh/gitea repo create, key store, git worktree prune | create/register repos, write config, manage **secrets**, cleanup; **not** route work, set policy, implement/merge | soft (secret isolation) |
| **controller** | Control | `ctrl/` | factory telemetry | low/read | observes all | factory events/metrics → reports/dashboards | OTEL/Grafana (factory self-obs) | **read-only** telemetry + write dashboards; no lifecycle mutation | hard (read-only tooling) |
| **planner** | Planning | `plan/` | molecule (beads+deps) | high (decomposition) | human (+analyst) → dispatcher via `kickoff=approved` | idea + RO codebase → molecule + gate flips | `bdry bd create\|dep\|label`, `bdry plan file\|approve` | create/decompose beads, open+resolve kickoff gate; **not** implement/merge/dispatch | soft |
| **analyst** | Planning | `analyst/` | none (read-only) | advisory | planner → planner | question → findings (text) | Grep/Glob/Read, Web, context7 | **read code + web only** | hard (tools omit Edit/Write/Task) |
| **dispatcher** | Integration | `disp/` | long-lived branch: main / epic-container / batch | medium | planner → developers/merger | ready beads → assignments + provisioned worktrees + merge signals; collapsed mode inlines implementation | `bdry work assign\|start`, Task (fanout/deep), `bd ready\|show`, git worktree, Edit/Write (collapsed only) | assign (orchestrator-only), provision, read gates, implement **only in collapsed**, sub-dispatch **≤1** in deep; **not** merge | soft `assign` (→propose hard); implement/Task ceilings hard per def |
| **developer** | Integration | `dev/` | one **ephemeral** `bead/<id>` | low | dispatcher → reviewer → merger | one assigned bead → validated branch + `review:pending` | git worktree/commit/SSH-sign, Edit/Write, `validate_cmd`/CI, `bdry work claim\|submit\|resume` | write **within its one bead branch**, claim own work, submit; **not** orchestrate/merge/assign/publish | claim wrong-seat/owner = hard |
| **reviewer** | Integration | `rev/` | a submitted branch | medium (verdict) | developer (submit) → dispatcher/merger | branch + acceptance criteria → gate verdict | git RO checkout, test/demo, `bd` gate | resolve gate; cross-seat **advised** (rubber-stamp guard), policy-configurable to hard else human-supervised; **not** implement/merge | advisory default, optional hard policy per rig |
| **merger** | Integration | `merge/` | integration line + slot | medium | reviewer approval → green line | approved bead/branch → `--no-ff` merge + closed bead | git merge/push, merge-slot lock, `bd close` | write integration branch, hold slot, close; **not** implement/dispatch/publish | soft |
| **warden** | Assurance | `warden/` | a change/release under gate | high (block) | pre-merge / pre-cut / pre-publish → verdict back | diff/release + policy → **security + policy** verdict, findings | git RO, secret-scan, SBOM, policy-as-code | **read + block**; no writes; provenance **not** in scope | hard gate (proposed), `security:*` parallel to review |
| **verifier** *(lens)* | Assurance | `verify/` | a branch/release | medium | developer → merger/releaser | acceptance/e2e suite → acceptance evidence | e2e harness/CI | run tests, publish evidence; no code writes | soft (kept as a lens) |
| **releaser** *(roadmap)* | Release | `release/` | green line → tag | high | merger → Delivery | green line → cut release (version/changelog/tag) | git tag, `cz`, gh/gitea releases | create tags/releases, write changelog; **not** deploy/implement | hard release gate (proposed) |
| **contributor** *(roadmap)* | Contribution | `contrib/` | external rig + dossier | high (publish) | planner (external assignment) → upstream repo | internal change + dossier → upstream PR after provenance scrub | gh/gitea fork+PR, provenance scrub | **only seat allowed to publish to an external tracker**, behind a human-only publish gate | **hard (already exists)** |
| **operator** *(roadmap)* | Delivery | `ops/` | env desired-state | high (prod) | releaser → running system | cut release + IaC/gitops manifests → reconciled deployment | gitops (Argo/Flux), IaC (Terraform), CD | apply to target env, rollback; **not** implement/merge | hard env gate (proposed) |

---

## 5. Retired / renamed names

| Retired | Becomes |
|---|---|
| `superintendent` | Control-plane split → supervisor / director / custodian / controller |
| `coordinator` (`coord/`) | `dispatcher` (`disp/`) |
| `epic-coordinator` | dispatcher @ batch (collapsed) |
| `epic-coordinator-deep` | dispatcher @ batch (collapsed + `sub-dispatch:1`) |
| `foreman` | dispatcher @ batch (collapsed) |
| `crew/` prefix | `dev/` |

Gas-Town names survive only as optional aliases.

---

## 6. Missing seats & planes (future work)

- **Delivery / Deployment plane** (roadmap, named) — **operator**; the real "AGF stops at merge" gap
  (release → deploy → running via gitops + IaC). Runner identities out of scope for now.
- **Feedback / Operations plane** — speculative; prod telemetry/incidents → new beads back into
  Planning, closing the loop.
- **verifier** — a lens today (developer-check + reviewer-demo + CI); becomes a seat only when e2e
  needs its own test-env identity, likely alongside operator/Delivery.
- **scribe** (docs/changelog) — optional, low priority.
