# Roles & seats

How the factory's org model works: what a role is versus a seat versus a session, the seats by
plane and their duties, the Gas Town naming layer, and how a seat is launched.

## Role vs seat vs session

- A **role** is the archetype — the definition of a job (its duties, its skill, the tools and
  model it can reach). It is abstract and reusable.
- A **seat** is a role instance bound to an **identity** + permission archetype: a role plus a
  prefixed identity (`disp/<name>` for a dispatcher, `dev/<name>` for a developer), a scoped set of
  permissions derived from `(plane, function, resource scope, decision authority)`, a **worktree**,
  and a **rig**. The same role can be seated many times concurrently — many developers, one per
  bead — each its own seat with its own identity and worktree.
- A **session** is a running loop (agent or human-supervised). **Any** session MAY hold **multiple**
  seats over its life (e.g. controller → director → merger), the way one collapsed loop already
  works many beads. Least-privilege is preserved **per action**: every `bh` action re-stamps the
  acting identity via `--as <seat>/<name>`, so at any instant the session wields exactly one seat's
  permissions, never the union. **Multi-seat session, single-seat per action.**

"Sub-roles" are not name suffixes — a look-alike variant is the same function bound to a different
*resource scope* (which branch / which registry / which config). This is why there are no `-deep`
seats: one role parameterized by scope + capability, not a new name.

## Seats by plane

Each seat owns a functional input → output on one plane and needs scoped permissions to real
resources. The canonical reference is [roles-rbac-matrix.md](../../../../docs/design/roles-rbac-matrix.md).

| Seat | Identity | Plane | Duty |
|---|---|---|---|
| **supervisor** | `super/` | Control | Govern the whole factory: set policy, launch and oversee the other control seats (org root). |
| **director** | `dir/` | Control | Intake + fleet work routing (intake→plan→work); the interface to the per-rig dispatchers. |
| **custodian** | `cust/` | Control | Config + secrets + repo provisioning + resource cleanup — the only control seat touching key material. |
| **controller** | `ctrl/` | Control | Factory telemetry/efficiency — read-mostly throughput, health, and OTEL of the factory itself. |
| **planner** | `plan/` | Planning | Turn a raw idea into a gated molecule (ideate → research → architecture → decompose → file). |
| **analyst** | `analyst/` | Planning | Fire-and-forget read-only research sub-agent for the planner (codebase + web); never implements. |
| **dispatcher** | `disp/` | Integration | Deliver an epic: assign ready beads, provision worktrees, watch gates, signal merges; **collapsed** mode inlines the implementation itself. |
| **developer** | `dev/` | Integration | Take one assigned bead to a validated, reviewable state in its own ephemeral `bead/<id>` worktree, then submit. |
| **reviewer** | `rev/` | Integration | Walk an approved branch — read intent + change, run tests and a demo, resolve or bounce the gate. |
| **merger** | `merge/` | Integration | Serialize approved beads onto the always-green integration branch, `--no-ff`, preserving history. |
| **warden** | `warden/` | Assurance | Cross-cutting **security + policy** gate (secret-scan, SBOM, policy-as-code); read + block, no writes. |

**verifier** (`verify/`, Assurance) is kept as a *lens*, not a seat yet — acceptance/e2e/QA covered
by developer-check + reviewer-demo + CI; promoted to a seat only when e2e needs its own test-env
identity.

### The dispatcher — one seat, scope × mode

A **dispatcher** coordinates a *set* of beads to deliver an epic and lives on **long-lived
branches**; a **developer** implements **one** bead on an **ephemeral** `bead/<id>` branch. The
collapsed epic worker is a *dispatcher* variant, not a developer. The org model, docs, and identity
see **one seat, `dispatcher` (`disp/`)**, with scope + mode as dispatch metadata:

- **mode = fanout vs collapsed.** *Fanout* delegates each bead to a `developer` (the dispatcher
  holds no Edit/Write); *collapsed* inlines the developer work on the shared batch branch (Edit/Write
  on). `work.dispatch.{mode,max_depth}` selects it.
- **`implement` (Edit/Write) and `sub-dispatch` (Task) are hard ceilings** — presence/absence in the
  selected def. The retired `epic-coordinator`, `epic-coordinator-deep`, and `foreman` names all fold
  into *dispatcher @ batch (collapsed)*; the "deep" escape valve is just the `sub-dispatch:1`
  capability. See [dispatch-and-scheduling.md](dispatch-and-scheduling.md).

## Roadmap seats

| Seat | Identity | Plane | Note |
|---|---|---|---|
| **releaser** | `release/` | Release | version + changelog + tag/release |
| **contributor** | `contrib/` | Contribution | name kept — owns the target-repo dossier + provenance scrub + human publish gate; the only seat allowed to publish to an external tracker |
| **operator** | `ops/` | Delivery | gitops reconcile + IaC apply + rollback (inference seat; runner identities out of scope) |

The **`contributor`** is a dedicated, persistent, rig-scoped seat for the Contribution plane —
built on the read-only analyst research primitive but owning a target-repo dossier and driving
gated upstream PRs over external rigs. See the Contribution plane in
[agf-and-planes.md](agf-and-planes.md).

## Alternate seat nicknames — optional, non-normative aliases

A subset of seats carries an alternate nickname from an earlier naming pass. These survive only
as **optional, non-normative aliases** mapped onto the canonical seat names above — never a
canonical seat name themselves. See
[docs/design/gas-frameworks-comparison.md](../../../../../docs/design/gas-frameworks-comparison.md)
for the full alias mapping and where it came from.

## Role modes — launching a seat as the main loop

Any seat can run as the **main** Claude Code loop instead of as a task-spawned sub-agent.
Two equivalent entry points:

- `bh role <seat>` — exports the role, then execs the seat's agent definition.
- `claude --agent bh:<seat>` — resolves the seat definition from the `bh` Claude Code plugin
  (a local `.claude/agents/<seat>.md` override outranks the plugin).

When a seat launches as a role mode, its definition body becomes the system prompt, its
frontmatter preloads the role skill (plus `work` for every seat that drives a bead lifecycle), and
its tools / model fields scope what the seat can reach. The control-plane seats do **not** pair with
`work` — they commission and govern the factory rather than driving beads.

## Dispatch vs implement — who does the coding

The distinction between fanout and collapsed dispatch is **who does the coding**:

- A **fanout dispatcher** *dispatches only*: it never implements a bead itself. It fans each ready
  bead out to a developer sub-agent in that bead's own worktree, watches the gate, and serializes
  the merge (it holds no Edit/Write).
- A **collapsed dispatcher** (`dispatcher @ batch`) *implements*: it works every ready bead of one
  epic sequentially in one shared batch worktree, merging the set once via `merge --group` →
  `finish`. The **deep** variant additionally holds `sub-dispatch:1` — an escape valve to kick a
  single risky bead back out to its own worktree.

Which variant runs is decided by seat-typed, depth-bounded dispatch — a leaf bead goes to a
developer, an epic to a dispatcher, and a collapsed epic to a collapsed dispatcher chosen by
`work.dispatch.max_depth`. See [dispatch-and-scheduling.md](dispatch-and-scheduling.md).
