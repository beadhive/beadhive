"""gitworkspace.is_seeded / ensure_seeded — internal-mode workspace-root provisioning
(bh-cgcg.2).

Pure filesystem functions: no env/config isolation needed beyond a plain `tmp_path` root.
"""

from __future__ import annotations

import tomllib

from beadhive import gitworkspace


def test_is_seeded_false_for_missing_root(tmp_path):
    assert gitworkspace.is_seeded(tmp_path / "nope") is False


def test_is_seeded_false_for_empty_dir(tmp_path):
    assert gitworkspace.is_seeded(tmp_path) is False


def test_is_seeded_false_when_only_lockfile_present(tmp_path):
    """workspace-lock.toml is generated state, not a source config — its presence alone must
    not count as 'seeded'."""
    (tmp_path / "workspace-lock.toml").write_text("")
    assert gitworkspace.is_seeded(tmp_path) is False


def test_is_seeded_true_with_workspace_toml(tmp_path):
    (tmp_path / "workspace.toml").write_text("")
    assert gitworkspace.is_seeded(tmp_path) is True


def test_is_seeded_true_with_split_workspace_config(tmp_path):
    (tmp_path / "workspace-work.toml").write_text("")
    assert gitworkspace.is_seeded(tmp_path) is True


def test_ensure_seeded_creates_missing_root_and_seed_file(tmp_path):
    root = tmp_path / "ws"

    created = gitworkspace.ensure_seeded(root)

    assert created is True
    assert root.is_dir()
    toml = root / "workspace.toml"
    assert toml.is_file()
    assert tomllib.loads(toml.read_text()).get("provider", []) == []  # minimal: empty providers


def test_ensure_seeded_rerun_is_a_genuine_no_op(tmp_path):
    """Idempotency is a hard requirement (bh-cgcg.2): re-running against an already
    provisioned root must not duplicate provider blocks, clobber, or truncate the file."""
    root = tmp_path / "ws"
    gitworkspace.ensure_seeded(root)
    toml = root / "workspace.toml"
    hand_edited = '[[provider]]\nprovider = "github"\nname = "acme"\npath = "github"\n'
    toml.write_text(hand_edited)  # simulate `git workspace add` having run since

    created = gitworkspace.ensure_seeded(root)

    assert created is False
    assert toml.read_text() == hand_edited  # byte-identical — never touched


def test_ensure_seeded_leaves_existing_split_config_untouched(tmp_path):
    """A root already seeded via a workspace-<name>.toml (no bare workspace.toml) must not
    get a second, redundant workspace.toml written on top."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "workspace-personal.toml").write_text("# hand-authored\n")

    created = gitworkspace.ensure_seeded(root)

    assert created is False
    assert not (root / "workspace.toml").exists()
