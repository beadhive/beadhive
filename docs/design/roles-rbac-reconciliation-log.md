# Roles/RBAC reconciliation log — existing-epic alignment

> Reviewable artifact for the bd-metadata reconciliation beads under epic
> (batch `9r07-align`). These beads carry no source/doc changes of their
> own; their deliverable is aligning OTHER epics' beads (titles/descriptions/notes + dep edges)
> to the canon in [`roles-rbac-matrix.md`](./roles-rbac-matrix.md). Canon-alignment notes are
> appended non-destructively (`bd update --append-notes`) so the target beads' original planning
> content is preserved. This log records what each bead touched.

## — Contribution epic (uxam) → contributor/warden split

Targets:, `.5`, `.7`. Canon refs: §1, §2.2, §2.3, §4.

- **uxam.3 (contributor role + dossier)** — note appended: seat name **contributor** is KEPT
  (`contrib/`, Contribution plane); contributor OWNS the provenance scrub + the hard human-only
  publish gate. Security+policy scanning is a SEPARATE Assurance gate owned by **warden**
  (`warden/`), not the contributor. Single seat, no duplicate.
- **uxam.5 (anti-slop reviewer)** — note appended: this is the existing **reviewer** seat (`rev/`)
  in an external-rig MODE, not a new/parallel seat and not warden. Dossier-convention + anti-slop
  enforcement stays with reviewer; security+policy belongs to warden; provenance stays with
  contributor. No duplicate reviewer seat.
- **uxam.7 (docs/wiring)** — note appended: register the contributor seat; document the
  warden (security+policy) vs contributor (provenance + publish gate) split; the specialized
  reviewer is a reviewer mode, not a new seat.

Dep edges recorded: `relates_to` from `9r07.20` to `uxam.3`, `uxam.5`, `uxam.7` (the epic-level
`relates_to` to already existed).

## — sequence seat-prefix migration with rename epics (limn/kkke)

Targets:, and the 9r07 seat-prefix beads
`.19/.26/.29/.32`. Canon ref: §5 (retired/renamed names).

The seat-prefix rename (`coord/`→`disp/`, `crew/`→`dev/`) is a DIFFERENT rename from ws→bdws
(limn) and the bead-prefix →`ws/bdws` (kkke). Sequencing note appended to:

- **limn** and **kkke** — the seat-prefix rename lands now under epic 9r07 (P2); limn/kkke are
  later (P3); they are independent but the reference-review sweep should be BATCHED with kkke's
  full-reference-review so shared files/docs/skills are not rewritten twice. 9r07 is NOT blocked
  on limn/kkke.
- **9r07.19/.26/.29/.32** — reciprocal note to coordinate/batch with kkke, no blocking dependency.

Dep edges recorded (no blocking dep — avoids stalling the P2 epic on the P3 renames):
`relates_to` `kkke ↔ 9r07.19` and `9r07.21 ↔ 9r07.19`; the epic-level `relates_to`
`9r07.21 ↔ limn` and `9r07.21 ↔ kkke` already existed.

## — collapsed/tier/batch coordinator epics → dispatcher@scope×mode

Targets:,. Canon refs: §2.2, §5.

Canon-alignment notes appended mapping legacy coordinator vocabulary onto the single
**dispatcher** seat (`disp/`) parameterized by **scope** (main / epic-container / batch) ×
**mode** (fanout / collapsed):

- **e3r9 (collapsed dispatch mode)** → dispatcher @ batch (collapsed); epic-coordinator /
  epic-coordinator-deep / foreman names superseded; "deep" = `sub-dispatch:1`.
- **695w (tier-aware coordinator, nested coordinators)** → dispatcher @ scope; nested = dispatcher
  @ epic-container (fanout).
- **ih8 (coordinator batch scheduling, work groups)** → dispatcher @ batch (collapsed); the
  `claim --group` / `merge --group` work-group mechanism.

Dep edges recorded: the `relates_to` edges `9r07.22 ↔ e3r9`, `↔ 695w`, `↔ ih8` already existed.

## — superintendent epics (1684.6/czso) → Control split

Targets:,. Canon refs: §2.1, §5.

Canon-alignment notes appended mapping the retired **superintendent** onto the Control-plane
four-seat split (supervisor / director / custodian / controller):

- **1684.6 (superintendent seat on the server)** → the **supervisor** persistent Control-plane
  terminal under the §2.1 collapse path (small/single-rig runs just the supervisor, absorbing
  director/custodian/controller); split into own seats as blast radii diverge.
- **czso (superintendent as escalation consumer/relay)** → the **director** seat (fleet routing +
  interface to per-rig dispatchers); escalation relay is director scope, not a widened
  superintendent.

Dep edges recorded: `relates_to` `9r07.23 ↔ 1684.6` (added; the `↔ czso` edge already existed).

## — plane/layering epics (puvt/wvqx) → plane vocabulary

Targets:,. Canon refs: §1, §2.2, §2.3.

Canon-alignment notes appended anchoring the beads to the canonical plane names (Control /
Planning / Integration / Assurance; roadmap: Release / Contribution / Delivery):

- **puvt (relocate molecule kickoff + tier-aware coordinator)** → kickoff moves onto the
  **Integration** plane; tier-aware coordinator → **dispatcher** @ scope.
- **wvqx (harden plan/work layering + planning-plane convention gate)** → the **Planning** plane
  (planner/analyst) vs **Integration** plane (dispatcher/developer/reviewer/merger) boundary; the
  convention gate + raw-bd-passthrough gate sit on the Planning plane.

Dep edges recorded: the `relates_to` edges `9r07.24 ↔ puvt` and `↔ wvqx` already existed.

## — new Assurance plane doc (`docs/ASSURANCE.md`)

Deliverable (operational slice): `docs/ASSURANCE.md` describing the Assurance plane as a
**cross-cutting gate layer** and the **warden** seat (`warden/`) — **security + policy only**
(secret-scan / SBOM / policy-as-code) via the `security:*` gate that blocks the merge in **parallel
with review**. Canon refs: §1, §2.3, §4. The doc pins the scope boundary: **provenance stays with
the `contributor` seat** (`contrib/`), and acceptance / e2e is the **verifier lens**, not the
warden.

**Roadmap slices intentionally deferred (NOT created here):** `RELEASE-PLANE.md` (releaser /
Release plane) and `DELIVERY-PLANE.md` (operator / Delivery plane) are roadmap backlog, tracked by
 (releaser + Release gate) and (operator + Delivery
env gate). Only the operational `ASSURANCE.md` slice landed.

## — new warden agent def + contributor reconcile

Deliverable (operational slice): `plugins/agf/agents/warden.md` — the Assurance seat def, scoped
per the matrix (§2.3, §4): **read-and-block, no Edit/Write, no merge/dispatch**; resolves the
`security:*` gate; **security + policy remit only**. Modeled on the read-and-block seats
(`reviewer.md` / `controller.md`); tools `Bash, Read, Grep, Glob, Skill`, `model: opus`.

**Contributor reconcile (with):** the `contributor` seat name is **KEPT**
(`contrib/`, Contribution plane) and **provenance ownership stays with the contributor** — the
provenance scrub + the hard human-only publish gate are the contributor's, NOT the warden's. The
warden def is therefore scoped to explicitly **exclude provenance** ("Provenance is NOT in scope —
that stays with the contributor"), so when `uxam.3` lands `plugins/agf/agents/contributor.md` the
two defs do not overlap: warden = security + policy, contributor = provenance + publish gate. No
duplicate contributor seat is introduced. (`contributor.md` itself is authored by `uxam.3`, not
here; this bead only reconciles the boundary the warden def must respect.)

**Roadmap defs intentionally deferred (NOT created here):** `releaser` and `operator` agent defs
are roadmap backlog, tracked by (releaser) and
(operator). Only the operational `warden.md` def landed.

## — config `work.identity.crews` key decision (crews → devs)

Target: `src/ws/config.py::work_identity`. Canon refs: §2.2, §5 (`crew/` → `dev/`).

Decision: the per-seat attribution mapping key is renamed **`crews` → `devs`** to match the
`dev/` developer seat prefix. The legacy `crews` key is kept as a **DEPRECATED alias** — both
global and per-rig `crews`/`devs` are merged, with `devs` winning on collision — so existing
configs keep resolving through the migration window (removed later per the limn/kkke sequencing,
alongside the `coord/`/`crew/` prefix back-compat shim, bead .32). `otel_role` /
`otel_genai_*` docstrings now name the **dispatcher** / **developer** seats instead of
`coordinator`. Config template example identity name → `dev/claude`.
