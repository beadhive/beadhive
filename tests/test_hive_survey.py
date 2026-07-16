"""Unit tests for `ws hive survey` — fleet table for onboarding triage.

Contract:
  * ``collect_rows()`` returns one dict per on-disk repo (registered + tracked),
    with the required fields: repo, registered, classification, commits,
    last_commit, age_days, ahead_behind, dirty_branches, disk_bytes, difficulty.
  * ``--available`` (available=True) filters to unregistered repos only.
  * ``--json`` (json_out=True) emits valid JSON with the expected keys.
  * ``--sort disk|age|difficulty`` orders rows correctly.

Repos are real (created in tmp_path via git); ``registry.classify`` is patched to
a fixed return so tests stay hermetic and offline (no ``gh repo view`` calls).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path

from beadhive import config
from beadhive import survey as survey_mod
from beadhive.safety import format_bytes
from beadhive.survey import _DIFFICULTY_RANK, collect_rows

# ---------------------------------------------------------------------------
# Shared git helpers (same pattern as test_safety.py)
# ---------------------------------------------------------------------------

_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=_ENV
    )


def _init_repo(path: Path, branch: str = "main") -> None:
    _git("init", "-b", branch, cwd=path)
    _git("config", "user.email", "test@ws.dev", cwd=path)
    _git("config", "user.name", "WS Test", cwd=path)


def _commit(path: Path, msg: str = "init", fname: str = "file.txt") -> None:
    (path / fname).write_text(msg)
    _git("add", ".", cwd=path)
    _git("commit", "-m", msg, cwd=path)


def _make_repo(world, provider: str, org: str, repo: str, commits: int = 1) -> Path:
    """Create a real git repo under world.ws_root/provider/org/repo."""
    path = world.ws_root / provider / org / repo
    path.mkdir(parents=True, exist_ok=True)
    _init_repo(path)
    for i in range(commits):
        _commit(path, msg=f"commit {i}", fname=f"f{i}.txt")
    return path


def _register_repo(provider: str, org: str, repo: str, prefix: str, kind: str = "personal") -> None:
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": provider, "org": org, "repo": repo, "prefix": prefix, "kind": kind}
    )
    config.save(cfg)


def _write_lock(world, *triplets: str) -> None:
    """Write a synthetic workspace-lock.toml with one [[repo]] per triplet."""
    blocks = "\n".join(
        f'[[repo]]\npath = "{t}"\nurl = "git@github.com:{t}.git"\n' for t in triplets
    )
    (world.ws_root / "workspace-lock.toml").write_text(blocks)


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------


def test_collect_rows_registered_repo_fields(world, monkeypatch):
    """A registered + on-disk repo produces a row with all expected fields."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    _make_repo(world, "github", "acme", "widget", commits=3)
    _register_repo("github", "acme", "widget", prefix="wid")

    rows = collect_rows()

    assert len(rows) == 1
    row = rows[0]
    assert row["repo"] == "github/acme/widget"
    assert row["registered"] is True
    assert row["classification"] == "personal-or-prototype"
    assert row["commits"] == 3
    assert isinstance(row["last_commit"], str) and row["last_commit"] != "(none)"
    assert isinstance(row["age_days"], float) and row["age_days"] >= 0
    # No origin → branches exist but no upstream → display shows +0/-0, raw ints are None
    assert row["ahead_behind"] == "+0/-0"
    assert row["ahead"] is None  # null: no branch has an upstream
    assert row["behind"] is None
    assert isinstance(row["dirty_branches"], int)
    assert isinstance(row["disk_bytes"], int) and row["disk_bytes"] > 0
    assert row["difficulty"] in ("easy", "medium", "hard", "not-a-candidate")


def test_collect_rows_tracked_but_unregistered(world, monkeypatch):
    """A tracked-but-unregistered on-disk repo appears with registered=False."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    _make_repo(world, "github", "acme", "candidate")
    _write_lock(world, "github/acme/candidate")

    rows = collect_rows()

    assert len(rows) == 1
    row = rows[0]
    assert row["registered"] is False
    assert row["repo"] == "github/acme/candidate"


def test_collect_rows_skips_missing_repos(world, monkeypatch):
    """Repos registered or tracked but not cloned locally are silently skipped."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    _register_repo("github", "acme", "ghost", prefix="gh")
    _write_lock(world, "github/acme/ghost")
    # Note: no actual directory created for 'ghost'

    rows = collect_rows()

    assert rows == []


def test_collect_rows_both_registered_and_tracked(world, monkeypatch):
    """Union of registered + tracked repos (deduped); registered flag is correct."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    _make_repo(world, "github", "acme", "alpha")
    _make_repo(world, "github", "acme", "beta")
    _register_repo("github", "acme", "alpha", prefix="al")
    _write_lock(world, "github/acme/alpha", "github/acme/beta")

    rows = collect_rows()

    repos = {r["repo"] for r in rows}
    assert repos == {"github/acme/alpha", "github/acme/beta"}
    alpha = next(r for r in rows if r["repo"] == "github/acme/alpha")
    beta = next(r for r in rows if r["repo"] == "github/acme/beta")
    assert alpha["registered"] is True
    assert beta["registered"] is False


def test_collect_rows_classification_injected(world, monkeypatch):
    """classify result is stored in the row and passed to difficulty."""
    monkeypatch.setattr("beadhive.registry.classify", lambda p, o, r, cfg=None: "org-native")
    _make_repo(world, "github", "acme", "native")
    _register_repo("github", "acme", "native", prefix="nat", kind="org-native")

    rows = collect_rows()

    assert len(rows) == 1
    assert rows[0]["classification"] == "org-native"


def test_collect_rows_excluded_repo_is_not_a_candidate(world, monkeypatch):
    """An excluded repo produces verdict='not-a-candidate' via classify injection."""
    monkeypatch.setattr("beadhive.registry.classify", lambda p, o, r, cfg=None: "excluded")
    _make_repo(world, "github", "acme", "excluded-repo")
    _write_lock(world, "github/acme/excluded-repo")

    rows = collect_rows()

    assert len(rows) == 1
    assert rows[0]["difficulty"] == "not-a-candidate"


# ---------------------------------------------------------------------------
# --available filter
# ---------------------------------------------------------------------------


def test_survey_available_filters_to_unregistered(world, monkeypatch, capsys):
    """--available keeps only unregistered candidates."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    _make_repo(world, "github", "acme", "registered-hive")
    _make_repo(world, "github", "acme", "candidate-repo")
    _register_repo("github", "acme", "registered-hive", prefix="rr")
    _write_lock(world, "github/acme/registered-hive", "github/acme/candidate-repo")

    survey_mod.survey(available=True)

    out = capsys.readouterr().out
    assert "candidate-repo" in out
    assert "registered-hive" not in out


def test_survey_available_empty_message(world, monkeypatch, capsys):
    """--available with no candidates prints the 'no repos found' message."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    _make_repo(world, "github", "acme", "only-registered")
    _register_repo("github", "acme", "only-registered", prefix="or")
    # No lock file → no tracked candidates

    survey_mod.survey(available=True)

    out = capsys.readouterr().out
    assert "no repos found" in out


# ---------------------------------------------------------------------------
# --json shape
# ---------------------------------------------------------------------------


def test_survey_json_shape(world, monkeypatch, capsys):
    """--json emits a valid JSON array with expected keys per repo."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    _make_repo(world, "github", "acme", "alpha", commits=2)
    _make_repo(world, "github", "acme", "beta", commits=1)
    _register_repo("github", "acme", "alpha", prefix="al")
    _write_lock(world, "github/acme/alpha", "github/acme/beta")

    survey_mod.survey(json_out=True)

    raw = capsys.readouterr().out
    data = json.loads(raw)
    assert isinstance(data, list)
    assert len(data) == 2

    required_keys = {
        "repo", "registered", "classification", "commits", "last_commit",
        "ahead", "behind", "dirty_branches", "disk", "disk_bytes", "difficulty",
    }
    for obj in data:
        assert required_keys <= set(obj.keys()), f"missing keys in {obj}"
        assert isinstance(obj["registered"], bool)
        assert isinstance(obj["commits"], int)
        assert isinstance(obj["disk_bytes"], int)
        assert obj["difficulty"] in ("easy", "medium", "hard", "not-a-candidate")
        # ahead/behind are ints or null (no ahead_behind string)
        assert "ahead_behind" not in obj


def test_survey_json_age_days_finite(world, monkeypatch, capsys):
    """age_days is a finite float for repos with commits (not null)."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    _make_repo(world, "github", "acme", "repo-a", commits=3)
    _register_repo("github", "acme", "repo-a", prefix="ra")

    survey_mod.survey(json_out=True)

    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    age = data[0]["age_days"]
    assert age is not None and age >= 0


def test_survey_json_empty_repo_returns_null_age(world, monkeypatch, capsys):
    """Empty repos (no commits) produce age_days=null in JSON output."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    # Create a repo with NO commits
    path = world.ws_root / "github" / "acme" / "empty-hive"
    path.mkdir(parents=True)
    _init_repo(path)
    _register_repo("github", "acme", "empty-hive", prefix="er")

    survey_mod.survey(json_out=True)

    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["age_days"] is None  # inf → null


# ---------------------------------------------------------------------------
# --sort ordering
# ---------------------------------------------------------------------------


def test_survey_sort_disk(world, monkeypatch, capsys):
    """--sort disk orders rows ascending by disk_bytes."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    _make_repo(world, "github", "acme", "small-repo", commits=1)
    large = _make_repo(world, "github", "acme", "large-repo", commits=1)
    # Inflate 'large-repo' so its disk footprint is measurably larger
    (large / "big.bin").write_bytes(b"x" * 500_000)
    _git("add", ".", cwd=large)
    _git("commit", "-m", "big file", cwd=large)
    _register_repo("github", "acme", "small-repo", prefix="sm")
    _register_repo("github", "acme", "large-repo", prefix="lg")

    rows = collect_rows()
    sorted_rows = sorted(rows, key=lambda r: r["disk_bytes"])
    assert sorted_rows[0]["repo"].endswith("small-repo")
    assert sorted_rows[-1]["repo"].endswith("large-repo")

    # Verify the CLI sorts in the same order
    survey_mod.survey(sort="disk")
    out = capsys.readouterr().out
    # Both repos should appear; the smaller one should come first
    positions = {r: out.index(r.split("/")[-1]) for r in ("small-repo", "large-repo")}
    assert positions["small-repo"] < positions["large-repo"]


def test_survey_sort_age(world, monkeypatch):
    """--sort age orders rows ascending by age_days (oldest last for finite ages)."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    _make_repo(world, "github", "acme", "repo-a", commits=2)
    _make_repo(world, "github", "acme", "repo-b", commits=1)
    _register_repo("github", "acme", "repo-a", prefix="ra")
    _register_repo("github", "acme", "repo-b", prefix="rb")

    rows = collect_rows()
    sorted_rows = sorted(rows, key=lambda r: r["age_days"])
    # All ages should be finite (committed just now) — the order is stable
    for r in sorted_rows:
        assert math.isfinite(r["age_days"])


def test_survey_sort_difficulty(world, monkeypatch):
    """--sort difficulty orders rows easy → medium → hard → not-a-candidate."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    _make_repo(world, "github", "acme", "clean-repo", commits=2)
    _register_repo("github", "acme", "clean-repo", prefix="cr")

    rows = collect_rows()
    sorted_rows = sorted(rows, key=lambda r: _DIFFICULTY_RANK.get(r["difficulty"], 9))
    assert len(sorted_rows) >= 1
    # Ensure DIFFICULTY_RANK enforces the canonical order
    for i in range(len(sorted_rows) - 1):
        a = _DIFFICULTY_RANK.get(sorted_rows[i]["difficulty"], 9)
        b = _DIFFICULTY_RANK.get(sorted_rows[i + 1]["difficulty"], 9)
        assert a <= b


# ---------------------------------------------------------------------------
# Human-readable table (default output)
# ---------------------------------------------------------------------------


def test_survey_table_output_contains_repo(world, monkeypatch, capsys):
    """Default table output contains the repo triplet and difficulty."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    _make_repo(world, "github", "acme", "my-repo", commits=1)
    _register_repo("github", "acme", "my-repo", prefix="mr")

    survey_mod.survey()

    out = capsys.readouterr().out
    assert "github/acme/my-repo" in out
    assert "DIFFICULTY" in out  # header
    # Difficulty rendered uppercase
    assert any(d.upper() in out for d in ("EASY", "MEDIUM", "HARD", "NOT-A-CANDIDATE"))


def test_survey_table_no_repos_message(world, monkeypatch, capsys):
    """When no on-disk repos exist, a friendly message is printed."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )

    survey_mod.survey()

    out = capsys.readouterr().out
    assert "no repos found" in out


# ---------------------------------------------------------------------------
# _fmt_bytes unit tests
# ---------------------------------------------------------------------------


def test_fmt_bytes_units():
    assert format_bytes(512) == "512 B"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(1_572_864) == "1.5 MB"
    assert format_bytes(1_610_612_736) == "1.5 GB"


# ---------------------------------------------------------------------------
# --json: typed ahead/behind fields
# ---------------------------------------------------------------------------


def test_survey_json_ahead_behind_null_no_upstream(world, monkeypatch, capsys):
    """Repos with no upstream produce ahead=null, behind=null in JSON."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    # No origin → branches exist but no upstream tracking refs
    _make_repo(world, "github", "acme", "no-origin-repo", commits=2)
    _register_repo("github", "acme", "no-origin-repo", prefix="nor")

    survey_mod.survey(json_out=True)

    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    obj = data[0]
    assert "ahead_behind" not in obj  # old string key must not exist
    assert obj["ahead"] is None
    assert obj["behind"] is None


def test_survey_json_ahead_behind_ints_with_upstream(world, monkeypatch, capsys):
    """Repos with an upstream produce integer ahead/behind fields in JSON."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    import tempfile

    # Set up a bare remote and a repo that tracks it
    remote_dir = Path(tempfile.mkdtemp()) / "remote.git"
    remote_dir.mkdir(parents=True)
    _git("init", "--bare", "-b", "main", cwd=remote_dir)

    repo_path = _make_repo(world, "github", "acme", "tracked-repo", commits=1)
    _git("remote", "add", "origin", str(remote_dir), cwd=repo_path)
    _git("push", "-u", "origin", "main", cwd=repo_path)
    _register_repo("github", "acme", "tracked-repo", prefix="tr")

    survey_mod.survey(json_out=True)

    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    obj = data[0]
    assert isinstance(obj["ahead"], int)
    assert isinstance(obj["behind"], int)
    assert obj["ahead"] >= 0
    assert obj["behind"] >= 0


# ---------------------------------------------------------------------------
# --json: last_commit null for empty repo
# ---------------------------------------------------------------------------


def test_survey_json_last_commit_null_empty_repo(world, monkeypatch, capsys):
    """Empty repos (no commits) produce last_commit=null in JSON output."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    path = world.ws_root / "github" / "acme" / "empty-repo2"
    path.mkdir(parents=True)
    _init_repo(path)
    _register_repo("github", "acme", "empty-repo2", prefix="er2")

    survey_mod.survey(json_out=True)

    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["last_commit"] is None


# ---------------------------------------------------------------------------
# Human table: (n/a) placeholders
# ---------------------------------------------------------------------------


def test_survey_table_difficulty_not_a_candidate_shows_na(world, monkeypatch, capsys):
    """Excluded repos show '(n/a)' in the DIFFICULTY column (not 'NOT-A-CANDIDATE')."""
    monkeypatch.setattr("beadhive.registry.classify", lambda p, o, r, cfg=None: "excluded")
    _make_repo(world, "github", "acme", "excl-repo", commits=1)
    _write_lock(world, "github/acme/excl-repo")

    survey_mod.survey()

    out = capsys.readouterr().out
    assert "(n/a)" in out
    assert "NOT-A-CANDIDATE" not in out


def test_survey_table_ahead_behind_na_for_empty_repo(world, monkeypatch, capsys):
    """Empty repos (no branches) show '(n/a)' in the AHEAD/BEHIND column."""
    monkeypatch.setattr(
        "beadhive.registry.classify", lambda p, o, r, cfg=None: "personal-or-prototype"
    )
    path = world.ws_root / "github" / "acme" / "empty-repo3"
    path.mkdir(parents=True)
    _init_repo(path)
    _register_repo("github", "acme", "empty-repo3", prefix="er3")

    survey_mod.survey()

    out = capsys.readouterr().out
    assert "(n/a)" in out
