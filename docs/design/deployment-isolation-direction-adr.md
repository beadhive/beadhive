# Deployment isolation ADR — tool provenance, seat reachability, and the two-axis integration model

**Status:** proposed · **Date:** 2026-08-03 ·
**Supersedes:** nothing · **Amends:** no other ADR (Decision 5 narrows Decision 1 in place) —
but see [Relationship to other epics](#relationship-to-other-epics), because Decision 3 moves a
question out of `bh-lx6e` and into `bh-c6dk`.

Records the direction taken after three spikes (`bh-0gpn.1`, `bh-lx6e.1`, `bh-lx6e.2`) answered
the two questions the container plane could not: **where do pinned tools come from**, and **how
does a human reach a conversational seat when access is remote-only**.

---

## Context

Beadhive has two deployment shapes that pin their components differently. The container plane pins
everything in `docker-bake.hcl` — sha256 per arch, fetched by `docker/fetch-tool.sh`. The native
plane pins nothing: `Brewfile` installs latest-then-frozen, and `.mise.toml` pins a *different* set
of versions again (`bh-lgj2` measured the divergence: just 1.54.0 vs 1.57.0, gh 2.95.0 vs 2.97.0).

Simultaneously, `bh role <seat>` execs `claude --agent bh:<seat>` with **inherited stdio**
(`role.py:223`). That one line is the whole session model — foreground, TTY-bound, no identity, no
detach, no handle. Survivable on a laptop; not survivable in a container, and the operating
constraint is explicitly **remote-only access, no easy local shell**.

A third constraint shapes both: whatever ships as the default **must not require a plugin**.

---

## Decision 1 — mise owns the native plane; brew is a convenience, not a pinning mechanism

**Homebrew was never able to do what it was being asked to do.** `brew pin` freezes a formula
already installed; it cannot install a chosen version, and homebrew-core dropped versioned
formulae for most things. That gap is why `brew "beads", args: ["HEAD"]` needed a twelve-line
justification in the Brewfile — the HEAD pin is a workaround for a pinning mechanism that isn't one.

`bh-0gpn.1` established mise as the replacement, with measured evidence on both architectures:

| tool | mechanism | result |
|---|---|---|
| `git-workspace` | `cargo:git-workspace@1.10.1` | both arches; same mechanism the Dockerfile already uses |
| `bd` | `go:github.com/steveyegge/beads/cmd/bd@<sha>` | byte-identical binaries (sha256 `f842bfbd…`) across three invocations |
| `dolt` | `aqua:dolthub/dolt` | resolves 2.2.3 — exactly the `docker-bake.hcl` pin |

**A correction the spike had to make, and the reason it matters:** the candidate path this work
started from — `go:github.com/gastownhall/beads` — *resolves* versions through the module proxy but
**does not build**. Go enforces the module path declared in `go.mod`, which still reads
`github.com/steveyegge/beads` after a GitHub org transfer. Resolution is not buildability, and a
version list from a proxy is not evidence that an install works.

Consequence: brew remains a fine way to *get* a package locally. It carries no pinning
responsibility, and no spike should be spent trying to make it carry one.

**Limitation.** The Linux-x86_64 legs ran under **QEMU emulation**, not native hardware — notable
because the Dockerfile's own comments record QEMU crashing gcc's `cc1` and clang's integrated
assembler on this exact cargo build. It succeeded under emulation; that is not the same claim as
native, and a native re-run is owed before this is load-bearing in CI.

**That native re-run happened (2026-08-05, bh-q160.12) and it corrected the `git-workspace` row
above.** On a bare Debian 13 x86_64 host, `cargo:git-workspace@1.10.1` does not install: it needs
a Rust toolchain (745M via mise) *and* the apt packages `libssl-dev` + `pkg-config`, proven by the
compiler's own message — `openssl-sys = 0.9.112 … Make sure you also have the development packages
of openssl installed`. The emulated leg presumably had those present. `brew install git-workspace`
is no escape: homebrew-core bottles it for `arm64_sonoma` **only**, with build deps `pkgconf` and
`rust`, so it is the same source build. The apt half needs **root**, which puts it in the
provisioning phase rather than the unprivileged bootstrap — the concrete reason **Decision 5**
narrows this decision to the developer plane.

---

## Decision 2 — the PTY seam is the near-term seat surface

`bh-lx6e.1` returned GO with measurements rather than argument, against the baked
`beadhive/agent:dev` image:

- A hard client disconnect (`SIGKILL`, exit 137) did **not** kill the session; a genuinely
  different client reattached with scrollback intact.
- Both baked harnesses (Claude Code 2.1.220, Codex 0.146.0) render their full interactive TUI —
  colour, resize — inside a detached pane with zero attached clients. The contrast test is the
  load-bearing half: **the same binary refuses to run interactively without a pty**, which is
  precisely the degradation risk the bead named. tmux's pty allocation is what avoids it.
- Transport parity over two transports (`docker exec` + an ephemeral in-container sshd), identical
  verbs against one live session.
- Cost: **+0.62 MiB** measured via a real derived-image build and diff.

**Licence finding.** tmux is ISC and within the allowed set. Debian's tmux package, however, hard-
depends on `libutempter0` (LGPL-2.1). Not disqualifying today — it is mechanically outside
`tests/test_component_licenses.py`'s scope, exactly as `git` (GPL-2) already is — but it is a real
decision rather than a non-issue, and tmux's own `configure.ac` disables `--enable-utempter` by
default, so a source build avoids it entirely.

**The gap that constrains what can be built on this.** tmux session state lives at
`/tmp/tmux-<uid>/` — **none of the four durable volumes**. Seats therefore survive client
disconnect but **not a container recreate**: `down && up` loses every seat. This is a minor
annoyance under Docker and a **structural problem under Kubernetes**, where pods are rescheduled
routinely. It must be fixed before anything is built on top of the seam, and more urgently if the
Helm/k3s direction is real.

Second documented gap: stale in-container `docker exec` client processes are not reaped by
`init: true` — docker-init reaps zombies, not live orphans.

---

## Decision 3 — view/input and orchestration are ORTHOGONAL axes, scored separately

This is the decision that reorganizes everything else, and it corrects an error in how the
question was originally posed.

`bh-lx6e.2` was asked whether one interface could serve every candidate front end. It returned
**NO-GO**, splitting five candidates into four "shapes." That framing was wrong — not the
evidence, the taxonomy. It fused two independent axes into one, and therefore made adopting a
candidate's UI look inseparable from adopting its orchestrator.

**The correct model: every candidate is a mode of view and input. Some ADDITIONALLY carry an
orchestrator component, and that component sits on an axis bh already owns.**

| Candidate | Axis 1 — view & input | Axis 2 — orchestrator |
|---|---|---|
| terminal (PTY / tmux) | yes — the baseline | — |
| orca (server + client app) | yes — PTY | — |
| openchamber | yes — HTTP+SSE, OpenCode-locked | — |
| OpenHands (browser UI) | yes | **yes** — native mode |
| qm (UI) | yes | **yes** |

**Axis 2 is not new.** It is `work.runtime`, defined by `bh-c6dk` as `claude | local | temporal`
(Claude Code teams / local async poll loop / Temporal). qm and OpenHands are two further candidate
tiers on that existing seam — competitors to a design bh already has, not a foreign concept
arriving through the front door.

Three consequences:

1. **`bh-lx6e.2`'s "Shape C" is an Axis-2 finding.** It describes bh becoming a callable inside an
   external runtime's loop. That is a runtime-tier question and belongs to `bh-c6dk`. The NO-GO
   verdict stands on Axis 1 and must **not** be read as "qm rejected."
2. **A candidate may be adopted on one axis without the other.** qm's UI without qm's runtime;
   OpenHands' runtime without its UI. The earlier claim that "adopting qm means adopting an
   orchestrator" was an artifact of the fused framing.
3. **Two live epics intersect and neither accounted for the other.** `bh-lx6e` (seat access) and
   `bh-c6dk` (runtime tiers) both land on qm and OpenHands.

**Decision rule going forward:** score every candidate on both axes, adopt per axis, and never let
a UI preference drag in a runtime commitment or the reverse.

### What is buildable now

Axis 1 splits cleanly. **The PTY seam covers the two pure view/input candidates — terminal and
orca — with no new protocol work**, which is why Decision 2 is the near-term investment. ACP-over-
stdio is the second Axis-1 shape and is already scoped under `bh-xls2.4` / `bh-xls2.5`; it is the
only candidate with a standardized permission-approval method (`session/request_permission`) and is
worth building on its own merits, separately from this ADR.

openchamber is Axis-1 only but **harness-locked**: it speaks OpenCode's SDK, so it can only ever
drive an OpenCode-backed seat. `role.py` already lists `opencode` in `KNOWN_HARNESSES`, so the gap
is a new `opencode serve` launch mode rather than harness support.

---

## Decision 4 — the image's `bd` gets a bridge, and shared-server retires it

`bh-0gpn.1` surfaced an unplanned defect. Verified independently:

```text
docker run --rm beadhive/core:dev bd version   ->  bd version 1.1.2
docker-bake.hcl:149                            ->  BD_VERSION = "1.1.2"
src/beadhive/setup.py:79                       ->  BD_LAST_RELEASE_WITHOUT_DOLT_FIX = (1, 1, 2)
```

**bh's own advisory logic flags the exact version its own image ships.** `dolt_fix_advisory()`
warns that `bd dolt pull` can hang indefinitely on a large store (upstream beads#4770) for any
release at or below 1.1.2. The Brewfile avoids this on macOS with a `--HEAD` pin; the image walked
into it. Container and host disagree about a bug the repo has already documented twice.

**The fix is a bridge, not a rebuild**, because the Brewfile already recorded the retirement
condition:

> Drop `args: ["HEAD"]` when EITHER: a tagged release pins dolt >= v2.2.0, **OR bh-00cq lands** —
> running bd against an external `dolt sql-server` decouples the dolt version from bd's release
> cadence entirely, since bd issues fetch/pull as `CALL DOLT_FETCH(...)` over the connection, so
> the SERVER's dolt does the work.

So:

1. **Interim** — build `bd` from a pinned HEAD sha in the image, using the mechanism Decision 1
   proved: `go install github.com/steveyegge/beads/cmd/bd@<sha>`, byte-reproducible, with
   `libicu-dev` as the Debian companion dependency. This replaces the release-tarball + sha256
   fetch **for `bd` only**; every other component keeps its current pinning.
2. **Retirement** — the shared-server migration moots it. Once the container's `bd` talks to an
   external `dolt sql-server`, the server's dolt performs the transport and the embedded version
   stops mattering. `docs/CONTAINER.md` already flags `dolt` as becoming a core service under
   `bh-erwe.3`.

**Therefore the interim must not be over-engineered** — a pinned-sha Go build stage, explicitly
marked as bridging, cross-referencing `bh-00cq`.

Two cautions carried into that work. `bh-bmsg` records that **reverting to stable is not a clean
rollback once the store has migrated** — the bridge contains a one-way door. And the decoupling
argument covers *transport*; whether the container's `bd` still uses embedded dolt for anything
local once `dolt.backend` is not `none` has **not been verified** and should be before the bridge
is retired.

---

## Decision 5 — local-install adopts a Nix flake; mise keeps the developer plane

**This narrows Decision 1, it does not reverse it.** mise keeps the plane it is good at and loses
the one it is not.

| plane | toolchain | status |
|---|---|---|
| **Developer** — macOS, contributor laptops | mise + Brewfile, `just bootstrap` | unchanged; Decision 1 holds verbatim |
| **local-install** — Linux hosts (`bh-q160`) | Nix flake; Nix installed by **root** in daemon mode during provisioning | new; no mise and no Homebrew on a provisioned host |

**The two planes have different jobs.** Development optimises for a pleasant checkout and per-tool
version choice — mise does that well. local-install optimises for exactly one thing: `bh` being able
to **reach** its dependencies on a machine nobody is sitting at. That is where mise failed, because
`bh` resolves tools with `shutil.which()` on the inherited `PATH` (`setup.py :: PROBE_TABLE`) while
mise installs into a tree that reaches `PATH` only once activated.

**Measured on beadhive-factory (Debian 13 trixie, x86_64, native — not emulated), 2026-08-05.**
The split is exact: Brewfile tools visible to `bh`, `.mise.toml` tools not.

| | mise + brew | Nix flake |
|---|---|---|
| `bh setup check` after a **successful** bootstrap | **2 of 4** — `git-workspace` and `gh` NOT FOUND | **4 of 4**, exit 0 |
| toolchain size | ~3.0G (brew 2.2G + mise 745M) | **1.2G** |
| `git-workspace` | Rust + `libssl-dev` + `pkg-config`, needs root | **1.10.1 prebuilt**, from cache |
| PATH-class blockers found | **5** in one session | structurally impossible |

Five distinct blockers, all one root cause — a tool is installed somewhere the next step cannot
see: the `just` entry point is circular (`just` arrives at line 2 of the recipe that needs it);
`uv sync` exits 127; `uv tool install` puts `bh` in `~/.local/bin`, off `PATH`; `bh` cannot see
`gh` or `git-workspace`; and mise's own `cargo` backend cannot find `cargo` after installing Rust.
A Nix store path is a real binary on `PATH` — there is no install-vs-activate gap to fall into.

`beads` still needs a bespoke HEAD override (nixpkgs carries 1.0.3, two releases *inside* the
range whose embedded dolt hangs `bd dolt pull`) — the same work the Brewfile HEAD pin does today.
The built binary's `go.mod` pins dolt dated 2026-07-15, so the source carries the fix.

**Costs, stated plainly.** Nix's daemon install needs **root**, which is provisioning-phase work
heavier than Homebrew. `flake.lock` pins the whole closure rather than per tool, so versions drift
from today's pins (`just` 1.57.0 vs 1.54.0, `gh` 2.97.0 vs 2.95.0) — stronger reproducibility,
less per-tool choice, and a genuinely different model.

**Limitation.** Only `x86_64-linux` was built and run. `aarch64-linux` and `aarch64-darwin`
evaluate and nothing more; `x86_64-darwin` is unlistable (`Nixpkgs 26.11 has dropped support`).
`aarch64-linux` is in scope and untested purely for want of a host — see Limitation 6.

---

## Consequences

- **The native plane gains a pinning mechanism it never had.** One manifest, both architectures,
  reproducible — and `.mise.toml` / `docker-bake.hcl` should stop being two independent version
  sources, test-enforced as `tests/test_image_build.py` already does for Dockerfile↔compose.
- **Docker remains the unit.** Nothing found argues otherwise. Helm stays blocked on `bh-erwe`
  (publishing), and Decision 2's durability gap raises its cost rather than lowering it.
- **Two spikes are superseded before running.** The homebrew-tap and devbox spikes existed to test
  candidates that Decision 1 answers or that the operator has since ruled out of the optimal path.
- **The smolvm spike survives with a different question.** As a dev environment it is unnecessary
  once mise lands; as a *distribution* mechanism — a portable machine artifact instead of a
  registry, against `bh-erwe` — it retains independent value.
- **`bh-c6dk` inherits two candidate tiers** and must weigh them against `local` and `temporal`.

## Limitations

1. ~~**The Linux legs were emulated**, not native (Decision 1). A native re-run is owed.~~
   **PAID 2026-08-05** (bh-q160.12) on native Debian 13 x86_64. It did not merely confirm the
   emulated result — it **corrected** the `git-workspace` row, which does not install natively
   without a Rust toolchain plus root-level apt packages. See Decision 1's amendment.
2. **Every qm judgment here is source-reading.** `bh-lrcw` — the qm capability probe — is filed but
   **unstarted**: the epic and all four probe children are open, `bh-lrcw.1` is blocked on its own
   unresolved human gate, and `docs/spikes/` contains no qm artifact. Earlier work in this
   direction incorrectly cited bh-lrcw as having produced evidence to consume. It has not. Both
   axes of the qm decision rest on reading its source, and Decision 3's placement of qm on Axis 2
   is a structural claim that its live probe could still revise.
3. **The interaction-type distribution was never captured** — OTEL is disabled locally, bd history
   is the wrong granularity, and transcripts exist in bulk without a validated classifier. No
   number was estimated. This is the single measurement that would most sharpen the Axis-1
   investment decision, and it remains a named gap.
4. **openchamber's permission wire schema was not independently verified** — it is OpenCode's own,
   and out of scope for a candidate that is fundamentally OpenCode's front end rather than bh's.
5. **Seat durability across a container recreate is unsolved** (Decision 2), and is a precondition
   rather than a follow-up for anything built on the seam.
6. **Decision 5 is proven on `x86_64-linux` only.** `aarch64-linux` and `aarch64-darwin`
   evaluate but have never been built or run. `aarch64-linux` is *in scope* for local-install
   and untested only because no arm64 Linux host was available — treat a first run there as
   unproven. macOS was deliberately skipped: Decision 5 excludes it by design, so proving it
   would mean installing Nix on a machine the architecture does not use.

## Relationship to other epics

| epic / bead | relationship |
|---|---|
| `bh-0gpn` | Track A spike molecule — Decision 1 is its `.1` verdict; `.2` and `.3` are superseded by it |
| `bh-lx6e` | Track B spike molecule — Decisions 2 and 3; `.5` must decide on the two-axis matrix |
| `bh-c6dk` | **gains qm and OpenHands as candidate `work.runtime` tiers** (Decision 3) |
| `bh-lrcw` | must run before any qm judgment on either axis (Limitation 2) |
| `bh-xls2.4` / `.5` | own the ACP Axis-1 shape and the permission-approval milestone |
| `bh-00cq` / `bh-erwe.3` | shared-server migration — retires Decision 4's bridge |
| `bh-lgj2` | its single-pin-source question survives; its mise-vs-fetch-tool framing is answered |
| `bh-4xwy` | adjacent to Decision 4 but distinct — that bead retires the *host* HEAD pin |
| `bh-q160` | clone-install molecule — **Decision 5 supersedes its local-install mechanism**; `.5`, `.6` and `.2` depend on `bh-q160.12` |
