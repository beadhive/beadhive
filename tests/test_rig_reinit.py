"""`rig init` re-init is non-destructive on an already-configured rig.

Regression: a plain re-init used to re-classify + re-derive the prefix and overwrite the
registered entry — clobbering a working prefix (→ workspace) and invalidating
every bead label rig-wide. These checks pin the contract:

  * plain re-init preserves the registered prefix/kind and only WARNS (no register());
  * `--prefix` is a targeted change — only the prefix moves, unrelated fields are kept —
    and a prefix that differs from the registered one requires `--yes` (:
    changing a rig's prefix orphans every existing bead ID);
  * `--force` re-registers (re-classifies kind) but PRESERVES the registered prefix
    (: it must never silently re-derive it);
  * a fresh (unregistered) rig still inits normally.

These run without real `bd`: a `.beads/` dir is pre-created so `rig init` skips `bd init`, and
classification (a `gh` call) is only reached by the force/fresh paths and is stubbed there.
"""

from __future__ import annotations

from harness.world import git
from ws import config, registry, rig


def _make_repo(world, *, org="myorg", repo="myrepo"):
    """A git repo under $GIT_WORKSPACE with `.beads/` present (so `rig init` skips `bd init`)."""
    main = world.ws_root / "github" / org / repo
    main.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=main)
    (main / ".beads").mkdir()
    world.chdir(main)
    return main


def _register(world, *, org="myorg", repo="myrepo", prefix="mr", kind="personal"):
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": "github", "org": org, "repo": repo, "prefix": prefix, "kind": kind}
    )
    config.save(cfg)


def _entry(org="myorg", repo="myrepo"):
    return registry.find_entry(config.load(), "github", org, repo)


def test_reinit_no_force_preserves_prefix_and_warns(world, capsys):
    _make_repo(world)
    _register(world, prefix="mr", kind="personal")

    rig.init()  # plain re-init — no flags

    out = capsys.readouterr()
    e = _entry()
    assert str(e["prefix"]) == "mr"  # core regression: prefix NOT re-derived/clobbered
    assert str(e["kind"]) == "personal"  # kind untouched
    assert "already configured" in out.err  # warns, listing what exists
    assert "mr" in out.err
    assert "registered" not in out.out  # register() never ran


def test_reinit_prefix_override_changes_only_prefix(world):
    _make_repo(world)
    _register(world, prefix="mr", kind="personal")

    rig.init(prefix="bc-myrepo", yes=True)  # targeted override — confirmed with --yes

    e = _entry()
    assert str(e["prefix"]) == "bc-myrepo"  # intentional change applied
    assert str(e["kind"]) == "personal"  # unrelated field preserved (no clobber)


def test_reinit_prefix_override_without_yes_refuses(world, capsys):
    #: changing a registered rig's prefix orphans every existing bead ID,
    # so an explicit differing --prefix hard-refuses without --yes (not --skip-check-able).
    import pytest
    import typer

    _make_repo(world)
    _register(world, prefix="mr", kind="personal")

    with pytest.raises(typer.Exit):
        rig.init(prefix="bc-myrepo")  # no --yes → refuse before any mutation

    out = capsys.readouterr()
    assert "orphans every existing bead ID" in out.err
    assert "--yes" in out.err
    assert str(_entry()["prefix"]) == "mr"  # registry untouched


def test_reinit_force_preserves_registered_prefix(world, monkeypatch):
    # regression (the re-derive path): --force on a registered rig used to
    # silently re-derive the prefix (mr → myrepo) and re-register under it, orphaning every
    # existing bead ID. Force may re-classify the kind, but the prefix stays registered.
    _make_repo(world)
    _register(world, prefix="mr", kind="personal")
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")

    rig.init(force=True)  # no explicit --prefix

    e = _entry()
    assert str(e["prefix"]) == "mr"  # NOT re-derived to 'myrepo'
    assert str(e["kind"]) == "prototype"  # kind still re-classified under --force


def test_reinit_warns_on_derived_vs_registered_mismatch(world, capsys):
    #: the registered prefix is an override ('bc-myrepo') that differs from
    # what derivation would yield for a prototype (the bare repo name 'myrepo'). A plain
    # re-init must NAME both prefixes + the --prefix override, while still preserving.
    _make_repo(world)
    _register(world, prefix="bc-myrepo", kind="prototype")

    rig.init()  # plain re-init — no flags

    out = capsys.readouterr()
    assert "derived prefix 'myrepo'" in out.err  # what derivation would produce
    assert "registered prefix 'bc-myrepo'" in out.err  # what is kept
    assert "--prefix" in out.err  # the override is surfaced
    assert str(_entry()["prefix"]) == "bc-myrepo"  # still preserved — no clobber (at0 intact)


def test_reinit_no_mismatch_warning_when_derived_equals_registered(world, capsys):
    # No spurious mismatch warning when the registered prefix already equals the derived value.
    _make_repo(world)
    _register(world, prefix="myrepo", kind="prototype")  # == prototype derivation of 'myrepo'

    rig.init()

    out = capsys.readouterr()
    assert "derived prefix" not in out.err  # no spurious mismatch warning
    assert "already configured" in out.err  # at0's preserve warning still fires
    assert str(_entry()["prefix"]) == "myrepo"


def test_reinit_force_notes_derived_vs_registered_mismatch(world, monkeypatch, capsys):
    # Under --force the derived value is still surfaced, but the registered prefix is kept
    # — the note names both and points at --prefix for a deliberate change.
    _make_repo(world)
    _register(world, prefix="mr", kind="personal")
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")

    rig.init(force=True)

    out = capsys.readouterr()
    assert "derived prefix 'myrepo'" in out.err
    assert "'mr'" in out.err  # names the prefix being kept
    assert "keeping the registered one" in out.err
    assert str(_entry()["prefix"]) == "mr"  # preserved, not re-registered as 'myrepo'


def test_fresh_rig_registers_normally(world, monkeypatch):
    _make_repo(world, repo="newrepo")  # unregistered
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    assert _entry(repo="newrepo") is None

    rig.init()

    e = _entry(repo="newrepo")
    assert e is not None and str(e["prefix"]) == "newrepo"  # fresh init still registers
