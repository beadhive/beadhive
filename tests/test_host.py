"""``~/.beadhive/host.yaml`` — the stable minted machine identity (bh-ytbb.2).

Covers the acceptance bar directly:
  * `bh config init` mints a UUID `host_id` + a human `label`.
  * the id is stable across a simulated machine rename and across unrelated config edits.
  * minting is once-only — an existing host.yaml is never silently regenerated, not even by
    `config init --force`.
  * host.yaml is excluded from any sync/template path — verified against the REAL sync
    manifest this repo has today (`hq.scaffold_layout`, the function that writes the files
    `bh hq init` pushes to the shared HQ remote), not a manifest (e.g. a future
    `.chezmoiignore`) that doesn't exist in this repo yet.

The autouse `_sandbox_bh_home` fixture (tests/conftest.py) isolates `BH_HOME` per test, so
every test below is against a throwaway dir — never the operator's real ~/.beadhive.
"""

from __future__ import annotations

import uuid

import pytest
from typer.testing import CliRunner

from beadhive import config, host, hq
from beadhive.cli import app

runner = CliRunner()


# ---- mint-once ----------------------------------------------------------------


def test_mint_if_needed_mints_a_uuid_host_id_and_a_hostname_label(monkeypatch):
    monkeypatch.setattr(host.socket, "gethostname", lambda: "my-laptop")

    minted = host.mint_if_needed()

    assert minted is True
    data = host.load()
    assert uuid.UUID(data["host_id"])  # a real, parseable UUID
    assert data["label"] == "my-laptop"


def test_mint_if_needed_returns_false_and_never_touches_an_existing_file():
    host.path().parent.mkdir(parents=True, exist_ok=True)
    original = "host_id: fixed-id-not-a-real-uuid\nlabel: hand-edited\n"
    host.path().write_text(original)

    minted = host.mint_if_needed()

    assert minted is False
    assert host.path().read_text() == original  # byte-identical — never rewritten


def test_load_raises_with_config_init_guidance_when_host_yaml_is_absent():
    with pytest.raises(FileNotFoundError, match="config init"):
        host.load()


# ---- stability --------------------------------------------------------------


def test_host_id_stable_across_repeated_mint_calls():
    host.mint_if_needed()
    first = host.host_id()

    for _ in range(3):
        host.mint_if_needed()  # every later call is a no-op

    assert host.host_id() == first


def test_host_id_stable_across_simulated_machine_rename(monkeypatch):
    monkeypatch.setattr(host.socket, "gethostname", lambda: "old-name")
    host.mint_if_needed()
    original_id = host.host_id()

    monkeypatch.setattr(host.socket, "gethostname", lambda: "new-name")
    host.mint_if_needed()  # the "rename" — host.yaml already exists, must stay a no-op

    assert host.host_id() == original_id
    assert host.label() == "old-name"  # label is a mint-time snapshot, not live-derived


def test_host_id_stable_across_config_edits():
    host.mint_if_needed()
    before = host.path().read_text()
    original_id = host.host_id()

    # An unrelated host.yaml-sibling config edit (config.yaml, not host.yaml).
    config.set_value("otel.enabled", "true")

    assert host.host_id() == original_id
    assert host.path().read_text() == before  # completely untouched by the config write


def test_label_is_freely_editable_without_disturbing_host_id():
    host.mint_if_needed()
    original_id = host.host_id()

    host.path().write_text(f"host_id: {original_id}\nlabel: renamed-by-operator\n")

    assert host.host_id() == original_id
    assert host.label() == "renamed-by-operator"


# ---- `bh config init` wiring -------------------------------------------------


def test_config_init_mints_host_yaml():
    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0
    assert host.path().exists()
    data = host.load()
    assert uuid.UUID(data["host_id"])
    assert f"wrote {host.path()}" in result.stdout


def test_config_init_second_run_does_not_regenerate_host_yaml():
    runner.invoke(app, ["config", "init"])
    original_id = host.host_id()

    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0
    assert host.host_id() == original_id
    assert f"skip {host.path()} (exists)" in result.stdout


def test_config_init_force_still_never_regenerates_host_yaml():
    """--force overwrites the other templated files but must NEVER touch host.yaml — identity,
    not template output (see beadhive.host module docstring)."""
    runner.invoke(app, ["config", "init"])
    original_id = host.host_id()

    result = runner.invoke(app, ["config", "init", "--force"])

    assert result.exit_code == 0
    assert host.host_id() == original_id
    assert f"skip {host.path()} (exists)" in result.stdout
    assert f"wrote {host.path()}" not in result.stdout


# ---- sync/template exclusion -------------------------------------------------
# host.yaml must never appear in the REAL sync manifest this repo has today:
# hq.scaffold_layout is the function `bh hq init` uses to write the files it pushes to the
# shared HQ remote (fleet.yaml / workspace.toml / hosts/README.md). There is no
# dotfile-templating mechanism landed in this repo yet (bh-7ns4.1's planned .chezmoiignore is
# a DIFFERENT, currently-unlanded molecule) — so this asserts against hq.py's real scaffold
# function, not a manifest file that doesn't exist here.


def test_host_yaml_absent_from_hq_scaffold_layout_output(tmp_path):
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    cfg = {"schema_version": 3, "managed_repos": []}

    written = hq.scaffold_layout(hq_dir, cfg)

    names = {p.name for p in written}
    assert "host.yaml" not in names
    assert names == {"fleet.yaml", "workspace.toml", "README.md"}  # the full, real sync surface

    # Not merely absent from the *written* list — genuinely never created on disk either.
    assert not (hq_dir / "host.yaml").exists()
    assert not any(p.name == "host.yaml" for p in hq_dir.rglob("*"))


def test_hq_scaffold_layout_source_never_references_host_yaml():
    """Regression guard directly on the sync function's source: if a future edit to
    `hq.scaffold_layout` starts writing/reading `host.yaml`, this fails immediately rather
    than relying only on the current output shape above. Scoped to the literal 'host.yaml'
    filename (not 'host_id') so it doesn't collide with the unrelated, legitimate
    `hosts/<host_id>.yaml` per-host MANIFEST placeholder text `scaffold_layout` already writes
    (bh-ytbb.3 — a different file, in HQ, keyed BY host_id; not this bead's host.yaml)."""
    import inspect

    source = inspect.getsource(hq.scaffold_layout)
    assert "host.yaml" not in source


def test_host_yaml_path_lives_outside_the_hq_store():
    """host.yaml is host-local config-home state, never part of the HQ store that gets
    cloned/pushed — the two directories must never overlap."""
    assert host.path().parent == config.home()
    assert config.hq_dir() not in host.path().parents
    assert host.path() != config.hq_dir()
