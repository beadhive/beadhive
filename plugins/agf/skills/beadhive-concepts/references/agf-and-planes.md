# AGF & the operational planes

**Beadhive is the factory that runs AGF on beads.** AGF (Agentic Git-Flow) is the methodology —
abstract and tracker-independent; Beadhive is the concrete factory that executes it, with `bh`
as its command and beads as its unit of work. This file states that framing once and then walks
the tenets and the planes.

## The tenets

1. **Operational planes, kept separate.** *Control* governs and configures the factory. *Planning*
   turns ideas into molecules. *Integration* executes them. Each operational plane has its own verb
   surface and seats; they hand off sequentially and never step into each other's role. **Assurance**
   is the exception — a *cross-cutting gate layer* (warden) that attaches at pre-merge, pre-cut, and
   pre-publish rather than running as one sequential handoff.
2. **Integration vs release.** *Integration* is high-frequency and dirty: each bead gets a
   worktree off the integration tip and lands on an **always-green** line. *Release* is a
   separate, deliberate, gated act. Merging is not releasing.
3. **Lossless history.** Agents do the merging, so history is audited: merge `--no-ff` at the
   boundary and **never squash there**.
4. **Tiered retention.** Squash only *local checkpoints* into a few clean conventional-commit
   digests *before* merge; the integration ledger is preserved forever.
5. **Unit of work = a bead.** Worktree → implement → refine → check → submit → review → merge.

## The planes

| Plane | Status | Owns (input → output) | Seats |
|---|---|---|---|
| **Control** | operational | workspace → governed + routed + configured + observed factory | supervisor · director · custodian · controller |
| **Planning** | operational | idea → gated molecule (epic + children + dep DAG) | planner · analyst |
| **Integration** | operational | kicked-off molecule → beads landed `--no-ff` on green line | dispatcher · developer · reviewer · merger |
| **Assurance** | proposed (cross-cutting gate layer) | change/release → security + policy verdict | warden (+ verifier as a *lens*) |
| **Release** | roadmap | green line → cut release (version + changelog + tag) | releaser |
| **Contribution** | roadmap | internal change → upstream PR over external rig | contributor |
| **Delivery** | roadmap (named now) | release + IaC/gitops desired-state → reconciled system | operator |

Each operational plane runs a distinct session with its own seats and verb surface, and hands off
to the next; Assurance attaches gates across them.

### Control plane — governing the factory

The control plane governs the *factory itself*, split into four seats over four conceptual
resources with different blast radii (a 3-level spine **supervisor → director → dispatcher**, where
dispatcher lives one plane down in Integration):

- **supervisor** (`super/`) — the whole factory + policy; ultimate/root, launches and oversees the
  other control seats.
- **director** (`dir/`) — intake + fleet work routing (intake→plan→work); the interface to the
  per-rig dispatchers. Directs work; holds no secrets, sets no policy.
- **custodian** (`cust/`) — config + secrets + repo provisioning + resource cleanup; the only control
  seat touching **key material**, doing the mechanical commissioning.
- **controller** (`ctrl/`) — factory telemetry/efficiency; read-mostly, no lifecycle mutation.

Head Office — the workspace registry at `~/.ws/config.yaml` — is partitioned: supervisor writes
policy, director writes fleet/`managed_repos` membership, custodian writes rig config, controller
reads. A **collapse path** lets a small/single-rig factory run just the **supervisor**, absorbing the
director/custodian/controller scopes; split them out as the factory grows. These are
human-supervised sessions that commission repos, configure them (`bh config set`), and do not pair
with the `work` skill. See [storage-model.md](storage-model.md) for Head Office and rig kinds.

### Planning plane — idea → gated molecule

The **planner** turns a raw idea into a molecule a dispatcher can execute. Loop:

```text
ideate → research → architecture → decompose → file molecule
```

A human-interactive session. For *deep* tiers the planner spawns the **analyst** sub-agent for
codebase and web research before decomposing. Filing a molecule opens **two distinct gates**,
never collapsed:

- **Plan approval** — `bh plan file <spec>` compiles the spec into beads (epic + children +
  deps + labels) and opens the kickoff gate (`kickoff=pending`). Gates whether the decomposition
  is right.
- **Kickoff approval** — `bh plan approve <epic>` resolves the gate and flips
  `kickoff=approved`; only now do the molecule's root beads surface in `bh work ready` for a
  dispatcher. Gates whether the work should start now.

### Integration plane — execute the molecule

The **dispatcher → developer → merger** chain executes a kicked-off molecule. The dispatcher finds
ready beads, assigns and provisions worktrees, launches developer sub-agents, watches review gates,
and serializes merges through the merger: **parallel devs, serial merge.** Each bead lands `--no-ff`
on the always-green integration line. The dispatcher is **one seat, scope × mode** — fanout delegates
each bead to a developer; collapsed inlines the implementation on a shared batch branch. See
[bead-lifecycle.md](bead-lifecycle.md) for the verb table and
[dispatch-and-scheduling.md](dispatch-and-scheduling.md) for how ready work becomes agents.

### Assurance plane — the cross-cutting gate layer _(proposed)_

**Assurance** is not a sequential plane — the **warden** (`warden/`) attaches gates at pre-merge
(Integration), pre-cut (Release), and pre-publish (Contribution). It owns **security + policy only**
(secret-scan, SBOM, policy-as-code): read + block, no writes. The Contribution provenance scrub +
human publish gate stay owned by the `contributor` seat. **verifier** (`verify/`) is kept as a *lens*
today — acceptance/e2e via developer-check + reviewer-demo + CI — promoted to a seat only when e2e
needs its own test-env identity.

### Release plane _(roadmap)_

A deliberate, gated act separate from integration: version determination plus a Commitizen (`cz`)
release gate (the **releaser**, `release/`) turns an always-green integration line into a cut
release. Merging is not releasing; the release plane is where releasing happens.

### Contribution plane _(roadmap)_

A sibling to Integration that operates over **external rigs** — our virtualized view of a repo
outside the factory boundary that we do not control and generally cannot push to. It is **always
fork-and-PR**: a dedicated **`contributor`** (`contrib/`) seat (built on the read-only analyst
primitive) owns a repo **dossier** — the target's CONTRIBUTING rules, PR-template and DCO
requirements, mined historical conventions, and AI-PR posture — whose conventions trump ours on any
conflict. An automated **provenance scrub** hard-blocks factory metadata from entering a PR, and a
human-only, non-agent-resolvable **`bh work pr`** publication gate clears an exceptionally high
quality bar before anything is published upstream.

### Delivery plane _(roadmap, named now)_

The real "AGF stops at merge" gap: a proper sequential plane (`release → deploy → running`) where the
**operator** (`ops/`) reconciles a cut release + IaC/gitops desired-state into a deployed system
(gitops apply, rollback). Runner service identities are out of scope as seats. Delivery feeds a
speculative **Feedback** plane that closes the loop back to Planning.

## The one-terminal loop

The whole integration plane runs from a **single Claude Code terminal**: a dispatcher finds
ready beads, assigns and provisions worktrees, launches developer sub-agents (model per bead),
watches the review gates, and serializes merges via the merger. Parallel devs, serial merge — one
terminal driving the molecule end to end.
