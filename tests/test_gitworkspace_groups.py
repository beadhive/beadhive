"""RepoGroup model — `[[provider]]` blocks parsed as first-class repo groups (bh-4y0r.1).

`providers()` / `orgs()` / `provider_host()` are thin views over `groups()`; their existing
byte-for-byte outputs (see test_ws.py) must not change.
"""

from __future__ import annotations

from beadhive import gitworkspace


def _write(tmp_path, monkeypatch, toml_text):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text(toml_text)


def test_groups_multi_group_same_provider(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        '[[provider]]\nprovider = "github"\nname = "agentguides"\npath = "github"\n\n'
        '[[provider]]\nprovider = "github"\nname = "octo-org"\npath = "github"\n\n'
        '[[provider]]\nprovider = "github"\nname = "briancripe"\npath = "contrib"\n',
    )
    groups = gitworkspace.groups({})
    assert len(groups) == 3
    assert {g.provider_type for g in groups} == {"github"}
    assert {g.path for g in groups} == {"github", "contrib"}
    assert {g.account for g in groups} == {"agentguides", "octo-org", "briancripe"}


def test_groups_path_ne_provider_shape(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        '[[provider]]\nprovider = "github"\nname = "briancripe"\npath = "contrib"\n'
        'skip_forks = true\ninclude = ["foo"]\nexclude = ["bar"]\n',
    )
    [g] = gitworkspace.groups({})
    assert g == gitworkspace.RepoGroup(
        provider_type="github",
        account="briancripe",
        path="contrib",
        skip_forks=True,
        include=("foo",),
        exclude=("bar",),
    )


def test_groups_defaults_path_to_provider_type(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, '[[provider]]\nprovider = "gitlab"\nname = "acme"\n')
    [g] = gitworkspace.groups({})
    assert g.path == "gitlab"
    assert g.skip_forks is False
    assert g.include == ()
    assert g.exclude == ()


def test_groups_skips_entry_with_no_path_and_no_provider(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, '[[provider]]\nname = "orphan"\n')
    assert gitworkspace.groups({}) == []


def test_providers_orgs_provider_host_are_thin_views_over_groups(tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        '[[provider]]\nprovider = "github"\nname = "agentguides"\npath = "github"\n\n'
        '[[provider]]\nprovider = "github"\nname = "briancripe"\npath = "contrib"\n',
    )
    cfg = {}
    assert gitworkspace.providers(cfg) == {"github", "contrib"}
    assert gitworkspace.orgs(cfg) == {"agentguides", "briancripe"}
    assert gitworkspace.provider_host(cfg, "contrib") == "github"
    assert gitworkspace.provider_host(cfg, "github") == "github"
    assert gitworkspace.provider_host(cfg, "unknown") == ""


def test_repo_group_is_frozen():
    g = gitworkspace.RepoGroup(provider_type="github", account="a", path="p")
    try:
        g.path = "other"
    except Exception:
        return
    raise AssertionError("RepoGroup should be frozen (immutable)")
