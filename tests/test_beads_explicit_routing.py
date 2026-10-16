"""bd routing declared explicit on bh-managed hives (bh-5lpl9.1).

docs/design/beads-multi-repo-adoption-adr.md section 3: a bh-managed hive declares bd's
`routing.mode: explicit` + `routing.default: .` so bd's actor heuristic is never the operative
rule. Onboard sets them when absent and doctor reports drift. Everything is behind
`work.beads.explicit_routing`, off by default.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beadhive import bd as bd_mod
from beadhive import config, doctor, hive_repair, onboard

CONFIG_YAML = """\
providers: [github]
managed_repos:
  - {provider: github, org: acme, repo: optin, prefix: oi, work: {beads: {explicit_routing: true}}}
  - {provider: github, org: acme, repo: plain, prefix: pl}
"""

FLAT_AND_NESTED = 'routing.mode: "explicit"\nrouting:\n    default: .\n'


class _BdConfigFile:
    """Fake `bd config set` that writes the key into `.beads/config.yaml` the way bd 1.3 does
    for `routing.mode` (flat dotted key) — enough for verify to re-read it."""

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, args, cwd, actor="", **kw):
        self.calls.append((str(cwd), list(args)))
        path = cwd / ".beads" / "config.yaml"
        text = path.read_text() if path.exists() else ""
        path.write_text(text + f'{args[2]}: "{args[3]}"\n')
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def hives(tmp_path, monkeypatch):
    ws_root = tmp_path / "ws"
    optin = ws_root / "github" / "acme" / "optin"
    plain = ws_root / "github" / "acme" / "plain"
    for d in (optin, plain):
        (d / ".beads").mkdir(parents=True)
        (d / ".beads" / "config.yaml").write_text("issue-prefix: x\n")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG_YAML)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    monkeypatch.delenv("BH_DEV", raising=False)
    monkeypatch.chdir(tmp_path)
    fake = _BdConfigFile()
    monkeypatch.setattr(bd_mod, "run", fake.run)
    return SimpleNamespace(optin=optin, plain=plain, fake=fake)


def test_read_routing_handles_bd_flat_and_nested_shapes(tmp_path):
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / "config.yaml").write_text(FLAT_AND_NESTED)
    assert hive_repair.read_routing(tmp_path) == {
        "routing.mode": "explicit",
        "routing.default": ".",
    }
    assert hive_repair.routing_drift(hive_repair.read_routing(tmp_path)) == {}


def test_read_routing_reports_unset_and_missing(tmp_path):
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / "config.yaml").write_text("routing:\n  mode: auto\n")
    current = hive_repair.read_routing(tmp_path)
    assert current == {"routing.mode": "auto", "routing.default": ""}
    assert hive_repair.routing_drift(current) == {"routing.mode": "auto", "routing.default": ""}
    assert hive_repair.read_routing(tmp_path / "absent") is None


def test_default_is_off_for_doctor_and_onboard(hives):
    cfg = config.load()
    plain = next(e for e in cfg["managed_repos"] if e["repo"] == "plain")
    assert config.beads_explicit_routing(cfg, plain) is False
    hives_reported = [f["hive"] for f in doctor._data_beads_routing(cfg)]
    assert hives_reported == ["github/acme/optin"]  # the plain hive is never inspected

    ctx = onboard.Ctx(hive="github/acme/plain", target="", cwd=str(hives.plain), cfg=cfg)
    ctx.provider, ctx.org, ctx.repo = "github", "acme", "plain"
    onboard._act_beads_routing(ctx)
    assert hives.fake.calls == []


def test_doctor_lists_drift_and_goes_silent_once_declared(hives, capsys):
    cfg = config.load()
    findings = doctor._data_beads_routing(cfg)
    assert findings[0]["actual"] == {"routing.mode": "", "routing.default": ""}
    assert "bd config set routing.mode explicit" in findings[0]["remediation"]
    doctor._render_beads_routing(findings)
    assert "# bd routing (1)" in capsys.readouterr().out

    (hives.optin / ".beads" / "config.yaml").write_text(FLAT_AND_NESTED)
    assert doctor._data_beads_routing(cfg) == []
    doctor._render_beads_routing([])
    assert capsys.readouterr().out == ""  # silent: no header when nothing drifted


def test_onboard_fills_unset_keys_but_never_overwrites(hives, capsys):
    cfg = config.load()
    (hives.optin / ".beads" / "config.yaml").write_text('routing.mode: "maintainer"\n')
    ctx = onboard.Ctx(hive="github/acme/optin", target="", cwd=str(hives.optin), cfg=cfg)
    ctx.provider, ctx.org, ctx.repo = "github", "acme", "optin"

    onboard._act_beads_routing(ctx)

    assert [args for _, args in hives.fake.calls] == [["config", "set", "routing.default", "."]]
    out = capsys.readouterr().out
    assert "routing.mode='maintainer'" in out and "left unchanged" in out
