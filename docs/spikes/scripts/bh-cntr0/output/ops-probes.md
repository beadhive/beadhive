# bh-cntr0 operational probes (auth, bind, healthz, logs, idle CPU)

Captured on 2026-10-30 against XTDB 2.2.0 (`ghcr.io/xtdb/xtdb:2.2.0`, image revision `c6c19623`),
containers started with `harness.py start` (pgwire published only on `127.0.0.1:15434`, healthz on
`127.0.0.1:18084`). `auth.py` is the probe script; the no-password run was an inline version of
the same four connections.

## Published ports (host side), and what the container listens on

```text
$ ss -ltnp | grep -E "1543|18084"        (host)
LISTEN 0 4096 127.0.0.1:18084  0.0.0.0:*
LISTEN 0 4096 127.0.0.1:15434  0.0.0.0:*
$ docker port xtdb-spike-bh-cntr0
5432/tcp -> 127.0.0.1:15434
8080/tcp -> 127.0.0.1:18084
$ docker logs xtdb-spike-bh-cntr0 | grep -E "Server started|Healthz|Flight"    (inside the container)
Flight SQL server started, port 3000
Server started at postgres://0.0.0.0:5432
Healthz server started at http://*:8080 - startup targets: {"xtdb" [-1]}
```

## Default config: no password configured (`!SingleRootUser`, TRUST)

```text
xtdb None -> connected; ('xtdb',)
xtdb wrong-password -> connected; ('xtdb',)
bob None -> connected; ('xtdb',)
postgres x -> connected; ('xtdb',)
```

## Started with `-e XTDB_PASSWORD=spike-secret` (`auth.py 15434`)

```text
user='xtdb' password=None: REJECTED: connection failed: connection to server at "127.0.0.1", port 15434 failed: fe_sendauth: no password supplied
user='xtdb' password='wrong-password': REJECTED: connection failed: ... password authentication failed
user='xtdb' password='spike-secret': CONNECTED as xtdb
user='bob' password='spike-secret': REJECTED: connection failed: ... password authentication failed
```

## Healthz routes (default config sets `healthz: port 8080, host '*'`)

```text
GET  /healthz/alive         -> 200
GET  /healthz/started       -> 200
GET  /healthz/ready         -> 200
GET  /metrics               -> Prometheus text (jvm_*, block_upload_timer_seconds, ...)
POST /system/finish-block   -> "Block flush message sent to 1 database(s)."  (35 ms)
```

## The image's own HEALTHCHECK reports `unhealthy` on a healthy node (XTDB 2.2.0 image)

```text
HEALTHCHECK: wget -q --spider http://localhost:8080/healthz/alive || exit 1
$ docker inspect -f '{{.State.Health.Status}} streak={{.State.Health.FailingStreak}}'   (2g-final run, node serving)
unhealthy streak=3
$ curl -s -I http://127.0.0.1:18084/healthz/alive           (HEAD)
HTTP/1.1 405 Method Not Allowed
$ curl -s -i http://127.0.0.1:18084/healthz/alive           (GET)
HTTP/1.1 200 OK
$ docker exec ... wget -S --spider http://127.0.0.1:8080/healthz/alive
  Content-Length: 0
Remote file does not exist -- broken link!!!            (wget exits 8 on the 405 to HEAD)
```

With the check overridden to a GET, six samples 5 s apart all read `healthy`:

```text
--health-cmd 'wget -q -O /dev/null http://127.0.0.1:8080/healthz/alive'
--health-interval 10s --health-start-period 30s
```

## Logging

```text
default:  22:55:45 | INFO  xtdb.pgwire | Server started at postgres://0.0.0.0:5432          (plain text)
-e XTDB_LOG_FORMAT=json:
{"timestamp":"2026-10-30T23:33:09.305Z","level":"INFO","logger":"xtdb.Metrics","message":"tagging all metrics with node-id: 4883d3c1","thread":"main"}
docker logs size after ~90 s of idle: 1755 bytes.  Docker daemon logging driver: json-file, no daemon.json (no rotation by default).
JVM warnings on every start: "sun.misc.Unsafe::allocateMemory has been called by io.netty ..." (4 lines, harmless).
```

## Idle CPU and memory (2g, node idle for ~30 s after start, `docker stats`)

```text
8.02%  391.4MiB / 2GiB  pids=80
3.69%  391.7MiB / 2GiB  pids=77
3.08%  392.4MiB / 2GiB  pids=77
```

## JVM sizing inside the container (`jcmd 1 VM.flags`, 2 GiB cap, no JDK_JAVA_OPTIONS)

```text
-XX:MaxHeapSize=536870912 (25% of the cap)  -XX:+UseG1GC  -XX:InitialHeapSize=33554432  java.version=25.0.4.1
direct memory (Arrow allocator, memoryCache.maxSizeRatio 0.5 of it) defaults to the max heap size
```

## Startup and shutdown timing

`harness.py wait` prints seconds from `docker start` or `run` until `/healthz/started` is 200.

```text
cold, empty data dir:  18.2 to 22.1 s over 11 starts (cap sweep 768m..2g, median about 20 s)
after kill -9, 117 MB log, no block flushed (full replay):  20.2 s
after kill -9 mid-write:                                    21.9 s
after kill -9, blocks flushed:                              20.7 s
restore from cp -a copy of stopped dir:                     20.8 s;  from copy taken while running: 20.6 s
docker stop (SIGTERM): 0.97 s, exit 143, log line "Stopping node..."
First log line ("Starting 2.2.0") ~5.6 s after container start; "Node started" ~20 s.
```
