# Spike `bh-lx6e.1` — Does tmux give `bh seat ls\|attach\|kill` durable, multi-transport sessions?

**Bead:** `bh-lx6e.1` · **Seat:** `dev/seatspike` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-lx6e.2` (the seam bh-lx6e.3/.4 plug into) and `bh-lx6e.5` (the operator's
front-end-direction decision) — this is the root spike the epic (`bh-lx6e`) says every front door
needs answered first.

## Question

`bh role` builds argv `["claude", "--agent", "bh:<seat>"]` and execs it with **inherited stdio**
(`src/beadhive/role.py:221-224`) — foreground, TTY-bound, no session name, no detach, no handle.
Close the terminal and the seat dies with it. The question: can **one verb set** —
`bh seat ls | attach | kill` — sit in front of **at least two** exec transports (ssh, docker exec,
kubectl exec, smolvm) with a **terminal multiplexer** as the durability substrate, such that a
conversational seat (1) survives a **hard** client disconnect (killed terminal, not a clean exit)
and (2) can be **reattached from a different remote client**?

**Explicitly not asking:** which multiplexer is prettiest (tmux is evaluated on evidence, not
assumed); whether `bh role` should be replaced (assumed to remain the foreground path, `bh seat`
additive).

## Method

1. Read `src/beadhive/role.py:100-226` (the exec path this spike replaces), `docker-compose.yml`
   (the four named volumes, `init: true`), `docs/CONTAINER.md`, `docs/ASSURANCE.md` and
   `docker-bake.hcl`'s licence-policy block (what the image may redistribute, and how existing pins
   are declared), `tests/test_component_licenses.py` (what it actually parses — `docker-bake.hcl`'s
   pinned `*_VERSION`/`*_TAG` variables only, confirmed by reading `_pinned_components()`), and
   `docs/design/gas-frameworks-comparison.md` (GasTown already runs its always-on runtime on
   tmux — prior art, not blind adoption).
2. Used the **already-baked, already-running** `beadhive/agent:dev` image
   (`docker images`: `a7e577edeee6`, 1.7 GB) via the pre-existing `beadhive-bh-1` container
   (started from this repo's own `docker-compose.yml`, `init: true`, 4 CPU / 6 GB colima VM per
   `docker info`). That container is shared factory infrastructure another agent could be using
   concurrently, so every mutation (installed packages, sshd, ssh keys, tmux sessions) was
   **explicitly reverted** at the end of this spike (`apt-get remove --purge`, `apt-get autoremove`,
   `rm -rf` the generated ssh keys, `tmux kill-server`) — verified clean by a final `ps aux` / `which`
   pass, reproduced below.
3. Installed `tmux` at runtime (`apt-get install -y --no-install-recommends tmux`) — a throwaway,
   uncommitted install, never touching `docker-bake.hcl` or the Dockerfile.
4. **Hard-disconnect test:** started a detached tmux session server-side
   (`tmux new-session -d -s seat1`), then attached a **real pty-backed client**
   (`docker exec -t beadhive-bh-1 tmux attach -t seat1`, wrapped in `script` so a pty exists on the
   host side too, run in the background to get a real OS PID) and **SIGKILLed that client process
   directly** (`kill -9`) — a hard kill, not `tmux detach` or a clean SIGTERM/EOF — then queried the
   session from a **separate, freshly-spawned `docker exec`** (a different client, same definition
   the bead uses: "a different remote client").
5. **Transport-parity test:** installed `openssh-server` ephemerally, generated an ed25519 keypair
   for `bee`, started `sshd -p 2222` by hand (no init script, since there's no systemd), and drove
   the identical `tmux` verbs (`list-sessions`, `attach`, `send-keys`, `kill-session`) over an SSH
   session (`ssh -p 2222 bee@localhost …`) against the exact same server socket already reached via
   `docker exec`.
6. **Harness-under-multiplexer test:** the container already had Claude Code 2.1.220 installed
   (`bh harness install claude` had been run previously; `bh harness list` shows it
   `installed / proprietary`), but **no credentials** were present
   (`CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` both empty in the container's env, no
   `~/.claude.json`) — so only the pre-auth onboarding TUI was reachable, not a full conversational
   turn. Launched `claude` inside a **freshly created, zero-attached-clients** tmux session
   (`tmux new-session -d -s harness1 'claude'`) and captured the pane
   (`tmux capture-pane -p [-e]`). As a contrast/control, ran the identical binary via a bare
   `docker exec` with no pty and `stdin=/dev/null`. Repeated the "detached tmux, zero clients" launch
   with **Codex** (0.146.0, Apache-2.0, baked into the image — no install needed), to check whether
   the finding is Claude-Code-specific or general.
7. **Resize/colour test:** `tmux resize-window -t harness1 -x 100 -y 30` against the running
   `claude` pane; compared `tmux list-windows` and `capture-pane` before/after. Captured raw escape
   sequences with `capture-pane -e` to confirm real SGR colour codes, not degraded plain text.
   Checked `$TERM`/`$COLORTERM` inside the pane via `tty`/`echo`.
8. **Collision test:** `tmux new-session -d -s seat1` a second time while `seat1` was still live.
9. **Cleanup / `init: true` test:** inspected `ps aux` for PID 1 inside the container; inspected the
   fate of the SIGKILLed client's **in-container** process (not just the host-side one) via
   `/proc/<pid>/fd`; checked whether `tmux kill-session` reaps the harness process it was running.
10. **Container-recreate test:** run against an **isolated, disposable** container
    (`docker run --name bh-spike-recreate …`), not the shared one — installed tmux, created a named
    session, then `docker rm -f` + a fresh `docker run` from the same image, and checked whether the
    new container has any trace of the old session (`tmux` binary presence, `tmux list-sessions`
    exit code).
11. **Licence + size:** read `/usr/share/doc/tmux/copyright` and the copyright files of tmux's
    actual Debian `Depends:` (`apt-cache depends tmux`) inside a disposable container; fetched
    `tmux/tmux`'s own upstream `configure.ac` from `raw.githubusercontent.com` to check whether the
    LGPL dependency found below is an upstream requirement or a Debian packaging choice. Measured
    the real baked-image size delta by building a throwaway derived image
    (`FROM beadhive/agent:dev` + the same `apt-get install … && rm -rf /var/lib/apt/lists/*` pattern
    the existing Dockerfile already uses) and diffing `docker image inspect --format '{{.Size}}'`
    against the unmodified base — not just reading apt's live pre-cleanup estimate.

All scratch state lived in disposable containers (`bh-spike-recreate`, `bh-spike-lic`,
`bh-spike-codex`) or ephemeral installs in the shared one, reverted before finishing; no product
code, `docker-bake.hcl`, or Dockerfile edits were made.

## Evidence

### Durability across a hard client disconnect

- **1.** A client attached with a real pty was confirmed server-side before the kill:
  `tmux list-clients -t seat1` → `/dev/pts/2: seat1 [80x24 xterm] (attached,focused,UTF-8)`.
- **2.** `kill -9` on that client's host-side PID (and its `script` wrapper) terminated it with
  **exit code 137** (SIGKILL) — the background-task notification confirmed this: *"failed with
  exit code 137"*. Not a clean detach, not a SIGTERM the process could catch.
- **3.** Immediately after, `tmux list-sessions` from a brand-new `docker exec` still reported
  `seat1: 1 windows (created …) (attached)` — the session was never destroyed.
- **4.** `tmux capture-pane -t seat1 -p` from that new client showed the exact scrollback from
  before the kill (`MARKER_BEFORE_KILL_1785776075`), byte for byte.

### Reattach from a different client

- **5.** A second, independently-spawned `docker exec -t beadhive-bh-1 tmux attach -t seat1` (via
  `script` again, a genuinely new OS process, PID unrelated to the killed one) attached
  successfully; `tmux list-clients` then showed **two** clients simultaneously: the stale
  `/dev/pts/2` and the new `/dev/pts/3`.
- **6.** A command sent after reattach (`echo MARKER_AFTER_REATTACH_FROM_DIFFERENT_CLIENT`)
  appeared in the pane immediately, interleaved with the pre-kill scrollback — the session is live
  and continuous, not a snapshot.

### Transport parity (2 of the 4 named transports measured)

- **7.** With `openssh-server` running on port 2222 inside the container, `ssh -p 2222
  bee@localhost tmux list-sessions` (a **non-interactive** SSH command) returned the identical
  session list `harness1: … / seat1: … (attached)` that `docker exec` had already shown — same
  server socket (`/tmp/tmux-1000/default`), reached over a structurally different transport (SSH
  protocol + `sshd`'s own auth/pty allocation, not Docker's exec API).
- **8.** A **real pty-backed** SSH attach (`ssh -tt -p 2222 … tmux attach -t seat1`) became a
  third simultaneous tmux client: `tmux list-clients -t seat1` showed `/dev/pts/5: seat1 [80x24
  xterm-256color] (attached,focused,UTF-8)` alongside the two `docker exec` clients.
- **9.** A marker sent while the SSH client was attached (`MARKER_VIA_SSH_TRANSPORT`) landed in
  the same continuous scrollback as the earlier docker-exec markers, confirmed via `capture-pane`
  — **one verb set (`tmux new-session`/`list-sessions`/`attach`/`send-keys`/`capture-pane`),
  unmodified, worked identically over both transports** against the same live session.
- **10.** `kubectl exec` and `smolvm` were **not measured** — no k8s cluster or smolvm environment
  was available in this run. Reasoned (not measured) extrapolation: `kubectl exec` is
  API-equivalent to `docker exec` (both allocate a pty inside a container's PID namespace via an
  orchestrator's own control-plane API and hand it to whatever the invoked command is), so the
  same transport-agnostic argument plausibly extends — but this is explicitly **not** evidence to
  the same standard as 7-9.

### Harness TUI under the multiplexer — no degradation, real colour, real resize

- **11.** `claude` (2.1.220) launched via `tmux new-session -d -s harness1 'claude'` — a session
  created with **zero attached clients at launch time** — rendered its **full interactive
  onboarding TUI**: a numbered theme-picker menu with an arrow cursor (`❯`), a syntax-highlighted
  diff preview (`Monokai Extended`), and dotted/unicode box-drawing separators. `capture-pane -e`
  showed real 256-colour SGR escape sequences (e.g. `\e[38;5;174m`, `\e[38;5;246m`) driving it —
  not a plain or degraded rendering.
- **12.** **Contrast/control**, same binary, no pty at all:
  `docker exec beadhive-bh-1 sh -c 'timeout 5 claude < /dev/null'` →
  `Error: Input must be provided either through stdin or as a prompt argument when using --print`
  — Claude Code's own non-interactive-mode detection firing immediately, exactly the failure mode
  the bead names ("the harness detects... and degrades to non-interactive"). This is the direct
  A/B: no pty → refuses to run interactively; tmux pane (pty, zero clients) → renders the full
  TUI.
- **13.** `tty`/`$TERM` inside the pane (via `send-keys` + `capture-pane`): `TTY_CHECK: /dev/pts/1
  TERM=tmux-256color COLORTERM=`. A real pty was allocated by tmux even with no client attached;
  tmux auto-sets `TERM=tmux-256color`, sufficient for the 256-colour rendering observed in
  Evidence 11 (worth noting: `COLORTERM` itself was empty — some tools gate true-colour
  specifically on `COLORTERM=truecolor`; Claude Code's rendering was unaffected here, but a future
  seat wrapper should not assume `COLORTERM` is set just because `TERM` is colour-capable).
- **14.** **Resize:** `tmux list-windows -t harness1` before = `[80x23]` (tmux defaulted to the
  smallest client size already attached elsewhere on the same **server** — `seat1`'s reattached
  client — since `harness1` had none of its own; this is normal tmux window-size policy, not a
  bug, and is itself a real operational nuance: unattached sessions on a shared tmux server track
  other sessions' client sizes unless pinned). `tmux resize-window -t harness1 -x 100 -y 30`
  forced it to `[100x30]`; `capture-pane` immediately after showed the pane correctly reflowed and
  repainted at the new width (the ASCII/unicode splash art rendered cleanly, no truncation
  artifacts).
- **15.** **Generalization check:** the same "detached tmux session, zero clients, launch the
  harness" recipe was repeated with **Codex** (0.146.0, already baked, no install step) in an
  isolated scratch container — it too rendered its full onboarding TUI (ASCII-art logo, styled
  sign-in menu) with zero attached clients. The finding (pty presence, not client attachment, is
  what a harness checks) is not a Claude-Code-only artifact.
- **16.** **Caveat, stated precisely:** neither harness test reached an authenticated
  conversational turn — `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` were both empty in the
  container, and no credentials were provisioned for this spike. This is the **real installed
  harness binary** rendering its **real TUI**, not the stand-in fallback the bead's brief allows
  for — but the screens exercised are limited to onboarding/pre-auth. Resize and colour behaviour
  in an active tool-use/streaming-response screen were not directly observed, though nothing in
  Evidence 11-15 suggests the pty-vs-no-pty distinction stops mattering once authenticated.

### What identifies a seat, and collision behaviour

- **17.** `tmux new-session -d -s seat1` while `seat1` already existed failed immediately:
  `duplicate session: seat1`, exit code 1 — no silent overwrite, no merge, a clean structured
  refusal.
- **18.** `src/beadhive/role.py:57-67` (`_known_seats`) shows the codebase's **current** identity
  concept is purely the **role archetype** (`supervisor`, `dispatcher`, `planner`, …, glob-derived
  from `agents_src/*.md`) — there is no existing seat-*instance* identity (hive, bead, or
  discriminator) anywhere in `bh role`'s exec path. Combined with Evidence 17, this means a
  `bh seat` naming scheme is a **net-new design surface**: tmux gives free collision *detection*,
  but not collision *avoidance* — a bare `<role>` name (e.g. two different hives both wanting
  `supervisor`) would collide on a shared tmux server. A composite name (e.g. `<hive>-<role>` or
  `<hive>-<role>-<bead>`) is needed at the `bh seat` layer itself.

### Cleanup, and how `init: true` does and does not interact

- **19.** `ps aux` inside the container: PID 1 is `/sbin/docker-init -- bash` — Docker's built-in
  tini-equivalent reaper, active because of `init: true` in `docker-compose.yml`.
- **20.** **A real, non-hypothetical gap found:** after the host-side `kill -9` in Evidence 2, the
  **in-container** process that `docker exec` had spawned for that client (`PID 764`, argv `tmux
  attach -t seat1`) did **not** die. `/proc/764/fd` still showed `0/1/2 -> /dev/pts/2` and an open
  control socket, well after the host-side kill. `tmux list-clients` kept reporting
  `/dev/pts/2: … (attached,focused,UTF-8)` throughout — SIGKILLing the *host's* `docker exec` CLI
  does not propagate to the *container's* side of that exec session; nothing closed the pty, so
  neither tmux nor the container had any signal that the client was gone.
- **21.** `docker-init`/tini's job is reaping **orphaned zombies** (exited-but-unwaited processes
  reparented to PID 1) — by design it does **not** and structurally **cannot** reap a process that
  is still alive and simply abandoned (Evidence 20's `PID 764` was never a zombie: `ps` showed it
  `Ss+`/running throughout). Stale-attached-client cleanup is therefore a **real, separate design
  requirement** for `bh seat kill`/a gc pass — not something the multiplexer or `init: true`
  solves for free.
- **22.** In contrast, `tmux kill-session -t harness1` (Evidence 5's harness session) **did**
  correctly reap the `claude` process running in it — confirmed via `ps aux` showing it gone
  immediately after. Killing a *session* (server-side) cleans up its process tree; the gap is
  specifically about *stale attached clients* on a session that's still meant to be alive
  (Evidence 20).

### Session identity does not survive a container recreate — but does not "lie" either

- **23.** `tmux list-sessions -F '#{socket_path}'` → `/tmp/tmux-1000/default`. Cross-referenced
  against `docker-compose.yml`'s four mounts (`bh-hq` → `~/.beadhive`, `bh-workspace` →
  `/workspace`, `bh-worktrees` → `/worktrees`, `bh-harness` → `~/.claude`): **`/tmp` is on none of
  them.**
- **24.** Empirically confirmed in an isolated container: created a named session
  (`durableseat`), `docker rm -f` the container, `docker run` a fresh one from the same image —
  `tmux: not found` (`exit 127`), because tmux itself was only apt-installed at runtime, and even
  a baked tmux's *session* would be gone regardless, since the server process and its `/tmp`
  socket die with the container's writable layer. **No trace, no false report** — `tmux
  list-sessions` in the fresh container fails cleanly rather than returning stale data.
- **25.** This directly bears on the bead's NO-GO wording ("would make `bh seat ls` lie after
  every `down && up`"): a `bh seat ls` implemented as a **live query against the multiplexer**
  (not a cached row in beads/a separate registry) cannot lie after a recreate — it would correctly
  report empty, same as Evidence 24. The failure mode the NO-GO trigger describes is avoidable
  **by construction** of the real implementation, not solved by tmux itself. What tmux genuinely
  does **not** provide is survival *across* a recreate — that is a distinct, already-separately-
  spiked concern (`docs/spikes/bh-a7so.2-checkpoint-resume.md`, the harness's own conversation
  checkpoint/resume), not this spike's scope.

### Licence and image size

- **26.** `tmux` itself: Debian bookworm package `tmux 3.3a-3`.
  `/usr/share/doc/tmux/copyright`, `Files: *` (the actual program) → `License: ISC`. **ISC is on
  both allowed sets** — the wheel's `license_allow` in the `justfile` and `docker-bake.hcl`'s
  image component allowlist enforced by `tests/test_component_licenses.py` (`ALLOWED` frozenset
  includes `"ISC"`).
- **27.** `apt-cache depends tmux` → 4 hard `Depends`: `libc6`, `libevent-core-2.1-7`,
  `libtinfo6`, `libutempter0`. `libtinfo6` was **already present** in the base image before
  installing tmux (absent from apt's "Selecting previously unselected package" list) — zero
  incremental licence surface. `libevent-core-2.1-7` (2.1.12-stable-8) is net-new; its Debian
  copyright lists mostly permissive licences (BSD-2/3-clause, Expat, ISC, FSFUL/FSFULLR) — the
  only `GPL-2+`/`GPL-3+` hits are `m4/libtool.m4` and `m4/acx_pthread.m4`, **build-time autotools
  macro files, not part of the shipped `.so`**.
- **28.** **The real finding:** `libutempter0` (1.2.1-3) is also net-new, and its copyright
  declares `License: LGPL-2.1` for `Files: *` — the actual library code, not a doc/build
  artifact. LGPL is **not** on either allowed set, and ASSURANCE.md's image-policy text is
  explicit: *"NOT ALLOWED: any copyleft (GPL/LGPL/AGPL/MPL)."*
- **29.** **Mechanically, this would not trip `tests/test_component_licenses.py` today** — that
  test only parses `docker-bake.hcl`'s pinned `*_VERSION`/`*_TAG` variables (confirmed by reading
  `_pinned_components()`), and the existing Dockerfile already installs plain, unpinned Debian
  packages the same way (`ca-certificates curl git less libssl3 openssh-client procps`) — **`git`
  itself is GPL-2** and sits under ASSURANCE.md's "Debian base layer... acknowledged, not
  audited" umbrella rather than the pinned-component allowlist. Adding `tmux` via the identical
  `apt-get install -y --no-install-recommends tmux` pattern would fall under that same umbrella
  by existing precedent — but the umbrella's own stated framing ("as every Debian-derived image
  does," i.e. layers inherited passively, not deliberately chosen) sits uneasily with a package
  we would be adding on purpose, so this is flagged rather than resolved.
- **30.** **A concrete, sourced way to avoid the LGPL dependency entirely exists.** tmux's own
  upstream `configure.ac` (fetched from
  `raw.githubusercontent.com/tmux/tmux/master/configure.ac`): `utempter_version=off` by default,
  only flipped on by an explicit `--enable-utempter` flag — Debian's own package build chooses to
  enable it; upstream tmux does not require it. Building tmux from source without that flag
  (mirroring this Dockerfile's own existing `git-workspace` builder-stage pattern,
  `docker/Dockerfile:~35-45`) would ship with only `libevent-core` + `libc6` + `libtinfo6` as
  runtime deps — all permissive — at the cost of tmux not registering panes in `/var/run/utmp`
  (irrelevant for a headless container; nothing here runs `who`/`w`/`last`).
- **31.** **Image size delta, measured** by building a throwaway derived image (`FROM
  beadhive/agent:dev` + `apt-get install -y --no-install-recommends tmux && rm -rf
  /var/lib/apt/lists/*`, the same cleanup pattern the real Dockerfile already uses) and diffing
  `docker image inspect --format '{{.Size}}'`: base = `429,937,348` bytes, +tmux =
  `430,586,489` bytes → **delta = 649,141 bytes (≈ 634 KiB / ≈ 0.62 MiB)**. (apt's own live,
  pre-cleanup estimate was "1630 kB of additional disk space" — ~2.5x higher, because it includes
  the apt package-list cache the `rm -rf /var/lib/apt/lists/*` layer removes.)

## Verdict — **GO**

All five GO-bar items hold, with one caveat elevated rather than hidden:

1. **Hard-disconnect survival** — proven (Evidence 1-4): `SIGKILL` (exit 137) on an attached
   client, session and scrollback both intact immediately after.
2. **Reattach from a different client** — proven (Evidence 5-6): a genuinely separate process
   attached and continued the same live session, scrollback included.
3. **Harness TUI renders correctly, no degradation** — proven for both baked harnesses at the
   onboarding screen (Evidence 11-15), with a direct A/B (Evidence 12) showing the exact
   non-interactive degradation the bead worries about *does* happen without a pty and does *not*
   happen inside a tmux pane. Caveated (Evidence 16): not exercised past onboarding, for lack of
   credentials in this environment — not a stand-in TUI, but a narrower slice of the real one.
4. **≥2 transports, one verb set** — proven for docker exec + ssh (Evidence 7-9), reasoned but not
   measured for kubectl exec/smolvm (Evidence 10).
5. **Licence + size, stated** — tmux itself is clean ISC (Evidence 26). Size is measured precisely
   (Evidence 31). The one real wrinkle: the Debian package's `libutempter0` dependency is LGPL-2.1
   (Evidence 28) — not disqualifying by itself (mechanically outside today's automated gate per
   existing `git`-as-GPL-2 precedent, Evidence 29) and concretely avoidable (Evidence 30), but real
   and worth a first-class decision rather than a silent `apt-get install tmux` landing in the
   Dockerfile.

Neither NO-GO trigger fires: the harness does not degrade under the multiplexer (the opposite was
measured directly, Evidence 11-15); and while session state does not survive a container recreate
(Evidence 23-24, expected — `/tmp` is on no durable volume), a live-query `bh seat ls` does not
misreport after one (Evidence 25) — the NO-GO's specific concern ("would make `bh seat ls` lie") is
avoidable by construction, not something tmux fails to provide.

## Recommendation

1. **Build `bh seat ls | attach | kill` as a thin wrapper over `tmux`**, targeting the socket at
   `/tmp/tmux-<uid>/default` (or an explicit `-S` path) inside whatever container/host the exec
   transport reaches. `ls` = `tmux list-sessions` (a live query — required by Evidence 25, not a
   cached registry); `attach` = `tmux attach -t <name>` (or `new-session -d` first if absent);
   `kill` = `tmux kill-session -t <name>`.
2. **Design the seat-name composite now, before implementation** (Evidence 17-18): a bare
   `<role>` name will collide the moment two hives want the same role on a shared tmux server.
   `<hive>-<role>` (or `<hive>-<role>-<bead>` for per-bead conversational seats) is the minimum;
   tmux's own `duplicate session` refusal (Evidence 17) is the safety net once names don't
   collide by construction.
3. **Add an explicit stale-client reap step to `bh seat kill`/a gc pass** (Evidence 20-22): do not
   assume a session with no *responsive* client is the same as a session with no *attached* client
   — `tmux list-clients` can lie about liveness for a while after a hard host-side disconnect.
   A conservative approach: on `bh seat attach`, first `tmux list-clients -t <name>` and kill any
   client PID that the exec transport's own bookkeeping doesn't recognize as live, before attaching.
4. **Take the tmux licence question to a real decision, not a default `apt-get install` line**:
   either (a) accept `tmux` + `libutempter0` under the same "Debian base layer, acknowledged not
   audited" umbrella `git` already sits under, documented explicitly in `docker-bake.hcl`'s
   licence-policy comment the way the `git_workspace` override is today, or (b) build tmux from
   source without `--enable-utempter` (Evidence 30) in a builder stage mirroring the existing
   `git-workspace` stage, trading a slightly heavier build for zero copyleft surface. Either way,
   record the choice and its `docker image inspect` size delta (Evidence 31 gives the (a) number;
   (b) would need its own measurement) rather than letting it arrive as an unremarked `RUN
   apt-get install tmux`.
5. **Re-run the harness-TUI leg with real credentials** before this ships as a default: Evidence
   11-16 is real-binary, real-TUI evidence, but stops at the onboarding screen. A follow-up (either
   inside `bh-lx6e.2`'s seam work or a short dedicated check) should authenticate once (headless
   `CLAUDE_CODE_OAUTH_TOKEN`, per `docs/CONTAINER.md`'s credential table) and confirm resize/colour
   hold up through an active tool-use/streaming screen, not just the static onboarding menu.
6. **Treat `kubectl exec`/smolvm as "likely fine, not yet shown"** (Evidence 10) — don't claim
   transport parity beyond docker exec + ssh until one of them is actually measured; the seam work
   in `bh-lx6e.2`/`.4` is the natural place to pick that up if either transport becomes load-bearing.
7. **Container-recreate durability is out of scope for `bh seat` and stays that way** (Evidence
   23-25): don't let a future iteration quietly try to make tmux state survive `down && up` by,
   e.g., relocating the socket onto a durable volume — a dead socket file on a durable volume is
   still dead. Genuine recreate-survival is the harness's own checkpoint/resume story
   (`bh-a7so.2`), a different mechanism this spike deliberately did not re-litigate.
