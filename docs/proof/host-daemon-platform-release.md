# Host-daemon platform release evidence

`bh-q0lol.14` ships real per-user LaunchAgent and `systemd --user` backends plus an explicit
Compose daemon service. Deterministic command adapters test their generated artifacts and
failure handling on every development host. Those adapters are not release evidence.

Before release, capture a JSON evidence document from real Darwin, Linux, and container targets
and run:

```console
just check-host-daemon-platform-release ./host-daemon-platform-evidence.json <git-revision>
```

The document has this shape:

```json
{
  "schemaVersion": 1,
  "platforms": {
    "darwin": {"available": true, "cells": {}},
    "linux": {"available": true, "cells": {}},
    "container": {"available": true, "cells": {}}
  }
}
```

Every `cells` object must contain the complete closed set printed by
`beadhive.daemon_platform.REQUIRED_RELEASE_CELLS`. Each cell value must contain:

```json
{
  "result": "passed",
  "execution": "real",
  "platform": "linux",
  "target": "stable-release-host-identity",
  "observedAt": "2026-09-04T00:00:00+00:00",
  "sourceRevision": "the-exact-tested-git-revision",
  "detail": "what was executed and observed"
}
```

Evidence older than seven days, from another revision or platform, without a concrete target,
marked simulated/fixture, unavailable, missing, or failed is rejected. The evidence file is an
external release artifact because it records a particular run; this repository intentionally
does not check in a timeless green claim.
