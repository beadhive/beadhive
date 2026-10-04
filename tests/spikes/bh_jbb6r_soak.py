"""OPTIONAL randomized-interleaving benchmark for the bh-jbb6r spike -- never in the land gate.

Opt-in only: this file is not a ``test_*.py`` module, so no pytest run collects it, and no just
recipe calls it. The gate evidence is the fixed-seed integration test
``test_fixed_seed_randomized_interleavings_hold_every_invariant`` (seeds 0-3).

By operator decision (2026-10-04) the spike's ">= 500 schedules" acceptance item was relaxed to
this optional benchmark: the 500-schedule sharded run loaded the shared host past what the
factory frame's heartbeat measurement tolerates. Defaults are therefore SMALL and polite:

* 25 seeds (``--seeds 0:25``), ONE world at a time in this process (no parallel shards);
* before every schedule it waits while the 1-minute load average is above ``--max-load``
  (default 16);
* ``--resume`` skips seeds already logged in ``--out``, so a run can be stopped and continued.

One schedule costs ~20-35 s (six real-driver events, recovery, the full invariant check and the
rewind). To scale up, raise ``--seeds`` (e.g. ``0:500``) and, only on an otherwise idle host,
``--parallel N`` (at most 4 shard processes, at most 2 of them with bd-server frames).

Every schedule is one JSON line in ``<out>/shard-*.jsonl``: seed, composition, the events it
drew with each outcome, the invariant result and any harness error. A seed replays the same
event sequence and barrier orders on a world built the same way::

    uv run python tests/spikes/bh_jbb6r_soak.py --out /tmp/jbb6r-bench            # 25 seeds
    uv run python tests/spikes/bh_jbb6r_soak.py --seeds 137:138 --out /tmp/replay  # one seed

Composition is a pure function of the seed (``seed % 4 < 2``: bd-embedded + bd-server + Dolt
CLI; otherwise two bd-embedded frames + Dolt CLI, which start no sql-server per frame). Each
composition gets the same warm-up (writer ``a`` writes, ``b`` adopts and writes) and every
schedule starts from that checkpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "src")]

SERVER = "server"
EMBEDDED = "embedded"


def composition(seed: int) -> str:
    return SERVER if seed % 4 < 2 else EMBEDDED


def _frames(kind: str):
    from harness.writer_fencing import BD_EMBEDDED, BD_SERVER, DOLT_CLI

    if kind == SERVER:
        return [("a", BD_EMBEDDED), ("b", BD_SERVER), ("c", DOLT_CLI)]
    return [("a", BD_EMBEDDED), ("b", BD_EMBEDDED), ("c", DOLT_CLI)]


def _warm(world) -> object:
    from harness import composed_fence as cf

    assert cf.write(world["a"], "warm-a").ok and cf.managed_push(world["a"]) == "pushed"
    placed = world.hq.place("b")
    assert cf.adopt(world.cluster, world["b"], placed.epoch).landed
    assert cf.write(world["b"], "warm-b").ok and cf.managed_push(world["b"]) == "pushed"
    return cf.checkpoint_world(world)


def wait_for_load(max_load: float) -> None:
    """Pause while the host is busy (operator host-resource rule)."""
    while max_load > 0 and os.getloadavg()[0] > max_load:
        print(f"load {os.getloadavg()[0]:.1f} > {max_load}: pausing", file=sys.stderr, flush=True)
        time.sleep(30)


def worker(kind: str, seeds: list[int], out: Path, length: int, max_load: float = 16.0) -> int:
    """Run ``seeds`` on one world of composition ``kind``; rebuild it after a harness error."""
    from harness import composed_fence as cf

    log = out.open("a")
    remaining = list(seeds)
    while remaining:
        with tempfile.TemporaryDirectory(prefix=f"jbb6r-{kind}-") as tmp:
            try:
                with cf.composed_world(Path(tmp), _frames(kind)) as world:
                    cp = _warm(world)
                    while remaining:
                        wait_for_load(max_load)
                        seed = remaining.pop(0)
                        schedule = cf.run_schedule(world, seed, length=length)
                        line = {
                            "seed": seed,
                            "composition": kind,
                            "seconds": schedule.seconds,
                            "error": schedule.error,
                            "ok": bool(schedule.result and schedule.result["ok"]),
                            "violations": (schedule.result or {}).get("violations"),
                            "events": schedule.events,
                            "acknowledged": len(schedule.acknowledged),
                            "stats": {
                                k: (schedule.result or {}).get(k)
                                for k in ("commits", "fenced", "bumps", "orphan_admitted")
                            },
                        }
                        log.write(json.dumps(line, sort_keys=True, default=str) + "\n")
                        log.flush()
                        try:
                            cf.rewind_world(world, cp)
                        except Exception as exc:  # rebuild the world, keep the seed's record
                            log.write(
                                json.dumps({"seed": seed, "rewind_error": repr(exc)[:500]}) + "\n"
                            )
                            break
                        if schedule.error:
                            break  # a harness error may leave state rewind cannot see
            except Exception as exc:
                log.write(json.dumps({"world_error": repr(exc)[:1000], "kind": kind}) + "\n")
                log.flush()
                time.sleep(5)
    log.close()
    return 0


def summarize(out: Path) -> dict:
    lines = []
    for path in sorted(out.glob("shard-*.jsonl")):
        for raw in path.read_text().splitlines():
            if raw.strip():
                lines.append(json.loads(raw))
    schedules = [line for line in lines if "events" in line]
    by_seed = {line["seed"]: line for line in schedules}
    violating = sorted(s for s, line in by_seed.items() if line["violations"])
    errors = sorted(s for s, line in by_seed.items() if line["error"])
    per_invariant: dict[str, list[int]] = {}
    for seed in violating:
        for key in by_seed[seed]["violations"]:
            per_invariant.setdefault(key, []).append(seed)
    events = Counter()
    outcomes = Counter()
    for line in by_seed.values():
        for event in line["events"]:
            name = event.get("event")
            events[name] += 1
            for key in ("outcome", "push", "pull", "adopt", "old_push"):
                if key in event:
                    outcomes[f"{name}.{key}={str(event[key]).split(':')[0][:40]}"] += 1
    stale = Counter()
    for line in by_seed.values():
        for event in line["events"]:
            if event.get("stale"):
                stale[f"stale_write={event.get('outcome')}"] += 1
    seconds = sorted(line["seconds"] for line in by_seed.values())
    return {
        "schedules": len(by_seed),
        "by_composition": dict(Counter(line["composition"] for line in by_seed.values())),
        "ok": sum(1 for line in by_seed.values() if line["ok"]),
        "violating_seeds": violating,
        "violations_by_invariant": per_invariant,
        "harness_error_seeds": errors,
        "harness_errors": {s: by_seed[s]["error"][:300] for s in errors},
        "world_errors": [line for line in lines if "world_error" in line or "rewind_error" in line],
        "events": dict(events),
        "outcomes": dict(sorted(outcomes.items())),
        "stale_writes": dict(stale),
        "schedule_seconds": {
            "median": seconds[len(seconds) // 2] if seconds else None,
            "p95": seconds[int(len(seconds) * 0.95)] if seconds else None,
            "max": seconds[-1] if seconds else None,
        },
        "acknowledged_writes": sum(line["acknowledged"] for line in by_seed.values()),
        "orphan_admitted": sum(
            (line["stats"] or {}).get("orphan_admitted") or 0 for line in by_seed.values()
        ),
    }


def _done(out: Path) -> set[int]:
    seen = set()
    for path in out.glob("shard-*.jsonl"):
        for raw in path.read_text().splitlines():
            if raw.strip() and '"events"' in raw:
                seen.add(json.loads(raw)["seed"])
    return seen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", default="0:25", help="START:END (END exclusive)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--length", type=int, default=6, help="events per schedule")
    parser.add_argument("--max-load", type=float, default=16.0, help="pause above this load")
    parser.add_argument("--resume", action="store_true", help="skip seeds already in --out")
    parser.add_argument(
        "--parallel", type=int, default=1, help="shard processes (<= 4; idle hosts only)"
    )
    parser.add_argument("--worker", nargs=2, metavar=("KIND", "SEEDS"), help=argparse.SUPPRESS)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    if args.worker:
        kind, seeds = args.worker
        shard = args.out / f"shard-{kind}-{os.getpid()}.jsonl"
        return worker(
            kind, [int(s) for s in seeds.split(",") if s], shard, args.length, args.max_load
        )

    wall = None
    if not args.summary_only:
        if not 1 <= args.parallel <= 4:
            parser.error("host rule: 1..4 shard processes")
        start, end = (int(x) for x in args.seeds.split(":"))
        skip = _done(args.out) if args.resume else set()
        seeds = [s for s in range(start, end) if s not in skip]
        groups = {k: [s for s in seeds if composition(s) == k] for k in (SERVER, EMBEDDED)}
        started = time.monotonic()
        if args.parallel == 1:  # the default: one world at a time, in this process
            for kind, group in groups.items():
                if group:
                    shard = args.out / f"shard-{kind}-{os.getpid()}.jsonl"
                    worker(kind, group, shard, args.length, args.max_load)
        else:
            server = min(2, max(1, args.parallel // 2))
            counts = {SERVER: server, EMBEDDED: args.parallel - server}
            procs = []
            for kind, group in groups.items():
                count = min(counts[kind], len(group)) if group else 0
                for i in range(count):
                    mine = ",".join(map(str, group[i::count]))
                    cmd = [sys.executable, str(HERE), "--out", str(args.out)]
                    cmd += ["--length", str(args.length), "--max-load", str(args.max_load)]
                    cmd += ["--worker", kind, mine]
                    log = (args.out / f"worker-{kind}-{i}.log").open("w")
                    procs.append((subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT), log))
            for proc, log in procs:
                proc.wait()
                log.close()
        wall = round(time.monotonic() - started, 1)
    summary = summarize(args.out)
    summary["wall_seconds"] = wall
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0 if not summary["violating_seeds"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
