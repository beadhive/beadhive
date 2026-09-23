# Alerts — proactive agent steering

`bh alerts show` is the small, normalized surface for conditions that warrant an
agent or operator's attention. It reports only active alerts, each with
`severity`, `code`, `message`, and `remediation`; `--json` emits that list directly
for a harness. The same list is available through the `beadhive://alerts` MCP
resource.

Today the first source is the warning set already calculated by `bh doctor`. New
warning sources register an alert rule here instead of adding a bespoke check to
every work or dispatch command.

## Filesystem capacity

The disk-pressure source reports two independent measurements: free capacity on the
filesystem containing the configured worktree root, and free capacity on the filesystem
mounted at `/`. Each measurement includes the path, mount point, filesystem type, and device
when the host exposes that information. An ephemeral worktree root on tmpfs is therefore
reported as worktree-filesystem pressure, while the `/` reading remains the host-root metric.

`alerts.worktree_filesystem_free_floor_mb` controls the worktree-root warning and defaults to
10,240 MB. `alerts.disk_free_floor_mb` controls the root-filesystem warning and also defaults
to 10,240 MB. Set either to `0` to disable that warning. If the new worktree setting is absent,
it inherits `disk_free_floor_mb`; this keeps the existing configured threshold in effect while
letting operators tune the two resources independently. `alerts.worktree_cap_mb` continues to
limit the aggregate managed-worktree footprint per hive.

For example, to warn at 4 GiB of tmpfs worktree capacity while retaining the 10 GiB root
filesystem floor:

```yaml
alerts:
  disk_free_floor_mb: 10240
  worktree_filesystem_free_floor_mb: 4096
```

The worktree alert directs cleanup to the measured worktree filesystem: prune safe merged or
abandoned worktrees, or move `worktrees.path` / `BH_WORKTREES` to a filesystem with more room.
The host-root alert points to `/` and directs cleanup there. Persistent worktree roots keep
working as before; their nearest existing parent supplies capacity and filesystem identity
until the configured root is created.

The doctor JSON keeps `worktree_disk_usage.disk_free_bytes` as a compatibility alias for the
worktree-root reading. New consumers should use `worktree_filesystem.free_bytes` and
`host_root_filesystem.free_bytes`, which name each measurement explicitly.

## Harness hook pattern

A harness hook such as Claude Code's `SessionStart` should call `bh alerts show
--json` (or read `beadhive://alerts`) and, only when the list is non-empty, inject
short steering text naming the active alerts and their remediation. That makes a
durable condition visible at the start of a session without turning every command
into a warning gate.

The hook is a convenience, not the only access path: an agent or operator may run
`bh alerts show` or read `beadhive://alerts` at any time. MCP clients that read the
resource establish a baseline; later MCP mutations emit `resources/updated` only
when the normalized alert list changes, prompting subscribed clients to re-read it.
