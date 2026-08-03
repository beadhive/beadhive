# Spike `bh-00cq` — does an external dolt sql-server (>= 2.2.0) resolve GH#4770 end-to-end?

**Bead:** `bh-00cq` · **Seat:** `dev/dev1` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-ukit.6` — adopt external/shared-server mode fleet-wide, so the
Brewfile can pin a released `beads` instead of `HEAD`

## Question

With stable `bd 1.1.2` (whose statically-linked embedded dolt engine predates the v2.2.0
git-transport fix for upstream #4770) pointed at an **external** `dolt sql-server` running
dolt >= 2.2.0, does a real cold `bd dolt pull`-class sync against the production
`git+ssh://git@github.com/beadhive/beadhive.git` remote **complete**, working past the
GH#2455 pre-pull dirty-config guard that left `bh-p24m`/prior `bh-00cq` Test B inconclusive?
Critically NOT asking: whether this bead should change any bh code, config, or the
Brewfile — those are explicitly out of scope here (see Recommendation).

## Method

Everything ran in an isolated scratch dir
(`/private/tmp/.../scratchpad/00cq/`), never touching `~/.beadhive`, any registered hive's
`.beads/`, or the machine-linked `bd` (`HEAD-af076b6`, left untouched throughout).

1. Downloaded `beads_1.1.2_darwin_arm64.tar.gz` from the `gastownhall/beads` v1.1.2 GitHub
   release, checksum-verified against `checksums.txt`, invoked by absolute path
   (`bd-1.1.2-dl/bd`) — same stable client Test A/B used.
2. Verified port 3399 was free, then started a standalone
   `dolt sql-server -H 127.0.0.1 -P 3399 --data-dir <scratch>/dolt-server-data` using the
   already-installed `dolt 2.2.2` (brew, untouched).
3. `bd init --server --external --server-host 127.0.0.1 --server-port 3399 --prefix scr00cq
   --non-interactive` — reproduced Test B's starting state (mode: server, `config` table
   dirty with `issue_prefix`).
4. `bd dolt remote add origin git+ssh://git@github.com/beadhive/beadhive.git`, then
   `bd dolt pull origin` — reproduced Test B's exact GH#2455 refusal, and confirmed
   `bd dolt commit` is the false-success no-op described in upstream
   `gastownhall/beads#5111`/`#4934` (both open, unpatched as of 2026-08-02).
5. Searched upstream (`gh issue view` / `gh search issues`) for the guard's cited "GH#2455"
   — it does **not** resolve to an issue in `gastownhall/beads`; treat it as bd's internal
   numbering, not a public tracker link (issue #5111's author independently noted the same).
6. Applied #5111's documented raw-SQL workaround (`bd sql "CALL DOLT_ADD('-A')"` +
   `bd sql "CALL DOLT_COMMIT('-m', ...)"`), confirmed the working set went clean, and
   re-ran `bd dolt pull origin` — this is the part Test B never reached.
7. That retry hit "no common ancestor" — a self-inflicted artifact of testing methodology
   (a plain `bd init` seeds its own disjoint local history, so an incremental pull can never
   reconcile with a remote it was never cloned from), not a #4770/GH#2455 symptom. bd's own
   error output names the fix: use `bd bootstrap` / `bd init --remote` for a first sync.
8. Re-ran cleanly in a fresh sibling workspace: `bd init --server --external ... --prefix
   scr00cq2 --remote git+ssh://git@github.com/beadhive/beadhive.git`, timed with `date +%s`
   brackets and `time`. Confirmed via `gastownhall/beads` source
   (`cmd/bd/bootstrap.go:cloneViaServer`) that this path opens a SQL connection to the
   external server and executes `CALL DOLT_CLONE(...)` — the same
   `internal/storage/versioncontrolops` SQL-driven family as the `CALL DOLT_FETCH(...)`
   that `bd dolt pull` issues, per this bead's own DESIGN. So the transfer this exercises is
   the same workload class (bulk cold-cache retrieval of the remote's dolt history over git
   transport, performed by the connected engine), just via the clone entry point rather than
   the incremental-pull entry point.
9. Tore down the dolt sql-server (`kill`) and confirmed port 3399 released; confirmed the
   real hive (`bh bd dolt status`) still reports `embedded` and the machine `bd` is still
   `HEAD-af076b6`.

## Evidence

1. **GH#2455 reproduces exactly as Test B found it, and has a real (if unsanctioned)
   workaround.** Fresh `bd init --server --external` leaves `config` dirty
   (`issue_prefix`); `bd dolt pull` refuses with the same
   `refusing to auto-commit 1 dirty internal config key(s) ... (GH#2455)` text; `bd dolt
   commit` prints `Committed.` while `SELECT * FROM dolt_status` still shows `config:
   modified` afterward — confirmed as a known, open, unpatched upstream bug
   (`gastownhall/beads#4934`, `#5111`), not something specific to this environment. The
   documented workaround from `#5111` — `bd sql "CALL DOLT_ADD('-A')"` then
   `bd sql "CALL DOLT_COMMIT('-m', ...)"` — cleared the dirty state (`dolt_status` returned
   `(0 rows)`) and let a subsequent `bd dolt pull` proceed past the guard into the actual
   fetch attempt.

2. **The GH#2455-cited issue number does not exist in the beads GitHub repo.** `gh issue
   view 2455 --repo gastownhall/beads` returns "Could not resolve to an issue" — an operator
   following the guard's own error text to look up context will not find it. Note this
   plainly; do not assume it points anywhere public.

3. **A real, cold, end-to-end transfer through the external dolt >= 2.2.0 engine
   completed — no hang.** `bd init --server --external ... --remote
   git+ssh://git@github.com/beadhive/beadhive.git` against the fresh `scr00cq2` database
   printed `Synced database from git+ssh://git@github.com/beadhive/beadhive.git (via server
   at 127.0.0.1:3399)` and exited 0 in **10 seconds** wall-clock (`date +%s` bracket:
   1785718755 → 1785718765; `time` built-in: `10.128s total`). The server-side database
   landed at **306 MB** on disk (`du -sh dolt-server-data/scr00cq2`), with dolt chunk-file
   timestamps (17:59:24–25) confirming the transfer window. This directly falsifies the
   #4770 hang for stable bd 1.1.2 when its dolt work is routed through an external >= 2.2.0
   engine, and lines up with Test C's dolt-layer 11s finding — but this time exercised
   *through* bd's own server-mode SQL path (`CALL DOLT_CLONE`, confirmed by source
   inspection — see Method 8), not by bypassing bd with the standalone `dolt` CLI.

4. **A distinct, orthogonal failure appears immediately after the successful transfer:
   schema-version drift.** Opening the freshly-cloned `scr00cq2` store failed with
   `Error: failed to open Dolt store: schema version mismatch: database is at v59, binary
   knows up to v53 (6 migrations ahead)`. This is expected and unrelated to #4770/GH#2455:
   this hive's live data was written under continuous `bd HEAD` development (the same
   `HEAD-af076b6` this experiment was constrained to leave untouched), so its schema has
   advanced 6 migrations past what the frozen 2026-07-26 stable release understands.
   `--ignore-schema-skew` did not recover a working session — the failed init left the
   workspace's mode wiring incomplete (a stray `.beads/embeddeddolt` appeared alongside the
   configured external `scr00cq2`, and `bd dolt status` afterward reported `embedded` with
   "Data directory does not exist," i.e. the partial-init failure state is itself a little
   confusing, though not something this spike needed to resolve further).

5. **Read-only discipline held throughout.** Only `git ls-remote` (read) and two `bd
   init --remote`/`bd dolt pull` (read/clone) operations touched the real remote; `bd dolt
   push` was never invoked. The real hive's `bh bd dolt status` reported `embedded` before
   and after: `Data: /Users/brian/workspace/github/beadhive/beadhive/.beads/embeddeddolt`.
   Machine `bd --version` reported `HEAD-af076b6 (Homebrew)` before and after.

## Verdict — **GO, with two caveats that change the adoption plan**

The core hypothesis holds: routing `bd`'s dolt work through an external `dolt sql-server`
>= 2.2.0 does put the fixed git-transport code in the pull/clone path, and a real,
cold, 306 MB, end-to-end transfer against the actual production remote completed in 10
seconds where embedded-mode stable bd (Test A) hung past 240s. GH#2455 is real but is not a
hard blocker — it has a working, upstream-documented SQL-level escape hatch.

**Caveat A — GH#2455's only fix is an unsanctioned raw-SQL workaround, upstream and open.**
No bd-native command (`bd dolt commit`, `bd vc commit`, `bd doctor`'s own remediation text)
actually clears a dirty internal config key today; the fix is two `bd sql` calls that
bypass bd's storage layer (`bd sql --help`'s own warning). `#4934`/`#5111` are both open,
unpatched. Any adoption plan needs either upstream's fix to land, or bh to bake this
workaround into its own pre-pull tooling for freshly-bootstrapped external-mode hives.

**Caveat B (new finding, not anticipated by this bead's DESIGN) — dolt-engine currency and
bd-schema currency are two separate problems, and adopting external mode only fixes the
first.** A stable bd client can clone the bytes fine through a modern external engine, but
still refuses to *open* a store whose schema was advanced by a newer bd (as this very hive's
schema now is, at v59, produced by the `bd HEAD` this bead was constrained not to touch).
The Brewfile's stated end goal — "decouple the dolt version from bd's release cadence" so a
**released, stable** bd can be pinned — needs the stable release to also be schema-current,
not just dolt-engine-current. External mode alone does not buy that.

## Addendum — steady-state read latency (measured after this doc's first draft)

Everything above concerns **transport**: whether a cold bulk transfer completes. A separate
controlled A/B, run once this spike had closed, measured **steady-state read latency**, and it
is a second, independent argument for the move — one this bead never set out to test.

Method: the same remote cloned twice in one session (embedded 300 MB, external server 308 MB), so
store contents are controlled — unlike the earlier per-hive baseline in `bh-ukit.1`, which
compared five different hives and therefore confounded mode with store size. `bd HEAD-af076b6`
on both sides (schema-current, so Caveat B never applies), dolt 2.2.2 server, read-only verbs,
3 warmups + 10 timed runs.

| verb | embedded (min / med) | ext-server (min / med) | delta (median) |
|---|---|---|---|
| `bd ready --json` | 225 / 250 ms | 106 / **118 ms** | −133 ms, −53% |
| `bd list --status=open --json` | 292 / 300 ms | 113 / **139 ms** | −161 ms, −54% |
| `bd stats` | 159 / 167 ms | 85 / **87 ms** | −80 ms, −48% |
| `bd show <id> --json` | 251 / 269 ms | 78 / **84 ms** | −185 ms, −69% |

**Isolating the addressable portion.** `bd --version` (process spawn, no DB opened) is 63 ms.
Subtracting it leaves engine-open + query: **187 ms embedded → 55 ms server, a 71% cut.** That is
the number a mode decision should quote, not the raw wall-clock — the 63 ms bd spawn is unchanged
by either mode, and so is bh's own ~140 ms Python startup. Neither is something a server fixes.

Bootstrap cost is a wash: 11 s embedded vs 9 s external for the same payload, consistent with the
10 s figure in Evidence above. Server mode costs nothing on first sync.

**Two caveats, both material:**

- **This is the least favourable case for server mode, not the best.** The store measured was
  300 MB — roughly homelab-sized. The `bh-ukit.1` baseline showed embedded latency tracks store
  size (344 MB → 273 ms; 1677 MB → 1129 ms), while a server holds the store open and should be
  far flatter in size. If that holds, HQ — the largest store *and* the slowest on every verb —
  gains most. **That is an inference from the two runs, not a measurement.** Measure it directly
  before `bh-ukit.4` decides.
- **Concurrency is entirely unmeasured.** 19 embedded engines versus one server under
  simultaneous load is where the fleet-wide resource argument actually lives, and nothing here
  touches it. Incidental evidence suggests it may dominate single-process latency outright: during
  this work, `bh plan repair`, `bh plan verify`, and `bd label list-all` each stalled past 120 s
  purely because a second bd process held the embedded store's exclusive lock.

Method note for anyone repeating this: `--dolt-server-port` is not a `bd init` flag. The correct
form is `bd init --server --external --server-host H --server-port P`.

## Recommendation

- **Latency is now a second independent reason to move, alongside #4770.** The transport argument
  (this doc's original finding) says embedded is *broken* on large cold pulls; the latency
  addendum says it is also *slower* in ordinary steady-state use, by roughly half on identical
  data. Either alone would justify the evaluation; together they make the direction clear. The
  caveats above bound the claim — they do not undercut it.
- **`bh-ukit.6` (fleet-wide adoption) can proceed on the strength of this evidence, but its
  scope must grow to cover Caveats A and B, not just the dolt-engine coupling it currently
  frames.** Concretely: (1) either wait for `gastownhall/beads#4934`/`#5111` to land, or add
  the `CALL DOLT_ADD('-A')` / `CALL DOLT_COMMIT(...)` sequence as a pre-pull step in bh's own
  tooling for newly-bootstrapped external-mode hives; (2) treat "pin a released stable bd"
  as gated on that release also being schema-current with whatever `bd HEAD` has already
  written to hives under active development — not just gated on dolt >= 2.2.0.
- **Since acted on (2026-08-03):** `bh-ukit.6` was replanned to carry the migration alone, and
  Caveat B's "pin a released stable bd" goal was split into its own bead, gated on a release that
  is both dolt-current and schema-current. `bh-wnly` covers making the schema skew a preflight
  check rather than an open-time failure.
- **This bead makes no code or config change.** Per its DESIGN's release-gate contract, the
  deliverable is this evidence; the Brewfile `args: ["HEAD"]` pin stays, and `bh-ukit.6`
  remains the (still-blocked) place to act on it.
- **Do not re-run this spike's Test B repro to "prove" the guard is fixed** — it isn't;
  the workaround is a bypass, not a fix, and should not be presented as sanctioned
  bd-supported behavior in any docs bh ships.
