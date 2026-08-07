"""`bh host` — the operator-facing roster surface (bh-ytbb.5).

Covers the acceptance bar directly:
  * `bh host init` mints THIS host's manifest into HQ (`hosts/<host_id>.yaml`), deriving
    os/arch from the running Python and label from `host.yaml`; refuses to clobber an
    existing manifest without `--force`; rejects an unknown `--role`/`--identity-kind`.
  * `bh host list` renders every manifest in HQ with role and last-seen — including the
    EMPTY roster case — and the row-rendering seam (`render_table`) stays generic enough
    for a later caller (bh-ytbb.13) to add a lease-state column without a rewrite.
  * `bh host show <host_id>` details one manifest; errors cleanly on an unknown id.

The autouse `_sandbox_bh_home` fixture (tests/conftest.py) isolates `BH_HOME` per test (and
sets `BH_SKIP_SETUP_CHECK`), so every `CliRunner` invocation below is against a throwaway
`hq_dir` — never the operator's real `~/.beadhive/hq`. Module-level unit tests
(`render_table`, `iter_manifests`) additionally pass their own `tmp_path` `hq_dir`, mirroring
tests/test_hosts.py's convention.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from beadhive import config, host, host_cli, hosts
from beadhive.cli import app

runner = CliRunner()

# `bh host init` is the first command in this suite to create a real `hq_dir` without a
# `fleet.yaml` (no `bh hq init` first) — CliRunner.Result.output mixes stdout+stderr, and a
# SECOND `runner.invoke()` in the same process can trip a pre-existing structlog/CliRunner
# stale-stream interaction on the best-effort "fleet.yaml missing" nudge, printing a spurious
# traceback to stderr. `.stdout` (pure stdout, same convention tests/test_help_hygiene.py
# already uses for this exact stdout/stderr-mixing class of issue) stays clean regardless —
# used below for every `--json` payload assertion.


def _mint_host(monkeypatch, host_id="11111111-1111-4111-8111-111111111111", label="fixture-host"):
    """Mint `~/.beadhive/host.yaml` deterministically — `bh host init` reads `host.host_id()`
    / `host.label()`, so every init-path test needs one, matching test_host.py's mint-first
    convention rather than depending on a real machine's hostname/uuid."""
    monkeypatch.setattr(host, "load", lambda: {"host_id": host_id, "label": label})
    return host_id, label


def _pin_platform(monkeypatch, system="Darwin", machine="arm64"):
    monkeypatch.setattr(host_cli.platform, "system", lambda: system)
    monkeypatch.setattr(host_cli.platform, "machine", lambda: machine)


# ---- bh host init -------------------------------------------------------------


def test_init_writes_a_manifest_deriving_os_arch_and_label(monkeypatch):
    host_id, label = _mint_host(monkeypatch)
    _pin_platform(monkeypatch, system="Darwin", machine="arm64")

    result = runner.invoke(app, ["host", "init", "--role", "executor"])

    assert result.exit_code == 0, result.output
    assert "wrote" in result.output

    manifest = hosts.load(config.hq_dir(), host_id)
    assert manifest.host_id == host_id
    assert manifest.label == label
    assert manifest.os == "darwin"
    assert manifest.arch == "arm64"
    assert manifest.role == "executor"
    assert manifest.identity.kind == "none"  # default identity-kind


def test_init_label_override_wins_over_host_yaml_label(monkeypatch):
    host_id, _label = _mint_host(monkeypatch)
    _pin_platform(monkeypatch)

    result = runner.invoke(app, ["host", "init", "--role", "viewer", "--label", "operator-chosen"])

    assert result.exit_code == 0, result.output
    manifest = hosts.load(config.hq_dir(), host_id)
    assert manifest.label == "operator-chosen"


def test_init_records_an_explicit_identity_mechanism(monkeypatch):
    host_id, _label = _mint_host(monkeypatch)
    _pin_platform(monkeypatch)

    result = runner.invoke(
        app,
        [
            "host",
            "init",
            "--role",
            "transient",
            "--identity-kind",
            "ssh_alias",
            "--identity-value",
            "github-operator",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = hosts.load(config.hq_dir(), host_id)
    assert manifest.identity.kind == "ssh_alias"
    assert manifest.identity.value == "github-operator"


def test_init_refuses_to_overwrite_an_existing_manifest_without_force(monkeypatch):
    _mint_host(monkeypatch)
    _pin_platform(monkeypatch)
    runner.invoke(app, ["host", "init", "--role", "viewer"])

    result = runner.invoke(app, ["host", "init", "--role", "executor"])

    assert result.exit_code == 0, result.output
    assert "skip" in result.output
    assert "exists" in result.output
    manifest = hosts.load(config.hq_dir(), host.host_id())
    assert manifest.role == "viewer"  # untouched


def test_init_force_overwrites_an_existing_manifest(monkeypatch):
    _mint_host(monkeypatch)
    _pin_platform(monkeypatch)
    runner.invoke(app, ["host", "init", "--role", "viewer"])

    result = runner.invoke(app, ["host", "init", "--role", "executor", "--force"])

    assert result.exit_code == 0, result.output
    assert "wrote" in result.output
    manifest = hosts.load(config.hq_dir(), host.host_id())
    assert manifest.role == "executor"


def test_init_rejects_a_role_outside_the_closed_set(monkeypatch):
    _mint_host(monkeypatch)
    _pin_platform(monkeypatch)

    result = runner.invoke(app, ["host", "init", "--role", "super-admin"])

    assert result.exit_code == 1
    assert "--role must be one of" in result.output


def test_init_rejects_an_identity_kind_outside_the_closed_set(monkeypatch):
    _mint_host(monkeypatch)
    _pin_platform(monkeypatch)

    result = runner.invoke(
        app, ["host", "init", "--role", "viewer", "--identity-kind", "carrier-pigeon"]
    )

    assert result.exit_code == 1
    assert "--identity-kind must be one of" in result.output


def test_init_requires_a_role():
    result = runner.invoke(app, ["host", "init"])
    assert result.exit_code != 0  # typer's own missing-required-option failure


# ---- bh host list ---------------------------------------------------------------


def test_list_renders_nothing_for_an_empty_roster():
    result = runner.invoke(app, ["host", "list"])

    assert result.exit_code == 0, result.output
    assert "no hosts registered" in result.output


def test_list_json_on_an_empty_roster_is_an_empty_array():
    result = runner.invoke(app, ["host", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []


def test_list_renders_every_manifest_with_role_and_last_seen(monkeypatch):
    _mint_host(monkeypatch, host_id="host-a", label="host-a-label")
    _pin_platform(monkeypatch)
    runner.invoke(app, ["host", "init", "--role", "viewer"])

    result = runner.invoke(app, ["host", "list"])

    assert result.exit_code == 0, result.output
    assert "HOST_ID" in result.output and "LABEL" in result.output and "ROLE" in result.output
    assert "LAST_SEEN" in result.output
    assert "host-a" in result.output
    assert "host-a-label" in result.output
    assert "viewer" in result.output


def test_list_json_shape_has_one_row_per_manifest_with_last_seen(monkeypatch):
    _mint_host(monkeypatch, host_id="host-b", label="host-b-label")
    _pin_platform(monkeypatch)
    runner.invoke(app, ["host", "init", "--role", "transient"])

    result = runner.invoke(app, ["host", "list", "--json"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    row = rows[0]
    assert row["host_id"] == "host-b"
    assert row["label"] == "host-b-label"
    assert row["role"] == "transient"
    assert "last_seen" in row and row["last_seen"]


def test_list_skips_a_malformed_manifest_with_a_warning_rather_than_aborting(tmp_path):
    """A broken manifest for one host must not black out visibility into every other one —
    `iter_manifests` (the seam `list` builds rows from) skips it with a stderr warning."""
    hq_dir = tmp_path / "hq"
    manifest_dir = hosts.hosts_dir(hq_dir)
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "broken.yaml").write_text("host_id: broken\nrole: not-a-real-role\n")

    rows = host_cli.list_payload(hq_dir)

    assert rows == []  # the only manifest present is malformed, so the roster is empty


# ---- render_table: the extensibility seam bh-ytbb.13 uses ------------------------


def test_render_table_empty_rows():
    assert host_cli.render_table([], host_cli.BASE_COLUMNS) == "(no hosts registered)"


def test_render_table_renders_base_columns():
    rows = [
        {
            "host_id": "h1",
            "label": "L1",
            "role": "viewer",
            "last_seen": "2026-01-01T00:00:00",
            "stale": "",
        }
    ]

    out = host_cli.render_table(rows, host_cli.BASE_COLUMNS)

    lines = out.splitlines()
    assert lines[0].split() == ["HOST_ID", "LABEL", "ROLE", "LAST_SEEN", "STALE"]
    assert "h1" in lines[1] and "L1" in lines[1] and "viewer" in lines[1]


def test_render_table_accepts_an_extended_column_spec_without_code_changes():
    """The seam bh-ytbb.13 (`bh host adopt|release|packup`) needs: a caller builds rows with
    an EXTRA key (a lease-state column keyed by hive/prefix) and an extended column spec,
    and `render_table` renders it with no change to this function."""
    rows = [{"host_id": "h1", "label": "L1", "lease": "held (by h1, hive=acme)"}]
    columns = (("host_id", "HOST_ID"), ("label", "LABEL"), ("lease", "LEASE"))

    out = host_cli.render_table(rows, columns)

    lines = out.splitlines()
    assert lines[0].split() == ["HOST_ID", "LABEL", "LEASE"]
    assert "held" in lines[1]


def test_render_table_blanks_a_missing_key_instead_of_raising():
    """A heterogeneous row set (e.g. some hosts have no lease row yet) still renders."""
    rows = [{"host_id": "h1"}, {"host_id": "h2", "lease": "free"}]
    columns = (("host_id", "HOST_ID"), ("lease", "LEASE"))

    out = host_cli.render_table(rows, columns)

    assert len(out.splitlines()) == 3  # header + 2 rows, no KeyError


# ---- bh host show ----------------------------------------------------------------


def test_show_renders_full_manifest_detail(monkeypatch):
    _mint_host(monkeypatch, host_id="host-c", label="host-c-label")
    _pin_platform(monkeypatch, system="Linux", machine="x86_64")
    runner.invoke(
        app,
        [
            "host",
            "init",
            "--role",
            "executor",
            "--identity-kind",
            "insteadOf",
            "--identity-value",
            "url.git@github.com-op:.insteadOf=git@github.com:",
        ],
    )

    result = runner.invoke(app, ["host", "show", "host-c"])

    assert result.exit_code == 0, result.output
    assert "host-c" in result.output
    assert "host-c-label" in result.output
    assert "executor" in result.output
    assert "linux/x86_64" in result.output
    assert "insteadOf" in result.output
    assert "url.git@github.com-op" in result.output


def test_show_json_shape_includes_last_seen(monkeypatch):
    _mint_host(monkeypatch, host_id="host-d", label="host-d-label")
    _pin_platform(monkeypatch)
    runner.invoke(app, ["host", "init", "--role", "viewer"])

    result = runner.invoke(app, ["host", "show", "host-d", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["host_id"] == "host-d"
    assert payload["role"] == "viewer"
    assert "last_seen" in payload and payload["last_seen"]


def test_show_unknown_host_id_errors_cleanly():
    result = runner.invoke(app, ["host", "show", "no-such-host"])

    assert result.exit_code == 1
    assert "no-such-host" in result.output


def test_show_malformed_manifest_fails_loudly_naming_the_offending_key():
    manifest_dir = hosts.hosts_dir(config.hq_dir())
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "broken.yaml").write_text(
        "host_id: broken\nlabel: x\nos: linux\narch: x86_64\nrole: not-a-real-role\n"
        "identity:\n  kind: none\n  value: ''\n"
    )

    result = runner.invoke(app, ["host", "show", "broken"])

    assert result.exit_code == 1
    assert "role" in result.output
