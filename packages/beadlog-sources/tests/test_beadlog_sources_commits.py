"""The neutral ``git-commits`` strategy: the two-step matcher, baseline-resolved bead ids,
append-only idempotent ``git.commits`` events, drift reporting and the shallow-clone refusal.

The git history is real (a throwaway ``git init`` repo of ``--allow-empty`` commits with fixed
dates); the baseline is a BeadLog of plain ``create`` events, as ``beadlog extract bd`` would
produce. No bd store and no BeadHive are involved.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import beadlog
from beadlog import BeadEvent, BeadRef, EventKind, fold, mint_event
from beadlog_sources import ExtractRequest, GitCommitsStrategy, ShallowCloneError, reconcile
from beadlog_sources.cli import discover_strategies, main
from beadlog_sources.commits import (
    FIELD,
    Commit,
    CommitsSourceError,
    candidate_pattern,
    commit_time,
    derive_namespaces,
    extract_candidates,
    is_shallow,
    match_commits,
    parse_linkage,
    read_commits,
    resolve_candidate,
    resolve_candidates,
)

GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000c1"
EXTRACTED = "2026-10-31T00:00:00Z"
LIVE = frozenset({"bh-100", "bh-200", "bh-300"})


# -- fixtures ------------------------------------------------------------------------------


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **(env or {})},
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    return root


_CLOCK = {"n": 0}


def _commit(repo: Path, subject: str, body: str = "", *, date: str | None = None) -> str:
    """One empty commit; each gets a distinct, increasing second unless ``date`` is given."""
    _CLOCK["n"] += 1
    when = date or f"2026-10-01T10:{_CLOCK['n'] // 60 % 60:02d}:{_CLOCK['n'] % 60:02d}+00:00"
    message = f"{subject}\n\n{body}" if body else subject
    _git(
        repo,
        "commit",
        "--allow-empty",
        "-m",
        message,
        env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )
    return _git(repo, "rev-parse", "HEAD")


def _create(bead: str, *, fields: dict[str, Any] | None = None, at: str = "2026-09-01T00:00:00Z"):
    return mint_event(
        "create",
        BeadRef(GROUP, bead),
        {"type": "task", "fields": {"title": f"Bead {bead}", **(fields or {})}},
        occurred_at=at,
        recorded_at=at,
        extracted_at=EXTRACTED,
        source=("bd", f"issues/{bead}#create"),
        provenance="observed",
    )


def _baseline(
    ids: Sequence[str] = tuple(sorted(LIVE)), existing: dict[str, str] | None = None
) -> tuple[BeadEvent, ...]:
    existing = existing or {}
    return tuple(
        _create(bead, fields={FIELD: existing[bead]} if bead in existing else None) for bead in ids
    )


def _request(repo: Path, baseline: Sequence[BeadEvent], **options: str) -> ExtractRequest:
    return ExtractRequest(
        root=repo, group=GROUP, extracted_at=EXTRACTED, baseline=tuple(baseline), options=options
    )


def _linkage(events: Sequence[BeadEvent]) -> dict[str, Any]:
    """The final ``git.commits`` per bead after folding ``events``."""
    out: dict[str, list[str]] = {}
    for ref, state in fold(events).items():
        raw = state.fields.get(FIELD)
        if raw is not None:
            out[ref.id] = parse_linkage(raw) or raw
    return out


def _proposed(repo: Path, baseline: Sequence[BeadEvent], **options: str) -> dict[str, list[str]]:
    events = GitCommitsStrategy().extract(_request(repo, baseline, rev="main", **options))
    return {event.bead.id: json.loads(event.data["value"]) for event in events}


# -- the canonical matcher -----------------------------------------------------------------


def test_candidates_over_match_and_resolution_drops_the_noise() -> None:
    pattern = candidate_pattern({"bh"})
    text = "fix(x): bh-100 and bh-infra, see bh-version; BH-200.\nbody: bh-q160-style, foobh-300"
    candidates = extract_candidates(text, pattern)
    assert candidates == ["bh-100", "bh-infra", "bh-version", "bh-200", "bh-q160-style"]
    resolved, dropped = resolve_candidates(candidates, LIVE)
    assert resolved == ["bh-100", "bh-200"]
    assert dropped == ["bh-infra", "bh-version", "bh-q160-style"]


def test_resolution_trims_to_the_longest_resolving_segment() -> None:
    live = frozenset({"bh-q160", "bh-baml-8u9", "bh-baml-8u9.17"})
    assert resolve_candidate("bh-q160-style", live) == "bh-q160"
    assert resolve_candidate("bh-baml-8u9.17", live) == "bh-baml-8u9.17"
    assert resolve_candidate("bh-baml-8u9.18", live) == "bh-baml-8u9"
    assert resolve_candidate("bh-infra", live) is None
    assert resolve_candidate("", live) is None


def test_a_token_glued_to_word_characters_is_not_a_candidate() -> None:
    pattern = candidate_pattern({"bh"})
    assert extract_candidates("foobh-1 bh-1x_y xbh-2 (bh-3)", pattern) == ["bh-3"]


def test_namespaces_are_read_off_the_ids_and_empty_is_refused() -> None:
    assert derive_namespaces({"bh-1", "bh-2", "bh-3"}) == {"bh"}
    assert derive_namespaces({"nodash"}) == set()
    with pytest.raises(ValueError, match="no namespaces"):
        candidate_pattern(set())


def test_longest_namespace_wins_when_one_prefixes_another() -> None:
    pattern = candidate_pattern({"bh", "bhui"})
    assert extract_candidates("bhui-do1 bh-4o07n", pattern) == ["bhui-do1", "bh-4o07n"]


def test_match_commits_dedupes_repeats_and_counts_unmatched() -> None:
    commit = Commit(
        "a" * 40, 1, "s", "fix bh-100\nCloses bh-100, see bh-100", "2026-10-01T00:00:00Z"
    )
    noise = Commit("b" * 40, 1, "s", "tweak bh-infra", "2026-10-01T00:00:01Z")
    out = match_commits([commit, noise], LIVE)
    assert out.by_bead == {"bh-100": [commit]}
    assert (out.examined, out.unmatched) == (2, 1)


def test_parse_linkage_accepts_only_a_json_array_of_strings() -> None:
    assert parse_linkage('["a", "b"]') == ["a", "b"]
    assert parse_linkage("[]") == []
    for bad in ("nope", '{"a": 1}', "[1]", '["a", 2]', "", None, ["a"]):
        assert parse_linkage(bad) is None


def test_commit_time_is_a_canonical_utc_timestamp() -> None:
    assert commit_time("2026-10-01T12:00:00+02:00") == "2026-10-01T10:00:00Z"
    assert commit_time("2026-10-01T12:00:00Z") == "2026-10-01T12:00:00Z"
    assert commit_time("not a date") is None
    assert commit_time("2026-10-01T12:00:00") is None


# -- parity with the old script ------------------------------------------------------------


def test_groups_by_bead_oldest_first_including_the_multi_bead_commit(repo: Path) -> None:
    c1 = _commit(repo, "feat(a): start work (bh-100)")
    c2 = _commit(repo, "feat(b): more work (bh-200)")
    c3 = _commit(repo, "chore(merge): batch bh-100, bh-200")
    assert _proposed(repo, _baseline()) == {"bh-100": [c1, c3], "bh-200": [c2, c3]}  # not bh-300


def test_the_body_counts_not_just_the_subject(repo: Path) -> None:
    c1 = _commit(repo, "fix(x): patch a thing", body="Fixes bh-200")
    assert _proposed(repo, _baseline()) == {"bh-200": [c1]}


def test_repeat_mentions_in_one_commit_record_one_sha(repo: Path) -> None:
    c1 = _commit(repo, "fix(x): bh-100 fix", body="Closes bh-100, see bh-100 for context")
    assert _proposed(repo, _baseline()) == {"bh-100": [c1]}


def test_false_positives_never_survive_resolution(repo: Path) -> None:
    _commit(repo, "bump: version 0.1.0 -> 0.2.0")
    _commit(repo, "fix(x): tweak bh-infra config", body="bh-version, bh-harness, bh-q160-style")
    events = GitCommitsStrategy().extract(_request(repo, _baseline(), rev="main"))
    assert events == ()


def test_ids_are_resolved_against_the_baseline_not_a_prefix(repo: Path) -> None:
    c1 = _commit(repo, "feat: work on zz-7 and bh-100")
    baseline = _baseline(["zz-7", "zz-8"])  # a hive whose namespace is zz
    assert _proposed(repo, baseline) == {"zz-7": [c1]}


def test_a_bead_of_another_group_or_a_deleted_bead_does_not_resolve(repo: Path) -> None:
    _commit(repo, "feat: work on bh-100 and bh-200")
    foreign = mint_event(
        "create",
        BeadRef("urn:uuid:other", "bh-100"),
        {"type": "task", "fields": {"title": "x"}},
        occurred_at=None,
        recorded_at=None,
        extracted_at=EXTRACTED,
        source=("bd", "x"),
        provenance="observed",
    )
    deleted = mint_event(
        "delete",
        BeadRef(GROUP, "bh-200"),
        {},
        occurred_at="2026-09-02T00:00:00Z",
        recorded_at="2026-09-02T00:00:00Z",
        extracted_at=EXTRACTED,
        source=("bd", "y"),
        provenance="observed",
    )
    baseline = (*_baseline(["bh-100", "bh-200", "bh-300"]), foreign, deleted)
    assert set(_proposed(repo, baseline)) == {"bh-100"}


def test_an_empty_baseline_is_refused(repo: Path) -> None:
    _commit(repo, "feat: bh-100")
    with pytest.raises(CommitsSourceError, match="baseline holds no beads"):
        GitCommitsStrategy().extract(_request(repo, (), rev="main"))


# -- events --------------------------------------------------------------------------------


def test_event_shape_time_provenance_and_evidence(repo: Path) -> None:
    c1 = _commit(repo, "feat: first bh-100", date="2026-10-01T10:00:00+02:00")
    c2 = _commit(repo, "feat: second bh-100", date="2026-10-02T09:30:00+00:00")
    (event,) = GitCommitsStrategy().extract(_request(repo, _baseline(), rev="main"))
    assert event.kind is EventKind.SET
    assert event.bead == BeadRef(GROUP, "bh-100")
    assert event.data == {"field": FIELD, "value": json.dumps([c1, c2])}
    assert str(event.provenance) == "derived"
    assert event.occurred_at == "2026-10-02T09:30:00Z"  # the newest appended commit's date
    assert event.recorded_at is None
    assert event.extracted_at == EXTRACTED
    assert (event.source.system, event.source.ref) == ("git", f"commit/{c2}")
    assert [(e.type, e.ref) for e in event.source.evidence] == [("commit", c1), ("commit", c2)]
    beadlog.BeadEvent.from_json(event.to_json())  # a valid event


def test_extracting_twice_yields_the_same_ids(repo: Path) -> None:
    _commit(repo, "feat: bh-100")
    _commit(repo, "feat: bh-200")
    first = GitCommitsStrategy().extract(_request(repo, _baseline(), rev="main"))
    second = GitCommitsStrategy().extract(_request(repo, _baseline(), rev="main"))
    assert [e.id for e in first] == [e.id for e in second] != []


def test_a_commit_older_than_the_bead_still_wins_the_fold(repo: Path) -> None:
    c1 = _commit(repo, "feat: bh-100", date="2026-08-01T00:00:00+00:00")
    baseline = _baseline(["bh-100"], existing={"bh-100": json.dumps(["f" * 40])})
    events = GitCommitsStrategy().extract(_request(repo, baseline, rev="main"))
    assert _linkage([*baseline, *events]) == {"bh-100": ["f" * 40, c1]}


# -- idempotence and append-only -----------------------------------------------------------


def test_rerunning_after_apply_proposes_zero_events(repo: Path) -> None:
    _commit(repo, "feat(a): start (bh-100)")
    _commit(repo, "feat(b): more (bh-200)")
    _commit(repo, "chore(merge): batch bh-100, bh-200")
    baseline = _baseline()
    first = GitCommitsStrategy().extract(_request(repo, baseline, rev="main"))
    assert len(first) == 2
    strategy = GitCommitsStrategy()
    assert strategy.extract(_request(repo, [*baseline, *first], rev="main")) == ()
    report = strategy.report()
    assert report is not None and report["proposed"] == 0 and report["present"] == 2


def test_new_history_appends_only_the_new_sha(repo: Path) -> None:
    c1 = _commit(repo, "feat(a): start (bh-100)")
    baseline = _baseline()
    first = GitCommitsStrategy().extract(_request(repo, baseline, rev="main"))
    c2 = _commit(repo, "feat(a): follow-up (bh-100)")
    applied = [*baseline, *first]
    second = GitCommitsStrategy().extract(_request(repo, applied, rev="main"))
    assert [json.loads(e.data["value"]) for e in second] == [[c1, c2]]
    assert [(x.type, x.ref) for x in second[0].source.evidence] == [("commit", c2)]
    assert _linkage([*applied, *second]) == {"bh-100": [c1, c2]}


def test_existing_order_is_preserved_and_only_appended_to(repo: Path) -> None:
    c1 = _commit(repo, "feat: first (bh-100)")
    c2 = _commit(repo, "feat: second (bh-100)")
    live = "d" * 40  # a branch sha the live writer recorded; not in this history
    existing = json.dumps([c2, live])  # deliberately not oldest-first
    baseline = _baseline(existing={"bh-100": existing})
    events = GitCommitsStrategy().extract(_request(repo, baseline, rev="main"))
    assert [json.loads(e.data["value"]) for e in events] == [[c2, live, c1]]
    assert _linkage([*baseline, *events]) == {"bh-100": [c2, live, c1]}
    # and a fully recorded bead proposes nothing, however it is ordered
    done = _baseline(existing={"bh-100": json.dumps([c2, c1])})
    assert GitCommitsStrategy().extract(_request(repo, done, rev="main")) == ()


# -- drift ---------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["not json", '{"a": 1}', '["a", 2]', ""])
def test_a_malformed_existing_value_is_drift_and_never_overwritten(repo: Path, bad: str) -> None:
    c1 = _commit(repo, "feat: bh-100 and bh-200")
    baseline = _baseline(existing={"bh-100": bad})
    strategy = GitCommitsStrategy()
    events = strategy.extract(_request(repo, baseline, rev="main"))
    assert [e.bead.id for e in events] == ["bh-200"]  # bh-100 is left alone
    report = strategy.report()
    assert report is not None
    (drift,) = report["drift"]
    assert drift["bead"] == "bh-100"
    assert drift["proposed"] == [c1]
    assert drift["existing"] == bad
    assert _linkage([*baseline, *events]).get("bh-100", bad) == bad


def test_drift_is_reported_in_the_archive_and_on_the_command_line(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, "feat: bh-100")
    baseline_path = tmp_path / "baseline.bdlg"
    beadlog.write_archive(
        baseline_path,
        _baseline(existing={"bh-100": "garbage"}),
        groups=[beadlog.Group(GROUP, "bh")],
        generator=beadlog.Generator("test", "0"),
    )
    out = tmp_path / "proposed.bdlg"
    code = main(
        [
            "extract",
            "git-commits",
            "--root",
            str(repo),
            "--group",
            GROUP,
            "--group-dir",
            "bh",
            "--extracted-at",
            EXTRACTED,
            "--baseline",
            str(baseline_path),
            "--option",
            "rev=main",
            "-o",
            str(out),
        ],
        entry_points=lambda: [_Point()],
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "DRIFT bh-100" in printed and "not overwritten" in printed
    with beadlog.ArchiveReader(out) as reader:
        annotation = reader.manifest.annotations["bdlg.git-commits"]
    assert annotation["drift"][0]["bead"] == "bh-100"
    assert annotation["proposed"] == 0


def test_reconcile_classes_an_append_as_drift_then_proposes_nothing(repo: Path) -> None:
    _commit(repo, "feat: bh-100")
    baseline = _baseline()
    proposed = GitCommitsStrategy().extract(_request(repo, baseline, rev="main"))
    plan = reconcile(proposed, baseline)
    assert [(str(e.bead.id), str(e.cls)) for e in plan.entries] == [("bh-100", "DRIFT")]
    assert plan.entries[0].changed == (f"fields.{FIELD}",)
    assert len(plan.events) == 1
    # applied: the same proposal against the new baseline changes nothing
    after = reconcile(proposed, [*baseline, *proposed])
    assert after.entries == ()
    assert after.events == ()


# -- git options ---------------------------------------------------------------------------


def test_rev_pins_the_walk(repo: Path) -> None:
    c1 = _commit(repo, "feat: bh-100")
    _git(repo, "branch", "pin")
    _commit(repo, "feat: bh-200")
    pinned = GitCommitsStrategy().extract(_request(repo, _baseline(), rev="pin"))
    assert [json.loads(e.data["value"]) for e in pinned] == [[c1]]


def test_since_a_commit_walks_only_the_commits_after_it(repo: Path) -> None:
    c1 = _commit(repo, "feat: bh-100")
    c2 = _commit(repo, "feat: bh-100 again")
    assert _proposed(repo, _baseline(), since=c1) == {"bh-100": [c2]}


def test_since_a_date_walks_only_the_commits_after_it(repo: Path) -> None:
    _commit(repo, "feat: bh-100", date="2026-01-01T00:00:00+00:00")
    c2 = _commit(repo, "feat: bh-100 again", date="2026-06-01T00:00:00+00:00")
    assert _proposed(repo, _baseline(), since="2026-03-01") == {"bh-100": [c2]}


def test_read_commits_is_newest_first_with_subject_and_body(repo: Path) -> None:
    c1 = _commit(repo, "s1", body="b1")
    c2 = _commit(repo, "s2")
    commits = read_commits(repo, "main")
    assert [c.sha for c in commits] == [c2, c1]
    assert commits[1].message.startswith("s1\n") and "b1" in commits[1].message
    assert commits[1].parents == 0 and commits[0].parents == 1
    assert read_commits(repo, "main", limit=1)[0].sha == c2


def test_git_failures_are_a_source_error(repo: Path) -> None:
    with pytest.raises(CommitsSourceError, match="failed"):
        read_commits(repo, "no-such-rev")
    with pytest.raises(CommitsSourceError, match="cannot run"):
        read_commits(repo, "main", git="/nonexistent/git")


# -- shallow clones ------------------------------------------------------------------------


def _shallow_clone(repo: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(dest)],
        check=True,
        capture_output=True,
    )
    return dest


def test_a_shallow_clone_is_refused_with_remediation(repo: Path, tmp_path: Path) -> None:
    _commit(repo, "feat: bh-100")
    _commit(repo, "feat: bh-100 again")
    shallow = _shallow_clone(repo, tmp_path)
    assert is_shallow(shallow) and not is_shallow(repo)
    with pytest.raises(ShallowCloneError, match=r"git fetch --unshallow"):
        GitCommitsStrategy().extract(_request(shallow, _baseline()))


def test_allow_shallow_accepts_the_truncated_history(repo: Path, tmp_path: Path) -> None:
    _commit(repo, "feat: bh-100")
    c2 = _commit(repo, "feat: bh-100 again")
    shallow = _shallow_clone(repo, tmp_path)
    events = GitCommitsStrategy().extract(_request(shallow, _baseline(), allow_shallow="true"))
    assert [json.loads(e.data["value"]) for e in events] == [[c2]]


def test_the_cli_reports_the_shallow_refusal(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, "feat: bh-100")
    shallow = _shallow_clone(repo, tmp_path)
    code = main(
        [
            "extract",
            "git-commits",
            "--root",
            str(shallow),
            "--group",
            GROUP,
            "--group-dir",
            "bh",
            "--extracted-at",
            EXTRACTED,
            "-o",
            str(tmp_path / "o.bdlg"),
        ],
        entry_points=lambda: [_Point()],
    )
    assert code == 1
    assert "git fetch --unshallow" in capsys.readouterr().err


# -- registration --------------------------------------------------------------------------


class _Point:
    name = "git-commits"

    def load(self) -> Any:
        return GitCommitsStrategy


def test_it_is_a_beadlog_extract_subcommand() -> None:
    assert "git-commits" in discover_strategies(lambda: [_Point()])


def test_entry_point_is_declared() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():  # the Pants sandbox ships sources only
        pytest.skip("package metadata not in this sandbox")
    text = pyproject.read_text(encoding="utf-8")
    assert 'git-commits = "beadlog_sources.commits:GitCommitsStrategy"' in text
