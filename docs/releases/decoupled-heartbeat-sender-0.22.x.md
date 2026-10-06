# Decoupled frame heartbeat sender (0.22.x, bh-i6ggn)

ADR outline F7 / `bh-32379` P-F7. Under load the old sender took about 3.5 minutes per beat,
because one process both renewed liveness and measured conformance (`hive_ready`, a `bd`
round trip per hive). A stalled measurement lapsed the beat and fenced every lifecycle write
(`bh-32379` L8). That is why `BH_FRAME_HEARTBEAT=advisory` exists.

## What changed (sender only)

- **Conformance job.** `python -m beadhive.heartbeat_sender conformance` measures the slow
  host checks (`host-config-partition`, `hives-ready`) and writes
  `$BH_HOME/heartbeat/conformance.json` atomically. The cache's `measured_at` is the time the
  measurement *started*, so its age is never understated. Overlapping runs skip instead of
  stacking.
- **Beat.** `python -m beadhive.heartbeat_sender beat` signs one `HeartbeatLease` and never
  measures conformance. It re-labels each cached check with the cache's `measured_at`. It also
  adds a `conformance-age` check, which fails once the cache is older than
  `CONFORMANCE_MAX_AGE_SECONDS` (900 s, one lease window), or is missing or unreadable. A
  stalled conformance job therefore shows up as a non-conformant beat. It no longer shows up as
  a stale frame. The release digest and host identity are still checked on every beat against
  the grant read at signing time.
- **One TTL source.** `beadhive.heartbeat_conformance` holds `LEASE_DURATION_SECONDS` (900,
  the contract ceiling and the factory's value since 2026-10-05), `INTERVAL_SECONDS` (60),
  `CONFORMANCE_INTERVAL_SECONDS` (300) and the staleness bound. `heartbeat_report.generate`
  (and so `bh host heartbeat-report` / `heartbeat-send`) and the units all read these
  constants. The in-tree sender no longer hard-codes 300.
- **Units.** `python -m beadhive.heartbeat_sender units [--install|--remove]` renders two
  systemd user timers (`beadhive-heartbeat`, `beadhive-heartbeat-conformance`) or two launchd
  agents (`dev.beadhive.heartbeat`, `dev.beadhive.heartbeat-conformance`). They run the
  interpreter of the `bh` install that rendered them. They replace
  `~/.beadhive/factory-local-heartbeat.py` and `beadhive-factory-heartbeat.service`.

## What did not change

The `HeartbeatLease` schema (`le=900`), the signing domain, the receiver and every reader
predicate are byte-identical. A 0.22.x reader accepts the new beats. Readers look only at
`conformance.status` and each check's `status`. Check ids and evidence text are sender data
that the existing schema already allows. Tests pin the lease JSON-schema fingerprint and run
the signed-liveness verifier against new-sender beats.

The sender is a module entrypoint, not a new `bh` leaf. So the published operation catalog
and the wire schemas are unchanged for this patch line.

## Start, stop, verify

The full command list is in the `beadhive.heartbeat_sender` module docstring. On the factory
(systemd):

```sh
PY=$(dirname "$(readlink -f "$(command -v bh)")")/python   # the attested bh env
systemctl --user disable --now beadhive-factory-heartbeat.timer   # retire the shim first
"$PY" -m beadhive.heartbeat_sender units --install
systemctl --user daemon-reload
systemctl --user enable --now beadhive-heartbeat-conformance.timer beadhive-heartbeat.timer
systemctl --user list-timers 'beadhive-heartbeat*'
"$PY" -m beadhive.heartbeat_sender status     # exit 0 while the cache is within bound
bh host list                                  # BEAT_AGE stays near one interval
```

Stop it with
`systemctl --user disable --now beadhive-heartbeat.timer beadhive-heartbeat-conformance.timer`.
Run only one sender per frame. Retire the factory shim before you enable the beat timer, so
only one process advances `seq`.

Rolling this out needs the attested release on the frame (C/O0, rotation step 3). The shim
and the factory unit are removed with `bh-3q5m9` (F9).
