"""bh-4y0r.2: orca.discover_repos keeps its fixed three-level (<group>/<org>/<repo>) walk as the
contract — deeper multi-owner nesting (which the lockfile readers already tolerate via
first/last-segment keying) is surfaced instead as a `bh doctor` warning."""

from __future__ import annotations

from beadhive import doctor, gitworkspace


def _lock(tmp_path, monkeypatch, text):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace-lock.toml").write_text(text)


def test_deep_nested_paths_flags_paths_over_three_segments(tmp_path, monkeypatch):
    _lock(
        tmp_path,
        monkeypatch,
        '[[repo]]\npath = "github/acme/api"\n\n'
        '[[repo]]\npath = "gitlab/group/subgroup/repo"\n',
    )
    assert gitworkspace.deep_nested_paths({}) == ["gitlab/group/subgroup/repo"]


def test_deep_nested_paths_empty_when_no_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    assert gitworkspace.deep_nested_paths({}) == []


def test_discover_repos_does_not_find_a_deeper_nested_clone(tmp_path, monkeypatch):
    """DECISION (bh-4y0r.2): the three-level walk is kept as-is — a clone nested one level
    deeper than <group>/<org>/<repo> is simply not discovered."""
    from beadhive import orca

    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    deep = tmp_path / "gitlab" / "group" / "subgroup" / "repo"
    (deep / ".git").mkdir(parents=True)
    assert orca.discover_repos({}) == []


def test_doctor_warns_on_deep_nested_lock_path(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace-lock.toml").write_text(
        '[[repo]]\npath = "gitlab/group/subgroup/repo"\n'
    )
    cfg = {"git_workspace": {"enabled": True}}
    warns = doctor._data_warnings(cfg, tmp_path, [], True, set(), set(), [], set())
    assert any("gitlab/group/subgroup/repo" in w for w in warns)


def test_doctor_no_warning_when_git_workspace_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace-lock.toml").write_text(
        '[[repo]]\npath = "gitlab/group/subgroup/repo"\n'
    )
    cfg = {"git_workspace": {"enabled": False}}
    warns = doctor._data_warnings(cfg, tmp_path, [], False, set(), set(), [], set())
    assert not any("subgroup" in w for w in warns)
