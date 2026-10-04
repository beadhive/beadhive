"""In-data epoch fence model for the writer-fencing molecule (spike bh-vje85). Test-only.

The proposal's fence (``docs/design/hive-writer-partitioning-proposal.md`` §2), installed into a
:class:`harness.writer_fencing.Cluster` hive and driven through each frame's own driver:

* ``bh_writer`` — ONE row ``{frame, epoch, revision}``. Only an adopt writes it, and every adopt
  rewrites ``revision`` (bh-cvk70: a write that leaves the CAS cell alone can merge past it).
* ``bh_epoch_live`` — ONE row (``id`` PK, ``epoch`` UNIQUE). A singleton, so two adopts conflict on
  the epoch itself instead of merging into a union of live epochs (bh-cvk70 E12).
* ``bh_write_mark`` — ``{id uuid, epoch}``, ``epoch`` a foreign key to ``bh_epoch_live(epoch)``.
  The guard trigger inserts one per guarded ``main`` write; adopt deletes every mark below the new
  epoch before it moves the live epoch, so a stale commit merged over the bump is a foreign-key
  violation, not a clean merge.
* ``bh_local_ident`` — the node's identity, ``dolt_ignore``d (never committed, never merged).
* Guard triggers — ``BEFORE INSERT`` / ``BEFORE UPDATE`` on ``issues`` (a VERSIONED bd table; bd's
  ``events`` and ``wisp_*`` are dolt-ignored since schema v62, so a mark anchored there would never
  travel). On ``main`` they refuse the write unless ``bh_writer.frame`` is this node, then insert
  the mark with ``INSERT ... SELECT`` (a scalar subquery inside ``VALUES`` hits a Dolt trigger bug).

The guard itself (NULL ``active_branch()``, other tables, provisioning) is bh-sieai's subject;
this module only needs it to stamp marks and to refuse a stale node's next ``main`` write.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from harness.writer_fencing import BD_SERVER, Cluster, Frame, Run

__all__ = [
    "GUARD_REFUSAL",
    "MONOTONIC_REFUSAL",
    "adopt",
    "bump_statements",
    "divert_to_orphan",
    "fence_audit",
    "fence_statements",
    "install_fence",
    "local_state",
    "local_writer",
    "mark_counts",
    "merge_orphan",
    "remote_branch_titles",
    "remote_fence",
    "run_script",
    "set_identity",
    "sync_to_remote",
]

GUARD_REFUSAL = "bh: not the writer for main"

_GUARD_BODY = (
    "BEGIN "
    "IF COALESCE(active_branch(), 'main') = 'main' THEN "
    "IF COALESCE((SELECT frame FROM bh_writer WHERE id = 1), '') <> "
    "COALESCE((SELECT frame FROM bh_local_ident WHERE id = 1), '?') THEN "
    f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{GUARD_REFUSAL}'; "
    "END IF; "
    "INSERT INTO bh_write_mark (id, epoch) SELECT UUID(), epoch FROM bh_writer WHERE id = 1; "
    "END IF; "
    "END"
)


#: Local DML on the fence rows may only move the epoch forward. Merges never fire triggers, so
#: this stops a stale node's own UPDATE (e.g. re-running an old adopt on top of the new head),
#: not a ``--strategy`` merge that resolves the conflict to the stale side.
MONOTONIC_REFUSAL = "bh: fence epoch must increase"
_MONOTONIC_BODY = (
    "BEGIN "
    "IF NEW.epoch <= OLD.epoch THEN "
    f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{MONOTONIC_REFUSAL}'; "
    "END IF; "
    "END"
)


def fence_statements(writer: str, epoch: int) -> list[str]:
    """DDL + seed rows for the fence, naming ``writer`` at ``epoch``."""
    return [
        "CREATE TABLE bh_writer (id INT PRIMARY KEY, frame VARCHAR(64) NOT NULL, "
        "epoch BIGINT NOT NULL, revision VARCHAR(64) NOT NULL)",
        "CREATE TABLE bh_epoch_live (id INT PRIMARY KEY, epoch BIGINT NOT NULL, "
        "UNIQUE KEY bh_epoch_live_epoch (epoch))",
        "CREATE TABLE bh_write_mark (id VARCHAR(64) PRIMARY KEY, epoch BIGINT NOT NULL, "
        "CONSTRAINT bh_write_mark_epoch FOREIGN KEY (epoch) REFERENCES bh_epoch_live (epoch))",
        "INSERT INTO dolt_ignore VALUES ('bh_local_ident', true)",
        f"INSERT INTO bh_writer VALUES (1, '{writer}', {epoch}, UUID())",
        f"INSERT INTO bh_epoch_live VALUES (1, {epoch})",
        f"CREATE TRIGGER bh_guard_issues_ins BEFORE INSERT ON issues FOR EACH ROW {_GUARD_BODY}",
        f"CREATE TRIGGER bh_guard_issues_upd BEFORE UPDATE ON issues FOR EACH ROW {_GUARD_BODY}",
        "CREATE TRIGGER bh_writer_monotonic BEFORE UPDATE ON bh_writer FOR EACH ROW "
        f"{_MONOTONIC_BODY}",
        "CREATE TRIGGER bh_epoch_live_monotonic BEFORE UPDATE ON bh_epoch_live FOR EACH ROW "
        f"{_MONOTONIC_BODY}",
    ]


def bump_statements(frame: str, epoch: int, *, sentinel: bool = True) -> list[str]:
    """Adopt step 2 as ONE commit: name the writer (fresh revision), retire every older epoch's
    marks, then move the singleton live epoch (the FK forbids moving it while marks point at it).

    ``sentinel`` also inserts an ``adopt-<epoch>`` mark, so EVERY bump changes ``bh_write_mark``.
    A stale bd-server frame always holds its own uncommitted mark (bd's server-mode write commit
    does not stage it), and Dolt refuses to merge a change to a table the working set has dirty
    ("local changes would be stomped by merge"); that closes the one merge entry point, ``bd vc
    merge``, that does not commit the working set first. ``sentinel=False`` is the proposal's
    original bump, kept to pin the hole it leaves."""
    statements = [
        f"UPDATE bh_writer SET frame = '{frame}', epoch = {epoch}, revision = UUID() WHERE id = 1",
        f"DELETE FROM bh_write_mark WHERE epoch < {epoch}",
        f"UPDATE bh_epoch_live SET epoch = {epoch} WHERE id = 1",
    ]
    if sentinel:
        statements.append(f"INSERT INTO bh_write_mark VALUES ('adopt-{epoch}', {epoch})")
    return statements


def run_script(frame: Frame, statements: Sequence[str]) -> Run:
    """Run ``statements`` (trigger bodies included) through the frame's own engine, no commit.

    ``dolt sql -q`` splits on ``;`` even inside ``BEGIN ... END``, so CLI/embedded frames get a
    ``DELIMITER //`` script via ``dolt sql -f``; a server frame sends each statement through
    ``bd sql`` (the server parses one compound statement whole).
    """
    if frame.kind == BD_SERVER:
        for statement in statements:
            res = frame.bd("sql", statement)
            if not res.ok:
                return res
        return res
    path = frame.dir / "script.sql"
    path.write_text("DELIMITER //\n" + "".join(f"{s}//\n" for s in statements) + "DELIMITER ;\n")
    return frame.dolt("sql", "-f", str(path))


def set_identity(frame: Frame) -> None:
    """Provision this node's ``bh_local_ident`` (dolt_ignore'd, so it never reaches a commit)."""
    run_script(
        frame,
        [
            "CREATE TABLE IF NOT EXISTS bh_local_ident (id INT PRIMARY KEY, "
            "frame VARCHAR(64) NOT NULL)",
            f"REPLACE INTO bh_local_ident VALUES (1, '{frame.name}')",
        ],
    ).check()


def install_fence(cluster: Cluster, writer: str) -> int:
    """Place ``writer`` at HQ, commit the fence from it, publish, and provision every frame."""
    epoch = cluster.hq.place(writer).epoch
    founder = cluster[writer]
    founder.pull().check()
    run_script(founder, fence_statements(writer, epoch)).check()
    founder.commit("bh: install in-data epoch fence").check()
    founder.push().check()
    for frame in cluster.frames.values():
        if frame is not founder:
            frame.pull().check()
        set_identity(frame)
    return epoch


def sync_to_remote(frame: Frame) -> None:
    """Make local ``main`` exactly the remote's (an adopter never merges into a stale main).

    Also how a fenced frame drops a half-done merge a refused pull left in its working set."""
    if frame.kind == BD_SERVER:
        frame.bd("sql", "CALL DOLT_FETCH('origin')").check()
        frame.bd("sql", "CALL DOLT_RESET('--hard', 'origin/main')").check()
    else:
        frame.dolt("merge", "--abort")  # no-op unless a refused pull left a merge open
        frame.dolt("fetch", "origin").check()
        frame.dolt("reset", "--hard", "origin/main").check()


def local_writer(frame: Frame) -> tuple[str, int]:
    row = frame.query("SELECT frame, epoch FROM bh_writer WHERE id = 1")[0]
    return row["frame"], int(row["epoch"])


def adopt(
    cluster: Cluster,
    frame: Frame,
    epoch: int,
    *,
    before_push: Callable[[], None] | None = None,
    attempts: int = 4,
    sentinel: bool = True,
) -> str:
    """Placement-first adopt, step 2 (bh-cvk70 Recommendation 3): start from the remote head,
    stop if the data already holds ``>= epoch``, stop if HQ moved, else commit the bump and push;
    a non-fast-forward rejection loops back to the data check."""
    for _ in range(attempts):
        sync_to_remote(frame)
        current = local_writer(frame)
        if current == (frame.name, epoch):
            return "landed"
        if current[1] >= epoch:
            return "abandoned: superseded in data"
        placement = cluster.hq.placement(asker=frame.name)
        if (placement.writer, placement.epoch) != (frame.name, epoch):
            return "abandoned: placement moved"
        run_script(frame, bump_statements(frame.name, epoch, sentinel=sentinel)).check()
        frame.commit(f"bh: adopt {frame.name}@{epoch}").check()
        if before_push is not None:
            hook, before_push = before_push, None
            hook()
        if frame.push().ok:
            return "landed"
    return "abandoned: retries exhausted"


def remote_fence(cluster: Cluster) -> dict:
    """Remote ``main``'s fence rows and issue titles (judge on ``main``, never on the ref)."""
    view = cluster.remote.view(("bh_writer", "bh_epoch_live", "bh_write_mark", "issues"))
    tables = view["tables"]
    writer = (tables["bh_writer"] or [{}])[0]
    return {
        "head": view["head"],
        "main": view["main"],
        "writer": (writer.get("frame"), writer.get("epoch")),
        "live": sorted(row["epoch"] for row in tables["bh_epoch_live"] or []),
        "marks": sorted(row["epoch"] for row in tables["bh_write_mark"] or []),
        "titles": {row["title"] for row in tables["issues"] or []},
    }


def local_state(frame: Frame) -> dict:
    """The frame's own view: writer row, live epoch, open merge, conflicts and violations."""
    writer = frame.query("SELECT frame, epoch FROM bh_writer WHERE id = 1")
    live = frame.query("SELECT epoch FROM bh_epoch_live")
    merging = frame.query("SELECT is_merging FROM dolt_merge_status")
    return {
        "main": frame.local_main(),
        "writer": (writer[0]["frame"], int(writer[0]["epoch"])) if writer else None,
        "live": sorted(int(row["epoch"]) for row in live),
        "merging": bool(merging and merging[0]["is_merging"] not in (0, False, "0", "false")),
        "conflicts": sorted(r["table"] for r in frame.query("SELECT `table` FROM dolt_conflicts")),
        "violations": sorted(
            r["table"] for r in frame.query("SELECT `table` FROM dolt_constraint_violations")
        ),
    }


def divert_to_orphan(frame: Frame, held_epoch: int) -> dict:
    """The managed path for a writer that has fallen behind (proposal §2, scenario 3).

    Fetch only (never merge), read ``bh_writer`` on ``origin/main``; if its epoch is past the one
    this frame holds, push the unpublished local ``main`` to ``frame/<id>/orphan`` and reset to
    the remote. Server frames first commit their working set: bd's server-mode write commits
    leave the trigger's mark unstaged (see the spike doc), and the orphan must carry it.
    """
    branch = f"frame/{frame.name}/orphan"
    if frame.kind == BD_SERVER:
        frame.bd("dolt", "commit", "-m", "bh: commit working set before orphan diversion")
        frame.bd("sql", "CALL DOLT_FETCH('origin')").check()
        remote = frame.query("SELECT frame, epoch FROM bh_writer AS OF 'origin/main'")[0]
        if int(remote["epoch"]) <= held_epoch:
            return {"diverted": False, "remote_writer": remote}
        frame.bd("sql", f"CALL DOLT_PUSH('origin', 'main:{branch}')").check()
    else:
        frame.dolt("fetch", "origin").check()
        remote = frame.query("SELECT frame, epoch FROM bh_writer AS OF 'origin/main'")[0]
        if int(remote["epoch"]) <= held_epoch:
            return {"diverted": False, "remote_writer": remote}
        frame.dolt("push", "origin", f"main:{branch}").check()
    sync_to_remote(frame)
    return {"diverted": True, "branch": branch, "remote_writer": remote}


def merge_orphan(frame: Frame, branch: str) -> dict:
    """The current writer's deliberate orphan merge: the stale marks violate the retired epoch's
    foreign key on purpose, so re-stamp them to the live epoch, clear the violation artifacts and
    commit. One SQL session so the forced transaction flag stays scoped to this merge."""
    if frame.kind == BD_SERVER:
        raise NotImplementedError("the spike drives the orphan merge from a Dolt CLI writer")
    frame.dolt("fetch", "origin").check()
    merged = frame.dolt(
        "sql",
        "-q",
        "SET @@dolt_force_transaction_commit = 1; "
        f"CALL DOLT_MERGE('origin/{branch}'); "
        "SELECT COUNT(*) AS restamped FROM bh_write_mark "
        "WHERE epoch NOT IN (SELECT epoch FROM bh_epoch_live); "
        # a variable, not a subquery: with the fence triggers installed, Dolt 2.3.5 fails the
        # subquery form with "unable to find field with index 5 in row of 4 columns"
        "SET @bh_live = (SELECT epoch FROM bh_epoch_live WHERE id = 1); "
        "UPDATE bh_write_mark SET epoch = @bh_live WHERE epoch <> @bh_live; "
        "DELETE FROM dolt_constraint_violations_bh_write_mark; "
        f"CALL DOLT_COMMIT('-Am', 'bh: merge {branch}, re-stamp its marks')",
    )
    return {"run": merged, "state": local_state(frame)}


def remote_branch_titles(cluster: Cluster, branch: str) -> set[str] | None:
    """Issue titles on remote ``branch`` (None if the remote has no such branch)."""
    observer = cluster.remote
    observer._dolt("fetch", "origin")
    res = observer._dolt(
        "sql", "-r", "json", "-q", f"SELECT title FROM issues AS OF 'origin/{branch}'"
    )
    if res.returncode:
        return None
    text = res.stdout.strip()
    rows = json.loads(text).get("rows", []) if text else []
    return {row["title"] for row in rows}


def fence_audit(cluster: Cluster, hive: str = "hive") -> dict:
    """What bh can detect AFTER the fact from remote ``main`` plus HQ (force paths, Evidence).

    * ``stale_marks``: marks on ``main`` whose epoch is not live, i.e. a write stamped by a
      retired epoch was committed past the foreign key (``dolt commit --force``, a forced merge).
    * ``epoch_regressed``: ``bh_writer.epoch`` on ``main`` is below an epoch its own history
      already reached (a strategy merge resolved ``bh_writer`` to a stale side).
    * ``placement_ahead``: HQ placement names a higher epoch than the data (a force push wiped the
      bump, or an adopt is still incomplete; bh-cvk70 Recommendation 3).
    """
    observer = cluster.remote
    observer._dolt("fetch", "origin")
    observer._dolt("branch", "-f", "bh-audit", "origin/main")

    def scalar(query: str) -> int:
        res = observer._dolt("sql", "-r", "json", "-q", f"CALL DOLT_CHECKOUT('bh-audit'); {query}")
        if res.returncode:
            raise AssertionError(f"audit query failed: {res.stdout}{res.stderr}")
        # the CALL prints its own result set first; the scalar is the last JSON document
        docs = [line for line in res.stdout.strip().splitlines() if line.startswith("{")]
        rows = json.loads(docs[-1]).get("rows", []) if docs else []
        value = next(iter(rows[0].values())) if rows else None
        return int(value or 0)

    current = scalar("SELECT epoch FROM bh_writer WHERE id = 1")
    stale_marks = scalar(
        "SELECT COUNT(*) FROM bh_write_mark WHERE epoch NOT IN (SELECT epoch FROM bh_epoch_live)"
    )
    history_max = scalar("SELECT MAX(epoch) FROM dolt_history_bh_writer")
    placement = cluster.hq.placement(hive)
    return {
        "writer_epoch": current,
        "stale_marks": stale_marks,
        "history_max_epoch": history_max,
        "epoch_regressed": history_max > current,
        "placement_epoch": placement.epoch,
        "placement_ahead": placement.epoch > current,
    }


def mark_counts(frame: Frame) -> dict[int, int]:
    rows = frame.query("SELECT epoch, COUNT(*) AS n FROM bh_write_mark GROUP BY epoch")
    return {int(row["epoch"]): int(row["n"]) for row in rows}
