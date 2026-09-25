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
