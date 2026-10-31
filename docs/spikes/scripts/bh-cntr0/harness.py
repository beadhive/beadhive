"""Container harness for the bh-cntr0 spike (throwaway; replaces the shell helpers).

    python harness.py start <cap> <data-dir> [extra docker run args ...]
    python harness.py wait [t0]            # seconds until /healthz/started answers 200
    python harness.py captest <label> <cap> [extra docker run args ...]
    python harness.py matrix               # the cap sweep reported in the spike doc

One container (xtdb-spike-bh-cntr0) at a time; pgwire only on 127.0.0.1:15434, healthz on
127.0.0.1:18084; data dir on real disk under SCRATCH (never /tmp, which is tmpfs here).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

NAME = "xtdb-spike-bh-cntr0"
SCRATCH = Path("/data/bees/beadhive/scratch/xtdb-spike/bh-cntr0")
HERE = Path(__file__).resolve().parent
UV = ["uv", "run", "--no-project", "--with", "psycopg[binary]"]


def sh(*cmd, **kw):
    return subprocess.run(cmd, text=True, **kw)


def start(cap, data_dir, *extra):
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    sh(
        *["docker", "run", "-d", "--name", NAME, "--memory", cap, "--memory-swap", cap],
        *["--user", f"{os.getuid()}:{os.getgid()}"],
        *["-p", "127.0.0.1:15434:5432", "-p", "127.0.0.1:18084:8080"],
        *["-v", f"{data_dir}:/var/lib/xtdb", *extra, "ghcr.io/xtdb/xtdb:2.2.0"],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def wait(t0=None, limit=60.0):
    t0 = time.time() if t0 is None else t0
    while time.time() - t0 < limit:
        try:
            url = "http://127.0.0.1:18084/healthz/started"
            if urllib.request.urlopen(url, timeout=1).status == 200:
                print(f"started after {time.time() - t0:.1f}s")
                return True
        except OSError:
            pass
        time.sleep(0.1)
    print("TIMEOUT")
    return False


def captest(label, cap, *extra):
    """Fresh data dir, start at <cap>, run the shape load, verify counts, tear down."""
    data_dir = SCRATCH / f"run-{label}"
    sh("rm", "-rf", str(data_dir))
    (SCRATCH / "out").mkdir(parents=True, exist_ok=True)
    sh("docker", "rm", "-f", NAME, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    start(cap, str(data_dir), *extra)
    if not wait(t0):
        return
    env = dict(os.environ, SETTLE=os.environ.get("SETTLE", "20"))
    out = SCRATCH / "out" / f"load-{label}.json"
    with open(out, "w") as f:
        sh(sys.executable, str(HERE / "loadrun.py"), label, str(data_dir), stdout=f, env=env)
    d = json.load(open(out))
    keys = ("label", "container_state_after_load", "rc", "idle_mib", "peak_mib", "end_mib")
    print({k: d[k] for k in (*keys, "load_wall_s", "data_bytes", "memory_events")})
    running = sh("docker", "inspect", "-f", "{{.State.Running}}", NAME, capture_output=True)
    if running.stdout.strip() == "true":
        sh(*UV, "python", str(HERE / "counts.py"))
        fmt = "{{.State.Health.Status}} streak={{.State.Health.FailingStreak}}"
        print(
            "health:", sh("docker", "inspect", "-f", fmt, NAME, capture_output=True).stdout.strip()
        )
    sh("docker", "rm", "-f", NAME, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sh("rm", "-rf", str(data_dir))


def matrix():
    runs = [
        ("1536m", "1536m"),
        ("768m", "768m"),
        ("512m", "512m"),
        ("1g-heap40", "1g", "-e", "JDK_JAVA_OPTIONS=-XX:MaxRAMPercentage=40"),
        ("768m-heap40", "768m", "-e", "JDK_JAVA_OPTIONS=-XX:MaxRAMPercentage=40"),
        ("2g-final", "2g"),
    ]
    for label, cap, *extra in runs:
        print("===", label, cap, *extra, flush=True)
        try:
            subprocess.run([sys.executable, __file__, "captest", label, cap, *extra], timeout=600)
        except subprocess.TimeoutExpired:
            print("rc=124 (timeout 600 s)", flush=True)
        sh("docker", "rm", "-f", NAME, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "start":
        start(*args)
    elif cmd == "wait":
        sys.exit(0 if wait(float(args[0]) if args else None) else 1)
    elif cmd == "captest":
        captest(*args)
    elif cmd == "matrix":
        matrix()
    else:
        sys.exit(__doc__)
