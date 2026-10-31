"""Run the bh-w56ts shape load against the bh-cntr0 container and sample its cgroup memory.

Usage: python loadrun.py <label> <data-dir>
Reads memory.current / memory.peak / memory.events straight from the container's cgroup
(cgroup v2), 2 samples per second, so the peak is exact (memory.peak) and the series is dense.
Prints one JSON object. Throwaway spike code, not product code.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

label, datadir = sys.argv[1], sys.argv[2]
NAME = "xtdb-spike-bh-cntr0"
cid = subprocess.check_output(["docker", "inspect", "-f", "{{.Id}}", NAME], text=True).strip()
cg = Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope")
MIB = 1024 * 1024
rd = lambda n: int((cg / n).read_text())  # noqa: E731


def events():
    return dict(ln.split() for ln in (cg / "memory.events").read_text().splitlines())


def du(p):
    return int(subprocess.check_output(["du", "-sb", p], text=True).split()[0])


def working_set():
    """What `docker stats` shows: memory.current minus inactive file cache."""
    inactive = 0
    for line in (cg / "memory.stat").read_text().splitlines():
        if line.startswith("inactive_file "):
            inactive = int(line.split()[1])
    return (rd("memory.current") - inactive) // MIB


series, ws_series, stop = [], [], threading.Event()
t_start = time.monotonic()


def sampler():
    while not stop.is_set():
        try:
            now = round(time.monotonic() - t_start, 1)
            series.append((now, rd("memory.current") // MIB))
            ws_series.append((now, working_set()))
        except OSError:
            break
        time.sleep(0.5)


idle = rd("memory.current") // MIB
th = threading.Thread(target=sampler)
th.start()
env = dict(os.environ, XTDB_DSN="host=127.0.0.1 port=15434 user=xtdb dbname=xtdb")
w56 = Path(__file__).resolve().parents[1] / "bh-w56ts"
t0 = time.monotonic()
p = subprocess.run(
    [
        "uv",
        "run",
        "--no-project",
        "--with",
        "psycopg[binary]",
        "--with",
        "transit-python2",
        "python",
        str(w56 / "load.py"),
    ],
    cwd=w56,
    env=env,
    capture_output=True,
    text=True,
)
load_s = round(time.monotonic() - t0, 1)
t_loaded = time.monotonic()
time.sleep(float(os.environ.get("SETTLE", "30")))
stop.set()
th.join()


def safe(f):
    try:
        return f()
    except OSError:
        return None  # cgroup is gone: the container died (OOM kill)


state = subprocess.check_output(
    [
        "docker",
        "inspect",
        "-f",
        "{{.State.Status}} exit={{.State.ExitCode}} oom_killed={{.State.OOMKilled}}",
        NAME,
    ],
    text=True,
).strip()
peak_series = max((m for _, m in series), default=None)
res = {
    "label": label,
    "container_state_after_load": state,
    "rc": p.returncode,
    "idle_mib": idle,
    "load_wall_s": load_s,
    "idle_docker_stats_mib": ws_series[0][1] if ws_series else None,
    "peak_docker_stats_mib": max((m for _, m in ws_series), default=None),
    "end_docker_stats_mib": safe(working_set),
    "peak_mib": safe(lambda: rd("memory.peak") // MIB) or peak_series,
    "end_mib": safe(lambda: rd("memory.current") // MIB),
    "memory_events": safe(events),
    "data_bytes": du(datadir),
    "log_bytes": du(datadir + "/log") if os.path.isdir(datadir + "/log") else None,
    "buffers_bytes": du(datadir + "/buffers") if os.path.isdir(datadir + "/buffers") else None,
    "load_stdout": p.stdout[-1500:],
    "load_stderr_tail": p.stderr[-800:],
    "series_every_5s": series[::10],
}
print(json.dumps(res, indent=1))
