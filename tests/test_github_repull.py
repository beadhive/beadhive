"""The opt-in overlap re-pull of the GitHub projection (bh-clqa5).

bd 1.3.0 checkpoints an incremental GitHub pull at the END-of-sync wall clock, so an issue edited
while a sync runs can be skipped by every later incremental pull (the real-bd reproduction is
``tests/test_github_checkpoint_repro_int.py``). With ``tracker_sync.overlap_repull`` on, bh lists
the issues the tracker port reports since its OWN inclusive checkpoint and re-pulls exactly those
numbers with ``bd github sync --pull-only --prefer-github --issues=<n,...>``.

Every bd answer is recorded (``RecordedBd`` beneath the real store transport); the tracker port is
a small in-file fake with the port's semantics (inclusive ``since``, paging, an opaque
checkpoint per page). Time is a pinned clock: nothing here reads the wall clock or a socket.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.test_github_projection import TOKEN, RecordedBd, _entry, _token_source
from tests.test_github_projection import ws as ws  # noqa: F401 - fixture resolved by name

from beadhive import bd as bd_mod
from beadhive import github_projection, github_repull, guard
from beadhive.config_partition import HOST, partition_of
from beadhive.github_repull import OverlapRepull, RepullUnavailable
from beadhive.modules.config.contracts import BeadhiveConfig
from beadhive.modules.tracker.contracts import (
    Checkpoint,
    IssueKey,
    IssueQuery,
    IssueState,
    Page,
    PageCursor,
    Revision,
    TrackerIssue,
)

STARTED = datetime(2026, 11, 2, 20, 23, 0, tzinfo=UTC)
REPO_URL = "https://github.com/acme/widget/issues/{}"
REPULL_OUT = "✓ Pulled {n} issues (0 created, {n} updated)\n"


class FakeReader:
    """The tracker port's ``list_issues`` with its documented semantics: ``since`` is inclusive,
    pages carry an opaque checkpoint (the newest revision on the page), oldest first."""

    def __init__(self, issues: dict[int, datetime], *, page_size: int = 2, url=REPO_URL):
        self.issues = issues
        self.page_size = page_size
        self.url = url
        self.queries: list[IssueQuery] = []
        self.error: Exception | None = None

    def list_issues(self, query: IssueQuery) -> Page[TrackerIssue]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        floor = (
            datetime.fromisoformat(query.since.token.removeprefix("fake:")) if query.since else None
        )
        rows = sorted(
            (when, n) for n, when in self.issues.items() if floor is None or when >= floor
        )
        offset = int(query.cursor.token) if query.cursor else 0
        window = rows[offset : offset + self.page_size]
        more = offset + self.page_size < len(rows)
        mark = Checkpoint("fake:" + window[-1][0].isoformat()) if window else query.since
        items = tuple(
            TrackerIssue(
                key=IssueKey(f"#{n}", self.url.format(n)),
                title=f"issue {n}",
                body="",
                state=IssueState.OPEN,
                labels=frozenset(),
                author=None,
                created_at=when,
                revision=Revision(when),
            )
            for when, n in window
        )
        return Page(items, PageCursor(str(offset + self.page_size)) if more else None, mark)


class RepullBd(RecordedBd):
    """``RecordedBd`` plus the re-pull: ``--issues=`` calls answer with their own pull line."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.repull_rc = 0

    def __call__(self, args, cwd, **kw):
        issues = [a for a in args if a.startswith("--issues=")]
        if args[:2] == ["github", "sync"] and issues:
            self.calls.append(SimpleNamespace(args=list(args), cwd=cwd, **kw))
            count = len(issues[0].removeprefix("--issues=").split(","))
            out = REPULL_OUT.format(n=count) if self.repull_rc == 0 else ""
            err = "" if self.repull_rc == 0 else f"Error: GitHub said no to {TOKEN}\n"
            return subprocess.CompletedProcess(args, self.repull_rc, out, err)
        return super().__call__(args, cwd, **kw)

    def repull_calls(self) -> list[list[str]]:
        return [c.args for c in self.calls if any(str(a).startswith("--issues=") for a in c.args)]


@pytest.fixture
def recorded(monkeypatch):
    fake = RepullBd()
    monkeypatch.setattr(bd_mod, "run", fake)
    return fake


@pytest.fixture
def mark_path(tmp_path):
    return tmp_path / "bhhome" / "state" / "tracker-sync" / "github-acme-widget" / "public.json"


def _overlap(reader, path, *, started=STARTED) -> OverlapRepull:
    return OverlapRepull(reader=reader, checkpoint_path=path, clock=lambda: started)


def _project(ws, overlap, **kw):
    kw.setdefault("token_source", _token_source(TOKEN))
    return github_projection.project(_entry(ws), overlap_repull=overlap, **kw)


def _saved(path) -> str:
    return json.loads(path.read_text())["checkpoint"]


# --- the projection leg ----------------------------------------------------------------------


def test_off_by_default_runs_only_the_incremental_pull(ws, recorded):
    report = github_projection.project(_entry(ws), token_source=_token_source(TOKEN))
    assert report.repull is None
    assert recorded.repull_calls() == []
    assert report.to_json()["repull"] is None


def test_first_run_repulls_only_issues_updated_since_shortly_before_the_pull(
    ws, recorded, mark_path
):
    margin = github_repull.FIRST_RUN_MARGIN
    reader = FakeReader(
        {
            1: STARTED - margin - timedelta(seconds=1),  # long settled: bd's pull covered it
            2: STARTED - margin,  # inclusive boundary
            3: STARTED + timedelta(seconds=1),  # edited while bd's pull ran
        }
    )
    report = _project(ws, _overlap(reader, mark_path))

    assert reader.queries[0].since is None  # no mark yet: the whole listing, once
    assert recorded.repull_calls() == [
        ["github", "sync", "--pull-only", "--prefer-github", "--issues=2,3"]
    ]
    leg = report.repull
    assert (leg.from_checkpoint, leg.listed, leg.repulled) == (False, 2, [2, 3])
    assert (leg.updated, leg.checkpoint_saved) == (2, True)
    assert report.ok
    assert _saved(mark_path) == "fake:" + (STARTED + timedelta(seconds=1)).isoformat()


def test_a_saved_checkpoint_bounds_the_listing_and_every_listed_issue_is_repulled(
    ws, recorded, mark_path
):
    edited = STARTED - timedelta(hours=3)  # before the clock: only the mark decides
    github_repull.write_checkpoint(mark_path, "acme/widget", "fake:" + edited.isoformat())
    reader = FakeReader({1: edited - timedelta(seconds=1), 2: edited, 4: edited + timedelta(1)})

    report = _project(ws, _overlap(reader, mark_path))

    assert reader.queries[0].since == Checkpoint("fake:" + edited.isoformat())
    assert recorded.repull_calls()[0][-1] == "--issues=2,4"  # the boundary repeats, never skips
    assert report.repull.from_checkpoint is True
    assert _saved(mark_path) == "fake:" + (edited + timedelta(1)).isoformat()


def test_the_repull_runs_after_the_incremental_pull_and_before_identity_and_commit(
    ws, recorded, mark_path
):
    reader = FakeReader({2: STARTED})
    _project(ws, _overlap(reader, mark_path), commit_after_pull=True)
    order = [
        "repull" if any(str(a).startswith("--issues=") for a in c.args) else c.args[0]
        for c in recorded.calls
    ]
    assert order.index("github") < order.index("repull") < order.index("list")
    assert order.index("repull") < order.index("dolt")


def test_a_failed_repull_is_a_problem_scrubbed_and_the_checkpoint_stays(ws, recorded, mark_path):
    github_repull.write_checkpoint(mark_path, "acme/widget", "fake:" + STARTED.isoformat())
    recorded.repull_rc = 1
    report = _project(ws, _overlap(FakeReader({2: STARTED}), mark_path))

    assert not report.ok
    assert any("overlap_repull" in p and "not advanced" in p for p in report.problems)
    assert TOKEN not in json.dumps(report.to_json())
    assert report.repull.checkpoint_saved is False
    assert _saved(mark_path) == "fake:" + STARTED.isoformat()


def test_a_tracker_listing_failure_is_a_problem_naming_only_its_type(ws, recorded, mark_path):
    reader = FakeReader({2: STARTED})
    reader.error = RuntimeError(f"secret detail {TOKEN}")
    report = _project(ws, _overlap(reader, mark_path))

    assert recorded.repull_calls() == []
    assert [p for p in report.problems if "overlap_repull" in p] == [
        "tracker_sync.overlap_repull: listing acme/widget through the tracker port failed "
        "(RuntimeError); the checkpoint was not advanced"
    ]
    assert not mark_path.exists()


def test_an_identity_problem_never_advances_the_checkpoint(ws, recorded, mark_path):
    recorded.rows = [dict(recorded.rows[0], external_ref="https://github.com/other/x/issues/1")]
    report = _project(ws, _overlap(FakeReader({2: STARTED}), mark_path))
    assert not report.ok and report.repull.checkpoint_saved is False
    assert not mark_path.exists()


def test_a_dry_run_reads_no_tracker_and_repulls_nothing(ws, recorded, mark_path):
    recorded.pull_out = ""
    reader = FakeReader({2: STARTED})
    report = _project(ws, _overlap(reader, mark_path), dry_run=True)
    assert reader.queries == [] and recorded.repull_calls() == []
    assert report.repull is None and not mark_path.exists()


def test_an_issue_of_another_repository_is_skipped_with_a_warning(ws, recorded, mark_path):
    reader = FakeReader({7: STARTED}, url="https://github.com/other/thing/issues/{}")
    report = _project(ws, _overlap(reader, mark_path))
    assert recorded.repull_calls() == []
    assert any("other/thing/issues/7" in w for w in report.warnings)
    assert report.ok and report.repull.checkpoint_saved is True


def test_a_long_listing_is_repulled_in_bounded_batches(ws, recorded, mark_path):
    count = github_repull.REPULL_BATCH + 5
    reader = FakeReader({n: STARTED for n in range(1, count + 1)}, page_size=40)
    report = _project(ws, _overlap(reader, mark_path))
    calls = recorded.repull_calls()
    assert len(calls) == 2
    assert len(calls[0][-1].removeprefix("--issues=").split(",")) == github_repull.REPULL_BATCH
    assert report.repull.repulled == list(range(1, count + 1))


def test_the_human_report_names_the_leg(ws, recorded, mark_path):
    report = _project(ws, _overlap(FakeReader({2: STARTED}), mark_path))
    text = "\n".join(github_projection.render(report))
    assert "overlap re-pull (first run, no checkpoint): 1 issue(s) re-pulled" in text
    assert "checkpoint saved" in text


# --- the checkpoint file ---------------------------------------------------------------------


def test_a_checkpoint_is_trusted_only_for_its_own_repository(tmp_path):
    path = tmp_path / "public.json"
    github_repull.write_checkpoint(path, "acme/widget", "tok")
    assert github_repull.read_checkpoint(path, "ACME/Widget") == "tok"
    assert github_repull.read_checkpoint(path, "acme/other") is None
    path.write_text("{not json")
    assert github_repull.read_checkpoint(path, "acme/widget") is None
    assert github_repull.read_checkpoint(tmp_path / "absent.json", "acme/widget") is None


def test_the_checkpoint_lives_under_the_bh_state_dir_per_hive(tmp_path):
    entry = {"provider": "github", "org": "Acme", "repo": "widget"}
    assert github_repull.checkpoint_path(entry, tmp_path) == (
        tmp_path / "state" / "tracker-sync" / "github-acme-widget" / "public.json"
    )


# --- the guard: the re-pull shape is the one selective pull a public store admits -------------


def test_guard_admits_the_numbers_only_repull():
    argv = ["github", "sync", "--pull-only", "--prefer-github", "--issues=2,17"]
    assert guard.public_store_github_refusal(argv) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["github", "sync", "--pull-only", "--issues", "2"],  # the space form
        ["github", "sync", "--pull-only", "--issues=widpub-1"],  # a bead id
        ["github", "sync", "--pull-only", "--issues="],
        ["github", "sync", "--pull-only", "--issues=0"],
        ["github", "sync", "--pull-only", "--issues=2", "--issues=3"],
        ["github", "sync", "--issues=2"],  # not pull-only
        ["github", "sync", "--pull-only", "--push-only", "--issues=2"],
        ["github", "sync", "--pull-only", "--parent", "x", "--issues=2"],
        ["github", "pull", "--issues=2"],
    ],
)
def test_guard_refuses_every_other_selective_shape(argv):
    assert guard.public_store_github_refusal(argv) is not None


# --- the config key --------------------------------------------------------------------------


def test_the_key_is_off_by_default_strict_and_host_owned(tmp_path):
    assert BeadhiveConfig.model_validate({"tracker_sync": {}}).tracker_sync.overlap_repull is False
    assert github_repull.enabled({}) is False
    assert github_repull.for_config({}, {}, tmp_path) is None
    cfg = BeadhiveConfig.model_validate({"tracker_sync": {"overlap_repull": True}})
    assert github_repull.enabled(cfg.model_dump()) is True
    with pytest.raises(ValueError):
        BeadhiveConfig.model_validate({"tracker_sync": {"overlap_repull": "true"}})
    assert partition_of("tracker_sync.overlap_repull") == HOST


def test_on_without_a_bound_tracker_refuses_naming_the_key(ws, tmp_path):
    cfg = {"tracker_sync": {"overlap_repull": True}}
    with pytest.raises(RepullUnavailable, match="tracker_sync.overlap_repull"):
        github_repull.for_config(cfg, _entry(ws), tmp_path)
