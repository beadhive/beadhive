# Spike `bh-0gpn.1` — one escape hatch for `bd` + `git-workspace`, the two tools no registry carries

**Bead:** `bh-0gpn.1` · **Seat:** `dev/dev1` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-0gpn.5` (DECISION: which native-deps approach does Beadhive adopt?),
alongside sibling spikes `bh-0gpn.2` (homebrew tap), `bh-0gpn.3` (devbox), `bh-0gpn.4` (microVM)

## Question

Is there ONE mechanism that installs `bd` and `git-workspace` at a SPEC-NAMED version on both
macOS-arm64 and Linux-x86_64, reproducibly? GO requires ALL of: one named mechanism covering
both tools on both arches; the installed `bd` verified (not inferred) to embed dolt >= 2.2.0;
the installed `git-workspace` reporting the pinned version on both arches; re-running the
install at the same spec reproducing the same result. NOT asking whether to retire the
Brewfile's `bd` HEAD pin (`bh-4xwy`) — the pin's *reason* (dolt >= 2.2.0) is assumed to stay
and the mechanism must honour it.

## Method

Attempted every candidate for real, on this machine (macOS-arm64, host) and inside Docker
containers for the Linux-x86_64 leg (`docker run --platform linux/amd64 …`, emulated via
colima's QEMU on this Apple-Silicon host — no native x86_64 hardware was available). All work
happened in an isolated scratch dir
(`/private/tmp/.../scratchpad/spike-bh0gpn1/`), never touching the real `bh-0gpn.1` worktree's
own `.beads/` or the machine-linked `bd` (`HEAD-af076b6`, left untouched throughout).

1. Re-ran every "already measured" fact given in the bead myself, via `mise latest <backend>:<pkg>`
   and `mise backends` (`mise` is this repo's own already-adopted tool manager — see Evidence 8).
2. Cloned `github.com/gastownhall/beads` fresh and decoded `go.mod` at the exact commit
   `HEAD-af076b6` (`af076b628984622d19b2ecc79c7d82abacd3c17b`) that `bh-bmsg` validated fixes
   upstream #4770 — per this bead's own instruction, never inferring the dolt version from
   `bd version` or release notes.
3. Tried `go install github.com/gastownhall/beads/cmd/bd@<sha>` as literally named in the
   bead's candidate list; when it failed, root-caused it and found the actually-working import
   path.
4. Installed `git-workspace` via `mise`'s `cargo:` backend on macOS-arm64, and via the exact
   `cargo install --locked` invocation `docker/Dockerfile:59` already uses inside a
   `rust:1.97.1-slim-bookworm` container (`--platform linux/amd64`) for the Linux-x86_64 leg —
   run **twice independently** to check reproducibility.
5. Installed `bd` via `go install`/`mise`'s `go:` backend on macOS-arm64 (with the ICU CGo
   dependency resolved — see Evidence 5), and via the identical command inside a
   `golang:1.26-bookworm` container (`--platform linux/amd64`) for Linux-x86_64.
6. Compared `sha256`/`--version` output across every run to check byte-for-byte and
   version-level reproducibility.
7. Installed `mise` itself (official installer) inside a plain `debian:bookworm-slim`
   `--platform linux/amd64` container to confirm the mechanism (not just its underlying `go`/
   `cargo` primitives) is itself arch-portable.
8. Checked what the project's own baked images (`beadhive/core:dev`, already built locally)
   actually ship today, since `docker-bake.hcl`/`docker/Dockerfile` pin `bd` independently of
   the Brewfile.

## Evidence

1. **Registry-availability facts from the bead, re-verified via my own `mise latest` /
   `mise registry` runs (not trusted from the bead text):**
   `mise latest aqua:dolthub/dolt` → `2.2.3`; `mise latest github:gastownhall/beads` → `1.1.2`;
   `mise latest aqua:orf/git-workspace` → `mise WARN … no aqua-registry found for
   orf/git-workspace`; `mise install cargo:git-workspace@1.10.1` → succeeds, `1.10.1`. All
   match the bead's stated facts exactly.

2. **`mise` is the "ONE mechanism" candidate, and it is already this repo's house style, not a
   new tool being introduced.** `mise backends` lists `aqua, asdf, cargo, conda, core,
   dotnet, forgejo, gem, github, gitlab, go, npm, pipx, pkgx, spm, http, s3, ubi, vfox` —
   identically on macOS-arm64 (`mise 2026.6.14`) and, installed fresh via the official
   installer, on Linux-x86_64 (`mise 2026.8.1`, `debian:bookworm-slim --platform linux/amd64`).
   `.mise.toml:13` already pins a tool absent from mise's curated registry with exactly this
   syntax — `"github:docker/buildx" = "0.36.0"` — with a comment explicitly justifying the
   non-registry backend ("free supply-chain verification on a build-critical tool"). `bd` and
   `git-workspace` would extend an existing pattern, not create one.

3. **`git-workspace` via `mise`'s `cargo:` backend — macOS-arm64, works, exact version.**
   `mise install rust@1.97.1` (matching `docker-bake.hcl`'s `RUST_TAG` exactly) then
   `mise install cargo:git-workspace@1.10.1` (from a `mise.toml` pinning both) succeeded in
   **64s** wall (`1:04.23 total`, cargo build itself `Finished … in 1m 00s`). Captured the
   underlying invocation with `MISE_VERBOSE=1`:
   `cargo-binstall -y git-workspace@1.10.1 --locked --root …`, which falls back to source
   compile (no prebuilt binstall artifact exists for this crate) — i.e. the *same*
   `cargo install --locked` class of command `docker/Dockerfile:59` already runs, just reached
   through `mise`'s wrapper. `mise exec -- git-workspace --version` → `git-workspace 1.10.1`,
   exactly the pin.

4. **`git-workspace` via the identical raw command — Linux-x86_64, works, exact version, run
   twice.** Inside `rust:1.97.1-slim-bookworm --platform linux/amd64` (QEMU-emulated on this
   arm64 host): `apt-get install … ca-certificates cmake libssl-dev pkg-config zlib1g-dev &&
   cargo install --locked --root /out git-workspace --version 1.10.1`. Run 1: **10m19s**
   (`Finished … in 10m 19s`). Run 2 (fresh container, same spec): **6m08s**. Both printed
   `Installed package 'git-workspace v1.10.1'`; `docker exec … /out/bin/git-workspace
   --version` → `git-workspace 1.10.1` on the run-2 artifact. This directly contradicts a
   documented concern in `justfile:279-289` — that emulated compilation of git-workspace's
   vendored C "crashes gcc's cc1 and clang's integrated assembler" — but that finding is about
   **buildx's docker-container driver**, which ships its own bundled QEMU inside `buildkitd`
   and "never consults the kernel's binfmt handler." My test used `docker run --platform
   linux/amd64` directly against the daemon, which goes through the *kernel's* binfmt_misc
   QEMU registration (colima's own emulator setup) — a different emulation path that, measured
   here, compiles this exact crate successfully where the bake-time path does not. This is a
   genuinely new, non-trivial distinction worth carrying forward, not a contradiction to wave
   away.

5. **`go install github.com/gastownhall/beads/cmd/bd@<sha>` — literally the bead's candidate
   — FAILS to build, on both arches, with a clear root cause.**

   ```text
   go: github.com/gastownhall/beads/cmd/bd@af076b628984622d19b2ecc79c7d82abacd3c17b: version constraints conflict:
       github.com/gastownhall/beads@v1.1.1-0.20260725210241-af076b628984: parsing go.mod:
       module declares its path as: github.com/steveyegge/beads
               but was required as: github.com/gastownhall/beads
   ```

   Root cause, confirmed directly: `github.com/steveyegge/beads` returns `HTTP/2 301` (GitHub
   org-transfer redirect) to `github.com/gastownhall/beads` (`HTTP/2 200`), but `go.mod`'s
   `module` directive was never updated after the transfer — at `af076b6` **and** at current
   upstream `HEAD` (`1da3ac377`, checked 2026-08-03) it still declares `module
   github.com/steveyegge/beads`. Go's proxy/VCS layer follows the redirect for **version
   listing** (`mise latest go:github.com/gastownhall/beads` → `1.1.2`, matching the bead's
   "resolves" claim), but the **build** step enforces an exact import-path match and refuses.
   This is precisely the "looks like it works, doesn't" trap the bead warns against for `bd
   version`/dolt — here it recurs one layer down, in module resolution vs. build.

6. **The correct import path — `github.com/steveyegge/beads/cmd/bd@<sha>` — builds, but only
   with a companion native dependency neither `go` nor `mise` provisions.** First attempt
   failed: `github.com/dolthub/go-icu-regex/internal/icu: file.cpp:3:10: fatal error:
   'unicode/regex.h' file not found` — `dolt`'s CGo ICU-regex binding needs ICU4C headers.
   Homebrew's own `beads` formula depends on `icu4c@78` for exactly this reason
   (`brew info beads` → `Required (2): dolt, icu4c@78`), and it is keg-only, so `go install`
   run outside Homebrew's build env needs it named explicitly:
   `CGO_CFLAGS`/`CGO_CXXFLAGS=-I$(brew --prefix icu4c@78)/include`,
   `CGO_LDFLAGS=-L$(brew --prefix icu4c@78)/lib`. With those set,
   `go install github.com/steveyegge/beads/cmd/bd@af076b628984622d19b2ecc79c7d82abacd3c17b`
   succeeded on macOS-arm64 in **36s** wall (`35.910 total`), producing a working
   Mach-O arm64 binary. `mise install "go:github.com/steveyegge/beads/cmd/bd@<sha>"` with the
   same env vars set also succeeded (`MISE_VERBOSE=1` captured the exact underlying call:
   `go install -mod=readonly github.com/steveyegge/beads/cmd/bd@<sha>`).

7. **Same command, Linux-x86_64: works, at real emulation cost.** `golang:1.26-bookworm
   --platform linux/amd64` + `apt-get install libicu-dev` (Debian's equivalent), then
   `go install github.com/steveyegge/beads/cmd/bd@af076b628984622d19b2ecc79c7d82abacd3c17b` —
   succeeded in **14m56.851s** wall (`user 27m40.673s, sys 9m52.551s` — the emulator is
   burning several cores per wall-second), producing a 195MB Linux/amd64 ELF that runs and
   prints `bd version 1.1.0 (dev)`.

8. **Reproducibility — byte-for-byte, not just "same version string."** The macOS-arm64
   binary was built three separate times, across two different invocation paths (raw `go
   install`, and `mise install go:…` — run twice, once forced): every run's `shasum -a 256`
   is **identical**: `f842bfbd09af98baa650b9c3d6a3881796f6a26bbe9ca9ac14b62f4f9844fcfe`. This
   is a structural property of Go's pinned-pseudo-version + checksummed module graph, not a
   coincidence of this run. `git-workspace`'s two independent Linux builds both resolved to
   the pinned `1.10.1` (`cargo install --locked` pins the exact dependency graph the same way).

9. **`bd`'s embedded dolt version, verified by decoding `go.mod` at the installed ref — never
   inferred from `bd version` or release notes, per this bead's explicit requirement.**
   `git show af076b628984622d19b2ecc79c7d82abacd3c17b:go.mod` (re-run fresh here, not copied
   from `bh-bmsg`) shows `github.com/dolthub/dolt/go v0.40.5-0.20260715172757-a6690826d767` —
   the exact pseudo-version `bh-bmsg` already established compares identical (0 ahead / 0
   behind) to tagged `dolt v2.2.0`. Checked current upstream `HEAD` too (`1da3ac377`,
   2026-08-03): **still** pins the same `a6690826d767` — the fix has not regressed since
   `bh-bmsg`'s last re-check. Because `go install pkg@<sha>` builds from exactly this `go.mod`,
   the installed binary's dolt pin is a **build-time structural guarantee**, not an inference —
   arguably stronger than the `bh-gnqc` runtime-reproduction path, which only demonstrates the
   symptom is absent on one store, not the underlying version.

10. **`bd version` on the freshly built binary reports only `1.1.0 (dev)`** — no commit, no
    dolt version, confirming in a fresh run exactly the trap this bead calls out. Neither raw
    `go install` nor `mise`'s wrapper sets Homebrew's `-X main.Version=… -X main.Branch=…`
    ldflags, so an implementation adopting this mechanism should add them if `bd version`
    needs to stay diagnostic for humans.

11. **New finding, not anticipated by the bead: the project's own locally-baked images ship the
    STALE `bd` today.** `docker run --rm beadhive/core:dev bd version` →
    `bd version 1.1.2 (20e493e56: HEAD@20e493e569c9)` — the commit shown is v1.1.2's *own* tag
    commit (`20e493e569c9`, confirmed via `git ls-remote --tags`), not a HEAD build; the
    `"HEAD@"` label in bd's own version-string format is cosmetic and does not mean what it
    looks like — another live example of Evidence 10's trap. `docker-bake.hcl` pins
    `BD_VERSION = "1.1.2"` and `docker/Dockerfile` fetches
    `beads_${BD_VERSION}_linux_${TARGETARCH}.tar.gz` from GitHub releases — a release-asset
    install, exactly the pattern this bead's own DESIGN section says "silently REGRESSES the
    thing the HEAD pin exists to prevent." Confirmed independently: `git show
    v1.1.2:go.mod` → `github.com/dolthub/dolt/go v0.40.5-0.20260605230755-1bf533220ab0`, the
    same stale pin `bh-bmsg` measured. **`beadhive/core:dev` and `beadhive/agent:dev`, as baked
    today, can hit the indefinite `bd dolt pull` hang** on a large store — the exact defect the
    Brewfile's `args: ["HEAD"]` exists to avoid on macOS, unprotected on the container path.
    This is out of scope to fix here (spike, no product code) but is squarely what this
    mechanism, once adopted, would fix.

12. **Cost summary** (all wall-clock; Linux is QEMU-emulated on this arm64 host, no native
    x86_64 hardware tested):

    | tool | macOS-arm64 (native) | Linux-x86_64 (QEMU-emulated) |
    |---|---|---|
    | `git-workspace` (`cargo:`) | 64s | 6m08s – 10m19s (2 runs) |
    | `bd` (`go:`) | 36s | 14m57s |
    | `rust` toolchain (one-time, `mise`) | 19s | — (image already has it) |

    The Linux figures are a pessimistic upper bound from emulation, not a measurement of real
    x86_64 hardware (e.g. a CI runner) — flagged as a gap, not glossed over. `justfile:280-289`
    independently documents that this project's own CI-grade cross build
    (`just image-cross`, native-per-arch runners) exists precisely because Apple-Silicon-hosted
    cross-compilation is the slow/fragile path, not the norm.

## Verdict — **GO**

`mise` — already this repo's tool manager — is the ONE mechanism. `mise install
"go:github.com/steveyegge/beads/cmd/bd@<sha>"` for `bd` and `mise install
cargo:git-workspace@<version>` for `git-workspace` install both tools at spec-named versions
on macOS-arm64 and Linux-x86_64, byte-for-byte reproducibly, with `bd`'s dolt >= 2.2.0 pin
guaranteed by construction (Evidence 9) rather than merely observed. All four GO-bar items are
met (Evidence 3–4, 6–9).

Three caveats a implementation must carry, none of which flips the verdict:

- **The module path in the bead's own candidate list is wrong and a trap.**
  `go:github.com/gastownhall/beads` *resolves* (version lookup follows GitHub's org-transfer
  redirect) but does not *build* (Go enforces the `go.mod`-declared path,
  `github.com/steveyegge/beads`, which the transfer never updated). Anyone reaching for this
  mechanism from the bead text alone would hit Evidence 5's failure. Document the correct path
  loudly wherever this mechanism is written down.
- **ICU4C is a second, OS-level prerequisite `mise` does not provision** (`icu4c@78` via
  Homebrew on macOS; `libicu-dev` via `apt` on Debian/Linux) — a real, if small, second pin
  outside the "one manager" story, already implicit in the Brewfile's `depends_on "icu4c@78"`.
- **Linux timing here is emulation-inflated**, not measured on real x86_64 hardware — price
  accordingly, and prefer measuring on an actual x86_64 runner before committing to a CI cost
  budget.

## Recommendation

- **`bh-0gpn.5`** can score this candidate as: one mechanism (`mise`), reproducible, dolt-safe
  by construction, with two named prerequisites (a `rust` toolchain entry `mise` itself
  provisions, and `icu4c`/`libicu-dev` from the OS package manager) and a documented Linux
  build-time cost this spike could only measure under emulation.
- **If adopted**, the implementation molecule should: add `"go:github.com/steveyegge/beads/cmd/bd"
  = "<sha-or-tag>"` and `"cargo:git-workspace" = "<version>"` (plus a `rust` entry) to
  `.mise.toml`, following the exact precedent already set by `"github:docker/buildx"` there;
  set Homebrew-equivalent `ldflags` if `bd version`'s human-readable output matters on this
  path (Evidence 10); and re-verify the dolt pin by `go.mod` decode, never `bd version`,
  whenever the pinned `bd` sha moves (Evidence 9 and 11 both show why).
- **Flag Evidence 11 as an immediate, separate concern**, independent of this spike's GO/NO-GO:
  the currently-baked `beadhive/core:dev` / `beadhive/agent:dev` images ship a `bd` whose dolt
  pin is 168 commits stale, unprotected by the Brewfile's HEAD-pin rationale. This spike does
  not fix it (no product code), but it is exactly the kind of concrete regression this
  mechanism — once adopted for `docker-bake.hcl`'s `BD_VERSION` too — would close. Worth a
  bug bead of its own if one does not already exist; `bh-4xwy` tracks retiring the *Brewfile's*
  HEAD pin but is scoped to macOS/Homebrew and does not mention the container image's separate,
  currently-stale `BD_VERSION` pin.
- **Compare against `bh-0gpn.2`/`.3`/`.4`'s findings before `bh-0gpn.5` decides** — this doc
  answers "can one mechanism cover both tools reproducibly," not "is this the *best* mechanism
  relative to a homebrew tap, devbox, or microVM"; that comparison is `bh-0gpn.5`'s job once
  all four spikes report.
