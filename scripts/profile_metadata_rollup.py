#!/usr/bin/env python3
"""Throwaway attribution harness for bh-f6w4d — where the ``metadata_rollup`` MISS cost goes.

``metadata.read_fleet`` cold is 10.66 s of a 25.21 s ``bh doctor`` (docs/BH_DATA_PIPELINE.md
§4). §1 of docs/METADATA-CACHE.md already attributed ``_section_fleet_health``'s OLD (pre-
cache) per-call cost across three buckets; this script re-runs that same isolation against the
REAL miss path exercised today — ``metadata.measure`` — over the exact key universe
``doctor._collect`` feeds into ``read_fleet`` (``git_repos | hive_keys_on_disk``, computed the
same way ``_scan`` does), plus the fourth bucket METADATA-CACHE.md's profile predates:
serialization (``json.dumps`` + atomic write of the whole cache).

Not shipped; run ad hoc via ``uv run python scripts/profile_metadata_rollup.py``.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from beadhive import metadata, safety


def _repos(root: Path) -> list[str]:
    out: list[str] = []
    gh = root / "github"
    if gh.is_dir():
        for org in sorted(p for p in gh.iterdir() if p.is_dir()):
            for repo in sorted(p for p in org.iterdir() if p.is_dir()):
                if (repo / ".git").exists():
                    out.append(f"github/{org.name}/{repo.name}")
    local = root / "local"
    if local.is_dir():
        for org in sorted(p for p in local.iterdir() if p.is_dir()):
            for repo in sorted(p for p in org.iterdir() if p.is_dir()):
                if (repo / ".git").exists():
                    out.append(f"local/{org.name}/{repo.name}")
    return out


def main() -> int:
    root = Path(os.environ.get("GIT_WORKSPACE", str(Path.home() / "workspace")))
    keys = _repos(root)
    n = len(keys)
    print(f"root={root}  repos={n}\n")
    if not n:
        print("no repos found")
        return 1

    t_fingerprint = 0.0  # metadata.fingerprint: rev-parse HEAD + .git stat
    t_disk = 0.0  # safety._measure_disk_usage: the os.walk inside safety.scan
    t_scan_git = 0.0  # safety.scan's OTHER git subprocess calls (walk neutralized)
    t_age = 0.0  # safety.last_commit_age_days
    t_maturity = 0.0  # safety._maturity_commit_count — a SECOND `rev-list --count HEAD`,
    #                    duplicating work scan() already did internally and threw away
    t_last_commit_date = 0.0  # metadata._last_commit_date

    real_measure = safety._measure_disk_usage

    wall0 = time.perf_counter()
    records: dict[str, metadata.RepoMetadata] = {}
    for key in keys:
        p = root / key

        f0 = time.perf_counter()
        metadata.fingerprint(p)
        t_fingerprint += time.perf_counter() - f0

        # scan() with the walk neutralized -> isolates its OTHER git calls
        safety._measure_disk_usage = lambda _p: 0  # noqa: E731
        s0 = time.perf_counter()
        safety.scan(str(p))
        t_scan_git += time.perf_counter() - s0
        safety._measure_disk_usage = real_measure

        d0 = time.perf_counter()
        real_measure(str(p))
        t_disk += time.perf_counter() - d0

        a0 = time.perf_counter()
        safety.last_commit_age_days(str(p))
        t_age += time.perf_counter() - a0

        m0 = time.perf_counter()
        safety._maturity_commit_count(str(p))
        t_maturity += time.perf_counter() - m0

        l0 = time.perf_counter()
        metadata._last_commit_date(str(p))
        t_last_commit_date += time.perf_counter() - l0

        # Build a real record so the serialization bucket below is measured on the real payload.
        records[key] = metadata.measure(p)
    wall_measure = time.perf_counter() - wall0

    # Serialization: what refresh()/store() do with the whole batch — json.dumps + atomic write.
    import json
    import tempfile

    cache = metadata.MetadataCache(
        version=metadata.CACHE_VERSION,
        last_updated=metadata._now(),
        workspace_root=str(root),
        repos=records,
    )
    from dataclasses import asdict

    s0 = time.perf_counter()
    payload = json.dumps(asdict(cache), indent=2)
    t_serialize_dumps = time.perf_counter() - s0

    s0 = time.perf_counter()
    fd, tmp = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        fh.write(payload)
    os.replace(tmp, tmp + ".final")
    os.unlink(tmp + ".final")
    t_serialize_write = time.perf_counter() - s0

    effective = t_fingerprint + t_scan_git + t_disk + t_age + t_maturity + t_last_commit_date
    serialize_total = t_serialize_dumps + t_serialize_write

    print("=== metadata.measure() cost, attributed (real code path, real fleet) ===")
    print(f"{'bucket':<38}{'total s':>10}{'per-repo ms':>14}{'% of measure()':>16}")
    for name, val in [
        ("fingerprint (rev-parse HEAD + stat)", t_fingerprint),
        ("scan(): disk walk (_measure_disk_usage)", t_disk),
        ("scan(): other git calls (remote/rev-list/stash/wt)", t_scan_git),
        ("last_commit_age_days (git log -1)", t_age),
        ("_maturity_commit_count (DUPLICATE rev-list --count)", t_maturity),
        ("metadata._last_commit_date", t_last_commit_date),
    ]:
        pct = val / effective * 100 if effective else 0.0
        print(f"{name:<38}{val:>9.3f}s{val / n * 1000:>13.1f}ms{pct:>15.1f}%")
    print(f"{'-' * 78}")
    print(
        f"{'sum of buckets (== 1x measure() per repo)':<38}{effective:>9.3f}s"
        f"{effective / n * 1000:>13.1f}ms{100.0:>15.1f}%"
    )
    print(f"{'measured wall (serial loop, this harness)':<38}{wall_measure:>9.3f}s")
    print()
    print(f"{'serialization: json.dumps(whole cache)':<38}{t_serialize_dumps:>9.4f}s")
    print(f"{'serialization: atomic write (mkstemp+replace)':<38}{t_serialize_write:>9.4f}s")
    print(f"{'serialization total':<38}{serialize_total:>9.4f}s")
    print()
    print(
        f"walk share of measure(): {t_disk / effective * 100:.1f}%   "
        f"git-plumbing share (scan_git+fingerprint+age+maturity+last_commit): "
        f"{(effective - t_disk) / effective * 100:.1f}%   "
        f"serialization share of TOTAL (measure+serialize): "
        f"{serialize_total / (effective + serialize_total) * 100:.2f}%"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
