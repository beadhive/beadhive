# Spike `bh-eybn7` — can a hermetic fixture simulate frames, one hive remote and HQ, with faults?

**Bead:** `bh-eybn7` · **Seat:** `dev/fixture` · **Type:** spike (test-only fixture, no product
code)
**Feeds decision on:** the writer-fencing molecule `bh-qlgmm` — `bh-vje85` (in-data epoch
fencing), `bh-sieai` (write guard and non-primary writes) and `bh-jbb6r` (end-to-end plus at
least 500 randomized interleavings) all reuse this fixture. Proposal:
[hive-writer-partitioning-proposal.md](../design/hive-writer-partitioning-proposal.md)
(see "Pre-kickoff probe evidence" and "Validation approach").

## Question

Can a deterministic, hermetic test fixture simulate N frames (each with its own Dolt replica or
shared-server sandbox), one shared hive remote and an HQ placement authority, with controllable
network partition, process kill, and barrier-ordered interleaving of push/adopt steps — driving
writes through the Dolt CLI **and** real bd 1.3 in both embedded and shared-server mode — at a
cost the integration gate can carry?

Critically NOT asking whether in-data epoch fencing, the guard triggers or the non-primary write
path are correct. Those are `bh-vje85` / `bh-sieai` / `bh-jbb6r` questions. This spike only has
to make their answers measurable.

## Method

1. Re-ran the leads in the planner's pre-kickoff research notes and probe scripts that the
   fixture depends on (the `git+file://` CAS path, bd's server mode, bd's join path). Everything
   ran in scratch directories with a private `HOME`, never the operator's shared server. Tools:
   bd `1.3.0 (f45b249ce)`, Dolt CLI `2.3.5`, git `2.55.0`.
2. Built the fixture from those results:
   [`tests/harness/writer_fencing.py`](../../tests/harness/writer_fencing.py) (cluster, frames,
   HQ, recorder) and
   [`tests/harness/writer_fencing_gate.py`](../../tests/harness/writer_fencing_gate.py) (the
   per-frame remote gate). No new conftest. Frame processes come from
   `harness.processes.process_context` (spawn).
3. Wrote one smoke test per capability in
   [`tests/test_writer_fencing_fixture_int.py`](../../tests/test_writer_fencing_fixture_int.py),
   marked `integration` and `dolt_server`. Ran them serially unfenced (`-n 0`), fenced and in
   parallel (`scripts/hermetic.sh … -n 4`), and inside the full `just test-integration-land`.
   For comparison, also ran the land gate with the new file deselected.
4. Benchmarked repeated interleavings on one cluster with a throwaway driver script (not
   committed). A 3-frame cluster (bd-embedded, bd-server, Dolt CLI) runs 6 rounds for each frame
   pair. Each round: rewind, two local writes, both frames parked before their CAS, release in
   random order.
5. Probed a local Dolt `remotesapi` remote as an alternative remote (Evidence 11).

### What the fixture is

| Requirement | Mechanism |
|---|---|
| One shared hive remote | A bare Git repo seeded with one commit on `main` (Dolt refuses a branchless Git remote). Dolt sees `git+file://…/remote.git`; Git sees `file://…/remote.git`. |
| N frames, own replica | `Cluster(root, [("a", BD_EMBEDDED), ("b", BD_SERVER), ("c", DOLT_CLI)])`. Each frame gets its own `HOME`, Git/Dolt identity and `PATH` shim. A bd frame is a Git clone plus `bd init`. A server frame also has its own `BEADS_SHARED_SERVER_DIR` and an ephemeral port. A CLI frame is a `dolt clone`. |
| Founding and joining | The first bd frame mints the store and pushes it. Every other frame joins the way a new host would: in parallel, `git clone` then `bd init [--shared-server]`, or `dolt clone`. |
| HQ placement authority | `HQ`: a flock-guarded JSON `(writer, epoch)` per hive. `place()` bumps the epoch and can CAS on `expected_epoch`. `partition(frame)` makes `placement(asker=frame)` raise `HQUnreachable`. `restore()` is the operator override. |
| Per-frame processes | One spawned agent per frame, `setsid` at start. Every frame command runs inside it, so `killpg` reaches bd, dolt and any parked gate below it. |
| Partition | `frame.partition()` / `heal()`. The frame's gate fails every remote Git call the way an unreachable host does. |
| Kill | `frame.kill()` SIGKILLs the agent's process group. For a server frame it also kills the `dolt sql-server` and any gate parked under it. It returns the pids and waits until they are dead. `restart()` starts a new agent and, for a server frame, runs `bd dolt start`. |
| Barriers | `hold = frame.hold(CAS)`, then `frame.push_async()`, then `hold.wait()`. The frame is now parked just before its remote compare-and-swap. `hold.release()` lets it go. The points are `CAS`, `FETCH`, `INFO` or any Git subcommand. `skip=n` parks the (n+1)th match. |
| Recorder | `Recorder(cluster)`. `with recorder.step("adopt", "b"):` records a snapshot before and after the step into a JSONL trace. Each snapshot has the remote `refs/dolt/data` head, the remote `main` hash and the remote `bh_writer` rows, HQ placement, and each frame's guard state. Guard state covers alive, busy, partitioned, HQ reachability and armed/held barriers, plus optional local tables or a custom `guard_probe`. |
| Repeated runs | `cp = cluster.checkpoint()` and `cluster.rewind(cp)`. Rewind moves the remote ref back, hard-resets every frame's `main`, heals, disarms and restarts frames. |

The gate is a `sh` shim named `git`, first on the frame's `PATH`. Local plumbing such as
`cat-file`, `update-ref` and `commit-tree` (dozens of calls per push) goes straight to the real
`git`. Only `fetch`, `push`, `pull`, `ls-remote`, `clone`, `fetch-pack` and `send-pack` go
through a stdlib-only Python gate. The gate logs the call, honours an armed hold (claimed by
atomic rename), checks the partition flag after any hold, and then `execv`s the real `git`.
Controls are plain files under the frame's `ctl/`, so no IPC is needed.

## Evidence

1. **bd joins an existing hive with `bd init`, not `bd bootstrap`, and only with a URL that has
   a scheme.**
   - In a clone whose `origin` has `refs/dolt/data`, `bd init [--shared-server] --prefix fx`
     clones the published store: the joiner lists the founder's issue and has the same
     `dolt_log`.
   - With a bare-path origin, bd refuses: `invalid remote URL: remote URL has no scheme`. Bare
     remotes must be added as `file://…`.
   - In shared-server mode, `bd bootstrap --yes` fails with `dolt server unreachable …
     connection refused`, because it does not start the server. This matches
     `storage_migrate.py`'s own note.
   - `bd dolt start --global` outside a workspace fails with `no active beads workspace found`.
   - `bd init --shared-server` does start the server. It costs about 9.5–10 s against about 2 s
     for an embedded join (`Cluster.timings`).
2. **All three drivers reach a `git+file://` remote by running `git` from `PATH`.**
   - A logging shim saw every Git call made by `dolt push`, by `bd dolt push` in embedded mode,
     and by the bd-managed `dolt sql-server` in server mode.
   - The server is the Dolt **CLI** binary (`~/.local/bin/dolt sql-server --config
     <server-dir>/dolt-server-config.yaml`), and it inherits the environment of the bd call that
     started it. So server-mode frames write through the CLI's Dolt (2.3.5). Only embedded
     frames run bd's linked Dolt.
   - The `test_cluster_starts…` test asserts that the server frame's gate log contains `cas`
     events. If a future build stops resolving `git` through `PATH`, the gate disappears and
     that test fails instead of passing silently.
   - One data push runs `fetch … +refs/dolt/data:…`, then
     `push --porcelain --force-with-lease=refs/dolt/data:<old> …` twice (the table file, then the
     manifest), then `push --force … __dolt_remote_info__`. Holding the first `cas` call parks a
     frame after it has read the remote and before it has changed anything there.
3. **Partition works per frame for every driver**
   (`test_partition_cuts_exactly_one_frame_from_the_remote`). Each frame is cut in turn:
   - Local writes still succeed.
   - `push` and `pull` fail with the gate's `writer-fencing fixture partition` message. bd
     surfaces it with its own credential hints.
   - The remote head does not move.
   - The next frame still pushes to the same remote.
   - After `heal()`, the cut frame pulls, pushes, and its write lands.

   Alternatives considered and rejected:
   - `chmod` on `remote.git` cuts every frame, because all frames run as the same uid.
   - Swapping the remote URL needs a different reconfiguration per driver: embedded config, the
     remote inside the server's database, and the CLI `repo_state`. It also changes the thing
     under test.
4. **Kill mid-push leaves the remote untouched, and the frame recovers**
   (`test_kill_mid_push…`). The test parks a bd-embedded frame, then a bd-server frame, at
   `cas` and kills it. For each:
   - Every victim pid is gone: the agent group, the parked gate, and for the server frame the
     `dolt sql-server`.
   - The in-flight call reports `killed`.
   - Remote `refs/dolt/data`, `main` and `issues` are byte-identical to before the push.
   - After `restart()`, the committed local write is still there and the push lands. The
     embedded store reopened after SIGKILL with no lock repair. `bd dolt start` restarted the
     server in about 0.6 s.
5. **Barriers make a true race deterministic**
   (`test_barriers_order_an_adopt_against_a_stale_write_both_ways`). Frame b adopts: HQ
   `place("b")` moves to epoch 2, and b updates `bh_writer` through the CLI. Frame a, the
   incumbent, makes a stale `bd create`. Both frames park at `cas` with the same remote head
   read; the snapshot shows both `held == ["cas"]`.
   - Releasing the adopt first lands `bh_writer = (b, 2)` and rejects the stale push
     (`non-fast-forward`).
   - Releasing the stale write first lands the stale issue with `bh_writer = (a, 1)` and
     rejects the adopt.
   - Both orders run on one cluster, rewound in between. The second order's remote does not
     contain the first order's stale write, which proves the rewind.
6. **A refused push still advances `refs/dolt/data`.** In every barrier run, the loser's
   rejected push changed the remote `refs/dolt/data` commit, while remote `main` stayed equal to
   the winner's. The test asserts both.
   - The loser's gate log shows the sequence: its leased `cas` is released, then
     `fetch, fetch, cas, info`. Dolt's gitblobstore retry re-applies the chunk/manifest upload
     after losing the lease (research note: `git_blobstore.go` CAS retry). Only the root update
     is refused.
   - Consequence: "the remote head moved" does not mean "a write landed". The recorder records
     both `head` and `main`, and fencing assertions in `bh-vje85` / `bh-jbb6r` must judge on
     `main` (or on table contents), never on `refs/dolt/data`.
7. **bd 1.3 embedded has no `bd sql`.** It answers `'bd sql' is not yet supported in embedded
   mode`. The fixture runs raw SQL on embedded frames with the Dolt CLI, opened on
   `.beads/embeddeddolt/<db>`. bd kept working afterwards: `bd create` and `bd dolt push` both
   ran, and the CLI-written commit sat under bd's. For `bh-sieai`, this means installing a guard
   on an embedded hive cannot go through `bd sql`.
8. **In server mode, `bd sql` does not commit.** `dolt_status` showed the new table unstaged
   until `bd dolt commit -m …`, which commits everything. `Frame.commit` uses the same split.
9. **Inherited environment silently re-routes bd.** Running bd in an embedded workspace with
   `BEADS_DOLT_SHARED_SERVER=1` in the environment printed `metadata.json pins
   dolt_mode="embedded". Using the shared server for this run.` and queried a different store.
   Frame environments are therefore built from scratch, with `PATH`, a private
   `HOME`/`DOLT_ROOT_PATH`/`GIT_CONFIG_GLOBAL` and only that frame's `BEADS_*`. They are never
   inherited, so the autouse `_sandbox_shared_server` neither leaks into a frame nor is relied
   on.
10. **Rewind is cheap and works for every driver.** The rewind does three things:
    - `git update-ref refs/dolt/data <checkpoint>` on the bare remote;
    - `dolt reset --hard` for CLI and embedded frames;
    - `CALL dolt_reset('--hard', …)` through `bd sql` for server frames.

    In the benchmark (Method 4), rewind had a median of 0.49–0.51 s, against about 18 s to build
    a 3-frame cluster. Each forced race round (two writes, both parked, random release) had a
    median of 3.9 s (embedded+CLI), 4.1 s (server+CLI) and 4.4 s (embedded+server), with a
    maximum of 4.6 s. Every round produced exactly one winner.
11. **A `remotesapi` remote is cheap but the wrong shape.** `dolt sql-server --remotesapi-port`
    on Dolt 2.3.5 is ready in 129 ms. `dolt clone http://127.0.0.1:<port>/db` takes 112 ms, and
    `dolt push` takes 183 ms. Root on loopback pushes without credentials. But it has no Git
    transport, no `--force-with-lease` and no `refs/dolt/data`, so it is not the production path
    (`git+ssh`). The gate cannot intercept it either, because it is HTTP inside the dolt process.
    Partitioning it means stopping the server, which cuts every frame. Not used. It is the
    substitute if Git transport ever becomes unavailable.
12. **Per-scenario wall time** (pytest `--durations`, call phase; cluster setup included):

    | Scenario (test) | Frames | Serial, unfenced | Fenced, `-n 4` |
    |---|---|---|---|
    | Start-up, HQ, recorder, writes via 3 drivers | emb + srv + cli | 42.7 s | 49.1 s |
    | Partition each frame in turn | emb + srv + cli | 45.4 s | 51.2 s |
    | Kill mid-push, both bd modes | emb + srv | 28.7 s | 39.8 s |
    | Barrier race, both orders, rewind | emb + cli | 21.2 s | 22.3 s |
    | **File total (wall)** | | 147 s | 53–57 s |

    Fixture building blocks (`Cluster.timings`): seeding the remote takes 0.06 s. Founding with
    an embedded frame takes 7.1–7.3 s (`bd init` about 5 s, plus a push). A server-frame join
    takes 9.9–10.4 s; a CLI join runs in parallel. The observer clone takes 0.2 s. bd
    push/pull takes 1.0–1.7 s, CLI push about 1.0 s, `bd create` 0.4–0.6 s.
13. **The integration gate carries the file.** `just test-integration-land` runs 16 workers,
    fenced, under a 900 s watchdog. Two back-to-back runs on the same loaded host (load average
    about 8, with other spikes running) gave these results:

    - With the new file: 85 items, 541 s wall. Every new test passed. The longest call was
      `test_partition…` at 51.8 s; the other three calls fell below it in the duration list. The
      kill test waited 300 s in *setup* for a `dolt_server` slot, which is the existing 4-slot
      ceiling at work, not fixture cost.
    - With the file deselected: 81 items, 737 s wall.

    Host noise therefore swamps the file's own cost. That cost is bounded by about 160
    slot-seconds (four tests of 22–51 s, each holding one of the 4 run-wide slots), so at most
    about 40 s of land wall when the run is slot-bound. Both runs stayed under the 900 s
    watchdog. Both had the same single, unrelated failure
    in `test_fleet_membership_e2e_int.py`
    (`test_public_seed_then_signed_two_frame_lifecycle_with_one_clock`, a seed-plan assertion),
    present with and without this file.

## Verdict — **GO**

The fixture exists and its four capabilities are each proven by a real-driver smoke test:
start-up with HQ and the recorder, per-frame partition, kill mid-push, and barrier-ordered
races. It writes through the Dolt CLI, bd's embedded Dolt and a bd shared-server, all against
one production-shaped `git+file://` remote. It is hermetic: every frame is under one tmp root
and nothing leaks after `close()`. It is deterministic because ordering comes from barriers
parked at named Git calls, never from sleeps.

The enabler is that every driver reaches the remote by exec'ing `git` from `PATH`, so a
per-frame gate is one choke point for partition, hold and trace. The four smoke tests fit the
integration gate (Evidence 12–13). Running 500 randomized real-driver interleavings inside that
gate does not fit, but that cost belongs to `bh-jbb6r` (Recommendation).

## Recommendation

- **`bh-vje85` / `bh-sieai`: build scenarios on `Cluster` + `Recorder` as they are.**
  - Adopt pattern: `hq.place(b, expected_epoch=…)`, then a `bh_writer` update (`frame.sql`) and
    `commit`, then `push`.
  - Guard state: pass a `guard_probe` (for example, a frame's `bh_local_ident` plus whether its
    trigger refuses a write).
  - Mid-write handoff (proposal scenario 2): `hold(CAS)` on the incumbent and run the adopter
    while it is parked.
  - Stale-writer pull then push (scenario 5): `pull()` and `push()` on the stale frame; for
    `bd sync` and auto-push, `frame.bd(...)`.
  - Judge "stale write landed" on remote `main` or table contents, never on `refs/dolt/data`
    (Evidence 6).
  - On embedded frames, guard install and trigger SQL go through the Dolt CLI (Evidence 7).
- **`bh-jbb6r`: do not put 500 real-driver interleavings in the integration gate.** At about
  5 s per interleaving (rewind plus a forced race, Evidence 10), 500 rounds take about 40 min
  serially.
  - Keep a small fixed-seed set (≤ 10 rounds, about 1 min on one rewound cluster) in
    `integration`.
  - Run the ≥ 500 randomized set as a seed-logged periodic/soak recipe outside the land gate,
    using `checkpoint`/`rewind` and `Recorder` traces. A failing seed then replays exactly.
  - Shard it: pairs without a server frame (embedded + CLI) start no `dolt sql-server` and need
    no `dolt_server` slot, so they parallelise beyond the 4-slot ceiling. Rounds that include a
    server frame stay slot-bound.
- **Cheaper start-up if the gate tightens.** Most of a cluster's setup is the founder's
  `bd init` (about 7 s) and bd's own `--shared-server` init (about 10 s). Two options:
  - cache one founded `remote.git` per session and copy it per test;
  - give tests a shared cluster and `rewind()` between them.

  Neither is needed for the four smoke tests.
- **Known limits, kept explicit:**
  - The gate depends on `git` being resolved through `PATH`. This is self-checked
    (Evidence 2).
  - `pid_alive` reads `/proc` when it can; elsewhere it falls back to `kill(pid, 0)`.
  - The observer clone reads the remote with the CLI's Dolt (2.3.5), not bd's.
  - The HQ stand-in has placement and reachability only. Lease TTLs and sessions
    (scenarios 7, 9, 10) are for the beads that need them to add, on top of `HQ`.
