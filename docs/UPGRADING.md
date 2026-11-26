# Upgrading `bh`

Narrative upgrade notes for releases that change the shape of a user's on-disk state or CLI
surface — what you must run, what's safe to delete, and what must never be copied between
machines. For the mechanical per-commit list, see [CHANGELOG.md](../CHANGELOG.md); this file
exists for the releases where "read the changelog" isn't enough to act on.

## 0.40.5 → 0.41.0 — a pluggable runtime seam, a hard `bd` floor, `hq.path`

0.41.0 is mostly additive: the runtime seam (Gas City integration, see
[RUNTIME-INTEGRATION.md](RUNTIME-INTEGRATION.md)) and a handful of config keys. Nothing changes on
a host that sets none of the new keys, with one exception: the `bd` floor below.

### Action required

- **`bd` 1.3.0 or newer is now a hard floor for claiming (bh-rwjxt).** `bd update --claim` is an
  atomic compare-and-set only from bd 1.3.0, and bh relies on that to lose a race cleanly. On an
  older `bd`, `bh work claim` and the loop's claim route now refuse by name
  (`bd <found> is too old to claim beads safely ... Upgrade bd to 1.3.0 or later`) where they
  previously proceeded. Upgrade `bd` on every host that claims beads. Any version that is
  not a plain tagged release passes the floor: a dev, `HEAD`, rc, pre or `.dev` build (for
  example `1.1.0 (dev)` or `HEAD-af076b6`), or no `bd` on `PATH`. A plain tagged release below
  1.3.0 is refused. Verified against `MIN_ATOMIC_CLAIM_BD` and `require_atomic_claim` in
  `src/beadhive/bd_floor.py` and `tagged_release` in `src/beadhive/bd_version.py`, called from
  `work_lifecycle.py` (the lease `acquire` and the `claim` verb's `BdTooOldError` handler).

### What is new (all opt-in)

- **`work.runtime` is syntax-only at config load, strict at use.** Config load only checks that
  the value is a well-formed name; it no longer consults the registry, so a hive that names a
  plugin runtime still loads on a host without the package. The registry check, which loads the
  entry point, runs in `bh work loop`, `bh config set`, `bh config validate`, `bh hive ready` and
  `bh doctor`, and an unregistered name fails closed there. Verified against
  [CONFIGURATION.md](CONFIGURATION.md#workruntime--what-wakes-the-lifecycle) and the registry in
  `src/beadhive/runtime/`.
- **`beadhive.runtimes` entry points and per-hive `work.runtime`.** A package registers a runtime
  by declaring an entry point in the `beadhive.runtimes` group whose name is the `work.runtime`
  value; `bh` imports only the entry point named by the value. Select it per hive
  (`managed_repos[].work.runtime`) or for the host. `local` stays built in and is never an entry
  point. The published config schema still lists only the built-ins (tracked as bh-esnpc), so an
  external validator of that schema rejects a plugin name until the next contract major; `bh`
  itself accepts it.
- **`work.dispatch.loop` (`bh` | `gc`) and the `dispatch-owner:gc` label.** Declares who owns the
  dispatcher loop for a hive (the default `bh` changes nothing) or for one molecule (the label).
  A delegated owner stands `bh` down from picking ready work, the dispatcher lease, reclaim and
  the supervised service; `bh host dispatch status` and `bh doctor` report `delegated (loop: gc)`.
  Lifecycle verbs (claim, submit, review, merge, gate resolve) are unaffected. Verified against
  `src/beadhive/dispatch_loop_owner.py` (`declared_loop`, `OWNER_LABEL_GC`), `work_queue.py` and
  `localloop.py`.
- **`hq.path` (HOST-only) relocates the HQ checkout.** HQ resolves as `BH_HQ` (legacy `WS_HQ`),
  then `hq.path`, then `<BH_HOME>/hq`. HQ-adjacent state (HQ backups, fleet authority state,
  beadyard adoption journals) lives beside the resolved HQ directory only when `hq.path` is the
  source; with `BH_HQ` set or nothing set, state stays under `BH_HOME`, so an upgrade strands
  nothing. Verified against `hq_dir` and `hq_home` in `src/beadhive/config_paths.py`.
- **Restart adoption of adoptable runtime handles (`bh.runtime.handle`).** For a runtime that
  declares `adoptable = True` (sessions that outlive the loop), the loop records one
  `bh.runtime.handle` metadata document per bead when it schedules a turn and clears it on
  harvest or cancel. After a restart `bh work loop` adopts a live session instead of reclaiming
  its bead or scheduling a second session, unless the claim's `lease_expires_at` has passed
  (including inside `bd reclaim`'s grace window), in which case it is not adopted. The built-in
  `local` runtime is not adoptable and writes nothing. Verified against
  `src/beadhive/runtime_handle.py` and `localloop.py`.

### Behaviour change for runtimes

- **A raising `observe()` or `cancel()` is retried instead of crashing the pass.** The loop reads
  a raise as transient: it logs it, answers `running` and asks again next pass. After
  `runtime_error_limit` consecutive raises for one bead (default 3, `DEFAULT_RUNTIME_ERROR_LIMIT`
  in `localloop.py`) it answers `failed` with the error as the detail, so the bead gets a written
  cause rather than a heartbeated claim nobody can read. One clean answer resets the count. This
  applies to every runtime the loop drives, `LocalRuntime` included; before, a raise propagated
  and stopped the pass.

### Smaller fixes you may notice

- `bh work next` and the loop skip `dispatch-owner:gc` members; a triplet-less fleet entry renders
  an error row (or a clean error) in `bh host dispatch` verbs instead of a traceback.
- `bh host beads start` prints a status/stop/start recovery ladder when it is refused
  ([BEADS-SERVICE.md](BEADS-SERVICE.md)).
- `bh doctor` renders a delegated (`loop: gc`) dispatch state instead of reporting it healthy.
- An unheld, empty api hive lease is read as no lease instead of an error.
- Hub prune is suppressed when a fleet entry is malformed, and the skipped entries are reported.
- Runtime conformance kit: `beadhive.runtime.conformance` runs without pytest installed.

### Not in 0.41.0

The HQ-track items previously pencilled in for 0.41.0 are **not** in this release: the
`hq.sql_guard` default flip (an unset `hq.route: sql` host is still untouched and silent),
`bh hq release-catalog revoke` on an `api` host, fleet config publication on `api`, the
`--operator-key` flag for `release-catalog import`, and the deferred operator verbs listed under
"Deferred to a later release" in the 0.39.0 → 0.40.0 notes below. The separate host certificate
is tracked as bh-pm1s4.

## 0.39.0 → 0.40.0 — every host reaches HQ through hq-service (`hq.route: api`)

0.40.0 lets every frame and every CLI path reach HQ only through hq-service, behind the Gateway
`hq` plane, with no `hq.sql` block on the host. Hosts cut over one at a time. Nothing changes on
a host until you set `hq.route: api` there: a 0.40.0 host left on `hq.route: sql` behaves as
0.39.0 did (with one new `bh doctor` warning, below).

> **DO NOT START the cutover on any host until both of these are deployed on the HQ host:**
>
> 1. **hq-service `v0.1.0`** (commit `4e0fc6b`), and
> 2. **beadhive-gateway `v0.1.0`** (commit `2a400f8`, which closes `bh-gw-vm2i`).
>
> beadhive-gateway `main` at `7e22180` cannot carry this cutover: it has no `/plan` and
> `/apply` operator routes and no history-query routing, its bound-identity attestation is off,
> and it mints only the caller kinds `worker` and `operator`, while hq-service `v0.1.0` requires
> `host` for manifest writes and `frame` for admissions. The capabilities that Gateway
> must have are listed under "Required first". bh 0.40.0 itself is published independently of the
> Gateway: installing it changes nothing until a host sets `hq.route: api`.

This note is written to be followed literally, host by host. Values you must supply are in
`UPPER_CASE` (for example `HQ_GATEWAY_ADDRESS`). The Gateway binding shapes below follow
beadhive-gateway `v0.1.0`, `docs/operations/hq-plane.md` ("Principal kinds" and "Recipe").

### What changed

- **`hq.route` (HOST) selects the adapter.** `sql` (default) opens the HQ SQL server, `api`
  goes through hq-service. There is no fallback between them. The route resolves in this order:
  `BH_HQ_ROUTE`, then `hq.route`, then `hq.mode: api`, then `sql`.
- **`hq.mode` stays a FLEET key.** Hosts cut over one at a time with host-scoped `hq.route: api`
  (or `BH_HQ_ROUTE=api` for one command). Fleet `hq.mode: api` is the end state, once every
  host is on `api`. **Do not set fleet `hq.mode: api` in 0.40.0:** fleet config publication has
  no api route until a later release, and a config that resolves to `hq.mode: api` beside an explicit
  `hq.route: sql` is refused (`hq.mode api needs hq.route api (or no hq.route)`), which would
  also close the per-host rollback below.
- **Liveness on `api` (bh-65s71).** `beadhive-session-renew.timer` (launchd
  `dev.beadhive.session-renew`) is the only liveness writer, every 60 s, through hq-service.
  `heartbeat_sender beat` is a no-op and prints `beat skipped: hq.route api beats through
  session renew` (exit 0). Renew applies to an ACTIVE grant only: a CANDIDATE slot is refused
  (`no_current_grant`) and cannot promote by renewing, so a join must reach ACTIVE through open
  admission. `hq.session_via_port` and `hq.session_dual_write` are ignored on `api`.
- **Join on `api` (bh-obhfx).** `bh host init` publishes the manifest, `bh hq authority join`
  registers the incarnation (operator-bound) and open-admits the frame (frame-bound). No SQL
  login, grant or escrowed secret is created. Signed-mode fleets have no api join (it is refused
  with `HqPlaneUnavailable` naming `publish_registration`).
- **Operator verbs on `api` (bh-lejfy).** `bh hq authority rebind`, `bh hq authority
  release-policy plan|apply`, `bh hq release-catalog verify` and `bh hq release-catalog import`
  go through hq-service. `import` signs with the key named by `BH_HQ_OPERATOR_KEY` (an
  environment variable in 0.40.0; the `--operator-key` flag for it is a later-release item).
- **The SQL plane guard (bh-d9anb).** Every HQ SQL opener off the hq-service host warns on an
  `api` host. Placement (`bh hq placement`), `bh hq migrate` and `bh hq authority prune-inbox`
  stay local to the hq-service host.
- **The `beadyard_id` pin (bh-jwymz).** On `api` each frame's host config must pin
  `hq.beadyard_id` equal to its committed manifest's `beadyard_id`. Without it a hive-lease claim
  fails with `beadyard_binding`. It is an explicit step below.
- **A frame's manifest comes from HOST keys (bh-6se58.3, wire 2.34.0).** On a host with
  `host.frame_id` set, `bh host init --role executor` publishes `frame_id`, `instance_ref`, the
  installed `release` and `capabilities` (from `host.instance_ref` or the new
  `bh host init --instance-ref` flag, and `host.frame.capabilities.*`; `arch` comes from the
  machine) in one `PUT /manifests`. It refuses, naming the key, when any input is missing, and on
  `hq.route: api` it also requires `hq.beadyard_id`. `--force` refreshes an existing manifest. A
  host with no `host.frame_id` is unchanged. The `--instance-ref` flag is new in wire 2.34.0.

### New and changed configuration keys

| Key | Scope | Env override | Meaning |
|---|---|---|---|
| `hq.route` | host | `BH_HQ_ROUTE` | `sql` (default) or `api`. |
| `hq.api.url` | host | none | Gateway `hq` plane base URL, e.g. `https://HQ_GATEWAY_ADDRESS:8443`. |
| `hq.api.unix_socket` | host | none | Absolute path of the Gateway `hq` plane proxy socket, instead of `url`. |
| `hq.api.client_cert`, `hq.api.client_key` | host | none | This host's mTLS identity (absolute PEM paths), set together. |
| `hq.api.ca_cert` | host | none | CA bundle that verifies the Gateway (absolute PEM path). |
| `hq.api.operator_public_key` | host | none | `ssh-ed25519 ...` operator key; authority, config and eligibility are verified against it. |
| `hq.api.floor_path` | host | none | Absolute path of this host's restore floor for api reads. **Required:** unset, every route=api authority read is refused and renew fails closed. |
| `hq.api.initial_revision` | host | none | Head the floor starts at when no floor file exists yet. |
| `hq.api.backend_identity` | host | none | Pinned HQ backend identity. |
| `hq.api.generation` | host | none | Pinned HQ authority generation (a string; see step C6). |
| `hq.beadyard_id` | host | none | Must equal the committed manifest's `beadyard_id` on `api`; `bh host init` on a `hq.route: api` frame refuses without it. |
| `host.instance_ref` | host | none | A frame's substrate instance identity; `bh host init --instance-ref` overrides it. Never inferred. |
| `host.frame.capabilities.isolation` | host | none | `kvm`, `microvm`, `container` or `workstation`. |
| `host.frame.capabilities.trust_zone` | host | none | `self-hosted` or `vendor-hosted`. |
| `host.frame.capabilities.harnesses` | host | none | List of harness names (set with `--json`). |
| `host.frame.capabilities.max_sessions` | host | none | Integer `>= 0`. |
| `hq.service_host` | host | `BH_HQ_SERVICE_HOST` | `true` on the hq-service host only. Default `false`. |
| `hq.sql_guard` | host | `BH_HQ_SQL_GUARD` | `warn`, `refuse` or `off`; unset by default. |
| `hq.mode` | fleet | none | Unchanged in 0.40.0 (see above). |

The five `hq.api` trust pins (`operator_public_key`, `floor_path`, `initial_revision`,
`backend_identity`, `generation`) are the same values hq-service is started with and the same
values a SQL frame already holds as `hq.sql.runtime_operator_public_key`,
`hq.sql.runtime_floor_path`, `hq.sql.runtime_initial_revision`,
`hq.sql.runtime_backend_identity` and `hq.sql.runtime_generation`.

### The SQL guard: what you will see

In 0.40.0 the guard acts only on a host that is `hq.route: api`, or that sets `hq.sql_guard`
explicitly. An unmarked `hq.route: sql` host stays silent. When something opens HQ SQL on such a
host it writes one stderr line per process, opener and call site, and changes no exit code or
output:

```text
warning: HQ SQL opener <Opener> is retired off the hq-service host (this host is hq.route: api; called from <module.function>). Reach HQ through hq-service (hq.route: api); a later bh release will refuse this. Silence: hq.sql_guard: off.
```

For placement, `bh hq migrate` and `prune-inbox` the text says `is hq-service-host-only` instead
of `is retired off the hq-service host`. `hq.sql_guard: refuse` raises `SqlPlaneRetired` before
connecting; `off` is silent. Setting `hq.route` back to `sql` silences it again (nothing else to
undo unless you set `hq.sql_guard`). You can preview the later default on a `sql` frame with
`hq.sql_guard: warn`.

`bh doctor` prints a new `# HQ route` section. The three shapes you will meet:

```text
# HQ route
  ⚠ WARN hq.route: sql
    hq.service_host: false
    hq.sql: present
    hq.sql_guard: unset
    hq.route is sql on a host not marked hq.service_host: the HQ SQL warning defaults on in a later release alongside refuse; cut this host over to hq.route: api
```

```text
# HQ route
  ⚠ WARN hq.route: api
    hq.service_host: false
    hq.sql: present
    hq.sql_guard: unset
    hq.route is api but a hq.sql block lingers: remove it (hq-service is the plane)
```

```text
# HQ route
  ✓ hq.route: api
    hq.service_host: false
    hq.sql: absent
    hq.sql_guard: unset
```

The last one is the post-cutover state of every host except the hq-service host. `bh doctor`
runs every section and can take a long time on a busy host; the quick equivalent is
`bh config get hq.route --scope host` (prints `api`) and `bh config get hq.sql --scope host`
(prints `✗ hq.sql is not set`, exit 1).

### Required first: hq-service and the Gateway

This release was proven against **hq-service `v0.1.0`, commit `4e0fc6b`**, in-process and against
its conformance corpora; no real staged run against a deployed hq-service is recorded (see
[the session cutover runbook](design/hq-service-session-cutover-runbook.md#2-required-before-the-cutover-a-real-staged-run-open-operator-step)).

1. **hq-service `v0.1.0` (`4e0fc6b`) is deployed and ready.** On the hq-service host, for the
   Compose deployment:

   ```sh
   docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}' "$(docker compose -f STACK_DIR/compose.yaml images -q hq-service)"
   ```

   Expect `v0.1.0 4e0fc6b...`. For a systemd deployment, confirm the binary at
   `/usr/local/libexec/beadhive-hq-service` was built from that commit. Then read readiness on the
   service's own socket (the Gateway does not expose it):

   ```sh
   sudo curl -sS --unix-socket /run/beadhive/hq-service/hq.sock http://hq/readyz
   ```

   Expect HTTP 200 and no `cutover_incomplete`. If an older build (before bh-hq-pio.7) serves
   today, first follow hq-service `deploy/README.md`, sections "Schema migrations" and "Cutover"
   (`hq-service migrate`, `hq-service cutover -dry-run`, backup, `hq-service cutover`), and flip
   each frame within about 120 s of the restart, as
   [the session cutover runbook, section 4](design/hq-service-session-cutover-runbook.md#4-service-step-5-switch-the-frame-within-one-ttl-of-the-restart)
   explains.
2. **hq-service is started with `-operator-apply`** if the operator will run rebind,
   release-policy apply or catalog import on `api`. Without it every apply is refused
   `operator_apply_disabled`. Its database account holds the grants of `deploy/README.md`, Steps,
   item 2, including the **trusted-mode** grants (open admission and manifest publication commit
   unsigned; the join below needs them).
3. **beadhive-gateway `v0.1.0` (`2a400f8`) is deployed** (the gate at the top). Required Gateway capabilities:
   - the `hq` plane signing with its own `hq-` key under issuer `gateway/hq` (`bh-gw-jozq`), and
     hq-service's `HQ_GATEWAY_ISSUER` set to `gateway/hq`;
   - bound-identity attestation **on** (`principal_kind`, `frame_id`, `host_id` claims), minting
     the caller kinds hq-service `v0.1.0` checks (`api/writes.go`): `operator` for
     `POST /incarnations`, `host` for `PUT` and `DELETE /manifests/{host_id}`, `frame` for
     `POST /admissions/{frame}`;
   - history-query routing: query strings on `GET /history?since=`,
     `GET /eligibility/{frame}?release_id=&release_digest=` and `GET /hive-leases?prefix=`;
   - the operator `/plan` and `/apply` routes: `POST /authority/rebind/plan|apply`,
     `POST /authority/release-policy/{frame}/plan|apply`,
     `POST /release-catalog/{catalog}/import/plan|apply`;
   - every other route bh 0.40.0 calls: `GET /authority`, `/config`, `/eligibility/{frame}`,
     `/hive-leases`, `/release-catalog/{catalog}`; `POST /sessions/{principal}/{epoch}/renew` and
     `/evidence`; `PUT /heartbeats/{request_id}`; `POST /hive-leases/{prefix}`;
     `POST /incarnations`; `PUT` and `DELETE /manifests/{host_id}`; `POST /admissions/{frame}`
     (all under `/hq/lifecycle/v0`).
4. **bh 0.40.0 is in the release catalog and admitted by each frame's release policy.** A frame
   that installs a release its grant does not admit turns ineligible. Check before step C1 below,
   on the host that imports releases today (with its catalog settings file, still on `sql`):

   ```sh
   bh hq release-catalog verify 0.40.0
   ```

   Expect exit 0. If 0.40.0 is not imported yet, import it the way you do today. If a frame's
   release policy does not admit 0.40.0, widen it first with `bh hq authority release-policy
   plan|apply`, also the way you do today on `sql`.

### Order of the cutover

1. The hq-service host: mark it (section "The hq-service host").
2. `bead-frame-01`: install 0.40.0, cut over, verify, and let it hold through at least two
   conformance intervals (10 minutes) before the next host.
3. `bead-frame-02`: the same.
4. `xeno-mac` (the operator workstation): its operator credential is needed for the
   `bead-frame-03` join.
5. `bead-frame-03`: a fresh join through hq-service.
6. `beadhive-factory`.

### When rollback to `route=sql` stops being possible

- **Every step in this note is reversible** while all three hold: the host's config backup from
  step C2 still exists (it carries the `hq.sql` block); the frame's SQL login and its
  `frame_<p>_<e>_session` / `_evidence` tables still exist on the HQ server; and, if hq-service
  `v0.1.0` (or any build from bh-hq-pio.7 on) started serving during this cutover, you roll that
  service build back too. Once that build serves, it judges liveness only from its own rows, so
  switching a frame back to `sql` alone leaves it stale there within about 300 s
  ([runbook section 9](design/hq-service-session-cutover-runbook.md#9-rollback)).
- **The point of no return is outside this note:** dropping a frame's SQL login
  (`DROP USER '<principal>'@...`) or its session and evidence tables, hq-service
  `deploy/README.md` "Retiring the Python layout". **Do not do that in 0.40.0.** After it, that
  frame can never return to `route=sql`.
- **`bead-frame-03` never has a `route=sql` state.** Its rollback is withdrawal (its section).

### The hq-service host

Only if bh is installed on the hq-service host. Nothing else in this note runs there except the
certificate and binding steps (G1-G2).

```sh
bh config set hq.service_host true --scope host
```

- Keys: `hq.service_host` (host). Expect `✓ hq.service_host = True`.
- Check: `bh doctor` shows `✓ hq.route: sql` and `hq.service_host: true` under `# HQ route`.
- This host stays `hq.route: sql`. Placement, `bh hq migrate` and `bh hq authority prune-inbox`
  run here only.
- Rollback: `bh config unset hq.service_host --scope host`.

### Gateway procedures (run on the hq-service host, as root)

These are the hq-service `v0.1.0` `deploy/compose/bootstrap.sh` commands and the Gateway
`hq-authn.json` binding shape, one client at a time. Paths are the Compose deployment's; for a
systemd deployment the Gateway credentials are `/etc/beadhive/gateway/credentials/hq-*` and the
unit is `beadhive-gateway-hq`.

**G1. Issue a client certificate** (`N` names the client, e.g. `bead-frame-01`):

```sh
sudo -i
umask 077
N=bead-frame-01
cd /var/lib/beadhive/hq-plane-ca      # where bootstrap.sh put the hq plane CA (ca.crt, ca.key)
openssl req -quiet -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$N" -keyout "$N.key" -out "$N.csr"
printf 'extendedKeyUsage=clientAuth\n' > "$N.ext"
openssl x509 -req -in "$N.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -days 365 -extfile "$N.ext" -out "$N.crt"
rm -f "$N.csr" "$N.ext"
openssl x509 -in "$N.crt" -outform DER | sha256sum | cut -d' ' -f1
```

- Keys: none. Expect the last line to print 64 hex digits: the certificate's identity for G2.
- Hand `$N.crt`, `$N.key` and `ca.crt` to the client (step C3). The key never goes anywhere else.
- Rollback: delete the three files; a certificate with no binding is refused (401).

**G2. Bind it in the Gateway.** Back up, edit a copy, validate, install, restart:

```sh
AUTHN=/etc/beadhive/gateway-hq/credentials/hq-authn.json
cp -p "$AUTHN" "/root/hq-authn.json.$(date +%Y%m%d%H%M%S)"
install -m 0600 "$AUTHN" /root/hq-authn.json.new
"${EDITOR:-vi}" /root/hq-authn.json.new          # add the binding to "bindings" (below)
python3 -m json.tool /root/hq-authn.json.new > /dev/null && echo valid
install -o root -g root -m 0400 /root/hq-authn.json.new "$AUTHN"
docker compose -f STACK_DIR/compose.yaml restart gateway-hq     # systemd: systemctl restart beadhive-gateway-hq
```

Gateway `v0.1.0` binds a certificate as exactly one `kind`: `worker`, `operator`, `host` or
`frame`. A `host` binding needs `host` and no `frame`; a `frame` binding needs both `frame` and
`host`. The scopes are `lifecycle:fleet` for manifests (`PUT` and `DELETE /manifests/{host_id}`,
`host` only), `lifecycle:admission` for admissions (`POST /admissions/{frame}`, `frame` only) and
`lifecycle:provision` for incarnations (`POST /incarnations`, `operator` only).

**One cert, one kind.** bh 0.40.0 has a single `hq.api.client_cert` and `hq.api.client_key`, and
the same `(provider, identity)` cannot be bound twice. A frame host therefore has one binding at
a time and swaps it: the `host` binding while it writes a manifest (`PUT` or `DELETE
/manifests/{host_id}`), the `frame` binding for admission, renew, eligibility and every other
call. Each swap is another run of G2 that replaces the entry for that certificate's identity.

A frame binding (`kind` frame):

```json
{"provider": "direct", "identity": "SHA256_FROM_G1", "principal": "FRAME_PRINCIPAL",
 "kind": "frame", "frame": "FRAME_ID", "host": "HOST_ID",
 "scopes": ["lifecycle:admission", "lifecycle:authority", "lifecycle:read",
            "lifecycle:session", "lifecycle:hive-lease", "lifecycle:heartbeat"]}
```

A host binding (`kind` host; its principal is a name you choose, matching
`^[a-z][a-z0-9_]{0,30}$`, for example `host_01`):

```json
{"provider": "direct", "identity": "SHA256_FROM_G1", "principal": "HOST_PRINCIPAL",
 "kind": "host", "host": "HOST_ID",
 "scopes": ["lifecycle:fleet", "lifecycle:read", "lifecycle:authority"]}
```

- `FRAME_PRINCIPAL` is the frame's hq-service registry principal (`frame_` plus 20 hex digits).
  The session routes refuse a call whose principal is not the caller's (`path_principal_mismatch`).
  Read it from the runtime database (`docker exec -it DOLT_CONTAINER dolt --host 127.0.0.1
  --port 3306 --no-tls --user root sql` on the Compose host):

  ```sql
  SELECT principal, frame_id, holder_identity, epoch FROM beadhive_hq_runtime.hq_principal_registry
  WHERE frame_id = 'FRAME_ID' ORDER BY epoch DESC;
  ```

  Use the row whose `holder_identity` is the frame's `HOST_ID` and whose epoch is highest.
- Expect `valid`, then the Gateway back up. Check from the client in step C5.
- Rollback: install the backup over `$AUTHN` (mode 0400) and restart the Gateway the same way.

### Cutover procedure for an existing host (C1-C10)

Run on the host being cut over, as the user that runs bh and its timers. Each step names the keys
it touches, what to expect, and whether rollback is still possible (it is, for every step, under
the conditions above).

C1-C10 writes no manifest: an existing frame's manifest is already published, `bh host init` and
`bh host rm` are the only steps that write one, and neither is in C1-C10. The frame therefore
holds only its `frame` binding (C4) throughout, and C1-C10 needs no binding swap. The swap
applies when you later write a manifest from this host (see "Known limitations").

**C1. Install 0.40.0, still on `route=sql`.**

```sh
uv tool install --force 'beadhive==0.40.0'      # keep your extras, e.g. 'beadhive[otel,github]==0.40.0'
BHPY="$(head -1 "$(command -v bh)" | sed 's/^#!//')"
"$BHPY" -c 'import beadhive.hq_frame_grant' && echo ok
```

- Keys: none. Expect `ok` (the module exists only from 0.40.0; do not trust `bh --version`).
- If the host runs the host daemon (`bh host daemon status`), restart it the way this host
  manages it (`bh host daemon stop`, then `bh host daemon start`). The timers pick up the new
  release on their next tick.
- Check: `bh host eligible` still reports the frame eligible. If `release_matches` fails, the
  grant does not admit 0.40.0: reinstall 0.39.0 (`uv tool install --force 'beadhive==0.39.0'`).
- `bh doctor` now shows the `⚠ WARN hq.route: sql` shape above. Expected.

**C2. Back up the host config.**

```sh
CFG="$(bh config path)"
cp -p "$CFG" "$CFG.pre-0.40.0"
HOST_ID="$(awk '/^host_id:/ {print $2}' ~/.beadhive/host.yaml | tr -d "'\"")"
echo "$HOST_ID"
```

- Keys: none. Expect a UUID. Keep `$CFG.pre-0.40.0` until the cutover has held: it is the
  rollback.

**C3. Install the client certificate from G1.**

```sh
install -d -m 0700 ~/.beadhive/hq-api
scp 'root@HQ_SERVICE_HOST:/var/lib/beadhive/hq-plane-ca/{N.crt,N.key,ca.crt}' ~/.beadhive/hq-api/
chmod 0600 ~/.beadhive/hq-api/*
```

- Replace `N` with this host's name from G1 (or copy the three files by any channel you trust).
- Keys: none.

**C4. Bind it** (G2 on the hq-service host, with this frame's `FRAME_ID`, `HOST_ID` and
principal). `bh config get host.frame_id --scope host` prints `FRAME_ID`.

**C5. Prove the binding before bh uses it.**

```sh
curl -sS --fail --cacert ~/.beadhive/hq-api/ca.crt --cert ~/.beadhive/hq-api/N.crt --key ~/.beadhive/hq-api/N.key https://HQ_GATEWAY_ADDRESS:8443/hq/lifecycle/v0/authority
```

- Keys: none. Expect `{"head": ..., "record": ..., "signature": ...}`. A 401 means the binding
  is missing; no TLS means the certificate is not from the plane's CA.

**C6. Write `hq.api` with the trust pins, and seed the api restore floor.** Read the pins this
host already holds:

```sh
bh config get hq.sql.runtime_backend_identity --scope host
bh config get hq.sql.runtime_generation --scope host
bh config get hq.sql.runtime_initial_revision --scope host
bh config get hq.sql.runtime_operator_public_key --scope host
bh config get hq.sql.runtime_floor_path --scope host
```

They must equal `HQ_BACKEND_IDENTITY`, `HQ_GENERATION`, `HQ_INITIAL_REVISION` in hq-service's env
file and its `operator.pub` (Compose: `/etc/beadhive/hq/`; systemd: `/etc/beadhive/hq-service/`).
Stop if they differ. Seed the api floor from the runtime floor, so the first verified read starts
at today's head (with no floor file it starts at `initial_revision` and must reach the current head
within 64 commits, `MAX_HISTORY` in `src/beadhive/hq_service_verifier.py`, or it is refused
`restore_floor`; derived from the code, not run against a real HQ):

```sh
install -m 0600 "$(bh config get hq.sql.runtime_floor_path --scope host)" ~/.beadhive/hq-api/floor.json
```

Then write the whole `hq.api` block in one command (`client_cert` and `client_key` must be set
together, and `generation` must stay a JSON string):

```sh
cat > ~/.beadhive/hq-api/hq-api.json <<'EOF'
{
  "url": "https://HQ_GATEWAY_ADDRESS:8443",
  "client_cert": "/ABSOLUTE/HOME/.beadhive/hq-api/N.crt",
  "client_key": "/ABSOLUTE/HOME/.beadhive/hq-api/N.key",
  "ca_cert": "/ABSOLUTE/HOME/.beadhive/hq-api/ca.crt",
  "operator_public_key": "ssh-ed25519 OPERATOR_PUBLIC_KEY",
  "floor_path": "/ABSOLUTE/HOME/.beadhive/hq-api/floor.json",
  "initial_revision": "INITIAL_REVISION",
  "backend_identity": "BACKEND_IDENTITY",
  "generation": "GENERATION"
}
EOF
"${EDITOR:-vi}" ~/.beadhive/hq-api/hq-api.json
bh config set hq.api --json --scope host "$(cat ~/.beadhive/hq-api/hq-api.json)"
```

- Keys: `hq.api.url`, `hq.api.client_cert`, `hq.api.client_key`, `hq.api.ca_cert`,
  `hq.api.operator_public_key`, `hq.api.floor_path`, `hq.api.initial_revision`,
  `hq.api.backend_identity`, `hq.api.generation` (all host).
- Expect `✓ hq.api = {...}`. Every path must be absolute (`hq.api ... must be an absolute path`
  otherwise). `hq.route` is still `sql`, so nothing uses these yet.
- Rollback: `bh config unset hq.api --scope host`.

**C7. Check the `beadyard_id` pin.**

```sh
bh config get hq.beadyard_id --scope host
bh host show "$HOST_ID" --json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("beadyard_id"))'
```

- Keys: `hq.beadyard_id` (host). Expect the same UUID twice. If the first prints
  `✗ hq.beadyard_id is not set` and the second a UUID, pin it:
  `bh config set hq.beadyard_id MANIFEST_BEADYARD_ID --scope host` (expect
  `✓ hq.beadyard_id = '...'`). If both are unset (`not set` and `None`), leave it.

**C8. Trial the route for one command, then persist it.**

```sh
BH_HQ_ROUTE=api bh hq authority status
BH_HQ_ROUTE=api bh host eligible
bh config set hq.route api --scope host
```

- Keys: `hq.route` (host); `BH_HQ_ROUTE` only for the two trial commands.
- Expect from `status` a JSON object with `"authority_ready": true`, from `eligible` the frame
  eligible, then `✓ hq.route = 'api'`. Setting the route reads the fleet config through
  hq-service; if hq-service is unreachable it fails with `HqPlaneUnavailable` and writes nothing.
- **From here the frame's SQL session row stops moving:** renew now goes only to hq-service.
- If any unit sets `Environment=BH_HQ_ROUTE=...`, that value wins over the config for that unit.
- Rollback: `bh config set hq.route sql --scope host` (with `hq.sql` still present).

**C9. Renew once, then verify liveness.**

```sh
"$BHPY" -m beadhive.heartbeat_sender renew
"$BHPY" -m beadhive.heartbeat_sender conformance
systemctl --user is-enabled beadhive-session-renew.timer beadhive-heartbeat-conformance.timer
```

- Keys: none. Expect `{"renewed": ...}` (exit 0), conformance exit 0, and `enabled` twice. On
  macOS check `launchctl print gui/$(id -u)/dev.beadhive.session-renew` instead of `systemctl`.
- `beadhive-heartbeat.timer` is now a no-op; leave it or
  `systemctl --user disable --now beadhive-heartbeat.timer`.
- After at least 10 minutes, count refusals; expect `0` (on macOS, `0` for each file):

  ```sh
  journalctl --user -u beadhive-session-renew -u beadhive-heartbeat-conformance --since -15min | grep -c 'refused'
  grep -c refused ~/.beadhive/heartbeat/renew.err.log ~/.beadhive/heartbeat/conformance.err.log   # macOS
  ```

  One `seq_not_advanced` tick that heals on the next tick is expected
  ([runbook section 7](design/hq-service-session-cutover-runbook.md#7-expected-noise-seq_not_advanced)).
- Check the pin is honoured on the api path: `bh host eligible --json` does not report
  `"beadyard_binding": false`. For a hive this frame runs: `bh host eligible --hive PREFIX`.
- Rollback: `cp -p "$CFG.pre-0.40.0" "$CFG"`, then `"$BHPY" -m beadhive.heartbeat_sender renew`
  (the next tick writes the SQL session row again).

**C10. Remove `hq.sql`, once the cutover has held.**

```sh
bh config unset hq.sql --scope host
bh config get hq.sql --scope host
bh doctor
```

- Keys: `hq.sql` (host), removed.
- Expect `✓ unset hq.sql`, then `✗ hq.sql is not set` (exit 1), then under `# HQ route`
  `✓ hq.route: api` and `hq.sql: absent`.
- Optional proof that no SQL path remains: `bh config set hq.sql_guard refuse --scope host`,
  then re-run C8's `status` and `eligible` and C9's `renew`; each must succeed. Any HQ SQL opener
  now raises `SqlPlaneRetired` instead of connecting. Undo with
  `bh config unset hq.sql_guard --scope host`.
- Rollback: still possible. `cp -p "$CFG.pre-0.40.0" "$CFG"` restores `hq.sql` and the old
  route together (under the conditions in "When rollback to `route=sql` stops being possible").

### bead-frame-01 and bead-frame-02: in-place upgrade, then `route=api`

1. G1 on the hq-service host with `N=bead-frame-01`.
2. C1-C3 on `bead-frame-01`.
3. G2 with the frame binding for `bead-frame-01` (C4).
4. C5-C9 on `bead-frame-01`. Wait 10 minutes; C9's journal check prints `0`.
5. C10 on `bead-frame-01` when you are satisfied the cutover holds. Keep `$CFG.pre-0.40.0`.
6. Repeat 1-5 for `bead-frame-02` (`N=bead-frame-02`).

Rollback (either frame): `cp -p "$CFG.pre-0.40.0" "$CFG"`, then
`"$BHPY" -m beadhive.heartbeat_sender renew` and expect `{"renewed": ...}`; `bh doctor` shows the
`⚠ WARN hq.route: sql` shape again. To also return to 0.39.0:
`uv tool install --force 'beadhive==0.39.0'`. Optionally remove its Gateway binding (G2 rollback).

### xeno-mac: the operator workstation

The operator signs with the operator key `K` (an absolute path to the private key); the
workstation's Gateway binding is an operator binding.

1. G1 with `N=xeno-mac`. G2 with an operator binding:

   ```json
   {"provider": "direct", "identity": "SHA256_FROM_G1", "principal": "hq_operator",
    "kind": "operator",
    "scopes": ["lifecycle:operator", "lifecycle:provision", "lifecycle:read", "lifecycle:authority"]}
   ```

   `lifecycle:provision` is what lets this host register a joining frame's incarnation.
2. C1-C3, C5. C6: if this host has no `hq.sql.runtime_*` pins, take the four pins from
   hq-service's env file and `operator.pub`, and seed the floor from `bead-frame-01` after its
   cutover:

   ```sh
   scp bead-frame-01:.beadhive/hq-api/floor.json ~/.beadhive/hq-api/floor.json
   chmod 0600 ~/.beadhive/hq-api/floor.json
   ```

3. C8, without `bh host eligible` if `bh config get host.frame_id --scope host` prints
   `not set` (this host is not a frame; skip C7 and C9 then).
4. Verify the operator verbs (expected output from bh-lejfy):

   ```sh
   bh hq authority status
   bh hq release-catalog verify --all
   ```

   `status` prints JSON with `"authority_ready": true` and a `"revision"` (the current head).
   `verify --all` needs the `beadhive[release-verify]` extra and exits 1 if any row fails.
5. C10 when it holds. Keep the SQL operator settings file (`BH_HQ_OPERATOR_SETTINGS`): the verbs
   deferred to a later release still need it (below). **Leave `hq.sql_guard` unset on this host** for
   the same reason.

Operator commands on `api` (run here, `K` = absolute path to the operator private key):

```sh
bh hq authority rebind --frame FRAME_ID --confirm --operator-key K --expected-revision HEAD
bh hq authority release-policy plan --frame FRAME_ID --spec '^0.40'
bh hq authority release-policy apply --frame FRAME_ID --spec '^0.40' --plan-sha256 PLAN_SHA256 --confirm --operator-key K
BH_HQ_OPERATOR_KEY=K bh hq release-catalog import 0.40.0
```

- `rebind` prints `{"revision": "<new head>"}`; a stale `HEAD` exits 1 with `stale_revision`. In a
  trusted fleet the key is not used (`signature_needed` false). `--duration 7d` is optional;
  without it the authority does not expire.
- `release-policy plan` prints JSON with `plan_sha256`. On `api` that digest is api-only and
  differs from the SQL plan digest. Pass `--allow-downgrade` / `--allow-unknown-readers`
  identically on plan and apply.
- `release-catalog import` prints a result of `inserted` or `unchanged`; it is always signed.
  Add `--bundle F --wheel F --trusted-root F` for an offline import.
- `hq.release_catalog.auto_import`, if on here, still writes the catalog through the SQL
  settings file (it warns on `api`). `release-catalog revoke` and `provision` are refused on
  `api`: run them with `BH_HQ_ROUTE=sql` and the SQL settings file until a later release.

Rollback: `cp -p "$CFG.pre-0.40.0" "$CFG"`. Every operator verb then uses the SQL settings file
again.

### bead-frame-03: a fresh join through hq-service

No SQL login, grant or escrow is created at any step, and `hq.sql` is never set. The fleet must
be trusted (`hq.default_authority_mode: trusted`): a signed fleet has no api join.
`xeno-mac` must be on `api` with its operator binding first.

The join is: the frame writes its manifest (J7), the operator publishes it with the rebind and
registers the incarnation (J8), the frame admits itself (J10). On bh 0.40.0-0.40.2 the manifest
J7 publishes is never served (no step moved the authority onto it, `bh-hb75g`); use a bh with
`bh hq authority publish-manifest` on `xeno-mac` and hq-service v0.1.3 (J8's preconditions).

**A trusted fleet keeps an operator-signed authority** (hq-service v0.1.4, `bh-hq-gr9`; bh
`bh-j7pdz`). Whenever the operator passes `--operator-key`, bh offers the signature (`can_sign`)
and signs every operator publication (`publish-manifest`, `rebind`, `release-policy`), so the
authority stays operator-signed and a host in the default signed authority mode keeps reading
it, learning the trusted fleet from the signed snapshot. Over an operator-signed authority
hq-service v0.1.4 refuses every unsigned publication (`operator_signature_required`), the frame's
admission included: J10 is then a signed hand-off (J10a-J10c). With hq-service v0.1.3 bh still
signs whenever it holds the key (it re-plans without `can_sign`, which v0.1.3 does not know), but
v0.1.3 admits the frame unsigned and so unsigns the authority: run v0.1.4 when the authority is
signed.

Steps J4 and J9 bind the same certificate first as `kind` host (to publish the manifest) and then
as `kind` frame (to admit and renew). Gateway `v0.1.0` binds a certificate as exactly one kind, so
J9 replaces J4's entry; it does not add a second one.

**J1. Install 0.40.0 and the host identity.** C1 (without the `eligible` check), then:

```sh
grep -E '^(host_id|signing_key):' ~/.beadhive/host.yaml
```

Expect a `host_id` UUID and an absolute `signing_key` path with `SIGNING_KEY.pub` beside it.

**J2. Declare the frame in HOST config.** On `bead-frame-01`, read the pin:
`bh config get hq.beadyard_id --scope host`. On `bead-frame-03` (values are examples; use this
frame's):

```sh
bh config set host.frame_id bead-frame-03 --scope host
bh config set host.instance_ref INSTANCE_REF --scope host
bh config set host.frame.capabilities.isolation kvm --scope host
bh config set host.frame.capabilities.trust_zone self-hosted --scope host
bh config set host.frame.capabilities.harnesses --json '["claude"]' --scope host
bh config set host.frame.capabilities.max_sessions 2 --scope host
bh config set hq.beadyard_id BEADYARD_ID_FROM_FRAME_01 --scope host
```

- Keys (all host): `host.frame_id`, `host.instance_ref`, `host.frame.capabilities.isolation`
  (`kvm`, `microvm`, `container` or `workstation`), `.trust_zone` (`self-hosted` or
  `vendor-hosted`), `.harnesses` (a list), `.max_sessions` (integer `>= 0`), `hq.beadyard_id`.
- Expect one `✓ <key> = ...` line each (`harnesses` prints `['claude']`, `max_sessions` prints
  `2`). Check: `bh config get host.frame.capabilities --scope host` prints all four.
- `INSTANCE_REF` is the substrate's identity for this machine (for example its VM name); bh never
  infers it. You may pass it as `bh host init --instance-ref INSTANCE_REF` in J7 instead.
- `hq.beadyard_id` is required on `api`: `bh host init` refuses without it.

**J3. Certificate.** G1 with `N=bead-frame-03`, then C3 on `bead-frame-03`.

**J4. Host binding.** G2 with the `kind` host binding (G2's host JSON), `"host": "HOST_ID"` and
a host principal you choose, `host_bead_frame_03`. It stays the only binding for this certificate
through J8, so J7's `PUT /manifests/HOST_ID` is host-bound.

**J5. `hq.api`.** C5 and C6, with the four pins from hq-service's env file and `operator.pub`,
and the floor seeded from `bead-frame-01`:

```sh
scp bead-frame-01:.beadhive/hq-api/floor.json ~/.beadhive/hq-api/floor.json
chmod 0600 ~/.beadhive/hq-api/floor.json
```

**J6. Route, in HOST config.**

```sh
bh config set hq.route api --scope host
```

Expect `✓ hq.route = 'api'`. Set it in the HOST config, not only through `BH_HQ_ROUTE`: in
0.40.0 `bh host init` decides whether `hq.beadyard_id` is required from the raw `hq.route` key
(`bh-wyofx`), so a frame on `api` only through the environment is not refused early and fails
later at renew with a beadyard mismatch.

**J7. Write the manifest (frame, host-bound).**

```sh
bh host init --role executor
```

- One `PUT /manifests/HOST_ID`. In a trusted fleet it commits the manifest, but hq-service does
  not serve it yet: the served config is the one the authority pins. Expect `✓ published HOST_ID
  to HQ; pending a rebind`, naming the local copy it leaves for the operator
  (`<hq>/hosts/HOST_ID.yaml`) and `bh hq authority publish-manifest`. A missing input is refused
  before anything is written, naming the key (for example `set host.instance_ref in the HOST
  config or pass --instance-ref`, or `set host.frame.capabilities.trust_zone in the HOST
  config`); set it and re-run. To republish after changing a key: `bh host init --role executor
  --force`.
- `bh host show HOST_ID` prints "no host manifest in HQ" until J8 publishes it: expected here.

**J8. Publish with the rebind, then register the incarnation (operator, on xeno-mac).**

Preconditions (`bh-hb75g`):

- hq-service **v0.1.3 or later**, started with **`-operator-apply`**. An older service answers
  404 to the publication route; without the flag the apply is refused `operator_apply_disabled`
  (the plan still works).
- hq-service's config-database account holds the grants the publication needs (hq-service
  `deploy/README.md`, "Signed-fleet manifest publication"): `INSERT`, `UPDATE`, `DELETE` on
  `hq_config_meta`, `hq_config_documents` and `hq_config_publications`, and `EXECUTE` on
  `dolt_add`, `dolt_commit` and `dolt_branch` (the plan predicts the config commit on a scratch
  branch), plus the runtime operator grants (`UPDATE` on `hq_authority`, `EXECUTE` on `dolt_add`,
  `dolt_commit`). A signed fleet holds none of the config grants by default; a trusted
  deployment has the config ones except `dolt_branch`. Without them the plan fails as a storage
  error.
- The fleet config carries the `fleet.json` bh publishes beside `fleet.yaml`. hq-service before
  **v0.1.2** (the `bh-hq-3k8` fix, merge `f5dba49`, first tagged in v0.1.2) cannot read such a
  config and refuses `config_unreadable`; v0.1.3 includes the fix.

Copy the frame's manifest (`<hq>/hosts/HOST_ID.yaml`, the path J7 printed) to `xeno-mac` as
`~/HOST_ID.yaml` and the frame's public key (`SIGNING_KEY.pub` from J1) as
`~/bead-frame-03.pub`. Preview, then publish:

```sh
bh hq authority publish-manifest --manifest ~/HOST_ID.yaml --plan
bh hq authority publish-manifest --manifest ~/HOST_ID.yaml --confirm
```

- `--plan` writes nothing. Expect JSON with `"plan": true`, `"host_id"`, `"manifest_sha256"`,
  `"config_head"` and `"config_head_after"`. After J7's `PUT`, expect `"config_published":
  false`: the config already holds the manifest and only the rebind runs.
- `--confirm` publishes the manifest (if needed) and rebinds the authority onto the config head
  that holds it, in one operation. There is no separate `bh hq authority rebind` step. Expect
  `"outcome": "accepted"` and a new `"revision"`. Add `--operator-key ~/.ssh/OPERATOR_KEY` (to
  `--plan` too, so the plan shows `"signature_needed": true`): a signed fleet requires it, and in
  a trusted fleet it keeps the authority operator-signed (see above). Only a key-less trusted
  fleet, whose authority is unsigned and whose hosts are pinned `hq.authority_mode: trusted`, may
  leave it out. A refusal `operator_signature_required` says why and names the key: the
  authority is operator-signed, or the fleet is signed.
- If it reports `did not complete`, run the same command again: bh has already re-sent the
  apply, and a new plan sees the committed manifest and only rebinds. `stale_revision`,
  `plan_expired` or `plan_mismatch`: run it again.
- Check, from the frame: `bh host show HOST_ID --json` prints `frame_id`, `instance_ref`,
  `release`, `capabilities` and a `beadyard_id` equal to J2's pin.

Then register the incarnation:

```sh
bh hq authority join --frame HOST_ID --public-key ~/bead-frame-03.pub --confirm
```

Expect JSON with `"registered": true`, `"admitted": false`, and `"principal"` and `"epoch"`.
Note the principal. If it refuses with "hq-service does not serve it", the publication above has
not completed: re-run it.

To preview a plain rebind at any time: `bh hq authority rebind --plan` (writes nothing; shows
`config_moved` and the heads). A `rebind --frame` additionally needs every managed hive's
`frame_policy.config_revision` to agree, else `rebind_policy_ambiguous`; the publication above
rebinds no frame grant and has no such precondition.

**J9. Frame binding.** G2 again for `bead-frame-03`: replace J4's entry (same
`identity`) with the `kind` frame binding (G2's frame JSON) with `"principal": "PRINCIPAL_FROM_J8"`,
`"frame": "bead-frame-03"` and `"host": "HOST_ID"`. From here the certificate can no longer write
a manifest.

**J10. Admit (frame, frame-bound).**

```sh
bh hq authority join --confirm
```

Expect `"admitted": true` (`bh host provision --push` does the same). **The principal includes
the config head:** if the head moved between J8 and J10, this fails with
`incarnation_unprovisioned`. Then re-run J8's `join --frame`, update J9's binding with the new
principal, and re-run J10.

Over an operator-signed authority (hq-service v0.1.4, `bh-j7pdz`) J10 instead prints
`"admitted": false`, `"signature_needed": true` and `"admission_plan"`: hq-service refused the
unsigned admission and bh planned it. The frame holds no operator key, so the plan travels to the
operator as a file and back:

**J10a. Plan (frame).** The J10 command above. It writes the hand-off file
`~/.beadhive/state/hq-admission-HOST_ID.json` (`--record FILE` names another path) and prints
`"plan_expires_at"`: **J10b and J10c must complete within 15 minutes** of the plan's
`"issued_at"`. Running J10 again before the operator signs reports the same pending plan; after
the window it plans afresh. The file holds the plan, the frame's public key and, once signed,
the signature: nothing secret.

**J10b. Sign (operator, on xeno-mac).** Copy the file to `xeno-mac` as `~/admission.json`, then:

```sh
bh hq authority sign-admission --record ~/admission.json
bh hq authority sign-admission --record ~/admission.json --operator-key ~/.ssh/OPERATOR_KEY --confirm
```

- Without `--confirm` it reviews and writes nothing. Both check the plan against the current
  authority (read with the operator's binding) and refuse to sign unless it admits exactly this
  frame's host with this key at the current head, inside its window, and changes no other frame
  or authority field. Expect `"frame_id"`, `"host_id"`, `"key_fingerprint"` (compare with
  `ssh-keygen -lf ~/bead-frame-03.pub`), `"revision"` and `"next_revision"`; with `--confirm`,
  `"signed": true`.
- It calls no hq-service write route: the admission route is the frame's alone.
- "the authority moved since the frame planned" or "the plan's window has passed": re-run J10a.

**J10c. Apply (frame).** Copy the signed file back over
`~/.beadhive/state/hq-admission-HOST_ID.json`, then re-run `bh hq authority join --confirm`.
Expect `"admitted": true` and `"signed": true`; the file is removed. `stale_revision`,
`plan_expired` or `plan_mismatch` void the plan (the file is removed): re-run J10a.
`operator_signature_invalid`: J10b used a key other than the one hq-service verifies with
`-operator-key`. A refusal naming `hq.default_authority_mode: trusted` means hq-service is not
`-trusted` or the committed fleet default is signed: a signed fleet has no api join.

Why a hand-off and not the operator applying the admission (`bh-j7pdz`): Gateway v0.1.1 binds
`POST /admissions/{frame}` (plan and apply alike) to a `kind` frame principal of that frame
(`hqOwnFrame`), so an operator credential is refused there before hq-service sees it. Letting
the operator apply would mean widening the operator's binding to every frame's admission route.
The hand-off keeps each seat on its own routes (the frame on its own admission, the operator on
reads), keeps the operator key off the frame, and lets the operator see exactly what it signs.

**J11. Liveness.** The grant is ACTIVE as admitted, so it renews straight away:

```sh
"$BHPY" -m beadhive.heartbeat_sender units --install
systemctl --user daemon-reload
systemctl --user enable --now beadhive-session-renew.timer beadhive-heartbeat-conformance.timer
"$BHPY" -m beadhive.heartbeat_sender renew
"$BHPY" -m beadhive.heartbeat_sender conformance
```

Expect `{"renewed": ...}` and exit 0 twice; then C9's journal check and `bh host eligible`.

**J12. Verify.** `bh doctor` shows `✓ hq.route: api` and `hq.sql: absent` from the start.
Optionally set `hq.sql_guard: refuse` (C10) and repeat J11's `renew`.

Rollback for `bead-frame-03` is withdrawal, from the frame itself. `DELETE /manifests` is
host-bound and the certificate is now bound as `kind` frame, so first put the host binding back:
G2, replacing the frame entry for this identity with J4's `kind` host binding. Then:

```sh
bh host rm HOST_ID --force --confirm
```

Then remove its Gateway binding (G2). The admitted grant stays in authority: `frame-retire` has
no api route until a later release.

### beadhive-factory

The factory is cut over last, with C1-C10 like a frame, plus these checks first:

```sh
bh config get host.daemon.failover.enabled --scope host
bh config get hq.release_catalog.auto_import --scope host
```

- If failover prints `True`: the director's failover loop is placement, which is
  hq-service-host-only. Turn it off here before C8
  (`bh config set host.daemon.failover.enabled false --scope host`, then restart the host
  daemon), or keep the factory on `sql` in 0.40.0.
- If auto-import prints `True`: it keeps writing the catalog through its SQL settings file and
  warns on `api` (see xeno-mac).
- Operator verbs run from the factory with a frame (worker) binding are refused at the Gateway
  (403): run them from `xeno-mac`.

Rollback: as for `bead-frame-01`; re-enable the failover loop if you turned it off.

### Known limitations in 0.40.0

- **Catalog trust gap** (operator-accepted, bh-21vpt). hq-service `v0.1.0` catalog rows are
  unsigned. bh's interim restore-floor check accepts a same-sequence `hq_release_catalog`
  change when every row verifies by hq-service's own catalog verdict, so for catalog changes the
  service is a trust root, and a deleted row is invisible. Both close when `/history` reports
  row-level change kinds (`bh-hq-34d`).
- **Eligibility predicates on `api` are weaker than on SQL** for the conformance profile and
  `release_matches`.
- **The join principal includes the config head:** re-register (J8's `join --frame`) if the
  head moves before the frame admits.
- **A signed admission is a file hand-off** (J10a-J10c, `bh-j7pdz`): plan, operator signature
  and apply within the plan's 15-minute window; there is no HQ-carried hand-off yet.
- **`DELETE /manifests` is host-bound:** only the host itself can withdraw its manifest.
- **One certificate, one binding kind (bh-pm1s4).** bh 0.40.0 has a single `hq.api` client
  certificate and Gateway `v0.1.0` binds a certificate as exactly one kind. Every later manifest
  write from a frame host needs the swap: G2 puts the `kind` host binding back temporarily, then
  you run the write (`bh host init --role executor --force` after a release upgrade, or `bh host
  rm HOST_ID --force --confirm` for the rollback withdrawal), then G2 restores the `kind` frame
  binding. Until the restore the frame cannot renew or be admitted. A separate host certificate
  is tracked as bh-pm1s4 (not in 0.41.0).
- **`bh hq release-catalog revoke` and `provision` are refused on `api`;** they arrive in
  a later release.
- **`describe_incarnation` and `preflight` have no api route** (`HqUnavailable` naming the
  method).
- **`hq-parity` is unsupported** for the admission, operator and release-eligibility corpora
  ([HQ-PARITY-RUNS](HQ-PARITY-RUNS.md)).
- **Deferred to a later release (not 0.41.0):**
  - `cordon`, `drain`, `park`, `resume`, `quarantine`, `frame-retire`, `emergency`;
  - `observe`, `bind-beadyard`, `mode`, `release-upgrade`;
  - fleet config publication;
  - catalog revoke;
  - the guard's default flip to `refuse`.

## Shipped in 0.39.0 — upgrade every frame before the first `fleet.json` republish

- **Ordering requirement (bh-vrqne, bh-se4se).** Every publish and repair of the HQ SQL config now
  also commits `fleet.json`. A `bh` that predates it is inferred to reject that unknown document
  path and fail the whole snapshot read closed (inferred from the path pattern, not run). Upgrade
  every frame and host first, then republish; the republish is one-way until then. See
  [the migration runbook](design/dolt-hq-config-migration-runbook.md#fleetjson-projection-bh-vrqne).

## 0.37.1 → 0.38.0 — backfill for merge-heavy and forked repositories

- **New, opt-in: the git reader's fork boundary and tag filters (bh-19q4u, #159).** The `git`
  reader options `exclude_revs` (refs resolved on every run and excluded, so a fork reads only its
  own commits and follows upstream with no config edit), `tag_scope = range`, `tag_include` /
  `tag_exclude` (globs, exclude wins) and `since_date` (an explicit date; `since` is unchanged)
  are set under `backfill.sources.git.options` or per hive in
  `backfill.hives.<prefix>.sources.git.options`. Nothing changes unless you set them. A fork that
  rebases onto upstream changes its shas and so its unit keys. See
  [BACKFILL.md](BACKFILL.md).
- **Behaviour change: a branch-sync merge no longer makes a bead (#154, bh-8h2k8).** A merge whose
  subject is `Merge branch '<x>'` (optionally `into <y>`) or `Merge remote-tracking branch '<x>'`,
  and that names no pull request, used to become a unit like any merge; in one real repository 127
  of 169 merge units were such syncs, each proposed as a bead. Such a merge is now flagged
  `noise: branch-sync` and identify gives it a derived `SKIP` verdict, so a default run projects
  nothing for it. The unit stays listed by `bh backfill view unit`; the commits it brings in are
  released to the `commit` rule (so a `feat` commit becomes a commit unit as it would on a plain
  history); an operator `decide` and an existing `external_ref` still win; a pull request merge is
  never flagged. Beads an earlier run already made for a sync merge stay linked and are not
  removed. Opt out with `backfill.collate.noise = []`.
- **Behaviour change: merge units are titled from the pull request, not the merge subject (#154,
  bh-8h2k8).** Most merge subjects read `Merge pull request #N from org/branch`, which says
  nothing about the work. A merge unit is now titled by the first of: the merge body's first line,
  the subject of the oldest commit it brings in, a readable branch name, else the merge subject.
  Titles feed identify's lexical step, so verdicts of merge units can shift, and the git reader
  now records `body_head` on merge artifacts, so the first run re-reads the evidence once. Unit
  keys (the merge sha) do not change: a bead created earlier stays PRESENT through its
  `external_ref` and is never retitled. Opt out with `backfill.collate.merge_titles = subject`.
- **To get the 0.37.1 output back**, set both in the host config (or per hive under
  `backfill.hives.<prefix>.collate`): `noise = []` and `merge_titles = subject`.
- **New, opt-in: `backfill.collate.merge_types`.** Unset (the default) changes nothing. A list of
  merge types (conventional types, `untyped`, or `*`) limits the `merge` rule to merges of those
  types, derived from the branch prefix, then the merge's commits, and records each unit's `type`
  (see [BACKFILL.md](BACKFILL.md)).
- **New, opt-in: doc attachment for merge-heavy repositories (#157, bh-dviao).** Nothing changes
  unless you set them; each is also per hive under `backfill.hives.<prefix>.collate.attach`.
  `max_per_unit` caps what one unit takes from the `shared-path` and `lexical` tiers (`explicit`
  and `same-change` are exempt; the overflow goes to the next tier, then stands alone).
  `rename_guard` keeps a reorganising merge from collecting the docs it moved (the git reader then
  records a `renamed` attr, so the first run after turning it on re-reads the evidence). `own-add`,
  added to `tiers` after `same-change`, makes a unit of a doc a `docs:` commit added when no unit
  claimed that commit. `rank = proximity` picks the `shared-path` unit nearest in time to the doc's
  add commit and records the runner-up, shown by `bh backfill view unit`. `related` links are not
  part of 0.38.0. See [BACKFILL.md](BACKFILL.md).
- **New in reports: hidden commits (#154, bh-yg3ae).** A merge unit that ended residual (or NOISE,
  or an operator SKIP) used to swallow the commits it brought in: they were in no unit, child or
  link, so the plan silently lacked that work. The plan summary of `bh backfill history` now
  carries `hidden_commits` (`count`, `units`), `bh backfill view unit --json` a top-level
  `hidden_commits` with each unit's commit shas and a `hidden_commits` list on each affected unit
  row, and the text view prints them. The keys appear only when something is hidden; a run that
  hid nothing is byte-identical, and created beads do not change. Nothing to opt out of.
- **New, opt-in: `backfill.collate.identify.merge_fallback = commits` (#154, bh-yg3ae).** Replaces a
  merge unit that ends residual, NOISE or SKIP by the `commit`-rule units of its commits, in the
  same run. Default `none`. It gives up the one-bead-per-PR grouping for those merges, and never
  releases a merge an operator decided (`skip` or `noise`). To keep the old behaviour leave it
  unset.
- **New, opt-in: `backfill.collate.identify.merge_identity = commits` (#154, bh-yg3ae).** A merge
  none of whose commits has a baseline candidate is NEW instead of residual on a weak candidate its
  title matched; a merge whose commits partly match keeps the existing flow and the collation
  records which commits matched (`commit_candidates`). Default `unit`. Both options can be set per
  hive under `backfill.hives.<prefix>.collate.identify`.
- **New: `bh backfill decide --select` decides many units at once (bh-espkv, #162).** Repeat
  `--select <field><op><value>` (`=` or `~` regex, ANDed; fields `rule`, `kind`, `area`, `title`,
  `key`, `decision`, `status`, `type`, `noise`) instead of a unit key, for example
  `bh backfill decide --select noise=branch-sync skip`. It reads the run's `collation.json`
  (`--run-dir`), skips attach units and units that already have a live bead, supports
  `--dry-run`, and refuses more than `--max` matches (new `backfill.decisions.select_max`,
  default `200`, per hive under `backfill.hives.<prefix>.decisions`) without `--yes`. All
  decisions are written in one batch, each its own decision bead with the selector as its reason.
  The single-unit form is unchanged and nothing changes unless you pass `--select`. See
  [BACKFILL.md](BACKFILL.md#operator-decisions).
- **Collation files.** `bdlg.collation/1` gained members, written only when used (no version
  bump); a 0.37 collation still reads. A default 0.38 run over a repository with branch-sync
  merges writes the new vocabulary, which a `bh` older than 0.38.0 refuses, so do not read a 0.38
  run directory with an older `bh`. With both opt-outs above and no other new option the run
  writes the 0.37.1 collation byte for byte (as long as the attach options above stay unset). The
  identify options `merge_fallback` and `merge_identity` and the record member `commit_candidates`
  are written only when the opt-ins are on.

## 0.37.0 → 0.37.1 — backfill apply fixes

- **Fix: a create-mode `bh backfill history --until apply` no longer drops epic edges (bh-dvb8s,
  #163).** The apply wrote one bead per load in bead-id order, so a child whose id sorted before
  its epic lost its parent-child edge, and verification then failed with "unexpected corpus
  drift" and rolled back. The apply now writes a bead's edge targets first, and a loss the
  loader reports during a real write that the dry run did not announce fails the apply naming the
  bead and edge. Nothing to run; re-run a backfill that rolled back.
- **Fix: beads a create-mode backfill makes are no longer all P0 (bh-ohzms, #164).** `bd import`
  fills priority 0 when a record omits it, so every created bead (area epics and doc units
  included) ranked first for `ready` and `schedule`. Each create now states a priority from the
  new `backfill.collate.create_priority` (default `2`, `bd create`'s own default; `0`-`4`),
  overridable per hive under `backfill.hives.<prefix>.collate`. Beads created by earlier runs keep
  P0, since a matched bead is never rewritten. To find and re-prioritise the open ones, review the
  list first, then run the following (it also catches an `origin:backfill` bead you deliberately
  set to P0):

  ```sh
  bd list --label origin:backfill --priority 0 --status open,in_progress --limit 0 --json | jq -r '.[].id' | xargs -r bd update --priority 2
  ```

## 0.36.0 → 0.37.0 — backfill re-run control

There is no 0.36.1: the fixes planned for it ship here (see *Fixes* below).

- **Docs and planning readers take `exclude` (bh-pieo5, #155).** Set
  `backfill.sources.docs.options.exclude` / `backfill.sources.planning.options.exclude` to a csv of
  globs (trailing `/` = subtree, no `/` = file name at any depth) to keep templates, archives or
  generated docs out of a backfill; an explicit file an exclude matches is dropped with a warning.
  Unset by default, so existing runs are unchanged.
- **Default change: a backfilled doc bead is no longer closed by its add commit unless the doc
  says it is done (bh-f4t6r, #153).** `backfill.collate.status.doc_default` is new and defaults
  to `from-doc`. A doc-only unit (no merged or committed change, release, closed tracker item or
  plan done marker behind it) whose doc declares `draft`, `proposed` (`status.doc_open`) or a
  "design only" status is now created **open**, with its add commit as the creation moment and no
  close event; other doc units still close, at the add commit or else the declared date. *Why:*
  backfill writes a timeline, not a snapshot, so a draft doc's add commit must not be recorded
  as a close that never happened. *Effect:* those beads now appear in `bd ready`; filter them with
  `bd ready --exclude-label origin:backfill`. *Opt out:* set
  `backfill.collate.status.doc_default = "closed"` for 0.36 output (identical for git-history
  runs; a docs-only unit with a declared date now closes at that date in every mode but
  `open`). **Existing beads are untouched**: only beads created by a run after the upgrade
  follow the setting, and a created bead is never rewritten. A collation recorded by 0.36
  reads back as `closed`.
- **A docs-only `create = propose` run no longer creates beads from nothing (bh-uysl1).** A unit
  with no evidence moment at all (no add commit, no declared date) is skipped and reported as
  `skipped: no evidence moment` in the plan summary and `bh backfill view unit`, whatever
  `doc_default` is. The default `git,docs,planning` run changes only as above.
- **Per-hive backfill config (bh-g71fi, #156).** `backfill.hives.<prefix>.*` in the host config
  overlays the `backfill` block for one hive (`BackfillConfig` minus `judge`; mappings merge, lists
  and scalars replace), so `collate.areas`, `collate.create` and the readers' options can differ
  per hive. `bh config get <key> --hive <prefix>` prints the effective value and its layer. Off by
  default: a hive with no overlay behaves as before. See
  [CONFIGURATION.md](CONFIGURATION.md#backfill-hives).
- **`bh backfill decide` records per-unit decisions (bh-2sicb, #155).** `decide <unit-key>
  skip|noise|link [<bead>]` (and `decide --deleted <bead-id> skip` for a bead already deleted)
  writes one closed `decision` bead per unit into the hive's bead store; re-runs honour it, and
  `backfill.decisions.on_delete = ignore|report|skip` (default `ignore`) can detect deleted beads.
  A collation holding a decision uses the new `decision` step and `SKIP` class, which a pre-0.37.0
  reader rejects; without decisions nothing changes.
- **A re-run now appends the later transitions of a bead it created (bh-1vkff.3).** With
  `from-doc` creating open beads, a re-run moves an open `origin:backfill` bead to `in_progress`
  when its unit has an opened review, and closes it when closing evidence appears (a merged
  review or change, a release, a closed tracker item, a plan done marker), each at the evidence's
  own time and never rewriting an earlier event. A closed bead, a hand-made bead and a bead a
  person moved are never touched. New `backfill.collate.status.close_on_evidence` and
  `in_progress_on_evidence` (both default `true`; `false` keeps the current phase). Newly
  created beads whose unit has an opened review start `in_progress` instead of `open`. A doc
  bead now also matches the change its doc later attaches to, instead of a duplicate bead. See
  [BACKFILL.md](BACKFILL.md#timeline).

### Fixes (no action required)

- **Python 3.11 loads again (bh-oyueb).** 0.33.0 through 0.36.0 crashed at command load on
  Python 3.11 (`ValueError: mutable default <class 'mappingproxy'>`) because nine frozen-dataclass
  fields used a shared `MappingProxyType` default; they now use `default_factory`. 3.12+ was
  unaffected.
- **A rolled-back backfill apply no longer hides what it created (bh-12hka).** Rolling back a
  failed create-mode `bh backfill history` apply still retires the created beads (closed, reason
  `backfill-rollback`; never deleted), but the audit now names them (`rollback_retired`,
  `rollback_retired_count`), a created bead that is still open or missing after rollback is
  reported as `rollback_residual` (state `rollback_failed`) instead of being skipped, and a
  re-run revives a retired bead in place rather than matching it as already present.
- **Roadmap is read once on case-insensitive filesystems (bh-29lke).** With no planning files or
  globs configured, `ROADMAP.md` and `roadmap.md` resolved to the same file on macOS and every
  item appeared twice; discovery now collapses names that are the same file. Explicit `files` and
  `globs` are unchanged.
- **Re-run behaviour documented (bh-muu6r).** `docs/BACKFILL.md` gains "Re-running after hand edits":
  a re-run matches by `external_ref`, never rewrites an edited, closed or reopened backfilled bead
  and proposes a deleted one again as NEW, so close unwanted beads rather than deleting them.

## 0.35.10 → 0.35.11 — patch fixes

No action required. The new `bh doctor` findings are read-only: the two outbox ones are silent
while intake, the outbox or the tracker plugin is off, and the stray-delivery one reads only the
bead store.

- **Epic landing never plain-closes a bound report (bh-p28yo).** A report bound to an external
  issue is tracked, not adopted, by the planner; if one still carries a child-of edge,
  `bh work finish` leaves it open and warns instead of closing it past the intake close path.
- **Closed report, open issue (bh-cd03d).** `bh doctor` gains section `intake_reports` (also the
  `intake_reports` key of `bh doctor --json` and the `beadhive://doctor` resource). Its
  `closed_open_issue` rows list a closed bound report whose issue the run's single issue listing
  shows open, and name the repair: `bh work outbox backfill-close`, `bh work outbox draft` for the
  shapes backfill does not cover, or `outbox send` when a close is already drafted. No listing is
  made when no closed report is bound to the tracker project; a failed listing is one
  `issue_listing_failed` row.
- **Abandoned completion notice (bh-c0x0i).** `notification_behind` rows list a closed report
  whose completion comment was abandoned (delivery, notified and the abandoned record id). A
  report whose issue a sent close closed counts as told and is not listed, so reports you replied
  to by hand and closed with `backfill-close` stay quiet. Repair: draft the reply with
  `bh work outbox draft` plus `backfill-close`, or leave it when the silence was deliberate.
- **Stray delivery on a rejected report (bh-qwv0t).** `stray_delivery` rows list a rejected report
  that still carries `delivery:`, and the new `bh work delivery sweep [--repair]` clears it. It is a
  dry run unless `--repair` is passed, writes only the bead store and never contacts the tracker;
  follow it with `bh work labels sync --include-closed` to draft the label removal on the issue.
  The wire catalog gains the `work delivery sweep` operation.

## 0.35.9 → 0.35.10 — tracked GitHub reports reply, close and relabel their issue (bh-r5dxi)

No action required.

- **Labels sync covers closed reports with open issues (bh-vojht).** `bh work labels sync` (without
  `--include-closed`) now also drafts for a closed bound report whose issue the run's single issue
  listing shows open, so a report closed at completion no longer leaves its live GitHub issue with
  stale `bh:delivery/pending` / `bh:progress/started` labels. A closed report whose issue is also
  closed still needs `--include-closed`. Still one issue listing per run; drafts only.

- **`triage apply` drafts the reply of a tracked or new proposal (bh-ac6tz).** A `tracked`,
  `partially-fixed` or `new` proposal that carries `reporter_text` now plans a `draft reply` step
  after its accept, on a bound report with an outbox, instead of silently dropping the verified text
  (GitHub #96). The reply is drafted only, held for review, and carries no close: the work is not
  done, so the issue stays open. `--dry-run` lists the step; a re-run drafts nothing new; a proposal
  without `reporter_text` plans no reply.

- **Completion drafts the reply and the issue close (bh-cir2q).** A report tracked onto work used
  to close locally when the work merged (or was released) with nothing drafted unless
  `intake.external.milestones.draft_on_<event>` was on, so its GitHub issue stayed open and the
  reporter heard nothing. With the outbox on, the milestone that completes a bound report (merged
  under `close-on-merge`, released under `close-on-release`) now ALWAYS drafts its record,
  whatever `draft_on_merge` / `draft_on_release` say, and that record carries an `issue.close`
  (reason completed) on its comment while the tracker enables `issue.close`
  (`plugins.<id>.write.issue_close.enabled`; without it the record is comment-only). The report
  closes locally only once that record's comment is sent, abandoned or superseded by a settled
  record; approve and send it through the outbox as usual. `draft_on_merge` / `draft_on_release`
  now only choose whether a NON-completion milestone drafts a progress notice (a `merged` notice
  under `close-on-release`, a `released` notice under `close-on-merge`);
  `append_release_to_closed` still only lets that `released` notice be drafted, comment only, for
  a report already closed. Unbound reports and hives with the outbox off close at once, as before.
  A re-run of `bh work merge` / `bh work delivery release` drafts nothing new, except that an open
  report that already completed with no completion record gets it. Under `close-on-merge`, a
  `released` record drafted while the `merged` record's close is unsent supersedes it and inherits
  its close; the report stays open until the `released` record is sent or abandoned. With
  `issue.close` disabled on the tracker (`close_enabled` off) the completion record is
  comment-only and the report closes locally once it is sent, leaving the issue open by operator
  choice.

- **`outbox backfill-close` recovers tracked reports closed with their issue open (bh-iyog8).**
  Reports completed before this release (the 0.35.9 reports among them) were closed locally at
  `delivery:merged` / `delivery:released` with nothing public drafted, or with a comment-only
  reply, so their GitHub issues are still open. `bh work outbox backfill-close [--dry-run]` now
  also considers a closed bound report with that delivery whose issue the run's one listing shows
  open: it attaches an `issue.close` (reason completed) to the latest reply or milestone record
  that has a live comment and no close, even if that comment was already sent (the close then
  approves and sends on its own), and otherwise drafts the completion record (the `released` or
  `merged` wording) with the close. Delivery `pending`, an unlinked report and an issue that is
  closed already are skipped with a reason; a re-run drafts nothing twice. Drafts only: approve
  each close with `outbox approve --op close:<digest>` and send it as usual. Needs the outbox and
  `plugins.<id>.write.issue_close.enabled`, as before.

## 0.35.8 → 0.35.9 — escalate, submit history budget, landing, verdict self-heal (bh-69snf)

No action required.

- **Long `bh escalate` messages (bh-3egoy).** `bh escalate '<message>'` now accepts a message of any
  length: the bead title is the message's first line (cut at a sentence or word boundary, at most
  120 characters) and the full message is the bead description, so a long or multi-paragraph report
  is no longer rejected by bd's 500-character title limit and no longer truncated in `bh hq intake`
  / `bh work list`. A failed `bd create` is reported with bd's own error message instead of a bare
  `{`.

- **Submit history budget (bh-69snf).** `bh work submit`, `submit --group` and the merge-time
  re-check now count only a branch's first-parent commits over its base against `max_commits`, and
  check conventional subjects on those commits (the refresh merge commit included). Commits that a
  refresh merge of the integration branch brought in are already reviewed upstream and no longer
  count, so a child that merged a refresh is no longer refused with "N commits over base". A branch
  with more own commits than `max_commits`, or a non-conventional own subject, is still refused.
  Epic submit is unchanged. Fixes GitHub #88, #27 and #26.

- **`review_gate: gh:pr` submit opens the PR and the gate waits on it (bh-srb1r).** Before,
  `bh work submit` pushed the branch and opened a `gh:pr` gate with no PR number, so `bd gate check`
  reported `no PR number specified` forever and `bh work land` refused the bead as not
  `pr-pending`. Now submit opens the PR for the pushed branch against the bead's integration base,
  or adopts the PR already open for it (a resubmit never opens a second one); opens the review gate
  with that PR number (`--await-id`), so `bd gate check` resolves it when the PR merges; and records
  `landing=pr-pending`, so `bh work land <id>` closes the bead once GitHub reports the PR MERGED (it
  also resolves the review gate if `bd gate check` has not yet). `gh` must be on PATH and the hive
  GitHub-backed; otherwise, or when `gh pr create` fails, submit refuses before opening any gate.
  Agents that opened the PR themselves after submit no longer need to: submit adopts an open PR. A
  bead stuck by the old behaviour unsticks with a plain resubmit, which replaces the PR-less gate.
  Contribution (`kind: external`) hives are unchanged. The `pr-merge` gate that `merge`/`finish`
  open under `work.landing: pr` now carries the PR number too. Fixes GitHub #35 and #50.

- **A lost verdict pointer heals from its green run (bh-0qazu).** A crash, OOM kill or `SIGKILL`
  after a validation run's manifest completed green but before its verdict pointer was written no
  longer throws the green away: the next verdict read for the same tree and command rebuilds the
  pointer from that manifest (when the pointer is missing or still names the previous green) and
  reuses it instead of re-running the gate. Only a retained completed, qualifying-green, fresh
  manifest for exactly that tree and command hash can do this; red, none, running, stale and
  different-identity runs still miss, and `bh release await` still tells a red gate from a missing
  one. One new verb, part of the fix and published in wire 2.26.0: `bh work validation reindex
  [--rev REV | --tree TREE] [--dry-run] [--json]` rebuilds pointers from the retained run manifests
  and lists each identity as `rebuilt`, `unchanged`, `refused` or `revoked`, with the reason; it is
  idempotent and `--dry-run` writes nothing. Optionally run it once after upgrading (`--dry-run`
  first) if a crash left greens without pointers; prefer it over `--override-validation`, which
  records a bypass rather than the green that ran. See [WORKTREES.md](WORKTREES.md). Fixes GitHub
  #89 and #110.

### Landing closes a bead whatever its assignee (bh-ikygx)

`bh work land` closed a bead directly with no actor, so bd's assignee-vs-actor guard refused it
whenever the landing seat was not the developer seat that did the work (reports #87, #44, #132).
`land` now closes through the same assignee-aware path as `merge` and `finish`; the assignee guard
stays on `claim` and `resume`. `bh worktree mark-landed` records the resolved acting identity
(`$BH_DEV`, `work.identity`, then git; the published wire catalog cannot take a new `--as` parameter
within this major) on the close, and lets a dispatcher/merger seat or the operator close another
seat's bead; a `dev/<name>` seat may only close its own. A close that still fails now names `bh
worktree mark-landed <id>` instead of telling you to close it manually or force it.

## 0.35.7 → 0.35.8 — outbox speed (bh-womf3)

One new HOST-only key, `hq.authority_expiry_recheck_s` (default `300`, `0` re-reads on every
command), sets how often a command re-reads the authority status for the expiry warning. Two new
outbox keys pace every outbox write (on by default, below), and two new host-level files hold the
pace's ledger; no record or document changes shape. The changes below make an outbox batch, or a
single send, cheaper or measurable, and one paces and stops it.

### Every outbox write is paced: 80 a minute, 500 an hour, and a stop on the first rate limit (bh-eqtxv)

**Behaviour change on upgrade, with no config change.** `bh work outbox send` and `send-approved`
pace every GitHub **write request** they make (not every operation: a `labels.change` is one `POST`
plus one `DELETE` per removed label, and a rate-limit resend counts again), counted across every
command and every hive on the host. Design:
[`intake-outbox-paced-send-design.md`](design/intake-outbox-paced-send-design.md).

- **Every outbox write request is paced at 80 a minute** (GitHub's documented limit of 80
  content-generating requests a minute), counted in a host-level ledger that records each
  request as it starts, so at most 80 start in any 60 s on the host (the exact bounds and
  their exceptions are below). A 270-operation batch
  that would take about 164 s after this release's other changes takes about 210 s (about 3.5
  minutes); today's batch, before them, starts far fewer than 80 a minute and is not slowed in
  practice. Sends stay serial.
- **A batch stops before an operation whose requests do not fit in 500 an hour**, with
  `stopped: hourly_budget`; the outcome says when enough of the hour frees
  (`pacing.budget_frees_at`, and the time in `stopped_detail`). It never waits the hour out.
- **A batch stops on the first rate limit** GitHub answers (`stopped: rate_limited`), including a
  `retry-after: 0`; the request that saw it finishes its own retries. Before, the operation was
  reported and the batch went on.
- **A batch stops on the first `SIGINT`** after the operation in flight finishes and writes its
  outcome (`stopped: interrupted`; a second `SIGINT` aborts as before), and **on an unexpected
  error in one operation** (`stopped: error`, the command exits non-zero; that operation may be
  left `sending`, which the next run recovers and `bh work outbox reconcile` settles). Before,
  both aborted the whole batch.
- **JSON contract change:** `stopped` is now a **reason code** (`not_primary`, `rate_limited`,
  `hourly_budget`, `interrupted`, `error`) and its text moves to a new `stopped_detail`. Before,
  `stopped` was the `not_primary` detail text. Scripts that test `stopped` for truthiness keep
  working; scripts that parse its text do not. The batch JSON also gains
  `"pacing": {"writes_per_minute", "writes_per_hour", "requests", "waited_seconds",
  "budget_frees_at"}`, and the `timings` block a `pacer_wait` phase.
- **Operations not started are now listed in `failed`** with the stop reason (before, a
  `not_primary` stop left later reports out of the outcome). Every one stays `approved` with its
  grant, so **re-running `send-approved` continues where the batch stopped**, with no new approval.
- A single `bh work outbox send` can be refused with a new refusal reason, **`hourly_budget`**,
  when its request does not fit in the hour.
- **Two new host-level files**, created on first use: the write ledger
  `$XDG_STATE_HOME/beadhive/github-writes/<api host>.ledger` (falling back to
  `~/.local/state/beadhive/github-writes/`; `<api host>` is `api.github.com`, or the GHES host of
  `plugins.tracker-github.base_url`) and its lock `<api host>.ledger.lock`. Deleting the ledger
  forgets the last hour; both are safe to delete while no send runs. An unreadable ledger is
  reported once and counted as a full minute; a ledger that cannot be written stops the batch
  with `stopped: error` before any further write-ahead (a single `send` fails `store_error`).

**The bounds, exactly** (derived from the code and each pinned by a test):

- **Minute: at most 80 request starts in any 60 s on the host, strictly**, across every command
  and hive: each request start is one locked ledger step that waits while the last 60 s hold 80.
  Two exceptions: a **forward step of the wall clock** makes the ledger's entries look older, so
  in the minute after the step **at most 80 extra (100% over) may start, once per step** (a step
  back only delays); and a ledger that **stops being writable mid-operation** lets that one
  operation's remaining requests go unrecorded (at most its own requests, below), then the batch
  stops with `error`.
- **Hour: at most 500 request starts in any rolling hour**, except that a request inside an
  operation is never refused. **One command alone** goes over by at most its in-flight
  operation's **unexpected** requests (its expected ones are checked before the write-ahead):
  3 rate-limit resends per request plus, for `issue.fields`, one retry on stale field ids with
  its own 3 resends, so **at most 7 extra (1.4% over) for `issue.fields`, 3 × (1 + removals) for
  `labels.change`, 3 (0.6%) for every other kind**. **k commands racing** on the shared ledger
  can each also start their own in-flight operation's requests past the same last free slot, so
  **at most the sum of their in-flight operations' requests plus those extras** (two other
  one-request operations: 2 extra, 0.4%). Every request over the hour is recorded and stops its
  command with `hourly_budget`.

**The keys** (`intake.external.outbox`, per hive; each command applies its own hive's values to the
shared host count):

| Key | Default | Allowed |
|---|---:|---|
| `send_writes_per_minute` | `80` | `1` to `80` |
| `send_writes_per_hour` | `500` | `1` to `500` |

These are safety limits, not tuning knobs: a value above GitHub's documented figures is **refused
at validation** (never clamped), and there is **no unpaced mode** (no `0`). Lower values stay
configurable (an account shared with heavy web-interface use, a sandbox). The cap covers this
host's outbox writes only, not other hosts, bh's label-definition writes, `gh` or the web
interface on the same account; the stop on the first rate limit is the backstop for those.

**Rotation.** The defaults apply as soon as a host runs 0.35.8, with nothing written. Only a hive
that wants **lower** values sets the keys, and because config sections forbid unknown keys it does
so only after every host that loads the hive's config runs 0.35.8 (a fleet config write: it needs
the operator's binding). **Rollback:** lowering the keys is the only knob; downgrading a host below
0.35.8 restores unpaced sends there (remove the keys first if they were set). No wire change: the
operation catalog is unchanged, so the wire stays at 2.25.0; the intake outbox document keeps
schema 1.

### One GitHub client and one field-definitions read per command

- **One HTTP client per command** (bh-vy7dx). A command (`bh work outbox send-approved`,
  `bh work labels approve --batch`, the other intake verbs) builds one GitHub client and
  connection pool for the whole command instead of one per operation, which cost about 34 s over
  a 270-operation batch. The credential is unchanged: the token is still resolved once per
  operation and sent on each request, so with `credential.per_invocation_cache` off a token
  rotated or revoked mid-command is still picked up by the next operation. The host daemon's
  discovery timer keeps its adapters for its whole life and still builds a client per
  operation.
- **The org's issue-field definitions are read once per command** (bh-n0h15). An `issue.fields`
  write used to `GET /orgs/{org}/issue-fields` first every time (135 extra requests, about 47 s,
  in a 270-operation batch). The answer is now remembered for the command. It is checked, not
  trusted: a field or option missing from it, or a `404`/`422` from the write, drops it, reads
  the definitions once more and retries once (on the same single-use grant, since a `4xx` wrote
  nothing); a second failure is the normal error. The daemon remembers nothing across
  operations.

### Every batch measures itself (`timings` in `--json`, bh-17g6b)

`bh work labels sync`, `bh work labels approve --batch`, `bh work outbox send-approved` and
`bh work outbox send` add a measured `timings` object to their `--json` output: process start to
first work, the config load, the intake lock and host check, the store's reads and writes and
GitHub's requests (each as a count and a total in milliseconds), in total and per hold of the
intake lock. It is additive output only: no config key, no state change, and the outbox record and
receipt shapes are unchanged, so the wire stays at 2.25.0. The shape is documented in
[CONFIGURATION.md](CONFIGURATION.md#where-a-batch-spends-its-time-the-timings-block-bh-17g6b). The
`-m perf` model now reads a recorded profile (`tests/fixtures/intake_batch_profile.json`) instead of
constants in the test.

### The outbox, thread and labels verbs start faster (bh-lwz8a)

`bh work outbox <verb>`, `bh work thread <verb>` and `bh work labels <verb>` no longer import the
whole command tree: the entry point composes just those groups from the operation catalog, with the
same root callback (config and identity memos, migrations, warnings, telemetry, setup gate) and the
same handlers, so flags, output, exit codes and `--help` are unchanged. On the factory this cut the
per-process floor of one of these commands (wall clock less the verb) from about 4.9 s to about
3.3 s; `timings.startup` in `--json` shows it per run. Logging is configured as before (`log.level`
/ `log.format` for every record). Shell completion, `bh --help`, `bh work --help`, a group's own
`--help` (`bh work outbox --help`) and every other verb still go through the full tree. Commands run
outside a managed worktree also skip importing the worktree module for the telemetry overlay.
Nothing to do on upgrade. The single-send target of 3 s is not met yet: the process floor alone is
still about 3.3 s, before the host check and GitHub's requests (a single send models at about 6.8 s,
was 7.9 s); bh-wmmfo follows it up.

## 0.35.6 → 0.35.7 — typer 0.29 supported, the typer cap lifted

`bh hq authority --duration` and `bh hq placement --tenure` no longer use the typer internals that
typer 0.29 removed; they parse `7d` / `36h` / plain seconds through typer's public
`Option(parser=...)`, so the value on the wire is still whole seconds, `--help` still shows
`INTEGER`, and a bad value is still a usage error. 0.35.7 therefore allows `typer>=0.12,<0.30`,
and a fresh install resolves typer 0.29. Nothing else changes: no new keys and no state change.
From typer 0.28 on, typer itself prints other option types as `<int>`, `<str>` and `<bool>` in
`--help`; the two duration flags keep the explicit `INTEGER`.

If you repaired a 0.35.5 install with `--with 'typer==0.26.8'`, drop the pin when you upgrade:

```sh
uv tool install --force 'beadhive[otel,github]==0.35.7'
```

The repository's own `uv.lock` stays on typer 0.26.8, because its dev-only SDK generator
(`openapi-python-client`) requires `typer<0.28`. The release extras smoke installs the wheel with
fresh resolution, so it is the check that runs the newest allowed typer before each publish.

## 0.35.5 → 0.35.6 — a typer cap (fresh installs of 0.35.5 and earlier are broken)

typer 0.29.0 (released 2026-11-09) removed a private module and an option parameter that `bh hq
authority` and `bh hq placement` build on, and every bh since 0.22.6 allowed any typer from 0.12
up. A fresh install of any of those versions therefore resolves typer 0.29 and fails on every
command except `bh --version` with `ImportError: cannot import name 'types' from 'typer._click'`.
An install made before 2026-11-09 keeps its older typer and works until it is reinstalled or
upgraded.

0.35.6 requires `typer>=0.12,<0.29` and changes nothing else: no new keys and no state change.
Upgrade, or repair an existing 0.35.5 install in place by pinning the last typer it was tested
with, keeping the extras you installed:

```sh
uv tool install --force 'beadhive[otel,github]==0.35.5' --with 'typer==0.26.8'
```

The release smoke now runs `bh --help`, `bh hq authority --help` and `bh hq placement --help` in
each install it checks, not only `bh --version` (which answers before the CLI is imported), so a
dependency release that breaks the CLI fails the release job before publication. 0.35.7 supports
typer 0.29 itself and lifts the cap (bh-q2a2k; see above).

## 0.35.4 → 0.35.5 — `outbox reopen`, faster batches, no `delivery` on a `duplicate-of`

### Reopen a GitHub issue on the operator's say (`outbox reopen`, bh-ih484)

0.35.3 closes the GitHub issue of a fixed, rejected or duplicate report, and a close that is wrong
(or one somebody else made) had no way back through bh. This release adds the `issue.reopen`
tracker operation behind `plugins.tracker-github.write.issue_reopen.enabled` (default `false`), an
`issue.reopen` operation kind in the outbox (its own digest, single-use grant and receipt) and one
new verb, `bh work outbox reopen <report> [--text T | --reply-file F] [--dry-run] [--json]`. The
PATCH carries only `state: open` and never the labels.

**A reopen is only ever the operator's explicit act.** Nothing drafts one on its own: `triage
apply` and `wake` do not. The verb drafts, for ONE report whose bound issue reads closed (closed by
anyone: the operator chose to), a reply carrying the reopen. Its text is `--text`, the contents of
`--reply-file` (either one must hold some text: blank is refused as `invalid_request`), or a short
template when neither is given. It refuses (a stable `reason` in `--json`) with the key off
(`reopen_disabled`), for an issue that is not closed or cannot be read (`reopen_not_needed`), and
while a reopen for the issue is already pending, unsent and unretired (`reopen_pending`, naming the
record: send it or `outbox discard` it first, so there is never a second one). `--dry-run` makes
every check and prints the planned reopen (`state: open`) and the reply text, writing nothing.
Nothing is approved or sent by the verb: review with `bh work outbox show <id>`, approve with `bh
work outbox approve <id> --op reopen:<digest>` (and `--op comment:<digest>` for the reply), and send
with `bh work outbox send-approved`.

**A reopen goes before its reply.** The reply's comment is refused (`reopen_not_ready`, before any
request) until the reopen is sent or retired, and the reopen is refused while another record of the
same issue still holds an unsent close. Per report the order, and the `send-approved` batch order,
is reopen, comment, labels, close. **Only `send-approved` guarantees that order across separate
records**: a question drafted on its own (`needs-info`) for a still-closed issue is a separate
record and is not held back by a pending reopen, so send the reopen first or use the batch.
Discarding the reopen retires its reply with it, and a report rejected or rerouted afterwards
retires its pending reopen and reply too.

The new verb is an additive wire change: the wire release moves from 2.24.0 to 2.25.0 (one new
operation, `work.outbox.reopen`); the intake outbox document keeps schema 1.

### Faster outbox, labels and intake commands (bh-25fvb)

Nothing to migrate: no record, receipt or document shape changes, and no new verb. What changes is
how often the work is done:

- **The intake lock's host check is about 1.9 s, not 13 s** (factory). The lock asked
  `guard_primary` and then `live_epoch` on every hold, and each re-ran the whole frame-eligibility
  evaluation; it now takes both from ONE evaluation (`guard.primary_epoch`), the lease
  `require_intake` just read is not read again, and the pure parts of an eligibility read are
  memoized: SSH key fingerprints (410 `ssh-keygen` processes per check before), the committed config
  carrier's validation, the HOST file inside one command, and the authority-expiry warning's read
  (skipped in trusted mode, at most once per 300 s otherwise). Refusals are unchanged.
- **Every one-shot `bh` command starts about 1 to 1.2 s sooner than on 0.35.3.** The operation
  catalog is no longer parsed quadratically at import (0.8 s), the config partition's leaf paths are
  found in linear time, and the stale-schema nudge reads `schema_version` from the config the
  command already loaded instead of fetching the SQL fleet snapshot a second time. On the factory
  `bh work outbox --help` went from about 5.1 s to 3.9 s and `bh work outbox show <id>` from about
  6.5 s to 5.3 s.
- **The batches pay per report, not per operation.** Under each hold a report is read once (the
  store session); `bh work outbox send-approved` sends a report's operations under one hold, and
  `bh work labels approve --batch` writes a report's approvals as one document write. Every
  operation still has its own digest, single-use grant, write-ahead and receipt.
  `bh work labels sync` now drafts inside the same batch scope, so
  `intake.external.outbox.batch_recheck_seconds` covers it too.

Modeled on measured latencies (`tests/test_intake_outbox_batch_perf.py`, `-m perf`) for the type
and Priority of 135 issues, with `batch_recheck_seconds: 300`: labels sync about 57 s (745 s
before), labels approve --batch about 46 s (1440 s), send-approved about 256 s (1770 s; 238 s with
the token cache below); a single send about 8 s (about 21 s).

**New opt-in key, default off:** `plugins.tracker-github.credential.per_invocation_cache` keeps the
first GitHub token resolved in memory for the rest of one command instead of running
`gh auth token` for every operation. It is the one exception to the never-cached credential: never
written or logged, and a token rotated mid-command is picked up by the next command. The host
daemon's discovery timer never caches, whatever the key says: it keeps its adapters for the
daemon's whole life, so it derives the token per operation as before. Set it only after every
host runs this release (see the rotation order below).

**Operator step after rollout:** once every host runs this release, set
`intake.external.outbox.batch_recheck_seconds: 300` at the factory's host scope (the default `0`
still checks the host on every hold, about 1.9 s each).

### A `duplicate-of` no longer carries `delivery` (bh-5kpwc)

`bh work triage apply --reviewed` linked a `duplicate-of` report to its canonical report's work
through the ordinary link, which wrote `delivery:pending` on the duplicate. The label projection
then published `bh:delivery/pending` on the closed duplicate's issue, and a later merge, release,
claim or notice could draft a milestone note for it, repeating the one the canonical report gets.
Now a `duplicate-of` link writes no `delivery` value (and clears one already there) and catches
nothing up; no milestone trigger drafts for a rejected report; and the label projection emits no
`bh:delivery/*` or `bh:progress/*` for one, so a `labels sync` drafts the removal of those labels.
A `tracks` link (`accept --work`, `--new-work`, `bh work link`) still writes `delivery:pending`. This
change adds no config key and no wire change.

**Repair reports already in this state.** List them first (the default writes nothing), then clear
the value, then draft the label removal on GitHub and review it as any label batch:

```sh
uv run python scripts/repair_duplicate_delivery.py --repo <hive checkout>
uv run python scripts/repair_duplicate_delivery.py --repo <hive checkout> --apply
bh work labels sync --include-closed
```

`bd set-state` cannot clear a dimension, so `--apply` removes the `delivery:<value>` label cache
entry with `bd label remove`; the bead's event history keeps the old value. Nothing is sent to
GitHub and no outbox record is written until an operator approves the label batch.

### Rotation order for both new keys (the 0.35.0 pattern)

This release adds two config keys, both default off:
`plugins.tracker-github.write.issue_reopen.enabled` and
`plugins.tracker-github.credential.per_invocation_cache`. An older host rejects either one
(`extra=forbid`), and fails closed on an `intake.outbox` document holding an `issue.reopen`
operation. So:

1. Install the release on **every** host that reads the hive's outbox (any host that runs
   `bh work outbox`, `bh work triage apply`, `bh work wake`, `bh doctor` or a merge / release hook
   on that hive).
2. Only then set either key in a shared config.

With `write.issue_reopen.enabled` unset nothing is read, drafted, approved or sent for a reopen;
with `credential.per_invocation_cache` unset every GitHub operation derives its token as before.

## 0.35.2 → 0.35.3 — close the GitHub issue after the reply and labels (switch default off)

`bh work triage apply --reviewed` closed fixed, rejected and duplicate reports in beads but left
their GitHub issues open. 0.35.3 adds the `issue.close` tracker operation behind
`plugins.tracker-github.write.issue_close.enabled` (default `false`) and an `issue.close`
operation kind in the outbox. With the switch on, apply drafts the close on the reply record
(`fixed` → `completed`, `reject` → `not_planned`, `duplicate-of` → `duplicate`; `needs-info` never
closes). Each close has its own digest, single-use grant and receipt: approve it with
`bh work outbox approve <id> --op close:<digest>` and send it with
`bh work outbox send <id> --kind close`. A close is refused (`close_not_ready`, before any
request) until the record's comment is sent, no other comment of the report is unsent (send or
discard a newer reply first), and the report's latest labels record is sent (an abandoned or failed
one needs a `bh work labels sync` first), so send per report in the order comment, labels, close.
The close PATCH never carries labels.

**`duplicate-of` changes whatever the key says.** While `intake.external.outbox.enabled` is on,
apply now drafts a reply for a `duplicate-of` report too (the reviewed `reporter_text`, or "This is
a duplicate of #N; the work is tracked there."), and every reply is drafted BEFORE the report is
linked or rejected, so a reply that cannot be drafted leaves the report untouched; the steps are
`draft reply, link, reject`. A rerun drafts the same reply once. With the outbox off (the default)
a `duplicate-of` is linked and rejected with no reply and no close, exactly as on 0.35.2.

`bh work outbox backfill-close [--dry-run]` drafts the close of every report already closed in
beads whose bound issue is still open and whose applied triage outcome was fixed, reject or
duplicate-of, on its latest reply. A report that never got a reply (a 0.35.2 `duplicate-of`, for
example) gets one drafted from the proposal's reviewed `reporter_text`, or the template apply uses
(`fixed` with the fix version computed now), with the close attached. It only drafts and never
drafts twice; approve both digests and send as above.

**A duplicate closes as a duplicate OF its canonical, and every `#N` a reply names is checked**
(bh-iamru). A `duplicate-of` close now names the canonical report's issue: the close's digest shows
`duplicate_of: <url>`, and the PATCH carries GitHub's `duplicate_issue_id` (one extra GET of the
canonical before the write; same switch, same grant). A canonical with no issue in the same
repository closes as a plain duplicate. `bh work triage check` and `apply --reviewed` now refuse a
`reporter_text` whose `#N` is not an issue of the bound repository (`mention_missing`), or whose
`#N` cannot be verified because no tracker with `issues.read` is bound (`mention_unverified`):
rewrite the text, or bind the tracker. One issue listing per run, only when a `#N` appears. A `#N`
that is another bound report is recorded in beads as a `relates-to` between the two reports.
GitHub issue dependencies and sub-issues stay unused: no bead edge means the same thing.

**Wire 2.23.0** (`docs/schemas/wire/v2.23.0/`): additive, the one new verb
`bh work outbox backfill-close`.

**Rotation order (the 0.35.0 pattern).** The new key and the new outbox operation kind both need
every host first: an older host rejects the key (`extra=forbid`) and fails closed on an
`intake.outbox` document holding an `issue.close` operation. So:

1. Install 0.35.3 on **every** host that reads the hive's outbox (any host that runs
   `bh work outbox`, `bh work triage apply`, `bh doctor` or a merge / release hook on that hive).
2. Only then set `plugins.tracker-github.write.issue_close.enabled: true` in a shared config.
3. Only then run `bh work triage apply --reviewed` or `bh work outbox backfill-close`.

With the key unset no close is drafted, approved or sent; the only change is the `duplicate-of`
reply above, and only while the outbox is on.

### The issue type and the Priority field, beside the labels (switches default off)

Type and priority reached GitHub only as `bh:type/*` and `bh:priority/*` labels. 0.35.3 adds the
`issue.type` and `issue.fields` tracker operations, each behind its own switch,
`plugins.tracker-github.write.issue_type.enabled` and `write.issue_fields.enabled` (default
`false`; each also gates reading the issue's type or field values), and the matching
`issue.type` / `issue.fields` operation kinds in the outbox. The type is set with
`PATCH /issues/{n}` carrying only `type`; a field value with
`POST /issues/{n}/issue-field-values` (field ids from one `GET /orgs/{org}/issue-fields`), which
keeps every other field. Both endpoints were checked against GitHub's REST reference.

With a switch on, `bh work labels sync` (and the `labels.on_transition` refresh) maps the bead's
type and priority, from the tracked work and falling back to the report, through
`intake.external.labels.issue_type.map` (default `bug`→`Bug`, `feature`→`Feature`,
`task`→`Task`, `chore`→`Task`) and `intake.external.labels.priority_field` (`Priority`; default
`P0`→`Urgent`, `P1`→`High`, `P2`→`Medium`, `P3`/`P4`→`Low`). An unmapped value, or one the issue
was not read for, is skipped with a note, never guessed. A difference is reported like label drift
and drafted on the issue's same `labels` record, so `bh work labels review` and
`bh work labels approve --batch <digest>` approve it with the labels, each operation with its own
digest, single-use grant and receipt. Send with `bh work outbox send <id> --kind type` and
`--kind fields`. Labels are unchanged: the `bh:type/*` and `bh:priority/*` labels are still
projected. Setting the type or a field needs the write role on the repository. Reading the
organization's fields needs `read:org` on a classic token; a fine-grained token or a GitHub App
needs the organization permission "Issue Fields" (read) for `GET /orgs/{org}/issue-fields` and the
repository permission "Issues" (write) for `POST .../issue-field-values` and the type's `PATCH`.

**Rotation order (the same 0.35.0 pattern).** An older host rejects the new keys (`extra=forbid`)
and fails closed on an `intake.outbox` document holding an `issue.type` or `issue.fields`
operation, so:

1. Install 0.35.3 on **every** host that reads the hive's outbox (any host that runs
   `bh work outbox`, `bh work labels`, `bh work triage apply`, `bh doctor` or a transition, merge
   or release hook on that hive).
2. Only then set `write.issue_type.enabled` / `write.issue_fields.enabled` (and any
   `intake.external.labels.issue_type` / `priority_field` override) in a shared config.
3. Only then run `bh work labels sync`.

With both keys unset nothing is drafted, read or sent for the type or fields, and a labels record
is byte-identical to one 0.35.2 wrote.

No new verb or option for these: `bh work outbox send|discard --kind` and `approve --op` accept
`type` and `fields`. The operation catalog moves to wire 2.24.0 only for `bh-pc9rf`'s new
`bh work outbox send-approved` verb (below).

### Faster outbox verbs and a one-process batch send

`bh work outbox show`, `approve` and `send <id>` now find a record through a local outbox-id index
(`<hive>/.bh/intake/outbox-index.json`, `bh-pc9rf`) and read ONE report instead of every report's
outbox. The index is only a cache: every answer is confirmed against the report's own document, a
miss scans once and rebuilds it, and deleting it is always safe. A one-shot `bh` verb also loads
the effective config once (one `outbox show` used to re-qualify the SQL fleet snapshot 7 times);
a config publish inside the same command drops that memo, and the long-lived `bh host` / `bh mcp`
never use it. Deriving a GitHub token now runs `gh auth token` alone and only on its failure
`gh auth status` (for the diagnostic), one subprocess per GitHub operation instead of two; the
token is still never cached. On the factory `bh work outbox show <id>` went from about 21 s to
about 8 s.

`bh work outbox send-approved [--kind comment|labels|type|fields|close] [--bead <id>]...` sends
every approved operation in one process and one store session, per report the comments, then the
labels, the type, the fields, then the close. Each operation still spends its own single-use grant
and writes its own receipt, and a refused or unknown one is reported while the rest go (the command
exits non-zero unless every operation was sent; a close whose comment did not go out is refused as
`close_not_ready`; losing the primary host stops the batch and reports what was sent).
`bh work labels approve --batch` runs on the same path. By default either batch still checks the
primary host and reads the epoch on every operation (about 13 s each on the factory). The new key
`intake.external.outbox.batch_recheck_seconds` (default `0`) opts a batch into reusing ONE check
for at most that many seconds; every operation still takes its own lock, grant and receipt. An
older host rejects the key (`extra=forbid`), so the factory sets it to `300` only after every host
runs 0.35.3. **Wire 2.24.0** (`docs/schemas/wire/v2.24.0/`): additive, the one new verb
`bh work outbox send-approved`.

## 0.35.0 → 0.35.2 — triage apply fixes and a hishel cap

0.35.1 was tagged but never published: hishel 2.0.0 (released the same day) fails githubkit's
import, and every githubkit allows it, so the release smoke of `beadhive[github]` failed. 0.35.2
carries 0.35.1's fixes plus `hishel<2` in the `[github]` extra. A fresh install of any earlier
`beadhive[github]` resolves hishel 2.0.0 and fails the same way; install 0.35.2, or add
`hishel<2` yourself.

No new keys and no state change. `bh work triage apply --reviewed` on 0.35.0 could leave a reject
half done (reply drafted, `intake:rejected` set, close refused because the reply was longer than
the 255-character close reason Beads stores) and refused needs-info questions whose comments the
analysis had read. On 0.35.2 rerun `bh work triage apply --reviewed`: it closes the half-rejected
reports without drafting their replies again and asks the questions. A proposal re-stored only to
move its `thread_seq` past a `thread considered` write needs no further change.

## 0.34.x → 0.35.0 — GitHub label projection and review-gated triage (switches default off)

0.35.0 adds two supervised features on top of intake and the outbox. The **label projection**
derives GitHub labels under one owned prefix (`bh:` by default) from the dimension registry and
keeps every bound issue's owned labels converged on bead state. **Review-gated batch triage**
stores an analyst's proposal on each report, records verifier verdicts, runs deterministic
pre-apply checks and applies only reviewed proposals. Every new capability is behind a switch that
defaults to **off**, nothing is sent without an operator's approval, and bh makes no LLM call, so
upgrading changes no behaviour until you turn something on.

**Wire 2.22.0.** The wire contract moves to 2.22.0 (`docs/schemas/wire/v2.22.0/`, `latest` in
`docs/schemas/wire/index.json`); the change is additive (two new `bh work` groups).

**New `bh work` verbs.** All refuse naming the key they need:

- `bh work labels sync [<report>|--all] [--include-closed] [--dry-run] [--prune-unmapped]`,
  `labels define [--dry-run] [--prune] [--approve <digest>]`, `labels review` and
  `labels approve --batch <digest>` (`intake.external.labels.enabled`; drafting and approving also
  `outbox.enabled` and `outbox.labels_enabled`). `define --approve` and `approve --batch` are
  operator-seat only, like `bh work outbox approve`.
- `bh work triage review --pending [--batch-size N] [--out DIR]`, `triage propose <report>
  <file|-> [--review-of <pass>]`, `triage check [--pending]` and `triage apply --reviewed
  [--dry-run] [--reroute-to <report>=<hive>]` (`intake.external.enabled`; `review` also
  `comments.enabled` and the tracker's `read.comments.enabled`). `apply` is operator-seat only and
  only drafts: replies and questions are sent with `bh work outbox`. `bh work intake` is unchanged.
- `--review-of` takes the review pass number (1, 2, ... in recording order); the batch skill's
  `B` and `C` are accepted for passes 1 and 2 of a proposal's first round.

**New config keys** (`docs/CONFIGURATION.md`): the `intake.external.labels` section (`enabled`,
`prefix`, `project`, `aliases`, `colors`, `overrides`, `on_transition`) and
`plugins.tracker-github.read.labels.enabled` / `write.label_definitions.enabled`. All switches are
`false`.

**Rotation order (bh-hmtjn pattern).** An older host rejects the new keys (`extra=forbid`) and
cannot decode an `intake.outbox` document holding the new `labels` record type (the issue label
changes `labels sync` drafts, with their own supersede group). So:

1. Install 0.35.0 on **every** host of the fleet, every host that reads a hive's outbox included
   (any host that runs `bh work outbox`, `bh doctor` or a merge / release hook on that hive).
2. Only then set the new keys in a shared config: `plugins.tracker-github.read.labels.enabled`,
   `write.label_definitions.enabled` (with `bh:` in `write.labels.owned_prefixes`) and
   `intake.external.labels.*`.
3. Only then run `bh work labels sync` (the first `labels` record), or turn on
   `labels.on_transition` (which drafts one on every transition).
4. Define the labels before the first sync: `bh work labels define`, review the plan, then
   `bh work labels define --approve <digest>` from an operator seat.

The triage proposal store (`intake.proposal`, a private document on the report bead) is new too
and only 0.35.0 reads it, so run the `bh work triage` verbs from 0.35.0 hosts.

## 0.33.x → 0.34.0 — the supervised GitHub triage loop (all new switches default off)

0.34.0 adds the supervised triage loop on top of intake and the outbox: it can read a bound
report's issue comments, ask the reporter for more information through a supervised reply, wait
for the answer, and tell the reporter how the tracked work is going. Every new capability is
behind a switch that defaults to **off**, and nothing is sent without an operator's approval, so
upgrading changes no behaviour until you turn something on.

**Wire 2.21.0.** The wire contract moves to 2.21.0 (`docs/schemas/wire/v2.21.0/`, `latest` in
`docs/schemas/wire/index.json`). Run the same release on every host before any new config
key is set (see below).

**New `bh work` verbs.** All are inert until their switch is on and refuse naming the key:

- `bh work thread show | ingest | considered`: read, ingest and mark-as-considered a bound
  report's comment thread (`comments.enabled`; `ingest` also needs
  `plugins.tracker-github.enabled` and `plugins.tracker-github.read.comments.enabled`). Reads only.
- `bh work needs-info <report> --ask <text>` and `bh work wake <report>` / `wake --all`
  (`needs_info.enabled`; `wake --all` also `needs_info.sweep`; the question also needs
  `outbox.enabled` and `comments.enabled`). The question is drafted as a supervised reply; it posts
  only after an operator approves it.
- `bh work investigate <report> (--work <id> | --new-work) [--remove]`: relate reproduction work
  to a report with an `investigates` edge that never fires delivery milestones and is separate from
  `tracks` (off unless `intake.external.enabled`).
- `bh work delivery <event>` notices: `started` (`milestones.draft_on_start`), `blocked` and
  `deprioritised` (`milestones.draft_on_blocked`, `milestones.draft_on_deprioritised`, each with a
  public `--reason`), plus `milestones.catch_up_on_link` for work that merged before it was linked.
  They only draft records in the outbox; they never move a dimension or complete a report.

**New config keys** (`docs/CONFIGURATION.md`): `comments.enabled`, `comments.deletion_confirmations`,
`comments.purge_deleted_bodies`, `comments.max_body_chars`, `needs_info.enabled`,
`needs_info.sweep`, `milestones.catch_up_on_link`, `milestones.draft_on_start`,
`milestones.draft_on_blocked`, `milestones.draft_on_deprioritised`. All switches are `false`.

**Do not use the new vocabulary before the fleet rollout (bh-hmtjn).** An older host rejects the
new keys (`extra=forbid`), the `intake:needs-info` label value (`bh label validate`) and an
`intake.outbox` document holding the new `started` / `blocked` / `deprioritised` records. So
until 0.34.0 is installed on every host of the fleet: set none of the keys above in a shared
config, do not run `bh work needs-info`, and do not apply `intake:needs-info` or the new
milestone events to any report.

The intake store keeps following `work.beads.route` (0.33.1): the new store operations behave the
same over the Beads service and over the `bd` CLI.

## 0.33.0 → 0.33.1 — `beadhive[github]` and `beadhive[xtdb]` install from PyPI

0.33.0 published `beadhive[github]` and `beadhive[xtdb]` extras that named unpublished
workspace packages (`beadhive-tracker-github`, `beadhive-beadlog-xtdb`), so
`uv tool install 'beadhive[otel,github]==0.33.0'` failed to resolve ("beadhive-tracker-github was
not found in the package registry"). 0.33.1 ships those plugin import packages
(`beadhive_tracker_github`, `beadhive_beadlog_xtdb`, `beadlog_xtdb`, each with its `plugin.json`)
inside the root wheel, like `beadhive-core`, and the extras now carry only the third-party
dependency: `beadhive[github]` is `githubkit`, `beadhive[xtdb]` is `psycopg[binary]` (LGPL-3.0).

What you need to do:

- **Installed the adapter with `--with ./packages/beadhive-tracker-github`** (the 0.33.0
  workaround)? Reinstall plainly; the `--with` is no longer needed and would shadow the wheel's
  copy:

  ```sh
  uv tool install --force 'beadhive[otel,github]==0.33.1'
  ```

- **Want the XTDB store?** `uv tool install --force 'beadhive[xtdb]==0.33.1'` (add `otel,github`
  as needed).
- **Bare install?** Nothing changes. `import beadhive` still never imports githubkit or psycopg,
  and no plugin is enabled by installing either extra.

A plugin that is **enabled without its extra** now refuses cleanly, naming the extra (for example
``install `beadhive[github]` ``), instead of failing later on an `ImportError`;
`bh plugin beadlog-xtdb doctor` reports the missing `psycopg` dependency. No config key, CLI verb or
wire format changed.

**Intake follows `work.beads.route`.** The intake verbs (`import`, `discover`, `intake`, `accept`,
`reject`, `link` and the rest) now follow `work.beads.route` like the work verbs, so under the
default `api` they need the hive's Beads service (`bh host beads start --hive <hive>`) or
`work.beads.route: api+cli-fallback`. `bh report` / `bh escalate` and `bh doctor`'s intake-hold
sweep stay on the `bd` CLI.

## 0.15.x → 0.16.0 — private state moves under the canonical roots

0.16.0 stops writing `.git/bh-validation-ledger.json` and `.bh/testreport/<tree>/` immediately.
Execution manifests and reusable green pointers now live below
`<git-common-dir>/bh/validation/`; raw artifacts and bounded per-tree summaries live below
`<hive>/.bh/validation/`. The legacy paths are read-only compatibility inputs in 0.16.x and
0.17.x. `bh doctor` names hives that still retain them. The fallback and warning may be removed
in **0.18.0**, but not before.

Per-worktree observability provisioning also moves from `.bh/otel.env` to
`.bh/observability/otel.env`. The loader checks the canonical file first and consults the legacy
file only when canonical state is absent; provisioning and self-healing never write the legacy
path. No manual copy is required. Re-provision the worktree (or let self-healing run with a live
collector), verify the canonical overlay works, then archive/remove `.bh/otel.env` during the
same bounded cleanup window. Both paths are covered by the local `.bh/` exclude, so a successful
upgrade leaves `git status --short` empty.

Migration is automatic, atomic, and best-effort. Readers ask the canonical path first and consult
only the exact bounded legacy input on a miss. A canonical write also promotes relevant old state.
The source stays untouched for rollback; deterministic imported run IDs and atomic summary/index
writes make retries idempotent. No Git ref, tracked file, validation subject, or published history
is rewritten.

| Legacy input | Canonical result | Authority after import |
|---|---|---|
| fresh exact `(tree, command hash)`, integer `rc=0` ledger row | completed run + green verdict pointer | reusable green under the old exact-match and TTL rule |
| stale zero or malformed row | retained as non-attesting history, or skipped if structurally unreadable | never green |
| ordinary nonzero ledger/triage row | completed red legacy run | exit-code evidence with explicit legacy provenance/confidence |
| `rc=143` | completed `none`, signal 15, reason `interrupted` | interruption, never code red or green |
| counts-only row or missing reports | manifest/summary with available counts | detail only; missing detail is not invented |
| multiple observed shas for one tree | capped `shas` metadata on the imported ledger run/pointer | metadata only; tree remains identity |
| per-tree fail→pass retries | canonical `.summary/<tree>/results.json` history | preserves identical-tree flake detection; imported green triage detail cannot attest |
| malformed file or unavailable canonical root | bounded miss / read-only fallback | fresh validation remains the safe path |

### Removal procedure (0.18.0 or later only)

1. Upgrade through a 0.16.x or 0.17.x release and exercise each retained tree (or let normal
   validation reads/writes promote it).
2. Run `bh doctor`; inspect any legacy-validation warning and confirm corresponding canonical
   manifests/summaries exist. A malformed legacy file may intentionally have no promoted record.
3. Archive the legacy paths outside the checkout if rollback evidence is required, then remove
   `.git/bh-validation-ledger.json`, `.bh/testreport/`, and any legacy worktree
   `.bh/otel.env`. Do not copy them into the canonical roots by hand.
4. Run one normal validation. It should use/write only canonical paths and leave `git status`
   unchanged. Keep the archive until the next successful release/merge boundary.

The source-code removal gate is tested: canonical writers may not recreate either retired path,
and fallback constants/warnings/documentation remain pinned through the two-minor window.

## The hub / HQ split — the aggregate leaves HQ (bh-89wxf)

**Who this is for:** anyone who has run `bh hq init`. HQ's Dolt database has been carrying two
different things — HQ's own authoritative `hq-`prefixed beads, and a per-host DERIVED
cross-hive aggregate that every host rebuilt wholesale and pushed. bd's rule is one database
per remote path, and since every mutation touches `updated_at`, a per-host wholesale rebuild
inside a replicated database is the maximal-conflict shape there is. The aggregate has moved
to the hub, where it belongs. Full reasoning: [HQ — Hub vs HQ](HQ.md#hub-vs-hq).

### What you must run

```sh
bh sync                            # builds the hub (rebuilds a legacy one automatically)
bh hq prune-aggregate              # dry run: how many hive-derived beads HQ is carrying
bh hq prune-aggregate --confirm    # delete them; HQ keeps its own hq- beads and fleet config
bh hq push                         # publish the now-single-purpose database
```

No Dolt surgery, and nothing to hand-edit. `bd delete` is an ordinary bd mutation, so the
cleanup replicates through `refs/dolt/data` — a second host picks it up on its next pull
instead of each host repairing its own copy. Nothing is lost: every pruned bead is a derived
copy of a bead that still lives in its own hive.

A hub minted before this change (bd prefix `hub`) is moved aside to
`~/.beadhive/hub.legacy-<epoch>` and rebuilt prefix-less on the next `bh sync`. It is renamed,
not deleted — delete it yourself when you want the disk back.

### What changed on the CLI

| Before | Now | Why |
|---|---|---|
| `bh hq bd …` — the cross-hive aggregate | `bh hub bd …` | two stores, two surfaces |
| `bh hq bd …` | HQ's OWN `hq-`prefixed beads | HQ is authoritative-only now |
| `bh hq intake` — fleet-wide inbox | `bh hub intake` | it was a cross-hive read wearing HQ's name |
| `bh hq intake` | HQ's own escalations | the surface names the scope |
| `bh hub …` (deprecated alias) | un-deprecated, means the hub | it is no longer an alias |
| `bh hq push --no-sync` / `--git-only` | **removed** | they only existed to dodge the aggregate refresh, which is gone |

`bh hq intake` prints a pointer to `bh hub intake`, so the rename doesn't strand a habit
mid-command.

## Ad-hoc PyPI → the managed path (any version)

Not tied to a release. This is for anyone who installed `bh` with `uv tool install` (or pipx,
pip, brew) and wants the managed toolchain instead — the route
[INSTALL.md](../INSTALL.md) now recommends. `bh doctor` tells you whether this applies to
you: it reports the install plane, and `pypi` means this section is for you.

**What you gain:** `bd`, `dolt`, `gh` and `git-workspace` become installed and version-pinned
together via `flake.lock`, instead of being whatever your machine happened to carry. **What it
costs:** nix, which needs root — see INSTALL.md's requirements before starting.

**Docker is deliberately out of scope here.** The managed path means nix. The container image
is a different deployment shape with its own guide ([CONTAINER.md](CONTAINER.md)), not a step
on this route.

### The order matters, and it is a safety property

Do these in order. The reasoning is worth understanding, because someone who understands it
won't reorder it:

1. **Sync your beads first**, so nothing exists only on this machine. If a later step goes
   wrong, your work is already durable.
2. **Archive the old workspace second**, while the old install still works. Archiving before
   reinstalling means the old tree is intact if the new install misbehaves.
3. **Install the managed path third.** By now nothing is at risk.

### 1. Sync beads

```sh
bh sync            # every hive
bh hq push         # if you have an HQ remote; skips harmlessly if you don't
```

**Verify it landed** — not just that the command exited 0:

```sh
bh hive list       # every hive present and not reporting unsynced work
```

### 2. Archive the old workspace

**There is no single `bh` verb for this yet, and this guide will not pretend otherwise.**
The migration that turns an external workspace into the internal one is
tracked as `bh-cgcg.3` and is **not yet released**. Until it lands this step is manual:

```sh
# with everything synced above, move the old tree aside rather than deleting it
mv ~/workspace ~/workspace.pre-managed
```

Two verbs that look like they fit and do **not**:

- `bh host retire --backup` decommissions **a host** — it releases leases, deregisters this
  machine's manifest and pushes HQ. That is the right shape but the wrong scope: you are
  migrating a workspace on a machine you are keeping, not retiring the machine.
- The `git-workspace:import` skill covers the **opposite** direction — bringing an existing
  tree under management.

**Verify it landed:** the archived tree is still readable at its new path, and the beads you
synced in step 1 are still listed by `bh hive list`.

### 3. Reinstall on the managed path

Follow [INSTALL.md](../INSTALL.md)'s managed path. `bh` itself is reinstalled by the same
`uv tool install` you already use, so this replaces your existing `bh` in place.

**Verify it landed:**

```sh
bh setup check     # expect 4 of 4 — this is the whole point of the migration
bh doctor          # install plane should no longer report pypi
```

If `bh setup check` still reports missing tools, your shell is finding the old `bh` or the nix
profile is not on `PATH` — `~/.nix-profile/bin` must precede the old locations.

### 4. Point the workspace at the internal root

Once `bh-cgcg.3` lands this becomes a command. Until then, re-register your hives against the
internal workspace root as you would on a fresh machine — see
[ONBOARDING.md](ONBOARDING.md)'s git-workspace and hive-onboarding phases.

## 0.11.x → 0.12.0 — the validation ledger re-keys; every entry goes cold once

0.12.0 ships **Attested Green** — a re-keyed validation ledger plus the landing-boundary reuse
and `work.always_run` / `work.validate_subset` tuning it enables. Full design:
[design/attested-green-adr.md](design/attested-green-adr.md); the two new operator-facing keys
are documented in [WORK.md](WORK.md#configuration) and shipped in `config.example.yaml`.

**What changes on disk, and why you'll notice:** the ledger's key shape moves from
`(commit sha, cmd hash)` to `(tree hash, cmd hash)` (attested-green-adr.md, "Consequences").
An old-shape entry can never match a new-shape lookup, so **every entry your ledger already
holds goes cold the moment you upgrade** — there is no migration, because there is nothing to
migrate a sha-keyed row *into*. The first validation for each `(tree, command)` pair you hit
after upgrading pays the full run again; every one after that is a normal reused hit, same as
before.

**This is expected and self-healing, not a regression** — it happens exactly once per
`(tree, command)` you still care about, and it's the same "harmless and self-healing"
degradation the ledger already handles for a TTL expiry or a size-cap eviction, just all at
once instead of trickling in. If the release that advertises skipping redundant validation
seems to be re-running everything right after you install it, this is why.

Nothing to do here — no flag, no migration command. If you want to confirm it for yourself,
inspecting `<hive>/.git/bh-validation-ledger.json` will show old sha-keyed rows aging out
under the existing TTL/cap eviction rather than being read back.

## 0.7.x → 0.8.0 — the store engine moves to bd's shared dolt server

0.8.0 changes **where a hive's beads physically live**. Until now every hive ran bd's *embedded*
Dolt engine: an in-process engine under the repo's own `.beads/embeddeddolt/`, holding an
exclusive file lock for as long as a command ran. 0.8.0 makes **bd's own shared
`dolt sql-server`** — one server per host, spawned and supervised by `bd`, on `127.0.0.1:3308` —
the default for newly onboarded hives, and ships a verb to move the hives you already have. The
decision and the measurements behind it are in
[dolt-server-mode-adr.md](design/dolt-server-mode-adr.md); the two-things-called-"server"
distinction is [DOLT.md](DOLT.md).

> **Nothing moves on its own.** Installing 0.8.0 migrates no existing hive. Everything you have
> keeps working exactly as it did, embedded, until you run `bh hive migrate-storage` yourself
> (§4). If you stop reading here, you have lost nothing and broken nothing.

### 1. BEHAVIOR CHANGE: newly onboarded hives are born on the shared server

`bh hive init`, `bh hq init`, and the legacy hub now init straight onto the shared server —
across all three `bd init` paths (furnished, bootstrap-from-`refs/dolt/data`, and
zero-footprint). Two facts are persisted rather than left to an environment variable:
`dolt_mode: "server"` in the hive's `.beads/metadata.json`, and `dolt.shared-server: true` in
its `.beads/config.yaml`.

Zero-footprint stays zero-footprint — server mode adds no tracked artifact, no extra commit.
Nothing about publishing changes either: issue history still travels as `refs/dolt/data` on
each repo's own git remote. Storage mode is a *local engine* choice, not a change of where your
data lives for anyone else.

### 2. Existing hives stay embedded until you say otherwise

The idempotent "`.beads/` already exists" skip in onboarding returns before any server-mode
wiring runs, so re-running `bh hive init` on an embedded hive leaves it embedded. This is
deliberate: moving a store is `bh hive migrate-storage`'s job and never a side effect of
re-running onboarding.

You can therefore run a mixed fleet indefinitely. `bh doctor`'s store-engine section reports
each hive's mode, and — for hives on server mode only — probes the endpoint, because server
mode is the first storage mode that can be *down*
([dolt-store-engine-liveness-adr.md](design/dolt-store-engine-liveness-adr.md)).

### 3. Prerequisites

- **Beads v1.3.0-rc.1 or newer**, which supports shared-server mode and embeds the Dolt
  v2.2.0-era transport fix. The managed Nix path pins that RC exactly. The same build
  requirement as [§11 below](#11-requirement-your-bd-build-must-embed-dolt--220) applies here
  for the same reason, and more sharply: migration moves whole stores.
- **Free disk during the move, not after it.** Migration takes a verified Dolt-native backup
  *before* it touches anything, restores that into the server-side database, and moves the old
  store aside rather than deleting it — so mid-run a hive briefly occupies roughly **3×** its
  store size (old store + backup + new server-side copy). HQ at 1.6 GB is the one to plan for.
- **`bh setup check` green**, so you are not diagnosing an unrelated dependency mid-migration.

### 4. The migration, end to end

`bh hive migrate-storage` is the verb. It is *not* `bh hive migrate` — that one is the
`ws`→`bh` rename (markers, hooks, skills), a completely different axis that happens to share a
word.

```sh
bh hive migrate-storage --dry-run              # whole fleet: sizes, target paths, nothing changes
bh hive migrate-storage beadhive --dry-run     # one hive
bh hive migrate-storage beadhive --confirm     # apply, one hive — start here
bh hive migrate-storage --confirm              # apply, whole fleet (Factory HQ last)
```

A real run refuses without `--confirm`. Per hive it does: **verified backup → migrate →
verify → report**, and it stops at the first step that cannot be verified:

- **Backup, two levels, both verified before anything destructive.** A portable JSONL export
  (line count cross-checked against `bd status`'s own issue count) plus a Dolt-native backup
  taken *over the connection* — the only format that restores into a *different* engine mode.
  A tarball of `embeddeddolt/` is not that format; nothing in a running SQL server reads a
  directory by that name. If either level cannot be verified, the hive is not migrated.
- **Verify means readable AND complete**, not "the command exited 0": the store re-opens, the
  issue count matches pre-migration exactly, a schema version is recorded, and the persisted
  `dolt_mode` really says `server`. An engine/metadata disagreement is a **failed** migration,
  not a warning — that silent drift is the specific failure this verb exists to prevent.
- **One migrator per hive**, enforced by a lock file (stale locks are reclaimed, not waited on
  forever). Two concurrent migrators against one remote-backed database fork the schema
  silently and unrecoverably.
- **Fleet-wide runs are resumable and per-hive isolated.** Outcomes are recorded in
  `~/.beadhive/storage-migrate-state.json` as each hive finishes, so a killed run picks up
  where it stopped rather than at hive 1, and one failure never strands the other 21. Factory
  HQ goes last, on purpose: it is the largest store and the hardest case.

Afterwards, each hive's database lives under `~/.beads/shared-server/dolt/<database>` (honoring
bd's own `BEADS_SHARED_SERVER_DIR` if you set it). Confirm with `bh doctor` or
`bh hive ready`.

### 5. Rollback, precisely

Two different one-way-ness questions get confused here, so state them separately:

- **Storage-mode migration is REVERSIBLE.** embedded → shared and back is `bd backup` /
  `bd backup restore`, full Dolt commit history preserved in both directions. The verb prints
  this before it does any real work.
- **bd-binary schema migration is NOT.** Beads v1.3.0-rc.1 upgrades this hive's schema one way
  from v62 through v66. Designate one migrator for each remote-backed store: it must run the RC
  first, publish the resulting Dolt state, and every other host must update before opening that
  store. Do not run any older Beads binary after the v62→v66 migration: releases through v1.2.2
  understand only schema v53 and cannot safely reopen it. This is orthogonal to storage mode —
  it closes when the designated migrator upgrades `bd`, not when it moves the store.

### 6. BEHAVIOR CHANGE: `backup.enabled` is set true on every migrated hive

`bd`'s auto-backup defaults **on** in embedded mode when a git remote exists and **off** in
server mode (upstream's reasoning: many clients on one shared server each registering a
same-named backup remote and full-syncing would be a self-amplifying storm). Migrating without
correcting that would quietly leave a hive less durable than it was found, so the verb sets
`backup.enabled: true` explicitly as part of migrating — including on hives it finds already
migrated, which is how a partially-applied earlier run gets healed.

**Policy update (`bh-eu0oi`, 2026-09-27):** that behavior was reversed after per-worktree backup
stores caused large duplicate copies and blocked safe worktree pruning. Current onboarding and
storage migration do not enable scheduled backups. An existing explicit `backup.enabled: true`
is preserved; a new shared-server hive remains at bd's OFF default. The migration's one-time
verified safety backup is unchanged.

### 7. What it buys, and what the numbers actually say

All measured on one machine, 2026-08-03. Quote them with their caveats or not at all:

| | embedded | shared |
|---|---|---|
| 5 readers + 1 writer, median of 3 (`bh-u562.1`) | 3.456 s | **0.720 s** |
| engine-open + query, A/B on identical cloned data ([`bh-00cq`](spikes/bh-00cq-external-dolt-sql-server.md)) | 187 ms | **55 ms** (−71%) |
| cold 306 MB clone from the production remote (`bh-00cq`) | hung past 240 s | 10 s |
| fleet disk, 22 hives × `.beads/embeddeddolt` | ~2.8 GB | reclaimable (§8) |

**The caveats, from the spike that produced the numbers:**

- The 187 ms → 55 ms A/B used a **300 MB store — the least favourable case for server mode**,
  not the best. It is engine-open + query with `bd`'s own 63 ms process spawn subtracted; raw
  wall-clock improvements were 48–69% by verb. Embedded latency tracks store size while a
  server holds the store open, so larger stores should gain more — but *that is an inference
  from two runs, not a measurement*.
- **Concurrency is unmeasured** in that A/B. The 5-readers-1-writer row above is a separate
  measurement, and the fleet-wide resource argument (19 embedded engines vs one server under
  simultaneous load) remains untested at fleet scale.
- The ~2.8 GB is this operator's fleet — 22 hives, worst offenders 864 MB (beadhive), 825 MB
  (workspace), 343 MB (homelab), and HQ alone at 1.6 GB. Yours will differ. `--dry-run` reports
  your real per-hive sizes before you commit to anything.
- Bootstrap cost is a wash (11 s embedded vs 9 s server for the same payload). Server mode
  costs nothing on first sync.

### 8. What's safe to delete — and why the disk isn't free yet

**Migrating does not reclaim a byte.** The old store is *moved aside*, never deleted:
`.beads/embeddeddolt/` becomes `.beads/embeddeddolt.pre-migrate-<UTC timestamp>/`. That is
deliberate — it keeps the pre-migration store recoverable by hand, and it keeps
"is this hive embedded?" answering correctly for everything else in `bh`. It also means the
~2.8 GB is still on disk until you remove those directories yourself.

Once `bh doctor` reports the hive on server mode and you have used it for long enough to trust
it:

```sh
du -sh <hive>/.beads/embeddeddolt.pre-migrate-*      # look before you delete
rm -rf <hive>/.beads/embeddeddolt.pre-migrate-*      # this is where the disk actually comes back
```

> **On a furnished hive, check `git status` first.** `.beads/.gitignore` ignores
> `embeddeddolt/` by exact name, so the moved-aside `embeddeddolt.pre-migrate-*` is **not**
> covered by it and will show up as untracked files in a hive whose `.beads/` is tracked. Do
> not commit it. (Zero-footprint hives are unaffected — all of `.beads/` is excluded there.)

Also safe to prune, once you are done migrating:

- **`~/.beadhive/storage-migrate-backups/<hive>/<timestamp>/`** — the pre-migration backups
  (JSONL export + Dolt-native). These are your rollback material for §5; keep them until you
  are certain, then delete them. They are artifacts, never the only copy of anything.
- **`~/.beadhive/storage-migrate-state.json`** — the resume ledger. Deleting it only makes the
  next fleet run re-examine every hive (each is idempotent, so that is safe, just slower).
- **`~/.beadhive/storage-migrate-locks/`** — per-hive lock files, reclaimed automatically when
  stale. Remove only if no migration is running.

Nothing in §"What's safe to delete, and what must never be copied" for 0.7.0 changes:
`~/.beadhive/host.yaml` is still machine-local forever, and `~/.beadhive/hq/` is still durable.

## 0.6.0 → 0.7.0 — Factory HQ, the multi-host model, and the fleet/host config split

0.7.0's real subject is **multi-host**. Before this release, "the machine `bh` runs on" was
implicit and untracked; every host quietly assumed it was the only one. 0.7.0 makes the host a
first-class concept with its own identity, its own manifest, and a write fence — and Factory HQ
graduates from a local cache into the fleet's durable, remote-backed store. None of that is
visible from a version number, and it changes what's on disk enough that this is a guide, not a
changelog entry.

> **Breaking change, read this first:** `bh backup <dest>` is now `bh backup export [dest]`.
> See [§6](#6-breaking-change-bh-backup-is-now-bh-backup-export) below — this is the one thing
> in 0.7.0 that breaks existing muscle memory or a script.

**If you only ever run `bh` on one machine and never touch `bh hq`/`bh host`,** almost none of
this changes your day-to-day: `guard_primary` (§1) only refuses a write when a host *lease*
exists naming another host, and a factory that has never adopted anything has no lease at all —
single-host stays single-host, functionally unchanged. What still applies to you regardless:
the `bh backup export` rename (§6), and the fact that your first `bh hq init` will write real
disk (§5) the moment you decide to stand one up.

### 1. The multi-host model

A **host** is one `~/.beadhive` config home (usually one machine). 0.7.0 gives it four new
pieces of state, all covered in depth by
[`docs/design/multi-host-model-adr.md`](design/multi-host-model-adr.md):

- **Host identity — `~/.beadhive/host.yaml`.** A `host_id` (UUID) plus a cosmetic `label`,
  minted once by `bh config init` and **never regenerated, never synced, never copied to
  another host** — it is this machine's identity, not fleet truth. A hostname rename never
  changes it.
- **Per-host manifests in HQ — `hosts/<host_id>.yaml`.** Once a host registers
  (`bh host init` / `bh host provision`), its role (`executor` / `transient` /
  `viewer`) and identity mechanism are recorded in Factory HQ, so `bh host list` can render the
  whole fleet's roster from one place.
- **The host lease + epoch fence.** Exclusive-write arbitration for a hive between hosts, split
  across two refs: a **lease** at `refs/bh/lease/<prefix>` in HQ (who *should* be primary —
  schedule, TTL, `bh host list`), and a **fence** at `refs/bh/epoch` beside the hive's own data
  (who *may* write — enforced atomically with the push). You manage this indirectly through
  `bh host adopt <hive>` / `bh host release <hive>` / `bh host packup`; you never touch either
  ref by hand.
- **`guard_primary()`.** The check every write verb (`bh work assign|claim|start|submit|merge`,
  `bh plan file`) now runs before mutating a hive: refuse if this host isn't the hive's leased
  primary. Reads (`ready`, `list`, `show`, `brief`, `sync`) are never gated. Critically: **an
  absent lease means "unconfigured", not "someone else's"** — until you `adopt` a hive from
  some host, every host may write it, exactly like before 0.7.0.

The practical upshot: multi-host is opt-in. Nothing forces exclusivity onto a single-machine
setup; it switches on the moment a *second* host adopts a hive you also work on.

### 2. Factory HQ

Factory HQ (`~/.beadhive/hq/`) is the fleet's durable, remote-backed store — full details in
[HQ.md](HQ.md). The verbs that matter for an upgrade:

| Command | Does |
|---|---|
| `bh hq init [--create] [--dry-run]` | Stand up (or idempotently re-wire) HQ: `bd`-init the local store (first run only), take a verified pre-push backup, scaffold `fleet.yaml`/`workspace.toml`/`hosts/`, then wire + push the remote. `--create` makes the remote as a private, empty repo when it doesn't exist yet. |
| `bh hq clone` | Bootstrap a **second** host with no local HQ: clone `main`, hydrate bead state from `refs/dolt/data`, register the `local/factory/hq` identity. Refuses if `~/.beadhive/hq` already exists — never clobbers. |
| `bh hq push [--dry-run]` | Git mode commits/publishes tracked HQ config and HQ-owned Beads; SQL mode publishes only HQ Beads state. It never refreshes the hub aggregate; run `bh sync` for that. |
| `bh hq status` | Read-only ahead/behind for both halves against the wired remote. |
| `bh hq restore [--list] [--from DIR] [--level auto\|tar\|jsonl] [--dry-run] [--confirm]` | Restore HQ from a pre-push backup — `--level tar` replaces the Dolt store, `--level jsonl` upserts the portable export (works even with no readable store). |

The first `bh hq init` on a real fleet is not a cheap operation — see §5 before you run it.

### 3. The fleet/host config split

A flat `~/.beadhive/config.yaml` — the only shape that existed before 0.7.0 — mixed fleet-wide
truth (`orgs`, `dimensions`, `work.validate_cmd`, `managed_repos`, …) with host-local truth
(`worktrees.path`, `otel.*`, `work.identity`, `hq.remote`, …) in one file. 0.7.0 splits that
partition into two files that `config.load()` deep-merges at read time: `fleet.yaml` (inside
`~/.beadhive/hq/`, identical on every host) as the base, with the host's own reduced
`config.yaml` merged over it. Which key belongs to which side is *data*
(`config_partition.py`'s `FLEET_PREFIXES`/`HOST_PREFIXES`), not a branch you have to reason
about by hand. Full schema in
[CONFIGURATION.md — Fleet + host config](CONFIGURATION.md#fleet-host).

**This matters for exactly one host, and it's a one-time step:**

- **The host that FOUNDS a fleet** (the first one to run `bh hq init`) must also run
  `bh config split` — see [the walkthrough](#the-upgrade-end-to-end) below. `bh hq init` writes
  `fleet.yaml` from a
  snapshot of your *current* resolved config, but it never rewrites your own `config.yaml` to
  drop the now-duplicated fleet keys; left alone, the next `config.load()` sees the same
  fleet-classified keys on both sides and raises a `ConfigError` naming every offending key.
  `bh config split` is what actually reduces your file, and it's idempotent — a config with
  nothing fleet-shaped left in it is a clean no-op on re-run.
- **A host JOINING an existing fleet needs no manual split at all.** `bh hq clone` — and
  `bh host provision`, which calls it — reconciles the host config automatically: it drops the
  stale fleet-shaped keys your local template left behind (never merging them anywhere; the
  freshly cloned `fleet.yaml` is authoritative) and **reports what it dropped**. This is the
  direction that matters for almost every upgrading host, and it needs zero operator action.

Don't run `bh config split` on a joining host "just in case" — the two directions move keys
opposite ways (split *publishes* your leaves into `fleet.yaml`; clone's reconciliation *drops*
your stale copies), and a joining host has nothing of its own worth publishing.

### 4. Host lifecycle

New verbs to adopt/decommission a host, and — the part that's easy to get wrong — **two of them
sound almost identical and are not**:

| Verb | Scope | Does |
|---|---|---|
| `bh host provision --role <role> [--auto] [--dry-run]` | new host | The whole adoption path in one idempotent, resumable call: `config init` → `git workspace update` → resolve `hq.remote` → `hq clone` (auto-reconciling, per §3) → `host init` → per-hive `bead sync` → fix `.beads` permissions → verify. Every step probes before acting, so re-running against partial state is always safe. **Prerequisite:** `bh setup check` — see §10. |
| `bh host init --role <role>` | this host | Mint/write just this host's own manifest into HQ (`hosts/<host_id>.yaml`). What `provision` calls internally as one of its steps. |
| `bh host lease adopt <hive> [--force]` | this host, one hive | Become primary for `<hive>`: fence its remote, then lease it in HQ. |
| `bh host lease release <hive>` | this host, one hive | Yield this host's lease for `<hive>` (a tombstone; the epoch survives). |
| `bh host lease release --all` | this host, every hive | Release every lease this host currently holds — the "I'm switching machines" ritual. |
| **`bh host retire [--dry-run] [--backup] [--confirm] [--purge]`** | **HOST-LOCAL** | Decommission **THIS host only**: one SAFE/NEEDS_BACKUP/BLOCKED verdict across every hive, worktree, held lease, and HQ, then release leases → sync+push every hive → reclaim local clones/worktrees → deregister this host's manifest → push HQ. **Never touches `managed_repos`/fleet registration** — the fleet still has every hive after this runs, just not on this machine. |
| **`bh host rm <host_id> [--dry-run] [--confirm] [--force]`** | **FLEET-WIDE** roster entry | Unregister an *orphaned* manifest from HQ (e.g. a wiped-and-rebuilt host whose old entry never self-cleared). Registry-only — no clone, worktree, or history is touched. Requires `--confirm`; `--force` additionally bypasses the live-lease and recent-last-seen guards. |

And the hive-level counterparts — where the scope distinction is the single easiest thing to
get catastrophically wrong, because the verb names are so close:

| Verb | Scope | Does |
|---|---|---|
| **`bh hive rm <id>`** | **FLEET-WIDE** | Unregister a hive from `managed_repos` — shared fleet truth, so **every host loses this hive**, not just this one. Registry-only; leaves `.beads`/the clone intact. |
| **`bh hive retire <id> [--dry-run] [--backup] [--confirm] [--purge]`** | **FLEET-WIDE** | Guarded teardown: assess → backup-or-consent → worktree teardown → soft-archive the clone → unregister. The unregister step is the same fleet-wide `managed_repos` drop as `rm`, even though the clone/worktree teardown only touches this host. |
| **`bh hive reclaim <id> [--dry-run] [--backup] [--confirm] [--purge]`** | **HOST-LOCAL** | The *host-local-only* twin of `retire`: identical assess → backup-or-consent → worktree teardown → soft-archive, but **never unregisters** — `managed_repos` (and every other host's copy) is left untouched, so the hive stays registered for the fleet. Use this when only this host no longer wants a local copy. |

If you want to stop working on a hive from *this laptop* but keep it live for the rest of the
fleet, that's `bh hive reclaim` or `bh host retire` — never `bh hive rm`/`bh hive retire`, which
take it away from everyone.

### 5. Backups and disk

0.7.0 starts writing backups nobody asked for by name — they're a safety net, but they consume
real disk, and nothing pruned them before this release. Three independent roots, each with a
different owner and retention policy (full design:
[backup-retention-boundary-adr.md](design/backup-retention-boundary-adr.md)):

| # | Root | Written by | When | Default retention |
|---|---|---|---|---|
| 1 | `~/.beadhive/hq-backups/<date>/` | `bh hq init`, before HQ's first remote push | once, per remote-wiring event | keep newest **5** dated dirs (`backup.hq_keep`), auto-pruned right after each new verified backup |
| 2 | `<hive>/.beads/backup/` | `bd`'s own Dolt-native backup | periodic, `backup.interval` (bd's own timer) | rotate past **500 MB** (`backup.hive_cap_mb`), keep **3** generations (`backup.hive_rotate_keep`) — operator-invoked, not automatic |
| 3 | `~/.beadhive/backups/<provider>/<org>/<repo>/` | `bh backup export` | manual, ad hoc | overwritten each run — no history to prune |

**A real first `bh hq init` wrote 744 MB** (a 743,894,216-byte Dolt tarball plus a 5,504,318-byte
JSONL export) into `hq-backups/` on one run, with no pruning at the time. On a real, already
populated fleet, `bh backup usage` reported **1.4 GB across 21 backup roots** — mostly root #2,
which grows continuously with every commit until you rotate it. Don't let a full volume be how
you find out:

```sh
bh backup usage              # size + policy for all three roots, this host
bh backup usage --json       # machine-readable
bh backup reclaim --dry-run  # preview what a reclaim would free, all roots
bh backup reclaim --root cache --dry-run   # show SUPERSEDED vs RETAINED per-hive caches
bh backup reclaim --root cache --confirm   # remove only caches superseded by local .beads
bh backup reclaim --root hq --confirm            # prune HQ's dated dirs to backup.hq_keep
bh backup reclaim --root hive --confirm          # rotate the CURRENT hive's bd backup once over cap
bh config set backup.hq_keep 3                   # keep fewer HQ snapshots
bh config set backup.hive_cap_mb 200             # rotate a hive's bd backup sooner
```

`bh backup reclaim --root hive` needs `--confirm` to actually mutate (it rotates bd's live
backup destination via bd's own sanctioned lifecycle verbs, never chunk-store surgery) —
`--dry-run` previews with zero writes either way.

### 6. BREAKING CHANGE: `bh backup` is now `bh backup export`

Pre-0.7.0, `bh backup [dest]` was a bare command that wrote the JSONL interchange mirror.
0.7.0 needed `usage`/`reclaim` as proper subcommands of the same top-level name, and a
positional-argument-vs-subcommand parse conflict makes a hybrid "bare action, but also a
subcommand group" shape actively ambiguous — so `bh backup` is now a **group**, and the old
bare invocation is a subcommand:

```sh
# before 0.7.0
bh backup                 # exported the JSONL mirror
bh backup ./some/dest     # exported to an explicit destination

# 0.7.0 and later
bh backup export                 # same behavior, new name
bh backup export ./some/dest     # same behavior, new name
```

**If you have a script, alias, or cron job that calls bare `bh backup`, it now does nothing
useful** (`bh backup` with no subcommand just prints help) — update it to `bh backup export`.
This is pre-1.0 (`major_version_zero = true`), so a CLI rename is a MINOR version bump per this
project's own versioning convention, not something a major-version gate would have caught.

### 7. BREAKING CHANGE: `bh hive rm` now requires `--confirm`

`bh hive rm` drops the hive from `managed_repos` — **fleet truth**, so every host loses it, not
just the one running the command (see §4). Before 0.7.0 it took no flags at all and performed
that drop immediately: it printed a warning naming the consequence on the line *before* the
mutation, which announced the outcome at the moment you could no longer prevent it.

It now matches every sibling destructive verb:

```sh
bh hive rm <id> --dry-run    # preview; changes nothing
bh hive rm <id> --confirm    # perform the fleet-wide unregister
bh hive rm <id>              # refuses, exit 1
```

This closes an asymmetry rather than inventing a rule: `bh hive retire` runs the *identical*
`registry.unregister` as its final step and has always gated it behind `--confirm`. The same
fleet-wide drop was protected in one verb and unprotected in the other.

Both the refusal and `--dry-run` print the `bh hive add` invocation that would restore the
entry. `rm` is recoverable — but only while you still know the provider/org/repo/prefix/kind,
which is exactly what unregistering takes away.

**If you have a script calling `bh hive rm`, add `--confirm`.**

### 8. BREAKING CHANGE: `bh host` verb renames

Two renames land together, both inside the `bh host` group that is itself new in 0.7.0 — so
these only affect you if you were tracking pre-release builds.

**`bh host remove` is now `bh host rm`, and requires `--confirm`.**

`rm` is the decided spelling for "unregister one entity" across the whole CLI
(`bh hive rm`, `bh worktree rm` — see the verb model in
[`design/cli-mcp-naming-conventions-adr.md`](design/cli-mcp-naming-conventions-adr.md) §5b-i);
`remove` shipped against that convention and is corrected before it sets. It also picks up the
same `--dry-run` / `--confirm` gate every other teardown verb carries, and the old `--yes`
(self-removal) folds into `--confirm` — the two flags asked the same question at different
scopes:

```sh
bh host rm <host_id> --dry-run    # preview the plan; changes nothing
bh host rm <host_id> --confirm    # perform the fleet-wide unregister
bh host rm <host_id>              # refuses, exit 1
```

`--force` is unchanged and still separate: it bypasses the live-lease and recent-last-seen
guards, and `--confirm` never implies it.

**The lease verbs moved under `bh host lease`, and `packup` is gone.**

| Before | Now |
|---|---|
| `bh host adopt <hive>` | `bh host lease adopt <hive>` |
| `bh host release <hive>` | `bh host lease release <hive>` |
| `bh host packup` | `bh host lease release --all` |

Their object is a *lease* — a renewable, time-bounded claim over a hive — not the host. Flat,
`bh host release` sat next to `bh host retire` and read as its milder synonym when one is
reversible and the other terminal, and `bh host adopt <hive>` took a hive argument in a group
where every other verb takes a `host_id`. `packup` disappears because fan-out is spelled
`--all`, never its own verb name.

**All three old spellings keep working as hidden aliases**, so scripts do not break; they are
just off `--help`. Prefer the new forms in anything you write from here.

### 9. BEHAVIOR CHANGE: onboarding no longer installs the pre-push fence hook

`bh hive init` / `bh hive onboard` used to furnish a `pre-push` git hook in every hive — the
multi-host fence's early refusal. **They no longer install anything.** It is now opt-in:

```sh
bh hive hook install [HIVE_ID]     # install the fence shim for one hive
bh hive hook pre-push [HIVE_ID]    # the hook contract itself, for your own dispatcher
```

Two reasons, and the second is the one that matters.

**It is not enforcement.** Current bd forces `core.hooksPath=/dev/null` in its Dolt transport,
so this hook does not run for `bd dolt push`. Managed publication now reserves
`refs/bh/epoch` by remote CAS before bd and verifies the same ticket afterward. That rejects a
stale host before data, but it is not atomic with bd's opaque push; `bh doctor` exposes the
CAS→push window and raw-bd bypass. The hook remains only a compatibility/diagnostic entrypoint.

**The old default was already failing quietly.** The installer is deliberately
non-destructive: finding a `pre-push` it did not write, it skips and reports
`"skipped (custom hook present)"`. So every hive whose operator used *any* hook manager already
had no fence, while appearing to have one. Opt-in replaces a silent partial default with an
explicit choice.

If you want the early refusal, run `bh hive hook install` per hive. **Re-run it after the
first `bd dolt push`** on a fresh hive: with bd's embedded engine the push that carries
`refs/dolt/data` originates in a transport repo bd creates lazily, so before that there is
nothing to install into (the verb says so rather than failing).

Already-installed hooks keep working untouched — they are simply never created or refreshed
again unless you ask. If you use a hook manager (lefthook, husky, pre-commit), prefer wiring
`bh hive hook pre-push` as a job instead; it reads git's ref list on stdin and exits non-zero
to refuse, so any dispatcher can call it. See
[`design/hooks-as-functionality-adr.md`](design/hooks-as-functionality-adr.md).

### 10. `bh setup check` — a prerequisite `bh host provision` doesn't announce

Nearly every `bh` verb is gated behind a passing post-install dependency cache (`setup`,
`config`, and `doctor` are the only exemptions) — on a fresh host that has never run the check,
`bh host provision`, the recommended entry point for adopting a new host, refuses before doing
anything:

```text
✗ `bh host` requires setup — run `bh setup check` first.
  Skip with BH_SKIP_SETUP_CHECK=1 (debug bypass).
```

Running `bh setup check` once clears it (cached from then on), so it's a required first step,
not a dead end — but neither `provision`'s own 8-step plan (which starts one step later, at
`config init`) nor most written adoption sequences say so up front. This was found the hard way
during the 0.7.0 release-readiness pass and is tracked as a documentation/UX gap
([bh-1kzc](design/0.7.0-release-readiness.md#gap-1--bh-setup-check-is-an-undocumented-prerequisite),
still open) — until it's fixed in the tool itself, treat it as step 0 of every sequence below.

### 11. REQUIREMENT: your `bd` build must embed dolt >= 2.2.0

**This is the one to read if you run more than one host.** 0.7.0's multi-host model syncs bead
data with `bd dolt pull`. On a `bd` whose embedded dolt predates **v2.2.0**, that pull can hang
**indefinitely** on a large store — upstream [beads#4770](https://github.com/gastownhall/beads/issues/4770),
a quadratic `git cat-file` read. Measured here: 170s then killed, versus 3.2s on a fixed build.

**Every tagged `bd` release through v1.2.2 is affected.** Those releases embed the pre-v2.2.0
Dolt transport code. Beads v1.3.0-rc.1 is the first release candidate with the fixed
v2.2.0-era dependency and is the Nix-pinned build. Do not substitute the ordinary stable
package for it: it cannot open a schema migrated from v62 through v66.

**Upgrading the standalone `dolt` CLI does not help.** dolt is statically compiled into `bd`
(a ~137 MB binary) and the CLI is never spawned. Verified by upgrading dolt 2.1.10 → 2.2.2 and
retesting: still hung.

Two escapes:

```sh
# 1. Beads v1.3.0-rc.1 (the managed Nix path pins this release candidate)
nix profile add github:beadhive/beadhive/latest#default

# 2. run bd against an external dolt sql-server >= 2.2.0
bd init --server --server-host 127.0.0.1 --server-port <port>
```

The second is the more durable answer: `bd` issues fetch/pull as `CALL DOLT_FETCH(...)` over
the database connection, so the **server's** dolt does the transport work — which takes the
dolt version out of `bd`'s release cadence entirely. Verified at the dolt layer: dolt 2.2.2
fetched the same remote in **11 seconds** where an affected embedded build hung past 240s.

`bh setup check` now warns when it detects an affected `bd`. It is a **warning, not a gate** —
the hang needs a large store to bite, so a small or new hive may never hit it, and blocking
setup over a probabilistic issue would be worse than the issue.

### The upgrade, end to end

**Once, on every host**, after installing the new `bh` version:

```sh
bh setup check     # clears the post-install dependency gate (§10) — do this first, always
bh config init     # idempotent: never touches an existing config.yaml, mints host.yaml if absent
```

**Then, exactly one path per host** — pick based on whether this host is starting a fleet or
joining one that already exists.

**Path A — this host FOUNDS the fleet** (you have no `hq.remote` anywhere yet, and no other
host has stood one up):

```sh
bh hq init --create          # stand up HQ; --create makes a private empty remote if needed
bh config split               # reduce this host's config.yaml now that fleet.yaml is real (§3)
bh host provision --role executor --auto   # finishes registration; earlier steps
                                                    # (config init, hq clone) are already done
                                                    # and report `skipped`, not repeated
```

**Path B — this host JOINS a fleet another host already founded** (`hq.remote` resolves to
someone else's HQ):

```sh
bh host provision --role executor --auto   # or --role transient for an intermittently-used
                                          # laptop; no manual `bh config split` needed (§3) —
                                          # `hq clone`'s own reconciliation handles it and
                                          # reports what it dropped
```

Either path, finish by checking you actually landed usable: `bh host provision`'s own final
step is a verify gate, but you can re-run it any time — `bh doctor` also folds in a fleet-health
section.

### What's safe to delete, and what must never be copied

0.7.0 doesn't change the answer for most of `~/.beadhive/` — the full durable / regenerable /
machine-local / artifact classification for every entry already lives in
[beadhive-home-layout-contract.md](design/beadhive-home-layout-contract.md); don't re-derive it
by hand. The two facts from that contract worth repeating here, because they're exactly what
0.7.0 makes newly relevant:

- **`~/.beadhive/host.yaml` is machine-local, by design, forever.** Minted once, never
  regenerated, and explicitly never synced or templated. **Do not copy it to another host, put
  it in dotfiles sync, or restore it from another machine's backup** — doing so would make two
  hosts share one `host_id`, which breaks the fencing/identity model `guard_primary` and the
  per-host HQ manifest both key off. If a host is wiped and rebuilt, it gets a *new* `host.yaml`
  (and its old HQ manifest becomes exactly the kind of orphan `bh host remove` exists to clean
  up).
- **`~/.beadhive/hq-backups/` and `<hive>/.beads/backup/` are artifacts, not sources of truth.**
  Safe to prune with `bh backup reclaim` (§5); never the *only* copy of anything durable.
  `~/.beadhive/hq/` itself, by contrast, **is** durable — a fresh host obtains it via
  `bh hq clone`, never by hand-copying another host's directory.

## See also

- [DOLT.md](DOLT.md) — the store engine vs the optional central server, and how to tell which
  mode a hive is on.
- [design/dolt-server-mode-adr.md](design/dolt-server-mode-adr.md) — why shared server, why not
  embedded or bd's owned mode, and the migration/rollback path.
- [design/dolt-store-engine-liveness-adr.md](design/dolt-store-engine-liveness-adr.md) — what
  `bh` does when the store engine is down.
- [spikes/bh-00cq-external-dolt-sql-server.md](spikes/bh-00cq-external-dolt-sql-server.md) — the
  transport and latency measurements, with their method and caveats.
- [HQ.md](HQ.md) — Factory HQ in full: layout, naming, hub-vs-HQ, `push`/`status` mechanics.
- [CONFIGURATION.md — Fleet + host config](CONFIGURATION.md#fleet-host) — the full partition
  schema, `--scope fleet|host`, and `bh config split`'s exact behavior.
- [design/multi-host-model-adr.md](design/multi-host-model-adr.md) — the decision record for
  the host/lease/epoch model, including the amendment that split the lease from the fence.
- [design/backup-retention-boundary-adr.md](design/backup-retention-boundary-adr.md) — why
  three backup mechanisms exist and the retention policy behind each.
- [design/beadhive-home-layout-contract.md](design/beadhive-home-layout-contract.md) — every
  `~/.beadhive/` entry classified durable / regenerable / machine-local / artifact.
- [design/0.7.0-release-readiness.md](design/0.7.0-release-readiness.md) — the verified pass
  this guide's upgrade sequence is drawn from.
- [CHANGELOG.md](../CHANGELOG.md) — the mechanical, per-commit record.
