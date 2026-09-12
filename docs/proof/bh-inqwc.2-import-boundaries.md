# Import-boundary check evidence (`bh-inqwc.2`)

Evidence source: foundation commit `8e00e5994c2ed83894741f3e28d3cb55aaff19ef`, measured
2026-08-31 UTC in the dedicated `bh-inqwc.2` worktree with CPython 3.11.15.

## Every-commit result and runtime

The `architecture-check` recipe is a dependency of both `just check` and `just check-all`. It
invokes only the CPython standard library. The checker parses source text with `ast` and reads the
TOML ledger with `tomllib`; it does not import `beadhive`, discover plugins, read a Dolt store,
open a socket, or start a transport or subprocess.

```text
$ TIMEFORMAT='elapsed=%3R user=%3U system=%3S'; time \
    .venv/bin/python scripts/check_import_boundaries.py
import-boundary-check: OK (180 files, 2021 import edges, 6 owned legacy cycles/288 edges)
elapsed=1.847 user=1.794 system=0.052
```

The checked ledger records 44 exact feedback edges by importing path, imported module, and symbol.
Removing those edges makes the current graph acyclic. A SHA-256 over all 288 edges (318 symbols)
inside the six current strongly connected components makes an added or altered edge fail even if
it happens to share an already-owned component. Wildcards, empty ownership or expiry metadata,
and boundary exceptions without an exact importer path, importer, target module, and symbol are
rejected by the checker.

## Representative failure output

This is the real CLI output for a synthetic application package using
`importlib.import_module("beadhive.cli")`; the process exited 1:

```text
import-boundary-check: FAILED
- src/beadhive/modules/orders/application/handler.py: forbidden application runtime import beadhive.modules.orders.application.handler -> beadhive.cli
```

The focused executable proof covers qualified and aliased literal `importlib` calls, literal
`__import__`, an OpenTelemetry SDK import from domain, a cross-capability domain import, an unowned
cycle, and strict exception-ledger matching. A nonliteral dynamic import fails closed because its
target cannot be verified statically. The allowed fixture deliberately raises at module top level;
its passing check proves source is parsed without executing it.
