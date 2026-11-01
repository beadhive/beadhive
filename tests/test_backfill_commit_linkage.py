"""Unit tests for `scripts.backfill_commit_linkage` -- the deprecated shim over the idempotent
full-history backfill of `git.commits` (bh-1b0rc.3, docs/design/bead-commit-linkage-contract.md).

The matcher itself moved to `beadlog_sources.commits` (the `git-commits` strategy, tested in
`packages/beadlog-sources/tests/test_beadlog_sources_commits.py`); the shim tests below pin that
the old script keeps working on the moved matcher, warns about its deprecation, refuses shallow
clones, and writes exactly the shas the strategy proposes (parity).

Two seams are mocked, same pattern as `test_git_linkage.py` / `test_bd_json_seam.py`:

  * `beadhive.bd._run` -- a `FakeBdStore` stands in for `bd show` / `bd update --set-metadata`,
    so no real `bd` binary or store is touched.
  * `scripts.backfill_commit_linkage.load_live_ids` -- avoids shelling out to `bh bd export`
    (which needs a real hive); everything downstream of it (`derive_namespaces`,
    `candidate_pattern`, `extract_candidates`, `resolve_candidates`) runs FOR REAL against a
    fixed live-ID set, so these tests exercise the actual canonical matcher, not a stub of it.

The git history itself is real: a throwaway `git init` repo built per-test with `--allow-empty`
commits, walked by the real `read_commits` / `git log`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from beadhive import bd as bd_mod
from beadhive import git_linkage
from beadlog_sources import ShallowCloneError

# `scripts/` is a namespace package (no `__init__.py`) rooted at the repo root, which pytest's
# default "prepend" import mode does NOT put on sys.path for a test module living under
# `tests/` (a sibling, not an ancestor, of `scripts/`) -- same reason
# `scripts/backfill_commit_linkage.py` inserts this itself when run directly. Match that here
# rather than reaching for a shared pytest config change for one test module.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import backfill_commit_linkage as backfill  # noqa: E402

_CP = namedtuple("CP", "returncode stdout stderr")

_LIVE_IDS = frozenset({"bh-100", "bh-200", "bh-300"})


class FakeBdStore:
    """Minimal stand-in for `bd show` / `bd update --set-metadata`, recording every call."""

    def __init__(self):
        self.beads: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.update_rc = 0
        self.update_err = ""

    def seed(self, bead_id: str, metadata: dict | None = None) -> None:
        self.beads[bead_id] = {"id": bead_id, "metadata": dict(metadata or {})}

    def __call__(self, cmd, **_kw):
        args = list(cmd)
        assert args[0] == "bd"
        args = args[1:]
        if args[:1] == ["-C"]:
            args = args[2:]
        if args[:1] == ["--actor"]:
            args = args[2:]
        self.calls.append(args)
        sub = args[0] if args else ""
        if sub == "show":
            bead = self.beads.get(args[1])
            return _CP(0 if bead else 1, json.dumps(bead) if bead else "", "")
        if sub == "update" and "--set-metadata" in args:
            if self.update_rc != 0:
                return _CP(self.update_rc, "", self.update_err)
            kv = args[args.index("--set-metadata") + 1]
            key, _, val = kv.partition("=")
            bead = self.beads.setdefault(args[1], {"id": args[1], "metadata": {}})
            bead.setdefault("metadata", {})[key] = val
            return _CP(0, "", "")
        return _CP(1, "", f"unexpected call: {args}")

    def set_metadata_calls(self):
        return [c for c in self.calls if c[0] == "update" and "--set-metadata" in c]


@pytest.fixture
def store(monkeypatch):
    fb = FakeBdStore()
    monkeypatch.setattr(bd_mod, "_run", fb)
    for bead_id in _LIVE_IDS:
        fb.seed(bead_id)
    return fb


@pytest.fixture
def live_ids(monkeypatch):
    """Bypass `bh bd export` -- the matcher's namespace-derivation and resolve-backed filtering
    downstream of this run FOR REAL against a fixed set, same as they would against a real
    `bh bd export` snapshot."""
    monkeypatch.setattr(backfill, "load_live_ids", lambda repo: _LIVE_IDS)
    return _LIVE_IDS


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return res.stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test User")
    _git(r, "config", "commit.gpgsign", "false")
    return r


def _commit(repo: Path, subject: str, body: str = "") -> str:
    message = f"{subject}\n\n{body}" if body else subject
    _git(repo, "commit", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


# ---- collect_bead_commits ------------------------------------------------------------------


def test_collect_groups_by_bead_oldest_first(repo, live_ids):
    c1 = _commit(repo, "feat(a): start work (bh-100)")
    c2 = _commit(repo, "feat(b): more work (bh-200)")
    c3 = _commit(repo, "chore(merge): batch bh-100, bh-200")

    bead_to_shas, examined, unmatched, ids, namespaces = backfill.collect_bead_commits(
        str(repo), rev="main"
    )

    assert examined == 3
    assert unmatched == 0
    assert bead_to_shas["bh-100"] == [c1, c3]  # oldest-first, including the multi-bead commit
    assert bead_to_shas["bh-200"] == [c2, c3]
    assert "bh-300" not in bead_to_shas  # never mentioned
    assert ids == live_ids
    assert namespaces == ["bh"]


def test_collect_counts_unmatched_commits(repo, live_ids):
    _commit(repo, "bump: version 0.1.0 -> 0.2.0")  # no bead mention at all
    _commit(repo, "fix(x): tweak bh-infra config")  # bead-shaped token that does NOT resolve

    bead_to_shas, examined, unmatched, _ids, _ns = backfill.collect_bead_commits(
        str(repo), rev="main"
    )

    assert examined == 2
    assert unmatched == 2
    assert bead_to_shas == {}  # a candidate that never resolves is never rendered


def test_collect_reads_body_not_just_subject(repo, live_ids):
    """The canonical extractor is fed subject AND body -- a trailer-only mention must count."""
    c1 = _commit(repo, "fix(x): patch a thing", body="Fixes bh-200")

    bead_to_shas, _examined, unmatched, _ids, _ns = backfill.collect_bead_commits(
        str(repo), rev="main"
    )

    assert unmatched == 0
    assert bead_to_shas["bh-200"] == [c1]


def test_collect_dedupes_repeat_mentions_of_the_same_bead_in_one_commit(repo, live_ids):
    c1 = _commit(repo, "fix(x): bh-100 fix", body="Closes bh-100, see bh-100 for context")

    bead_to_shas, _examined, _unmatched, _ids, _ns = backfill.collect_bead_commits(
        str(repo), rev="main"
    )

    assert bead_to_shas["bh-100"] == [c1]  # one sha, not three


# ---- run_backfill: dry-run vs write ---------------------------------------------------------


def test_dry_run_reports_without_writing(repo, live_ids, store, monkeypatch):
    _commit(repo, "feat(a): start (bh-100)")
    _commit(repo, "feat(b): more (bh-200)")

    spy_calls = []
    monkeypatch.setattr(
        git_linkage, "record_commits", lambda *a, **k: spy_calls.append((a, k)) or True
    )

    rep = backfill.run_backfill(str(repo), rev="main", dry_run=True)

    assert rep.dry_run is True
    assert rep.examined == 2
    assert rep.unmatched == 0
    assert rep.beads_with_commits == 2
    assert rep.beads_updated == 2
    assert rep.total_new_shas == 2
    assert set(rep.per_bead_new) == {"bh-100", "bh-200"}
    # the hard requirement: dry-run never calls record_commits, and never writes
    assert spy_calls == []
    assert store.set_metadata_calls() == []


def test_real_run_writes_via_record_commits(repo, live_ids, store):
    c1 = _commit(repo, "feat(a): start (bh-100)")
    c2 = _commit(repo, "feat(b): more (bh-200)")

    rep = backfill.run_backfill(str(repo), rev="main", dry_run=False)

    assert rep.dry_run is False
    assert rep.beads_updated == 2
    assert rep.total_new_shas == 2
    assert git_linkage.read_commits("bh-100", Path(str(repo))) == [c1]
    assert git_linkage.read_commits("bh-200", Path(str(repo))) == [c2]
    # writes went through the shared write-side helper, not a hand-rolled `bd update`
    assert len(store.set_metadata_calls()) == 2


def test_real_run_multi_bead_commit_accumulates_onto_both(repo, live_ids, store):
    c1 = _commit(repo, "chore(merge): batch bh-100, bh-200")

    rep = backfill.run_backfill(str(repo), rev="main", dry_run=False)

    assert rep.beads_updated == 2
    assert git_linkage.read_commits("bh-100", Path(str(repo))) == [c1]
    assert git_linkage.read_commits("bh-200", Path(str(repo))) == [c1]


# ---- the required property: rerun produces zero diffs ---------------------------------------


def test_rerun_produces_zero_diffs(repo, live_ids, store):
    """Running the backfill twice against the SAME fixture must make the SECOND run's `bd
    update` call count exactly zero -- verified via the store's own recorded calls, not eyeballed
    from output."""
    _commit(repo, "feat(a): start (bh-100)")
    _commit(repo, "feat(b): more (bh-200)")
    _commit(repo, "chore(merge): batch bh-100, bh-200")

    first = backfill.run_backfill(str(repo), rev="main", dry_run=False)
    assert first.beads_updated == 2
    assert first.total_new_shas > 0
    calls_after_first = len(store.set_metadata_calls())
    assert calls_after_first > 0

    second = backfill.run_backfill(str(repo), rev="main", dry_run=False)

    assert second.beads_updated == 0
    assert second.total_new_shas == 0
    assert second.per_bead_new == {}
    # the load-bearing assertion: zero NEW bd update calls on the rerun
    assert len(store.set_metadata_calls()) == calls_after_first


def test_rerun_after_new_commits_only_writes_the_new_ones(repo, live_ids, store):
    """A rerun after NEW history lands should append only the new sha, not re-touch the old
    ones -- exercising `record_commits`'s accumulate-not-overwrite behavior end to end."""
    c1 = _commit(repo, "feat(a): start (bh-100)")
    backfill.run_backfill(str(repo), rev="main", dry_run=False)
    calls_after_first = len(store.set_metadata_calls())

    c2 = _commit(repo, "feat(a): follow-up (bh-100)")
    rep = backfill.run_backfill(str(repo), rev="main", dry_run=False)

    assert rep.beads_updated == 1
    assert rep.per_bead_new == {"bh-100": 1}
    assert git_linkage.read_commits("bh-100", Path(str(repo))) == [c1, c2]
    assert len(store.set_metadata_calls()) == calls_after_first + 1


# ---- render / CLI ----------------------------------------------------------------------------


def test_render_mentions_dry_run_mode(repo, live_ids, store):
    _commit(repo, "feat(a): start (bh-100)")
    rep = backfill.run_backfill(str(repo), rev="main", dry_run=True)
    out = backfill.render(rep)
    assert "DRY RUN" in out
    assert "commits examined:     1" in out


def test_main_dry_run_smoke(repo, live_ids, store, capsys):
    _commit(repo, "feat(a): start (bh-100)")
    rc = backfill.main(["--repo", str(repo), "--rev", "main", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert store.set_metadata_calls() == []


def test_main_json_output(repo, live_ids, store, capsys):
    _commit(repo, "feat(a): start (bh-100)")
    rc = backfill.main(["--repo", str(repo), "--rev", "main", "--dry-run", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["beads_updated"] == 1


# ---- deprecation shim ------------------------------------------------------------------------


def test_main_prints_a_deprecation_naming_the_replacement(repo, live_ids, store, capsys):
    _commit(repo, "feat(a): start (bh-100)")
    assert backfill.main(["--repo", str(repo), "--rev", "main", "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "deprecated" in err
    assert "beadlog extract git-commits" in err and "bh backfill apply" in err


def test_the_correlation_script_is_deprecated_too(repo, live_ids, capsys, monkeypatch):
    from scripts import bead_commit_correlation as correlation

    monkeypatch.setattr(correlation, "load_live_ids", lambda repo: _LIVE_IDS)
    _commit(repo, "feat(a): start (bh-100)")
    assert correlation.main(["--repo", str(repo), "--rev", "main"]) == 0
    captured = capsys.readouterr()
    assert "beadlog extract git-commits" in captured.err
    assert "linked (tightened):   1/1" in captured.out


def test_the_shim_reexports_the_moved_matcher():
    from scripts import bead_commit_correlation as correlation

    from beadlog_sources import commits

    assert correlation.candidate_pattern is commits.candidate_pattern
    assert correlation.extract_candidates is commits.extract_candidates
    assert correlation.resolve_candidates is commits.resolve_candidates


def test_the_shim_refuses_a_shallow_clone_unless_allowed(repo, live_ids, store, tmp_path, capsys):
    _commit(repo, "feat(a): start (bh-100)")
    _commit(repo, "feat(a): more (bh-100)")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ShallowCloneError, match=r"git fetch --unshallow"):
        backfill.main(["--repo", str(shallow), "--dry-run"])
    assert backfill.main(["--repo", str(shallow), "--dry-run", "--allow-shallow"]) == 0


# ---- parity with the strategy ------------------------------------------------------------------


def test_the_shim_writes_exactly_the_shas_the_strategy_proposes(repo, live_ids, store):
    """The `git-commits` strategy and the old script agree on the fixture history: the shas the
    script wrote per bead are exactly the shas the strategy proposes for the same beads."""
    import beadlog
    from beadlog_sources import ExtractRequest, GitCommitsStrategy

    _commit(repo, "feat(a): start (bh-100)")
    _commit(repo, "feat(b): more (bh-200)")
    _commit(repo, "chore(merge): batch bh-100, bh-200")
    _commit(repo, "fix(x): tweak bh-infra config")  # noise: never linked
    _commit(repo, "fix(x): patch", body="Fixes bh-300, see bh-300")
    _commit(repo, "bump: version 0.1.0 -> 0.2.0")

    group = "urn:uuid:00000000-0000-4000-8000-0000000000c1"
    baseline = [
        beadlog.mint_event(
            "create",
            beadlog.BeadRef(group, bead_id),
            {"type": "task", "fields": {"title": bead_id}},
            occurred_at="2026-09-01T00:00:00Z",
            recorded_at="2026-09-01T00:00:00Z",
            extracted_at="2026-10-31T00:00:00Z",
            source=("bd", bead_id),
            provenance="observed",
        )
        for bead_id in sorted(_LIVE_IDS)
    ]
    proposed = GitCommitsStrategy().extract(
        ExtractRequest(
            root=repo,
            group=group,
            extracted_at="2026-10-31T00:00:00Z",
            baseline=tuple(baseline),
            options={"rev": "main"},
        )
    )
    from_strategy = {event.bead.id: json.loads(event.data["value"]) for event in proposed}

    backfill.run_backfill(str(repo), rev="main", dry_run=False)
    from_script = {
        bead_id: git_linkage.read_commits(bead_id, Path(str(repo))) for bead_id in sorted(_LIVE_IDS)
    }

    assert from_strategy == from_script
    assert set(from_strategy) == {"bh-100", "bh-200", "bh-300"}
