# Beads v1.3 Python client

`beads_v1_3` is generated from the official Beads 1.3.0 HTTP OpenAPI document at
[`f45b249ce6b40ba62aecc03949e6371e8f7c79d8`](https://github.com/gastownhall/beads/blob/f45b249ce6b40ba62aecc03949e6371e8f7c79d8/internal/httpapi/spec/openapi.v0.yaml).
The checked-in source is `spec/openapi.v0.yaml` (497,361 bytes, SHA-256
`9a33a349e5266bdba916246594a4fc457c63e39d7affb5acec9bd5b75acde4dd`).

The generator is exactly `openapi-python-client==0.29.1` in the workspace lock.
It runs with `--meta none`, leaving Hatchling and uv as the package owners.
After `uv sync --locked`, regenerate or check offline:

```sh
uv run --locked --offline python packages/beadhive-beads-client/regenerate.py --write
uv run --locked --offline python packages/beadhive-beads-client/regenerate.py
```

The second command compares every generated Python file and fails on drift. The
spec digest check fails before generation if the pinned source changes. A cold
machine needs one ordinary `uv sync --locked` to populate its wheel cache;
subsequent regeneration uses only local inputs and the cache.

The generated package is the wire authority: issue summaries, details, request
bodies, pagination, context, capabilities and RFC 9457 problems come from the
OpenAPI source. Handwritten session policy lives in a separate import package.

## Session composition

`beadhive_beads_client.BeadsSession` accepts either `RemoteEndpoint(url, token)`
or `LocalEndpoint(repo_root, fixed_port, bd_executable, token_file)`, plus an
`ExpectedContext(project_id, database)`. A remote endpoint must use HTTPS and
a bearer token unless it is explicitly loopback. A local endpoint starts
`bd serve` on the requested fixed loopback port and terminates it on exit.
The service has a bounded startup and request deadline.

Startup probes `/healthz`, authenticates `/v0/beads/context`, compares the
exact `v0` / `1.3.0` version and project/database identity, requires the
declared capabilities, stamps `Bd-Project-Id`, then queries ready work to
prove database readiness. A wrong context fails before any work read.

The session returns generated models. `ServiceProblem.problem` retains the
generated RFC 9457 payload. A read deadline raises `SessionTimeout`; a write
transport failure raises `IndeterminateWrite` and requires reconciliation by
reading the affected issue before any retry. `require_cli` names only approved
compatibility or administrative operations. It never retries an HTTP write
through `bd`. Delete and sweep are absent from the session surface.

The installed `operation_matrix_v1.json` is the routing authority for the
filed `bh-bwnys.1` and `bh-sy36q.2/.3/.5` consumers. Each named operation is
exactly one of `api-ready`, `cli-compatibility`, `administrative`, or `denied`.
Every API-ready row names its `BeadsSession` method and real-service evidence;
every compatibility or administrative row is accepted by `require_cli`.
Dedicated HTTP routes are supported for comments, claim release, and metadata
compare-and-set, while guarded issue update owns label changes.

Beads v1.3 has no gate lookup, create, or resolve route. Generic issue creation
cannot set `await_type` or atomically reproduce `bd gate create --blocks`, and
generic close is not a proven gate resolution. Review and kickoff gate
operations therefore remain named CLI compatibility routes. The same applies
to renewable lease heartbeat and reclaim, merge-slot coordination, Beadhive
state dimensions, and the unproved batch and partial-failure variants.
