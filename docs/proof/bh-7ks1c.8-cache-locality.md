# Cache locality correctness evidence for bh-7ks1c.8

Captured from committed tree `915701ce` using the controlled local wheel and tarball fixture.
That revision is the final cache-locality correctness implementation tree. The later branch
commit adds these proof artifacts and validator-compatibility coverage only; cache resolver and
benchmark sources are unchanged, so this is committed-tree evidence rather than dirty or
pre-implementation evidence.
Each case ran three fresh offline repetitions, with one cold and one warm install per repetition.
Timings remain in the raw JSON as descriptive, non-normative metadata; this evidence makes no
performance claim.

| checkout layout | manager | cache → target device | mode | target files inspected | hardlinked files observed by device/inode/link count | copy fallback phases | warnings |
|---|---|---:|---|---:|---:|---:|---:|
| ephemeral | uv | 42 → 42 | hardlink | 144 | 18 | 0/6 | 0 |
| ephemeral | pnpm | 42 → 42 | hardlink | 36 | 12 | 0/6 | 0 |
| persistent | uv | 65024 → 65024 | hardlink | 144 | 18 | 0/6 | 0 |
| persistent | pnpm | 65024 → 65024 | hardlink | 36 | 12 | 0/6 | 0 |
| cross-device | uv | 42 → 65024 | copy | 144 | 0 | 6/6 | 0 |
| cross-device | pnpm | 42 → 65024 | copy | 36 | 0 | 6/6 | 0 |

For same-device cases, the raw report includes target file paths and filesystem identities
(`st_dev`, `st_ino`, and `st_nlink`) for every observed multiply linked file; each identity is
on the cache device. Cross-device cases selected copy mode and recorded no multiply linked
target files. Package-manager install output contained no warnings, including no hardlink
fallback warning loop. The PNPM runs used `PNPM_CONFIG_*` controls and also exported the
`npm_config_*` compatibility aliases.

Minimum available capacity across each case's cache samples (bytes / inodes):

- Ephemeral UV: 18,023,575,552 / 4,831,422; pnpm: 17,994,317,824 / 4,820,146.
- Persistent UV: 170,334,371,840 / 12,838,503; pnpm: 170,332,409,856 / 12,838,023.
- Cross-device UV cache: 18,020,876,288 / 4,830,635; target: 170,335,440,896 / 12,838,763.
- Cross-device pnpm cache: 17,985,466,368 / 4,817,380; target: 170,332,401,664 / 12,838,022.

The raw JSON retains per-run and per-phase cache and target device ids, capacity before and
after installation, inspected-file totals, link identities, framework controls, return codes,
warnings, and descriptive timings. The fixture caches and checkout roots are retained by the
benchmark harness; no opaque cache contents were inspected or removed.
