# Channel branch seeding — one-time bootstrap, bh-7daa6.4

`latest` and `stable` did not exist on `beadhive/beadhive` before this. This file records
**how and why they were hand-created**, so nobody later mistakes two ordinary branches for
something the channel automation (bh-7daa6.2's fast-forward, bh-7daa6.3's promotion) produced.

**This was a one-time bootstrap, not a mechanism.** From here on, `latest` moves only via CI
after a successful publish, and `stable` moves only via explicit promotion. Nothing about this
bead repeats.

## Why both, and why at v0.8.4

- `latest` would self-seed on the next release, but the docs bead (bh-7daa6.7) cannot land
  before the branch exists. Seeding at `v0.8.4` lets docs switch now, leaving 0.8.5 to exercise
  the *automatic* advance (bh-7daa6.8's dogfood) — the thing that actually needs proving. If
  seeding and the first automatic release were the same event, they'd mask each other.
- `stable` is seeded even though install docs deliberately do not point at it yet. An
  existing-but-unadvertised channel is cheap, and it gives the promotion workflow something to
  fast-forward *from* on its first real run, rather than special-casing a missing branch.

## What was run

Commit named by `v0.8.4`:

```text
163c7ff94bb0623d4c56ced8083c677f5b28f369  bump: version 0.8.3 → 0.8.4
```

In the `bh-7daa6.4` worktree, `origin` = `git@github.com:beadhive/beadhive.git` (the public
`beadhive/beadhive` remote):

```bash
git rev-parse v0.8.4^{commit}
# 163c7ff94bb0623d4c56ced8083c677f5b28f369

git ls-remote origin refs/heads/latest refs/heads/stable
# (no output — neither branch existed; this is a bootstrap, not an overwrite)

git branch latest  163c7ff94bb0623d4c56ced8083c677f5b28f369
git branch stable  163c7ff94bb0623d4c56ced8083c677f5b28f369

git push origin latest:refs/heads/latest
git push origin stable:refs/heads/stable
```

Plain branches, no force-push, nothing else moved. After confirming (`git ls-remote`) both
landed on origin at the expected sha, the local `latest`/`stable` refs were deleted from the
worktree — the branches live on `origin` only, exactly as CI's fast-forward and the promotion
workflow will find them.

## Verification — the ref resolves, not just exists in the API

Before creating the branches, the ref genuinely 404'd:

```text
$ nix flake metadata github:beadhive/beadhive/latest
error: unable to download 'https://api.github.com/repos/beadhive/beadhive/commits/latest':
HTTP error 422 — "No commit found for SHA: latest"
```

After pushing, nix's tarball-fetch cache (TTL-based) still had to be bypassed to see the new
ref (`--option tarball-ttl 0 --refresh`):

```text
$ nix flake metadata github:beadhive/beadhive/latest --option tarball-ttl 0 --refresh
Resolved URL:  github:beadhive/beadhive/latest
Revision:      163c7ff94bb0623d4c56ced8083c677f5b28f369
...

$ nix flake metadata github:beadhive/beadhive/stable --option tarball-ttl 0 --refresh
Resolved URL:  github:beadhive/beadhive/stable
Revision:      163c7ff94bb0623d4c56ced8083c677f5b28f369
...
```

Then the literal acceptance command, against a throwaway scratch profile (so as not to mutate
the operator's real one):

```text
$ nix profile add --profile <scratch>/profile github:beadhive/beadhive/latest#default \
    --option tarball-ttl 0 --refresh

$ nix profile list --profile <scratch>/profile
Name:               beadhive
Flake attribute:    packages.x86_64-linux.default
Locked flake URL:   github:beadhive/beadhive/163c7ff94bb0623d4c56ced8083c677f5b28f369?...
Store paths:        /nix/store/1ds4axmkzsh2361bjc1ipc0b5vr6vp2j-beadhive-local-install-toolchain
```

The package actually built and installed from the `latest` ref at the `v0.8.4` commit — a real
resolve-and-build, not an API branch-list check.

## Status: done

Both branches exist on `origin` (`beadhive/beadhive`), pointing at `163c7ff94bb0623d4c56ced8083c677f5b28f369`
(`v0.8.4`). `github:beadhive/beadhive/latest#default` resolves and builds via nix. Nothing
further is expected from this bead — the automation that keeps these branches moving is
bh-7daa6.2 (`latest`) and bh-7daa6.3 (`stable`).
