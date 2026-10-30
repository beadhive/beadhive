# bh-3jp2g shell helpers (`xtdb.sh`, `sampler.sh`)

The two shell scripts used in this spike, verbatim. They are kept as fenced text because the
native impact map (`scripts/check_native_impact_map.py`) has no owner for `.sh` files under
`docs/spikes/`. To re-run, save each block under its name next to the Python scripts and
`chmod +x` it.

## `xtdb.sh`

```bash
#!/usr/bin/env bash
# Throwaway container lifecycle for spike bh-3jp2g (not product code).
#   xtdb.sh up      start a FRESH XTDB 2.2.0 node (refuses a non-empty data dir)
#   xtdb.sh down    docker rm -f the container and rm -rf its data dir
#   xtdb.sh wait    block until pgwire answers
# Data lives on real disk (ext4 /data), never /tmp (RAM tmpfs on the factory host).
set -euo pipefail

NAME=xtdb-spike-bh-3jp2g
BASE=/data/bees/beadhive/scratch/xtdb-spike/bh-3jp2g
DATA=$BASE/xtdb
PORT=15433

wait_ready() {
  for _ in $(seq 1 120); do
    if uv run --no-project --with 'psycopg[binary]' python -c \
      "import psycopg; psycopg.connect('host=127.0.0.1 port=$PORT user=xtdb dbname=xtdb', connect_timeout=2).close()" \
      2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "xtdb did not come up" >&2
  return 1
}

case "${1:-}" in
  up)
    mkdir -p "$DATA"
    if [ -n "$(ls -A "$DATA")" ]; then
      echo "refusing: $DATA is not empty (SYSTEM_TIME backfill needs a fresh database)" >&2
      exit 1
    fi
    docker run -d --name "$NAME" --memory 3g --memory-swap 3g --user "$(id -u):$(id -g)" \
      -p 127.0.0.1:$PORT:5432 -v "$DATA":/var/lib/xtdb ghcr.io/xtdb/xtdb:2.2.0 >/dev/null
    wait_ready
    ;;
  down)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf "$DATA"
    ;;
  wait)
    wait_ready
    ;;
  *)
    echo "usage: $0 up|down|wait" >&2
    exit 2
    ;;
esac
```

## `sampler.sh`

```bash
#!/usr/bin/env bash
# Background sampler for spike bh-3jp2g: container memory + data dir size every ~2 s, as JSON
# lines in $BASE/samples.jsonl, until $BASE/sampler.stop exists or the container is gone.
set -uo pipefail

NAME=xtdb-spike-bh-3jp2g
BASE=/data/bees/beadhive/scratch/xtdb-spike/bh-3jp2g
OUT=$BASE/samples.jsonl
rm -f "$BASE/sampler.stop"

while [ ! -e "$BASE/sampler.stop" ]; do
  mem=$(docker stats --no-stream --format '{{.MemUsage}}' "$NAME" 2>/dev/null | awk '{print $1}')
  [ -z "$mem" ] && break
  bytes=$(du -sb "$BASE/xtdb" 2>/dev/null | awk '{print $1}')
  mib=$(python3 -c "import re,sys; m=re.match(r'([0-9.]+)([KMG]i?B)', sys.argv[1]); \
f={'KiB':1/1024,'MiB':1,'GiB':1024,'KB':1/1024,'MB':1,'GB':1024}[m.group(2)]; print(round(float(m.group(1))*f,1))" "$mem")
  printf '{"t": %s, "mem_mib": %s, "data_dir_bytes": %s}\n' "$(date +%s.%N)" "$mib" "${bytes:-0}" >>"$OUT"
  sleep 2
done
```
