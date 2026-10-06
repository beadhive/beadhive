"""Failover reclaim in the adopt bump commit (bh-4z2rx, M3; M14 D5a).

Fast and in-memory: the hive is an SQLite image standing in for the remote head plus one node's
local ``main`` (``sync`` copies the remote image, ``push`` is a fast-forward-only CAS), so the
reclaim statements really execute against bd-shaped ``issues`` / ``labels`` / ``events`` /
``comments`` tables, and the bump-commit coupling, idempotence and non-fast-forward recompute are
judged on the "remote". ``tests/test_failover_reclaim_int.py`` runs the same hook against real
Dolt on the composed fence prototype and a real git backup remote.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from beadhive import failover_reclaim as fr
from beadhive import work_backup
from beadhive import writer_adopt as wa

DEAD = "frame-a"
ME = "frame-c"
OTHER = "frame-b"
PREFIX = "fx"
AT = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)

_SCHEMA = """
create table bh_writer (id int primary key, frame text, epoch int, revision text);
create table bh_epoch_live (id int primary key, epoch int);
create table bh_write_mark (id text primary key, epoch int, tbl text default '');
create table issues (id text primary key, status text not null default 'open', assignee text,
  started_at text);
create table labels (issue_id text, label text, primary key (issue_id, label));
create table events (id text primary key, issue_id text, event_type text, actor text,
  old_value text, new_value text, comment text, created_at text);
create table comments (id text primary key, issue_id text, author text, text text,
  created_at text);
create table config (key text primary key, value text);
"""


class FakeHive:
    """:class:`~beadhive.writer_adopt.FenceData` + :class:`~beadhive.failover_reclaim.ReclaimData`
    over an SQLite image. ``remote`` is the published head; ``local`` is this node's main."""

    def __init__(self, writer: str = DEAD, epoch: int = 7):
        seed = sqlite3.connect(":memory:")
        seed.executescript(_SCHEMA)
        seed.execute("insert into bh_writer values (1, ?, ?, 'r0')", (writer, epoch))
        seed.execute("insert into bh_epoch_live values (1, ?)", (epoch,))
        seed.execute("insert into bh_write_mark values (?, ?, 'bh_writer')", (f"m{epoch}", epoch))
        seed.commit()
        self.remote = seed.serialize()
        self.version = 0
        self.base_version = 0
        self.local = self._open(self.remote)
        self.commits: list[tuple[str, list[str]]] = []
        self.pushes = 0
        self.after_commit: list = []

    @staticmethod
    def _open(image: bytes) -> sqlite3.Connection:
        con = sqlite3.connect(":memory:")
        con.deserialize(image)
        con.row_factory = sqlite3.Row
        return con

    # ---- seeding (a write published by the old primary) --------------------------------------
    def seed(self, *statements: str) -> None:
        con = self._open(self.remote)
        for s in statements:
            con.execute(s)
        con.commit()
        self.remote = con.serialize()
        self.version += 1

    def claim(self, bead: str, assignee: str, *labels: str, status: str = "in_progress"):
        con = self._open(self.remote)
        con.execute(
            "insert into issues (id, status, assignee, started_at) values (?, ?, ?, ?)",
            (bead, status, assignee, "2026-10-06 10:00:00" if status == "in_progress" else None),
        )
        for label in labels:
            con.execute("insert into labels values (?, ?)", (bead, label))
        con.commit()
        self.remote = con.serialize()
        self.version += 1

    def policy(self, **kv: str) -> None:
        con = self._open(self.remote)
        for key, value in kv.items():
            con.execute(
                "insert or replace into config values (?, ?)",
                ("bh." + key.replace("__", "."), value),
            )
        con.commit()
        self.remote = con.serialize()
        self.version += 1

    def view(self) -> sqlite3.Connection:
        return self._open(self.remote)

    # ---- FenceData ---------------------------------------------------------------------------
    def sync_to_remote(self):
        self.local = self._open(self.remote)
        self.base_version = self.version

    def _writer(self, con):
        row = con.execute("select frame, epoch, revision from bh_writer where id = 1").fetchone()
        return None if row is None else wa.WriterRow(row[0], int(row[1]), row[2])

    def writer(self):
        return self._writer(self.local)

    def remote_writer(self):
        return self._writer(self._open(self.remote))

    def history_max_epoch(self):
        return self.writer().epoch

    def trigger_count(self):
        return wa.FENCE_TRIGGER_COUNT

    def commit_bump(self, statements, message):
        for s in statements:
            self.local.execute(s)
        self.local.commit()
        self.commits.append((message, list(statements)))
        for hook in self.after_commit:
            hook()

    def push(self):
        self.pushes += 1
        if self.version != self.base_version:
            return False
        self.remote = self.local.serialize()
        self.version += 1
        self.base_version = self.version
        return True

    # ---- ReclaimData ---------------------------------------------------------------------------
    def config_rows(self):
        return {
            r["key"]: r["value"]
            for r in self.local.execute("select key, value from config where key like 'bh.%'")
        }

    def claims(self):
        out = []
        for r in self.local.execute("select id, assignee from issues where status='in_progress'"):
            labels = frozenset(
                x[0]
                for x in self.local.execute("select label from labels where issue_id=?", (r[0],))
            )
            out.append(fr.Claim(r[0], r[1] or "", labels))
        return out


class FakeProbe:
    """D3 answers by bead; ``None`` = the fetch failed."""

    def __init__(self, backups: dict[str, fr.Backup] | None = None, *, fail: bool = False):
        self.backups = backups or {}
        self.fail = fail
        self.calls: list[tuple[list[str], str]] = []
        self.deleted: list[tuple[str, str]] = []

    def probe(self, claims, dead_frame, policy):
        self.calls.append(([c.bead for c in claims], dead_frame))
        if self.fail:
            return None
        return {c.bead: self.backups[c.bead] for c in claims if c.bead in self.backups}

    def delete(self, ref, sha, policy):
        self.deleted.append((ref, sha))
        return True


class FakePlacement:
    def __init__(self, frame: str | None, epoch: int):
        self.view = None if frame is None else wa.PlacementView(frame, epoch, f"tok{epoch}")

    def read(self):
        return self.view

    def cas(self, frame, epoch, *, expected):
        if expected != self.view:
            raise wa.PlacementLost("moved")
        self.view = wa.PlacementView(frame, epoch, f"tok{epoch}+")
        return self.view


class NoRef:
    def read(self):
        return None

    def cas(self, frame, epoch, *, expected):  # pragma: no cover - never called without a ref
        raise AssertionError("no ref")


def _cf(frame: str) -> str:
    return f"claim-frame:{frame}"


def _recoverable(bead: str, sha: str = "c0ffee" * 6 + "abcd") -> fr.Backup:
    return fr.Backup(work_backup.RECOVERABLE, work_backup.backup_ref(bead, DEAD), sha)


def _hive(mode: str = "apply", pairing: str = "true") -> FakeHive:
    hive = FakeHive()
    hive.policy(pairing__enabled=pairing, reclaim__failover__mode=mode)
    return hive


def _reclaim(hive, probe, failover=True):
    return fr.FailoverReclaim(hive, probe, failover, clock=lambda: AT)


def _adopt(hive, probe, *, displaced=(DEAD, 7), failover=None, attempts=None):
    placement = FakePlacement(*displaced) if displaced else FakePlacement(None, 0)
    return wa.coexistence_adopt(
        hive,
        placement,
        NoRef(),
        prefix=PREFIX,
        frame=ME,
        reclaim=_reclaim(hive, probe),
        failover=failover,
        attempts=attempts,
    )


def _issue(hive, bead):
    return dict(
        hive.view()
        .execute("select status, assignee, started_at from issues where id = ?", (bead,))
        .fetchone()
    )


def _labels(hive, bead):
    rows = hive.view().execute("select label from labels where issue_id = ?", (bead,))
    return sorted(r[0] for r in rows)


def _comments(hive, bead):
    rows = hive.view().execute("select author, text from comments where issue_id = ?", (bead,))
    return [dict(r) for r in rows]


# ---- the pure table --------------------------------------------------------------------------


def test_decide_follows_the_d5a_table_row_by_row():
    claims = [
        fr.Claim("s1", "dev/x", frozenset({_cf(DEAD), "review:pending"})),
        fr.Claim("s2", "dev/x", frozenset({_cf(DEAD), "review:approved"})),
        fr.Claim("r2", "dev/x", frozenset({_cf(DEAD)})),
        fr.Claim("r3", "dev/x", frozenset({_cf(DEAD)})),
        fr.Claim("r3b", "dev/x", frozenset({_cf(DEAD)})),
        fr.Claim("sus", "dev/x", frozenset({_cf(DEAD)})),
        fr.Claim("r5", "dev/y", frozenset({_cf(OTHER)})),
        fr.Claim("r6", "dev/z", frozenset()),
    ]
    backups = {
        "s1": _recoverable("s1"),
        "r2": _recoverable("r2"),
        "r3b": fr.Backup(work_backup.UNBACKED, "ref", "abc", "tip already in the integration base"),
        "sus": fr.Backup(work_backup.SUSPECT, "ref", "abc", "1 commit(s) not signed"),
    }
    got = {r.bead: r.outcome for r in fr.decide(claims, DEAD, backups)}
    assert got == {
        "s1": fr.Outcome.SUBMITTED,
        "s2": fr.Outcome.VIOLATION,  # submitted, but no backup ref: flagged, untouched
        "r2": fr.Outcome.RESUMABLE,
        "r3": fr.Outcome.REWOUND,  # no ref at all
        "r3b": fr.Outcome.REWOUND,  # tip already in base
        "sus": fr.Outcome.SUSPECT,
        "r5": fr.Outcome.OTHER_FRAME,
        "r6": fr.Outcome.UNATTRIBUTED,
    }


def test_a_failed_fetch_never_rewinds():
    claims = [
        fr.Claim("r4", "dev/x", frozenset({_cf(DEAD)})),
        fr.Claim("s1", "dev/x", frozenset({_cf(DEAD), "review:pending"})),
    ]
    got = {r.bead: r.outcome for r in fr.decide(claims, DEAD, None)}
    assert got == {"r4": fr.Outcome.PENDING, "s1": fr.Outcome.SUBMITTED}
    assert not fr.reclaim_statements(
        fr.decide(claims, DEAD, None), dead_frame=DEAD, frame=ME, epoch=8, at="t"
    )


def test_a_submitted_backup_that_misses_the_submitted_sha_is_a_violation():
    claim = fr.Claim("s1", "dev/x", frozenset({_cf(DEAD), "review:pending"}), "feed" * 10)
    backup = fr.Backup(work_backup.RECOVERABLE, "ref", "abc", covers_submitted=False)
    [row] = fr.decide([claim], DEAD, {"s1": backup})
    assert row.outcome is fr.Outcome.VIOLATION


def test_claim_frame_matches_by_ref_segment():
    claim = fr.Claim("b", "", frozenset({_cf("host.one")}))
    assert fr.decide([claim], "host.one", {})[0].outcome is fr.Outcome.REWOUND
    assert fr.decide([claim], "host-two", {})[0].outcome is fr.Outcome.OTHER_FRAME


# ---- the bump commit -----------------------------------------------------------------------


def test_failover_adopt_applies_the_table_in_the_same_commit_as_the_bump():
    hive = _hive()
    hive.claim("bh-rew", "dev/x", _cf(DEAD))
    hive.claim("bh-res", "dev/y", _cf(DEAD), "recovery:stale")
    hive.claim("bh-sub", "dev/z", _cf(DEAD), "review:pending")
    hive.claim("bh-live", "dev/w", _cf(OTHER))  # a surviving executor's live claim
    hive.claim("bh-legacy", "dev/v")  # unattributed
    hive.claim("bh-open", "", status="open")
    probe = FakeProbe({"bh-res": _recoverable("bh-res"), "bh-sub": _recoverable("bh-sub")})

    out = _adopt(hive, probe)

    assert out.step2.landed and out.epoch == 8
    [(message, statements)] = hive.commits  # ONE commit: the bump and the reclaim together
    assert message == f"{wa.ADOPT_COMMIT_PREFIX}{ME}@8"
    assert statements[0].startswith("UPDATE bh_writer") and any(
        "UPDATE issues" in s for s in statements
    )
    plan = out.step2.reclaim
    assert plan.applied and plan.mode == "apply" and plan.dead_frame == DEAD
    # Unbacked: indistinguishable from never-claimed in the lifecycle fields.
    assert _issue(hive, "bh-rew") == {"status": "open", "assignee": "", "started_at": None}
    assert _labels(hive, "bh-rew") == []
    # Backed up: open, resumable, its work named in the audit comment.
    assert _issue(hive, "bh-res") == {"status": "open", "assignee": "", "started_at": None}
    assert _labels(hive, "bh-res") == ["recovery:resumable"]
    [audit] = _comments(hive, "bh-res")
    assert f"{work_backup.backup_ref('bh-res', DEAD)}@{'c0ffee' * 6}abcd" in audit["text"]
    # Untouched rows.
    assert _issue(hive, "bh-sub")["status"] == "in_progress"
    assert _labels(hive, "bh-sub") == sorted([_cf(DEAD), "review:pending"])
    assert _issue(hive, "bh-live") == {
        "status": "in_progress",
        "assignee": "dev/w",
        "started_at": "2026-10-06 10:00:00",
    }
    assert _labels(hive, "bh-live") == [_cf(OTHER)]
    assert _issue(hive, "bh-legacy")["status"] == "in_progress"
    # Only the dead frame's claims were probed.
    [(probed, frame)] = probe.calls
    assert sorted(probed) == ["bh-res", "bh-rew", "bh-sub"] and frame == DEAD


def test_each_reclaimed_bead_carries_a_who_when_why_audit_comment():
    hive = _hive()
    hive.claim("bh-rew", "dev/x", _cf(DEAD))
    hive.claim("bh-res", "dev/y", _cf(DEAD))
    _adopt(hive, FakeProbe({"bh-res": _recoverable("bh-res")}))

    for bead, outcome in (("bh-rew", "rewound"), ("bh-res", "resumable")):
        [c] = _comments(hive, bead)
        assert c["author"] == f"ops/adopt@{ME}"  # who
        assert "2026-10-06 12:00:00 UTC" in c["text"]  # when
        assert f"({outcome})" in c["text"] and DEAD in c["text"] and "epoch 8" in c["text"]
    # bd's events table is node-local: the audit lives in the comment and the bump commit.
    assert hive.view().execute("select count(*) from events").fetchone()[0] == 0


def test_an_empty_backup_ref_is_deleted_only_after_the_bump_lands():
    hive = _hive()
    hive.claim("bh-rew", "dev/x", _cf(DEAD))
    ref = work_backup.backup_ref("bh-rew", DEAD)
    probe = FakeProbe({"bh-rew": fr.Backup(work_backup.UNBACKED, ref, "abc", "in base")})
    seen_at_commit: list = []
    hive.after_commit.append(lambda: seen_at_commit.append(list(probe.deleted)))

    _adopt(hive, probe)

    assert seen_at_commit == [[]]  # nothing deleted before the push
    assert probe.deleted == [(ref, "abc")]


def test_rerunning_the_adopt_does_not_reclaim_twice():
    hive = _hive()
    hive.claim("bh-rew", "dev/x", _cf(DEAD))
    probe = FakeProbe()
    first = _adopt(hive, probe)
    assert first.step2.reclaim.applied
    # A new claim by the same dead frame appears afterwards (it should not, but prove the
    # re-run touches nothing): re-running the same adopt stops at the data check.
    hive.claim("bh-late", "dev/x", _cf(DEAD))
    placement = FakePlacement(ME, first.epoch)
    again = wa.coexistence_adopt(
        hive, placement, NoRef(), prefix=PREFIX, frame=ME, reclaim=_reclaim(hive, probe)
    )
    assert again.resumed and again.step2.reclaim is None
    assert len(hive.commits) == 1 and len(probe.calls) == 1
    assert _issue(hive, "bh-late")["status"] == "in_progress"
    assert len(_comments(hive, "bh-rew")) == 1


def test_a_non_fast_forward_recomputes_the_plan_from_the_new_head():
    hive = _hive()
    hive.claim("bh-1", "dev/x", _cf(DEAD))
    hive.claim("bh-2", "dev/x", _cf(DEAD))
    probe = FakeProbe()

    def foreign_close():  # the dead primary's last publish races in: bh-2 was closed
        hive.after_commit.clear()
        hive.seed("update issues set status = 'closed' where id = 'bh-2'")

    hive.after_commit.append(foreign_close)
    out = _adopt(hive, probe)

    assert out.step2.landed and out.step2.attempts == 2
    assert len(probe.calls) == 2  # a fresh fetch for the new head
    assert [r.bead for r in out.step2.reclaim.rows] == ["bh-1"]
    assert _issue(hive, "bh-2")["status"] == "closed"  # never reopened by a stale plan
    assert _comments(hive, "bh-2") == []


# ---- kind and policy -----------------------------------------------------------------------


def test_a_planned_handoff_applies_nothing():
    hive = _hive()
    hive.claim("bh-1", "dev/x", _cf(DEAD))
    probe = FakeProbe()
    out = _adopt(hive, probe, displaced=("", 7))  # released placement: a tombstone
    assert out.step2.landed
    assert "planned handoff" in out.step2.reclaim.skipped
    assert probe.calls == [] and _issue(hive, "bh-1")["status"] == "in_progress"
    assert not any("issues" in s for s in hive.commits[0][1])


def test_explicit_kind_overrides_the_displaced_placement():
    hive = _hive()
    hive.claim("bh-1", "dev/x", _cf(DEAD))
    out = _adopt(hive, FakeProbe(), failover=False)
    assert "planned handoff" in out.step2.reclaim.skipped
    assert _issue(hive, "bh-1")["status"] == "in_progress"


def test_failover_kind_from_the_displaced_placement():
    assert wa.failover_kind(ME, wa.PlacementView(DEAD, 3)) is True
    assert wa.failover_kind(ME, wa.PlacementView("", 3)) is False
    assert wa.failover_kind(ME, None) is False
    assert wa.failover_kind(ME, wa.PlacementView(ME, 3)) is None


@pytest.mark.parametrize(
    ("mode", "pairing", "want_mode"),
    [("off", "true", "off"), ("report", "true", "report"), ("apply", "false", "report")],
)
def test_reclaim_mode_off_and_report_write_nothing(mode, pairing, want_mode):
    hive = _hive(mode, pairing)
    hive.claim("bh-1", "dev/x", _cf(DEAD))
    out = _adopt(hive, FakeProbe())
    plan = out.step2.reclaim
    assert plan.mode == want_mode and not plan.applied
    assert _issue(hive, "bh-1")["status"] == "in_progress"
    if want_mode == "report":
        assert [(r.bead, r.outcome) for r in plan.rows] == [("bh-1", fr.Outcome.REWOUND)]
        assert any("report only" in line for line in plan.describe())
    else:
        assert plan.rows == () and "off" in plan.skipped


def test_the_default_policy_is_off():
    hive = FakeHive()
    hive.claim("bh-1", "dev/x", _cf(DEAD))
    out = _adopt(hive, FakeProbe())
    assert out.step2.reclaim.mode == "off" and not out.step2.reclaim.applied


def test_a_probe_failure_lands_the_bump_and_writes_nothing():
    hive = _hive()
    hive.claim("bh-1", "dev/x", _cf(DEAD))
    out = _adopt(hive, FakeProbe(fail=True))
    assert out.step2.landed
    assert [r.outcome for r in out.step2.reclaim.rows] == [fr.Outcome.PENDING]
    assert _issue(hive, "bh-1")["status"] == "in_progress"


def test_a_raising_probe_is_not_computed_and_never_blocks_the_bump():
    class Boom(FakeProbe):
        def probe(self, claims, dead_frame, policy):
            raise OSError("git exploded")

    hive = _hive()
    hive.claim("bh-1", "dev/x", _cf(DEAD))
    out = _adopt(hive, Boom())
    assert out.step2.landed and "git exploded" in out.step2.reclaim.error
    assert _issue(hive, "bh-1")["status"] == "in_progress"


def test_for_fence_data_is_dormant_without_a_claims_reader(tmp_path):
    class FenceOnly:
        pass

    assert fr.for_fence_data(FenceOnly(), cwd=tmp_path) is None
    hook = fr.for_fence_data(FakeHive(), cwd=tmp_path)
    assert isinstance(hook, fr.FailoverReclaim) and isinstance(hook.probe, fr.GitBackupProbe)


def test_statements_quote_hostile_values():
    row = fr.Row("bh-1", fr.Outcome.REWOUND, assignee="o'brien")
    out = fr.reclaim_statements(
        [row], dead_frame="x'y", frame="z", epoch=2, at="2026-10-06 00:00:00"
    )
    hive = FakeHive()
    hive.claim("bh-1", "o'brien", _cf("x"))
    hive.sync_to_remote()
    for s in out:
        hive.local.execute(s)
    assert hive.local.execute("select status from issues").fetchone()[0] == "open"
