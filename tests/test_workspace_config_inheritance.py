"""HQ's workspace.toml is cloned AND wired — bh-9bkj (bh's reader) + bh-28ha (the child's file).

MEASURED on beadhive-factory, 2026-08-05: `bh hq clone` succeeded and left
`/home/bees/.beadhive/hq/workspace.toml` on disk — 1470 bytes, all seven providers — and
provisioning still reported

    3. • git workspace update — no workspace*.toml under /home/bees/workspace
         (or git_workspace.path) — place one

`host_answers.py` promises "a host that clones HQ inherits all of it". The host inherited the
FILE and never USED it. Two distinct gaps, one per bead, and both are needed:

* bh-9bkj — bh's own RESOLVER never looked at HQ's copy. Fixed by a documented three-layer
  precedence in `gitworkspace.config_paths`, no new config key.
* bh-28ha — the `git-workspace` BINARY takes only `--workspace <dir>` and reads
  `workspace*.toml` from inside it (`git-workspace --help`, 1.10.1). There is no config-path
  flag, so resolving well is not enough: provisioning links the resolved config into the
  workspace root. Fixing only bh-9bkj moves the failure from "skipped, place one" to
  "git-workspace found no config" — still step 3, still stuck.
"""

from __future__ import annotations

from pathlib import Path

from beadhive import config, gitworkspace, host_provision

PROVIDERS = '[[provider]]\nprovider = "github"\nname = "beadhive"\npath = "github"\n'
LOCAL_PROVIDERS = '[[provider]]\nprovider = "github"\nname = "hand-maintained"\npath = "github"\n'


class _Res:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _hq_carries_providers(text: str = PROVIDERS) -> Path:
    hq = config.hq_dir()
    hq.mkdir(parents=True, exist_ok=True)
    path = hq / "workspace.toml"
    path.write_text(text)
    return path


# ---- bh-9bkj: the resolver's three layers ---------------------------------------------


def test_hq_copy_resolves_when_the_workspace_root_has_none(world):
    """THE beadhive-factory state: HQ cloned, $GIT_WORKSPACE empty. No symlink, no copy, no
    hand-placed file — bh reads the providers it already fetched."""
    hq_copy = _hq_carries_providers()

    assert gitworkspace.config_paths({}) == [hq_copy]
    assert gitworkspace.orgs({}) == {"beadhive"}


def test_an_externally_managed_host_is_unchanged(world):
    """No HQ at all: exactly today's behaviour, verified rather than assumed."""
    (world.ws_root / "workspace.toml").write_text(LOCAL_PROVIDERS)

    assert gitworkspace.config_paths({}) == [world.ws_root / "workspace.toml"]
    assert gitworkspace.orgs({}) == {"hand-maintained"}


def test_the_hosts_own_copy_wins_over_hqs(world):
    """PRECEDENCE, defined rather than filesystem-dependent — and never a union of the two.
    A file under $GIT_WORKSPACE was written on purpose; HQ's arrives by clone."""
    _hq_carries_providers()
    (world.ws_root / "workspace.toml").write_text(LOCAL_PROVIDERS)

    assert gitworkspace.config_paths({}) == [world.ws_root / "workspace.toml"]
    assert gitworkspace.orgs({}) == {"hand-maintained"}


def test_an_explicit_path_still_wins_over_both(world, tmp_path):
    """The escape hatch stays one."""
    _hq_carries_providers()
    (world.ws_root / "workspace.toml").write_text(LOCAL_PROVIDERS)
    explicit = tmp_path / "elsewhere.toml"
    explicit.write_text('[[provider]]\nprovider = "github"\nname = "explicit"\npath = "github"\n')

    cfg = {"git_workspace": {"path": str(explicit)}}

    assert gitworkspace.config_paths(cfg) == [explicit]
    assert gitworkspace.orgs(cfg) == {"explicit"}


def test_no_hq_and_no_local_config_resolves_to_nothing(world):
    assert gitworkspace.config_paths({}) == []


# ---- bh-28ha: the file the CHILD reads --------------------------------------------------


def test_provision_links_hqs_copy_into_the_workspace_root(world, monkeypatch):
    """The beadhive-factory sequence end to end: HQ cloned, $GIT_WORKSPACE carrying no
    workspace.toml and no symlink, provision — and the child resolves providers."""
    hq_copy = _hq_carries_providers()
    monkeypatch.setattr(host_provision, "run", lambda *a, **k: _Res(0))

    result = host_provision._step_git_workspace_update(dry_run=False)

    link = world.ws_root / "workspace.toml"
    assert result.status == "done", result.detail
    assert link.is_symlink()
    assert link.resolve() == hq_copy.resolve()
    assert link.read_text() == PROVIDERS  # HQ stays the single source of truth


def test_the_link_is_idempotent_across_runs(world, monkeypatch):
    _hq_carries_providers()
    monkeypatch.setattr(host_provision, "run", lambda *a, **k: _Res(0))

    host_provision._step_git_workspace_update(dry_run=False)
    second = host_provision._step_git_workspace_update(dry_run=False)

    assert second.status == "done", second.detail
    assert (world.ws_root / "workspace.toml").is_symlink()


def test_an_existing_local_config_is_never_shadowed(world, monkeypatch):
    """An externally-managed host keeps its own file — bh does not link over it."""
    _hq_carries_providers()
    own = world.ws_root / "workspace.toml"
    own.write_text(LOCAL_PROVIDERS)
    monkeypatch.setattr(host_provision, "run", lambda *a, **k: _Res(0))

    host_provision._step_git_workspace_update(dry_run=False)

    assert not own.is_symlink()
    assert own.read_text() == LOCAL_PROVIDERS


def test_dry_run_places_no_link(world, monkeypatch):
    _hq_carries_providers()
    monkeypatch.setattr(
        host_provision, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("mutated"))
    )

    result = host_provision._step_git_workspace_update(dry_run=True)

    assert result.status == "would"
    assert not (world.ws_root / "workspace.toml").exists()


def test_the_skip_message_names_hq_as_a_place_to_look(world):
    result = host_provision._step_git_workspace_update(dry_run=False)

    assert result.status == "skipped"
    assert str(config.hq_dir()) in result.detail


def test_git_workspace_update_runs_after_hq_clone(world):
    """Ordering is the fix's other half: a host cannot clone the fleet's repos before it has
    the fleet's list of them."""
    plan = list(host_provision.PLAN)

    assert plan.index("hq clone") < plan.index("git workspace update")


def test_the_lockfile_lands_beside_the_link_where_bh_reads_it(world):
    """git-workspace writes `workspace-lock.toml` into `--workspace`, which is the workspace
    root — the same directory `gitworkspace.upstreams`/`repo_urls`/`tracked_repos` read it
    from, and a different filename from the link, so it never overwrites it."""
    _hq_carries_providers()
    (world.ws_root / "workspace-lock.toml").write_text(
        '[[repo]]\npath = "github/beadhive/beadhive"\n'
        'url = "git@github.com:beadhive/beadhive.git"\n'
    )

    assert gitworkspace.tracked_repos({}) == [("github", "beadhive", "beadhive")]
    assert gitworkspace.config_paths({}) == [config.hq_dir() / "workspace.toml"]
