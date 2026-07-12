"""Smoke checks for the grafana/otel-lgtm compose template and config wiring.

No docker required — these verify the compose file is valid YAML with the expected
structure/ports and that config accessors return correct paths and the config example
documents the local LGTM endpoint.  The manual-verify steps are in the compose template
header comment.
"""

from __future__ import annotations

from ruamel.yaml import YAML

from beadhive import config  # noqa: E402 — stdlib → third-party → local

_yaml = YAML()


def _load_compose():
    return _yaml.load(config.template("docker-compose.otel.yml").read_text())


# ---- compose template -------------------------------------------------------


def test_otel_compose_template_exists():
    assert config.template("docker-compose.otel.yml").exists()


def test_otel_compose_template_parses():
    data = _load_compose()
    assert "services" in data


def test_otel_compose_has_otel_lgtm_service():
    data = _load_compose()
    assert "otel-lgtm" in data["services"], "expected otel-lgtm service in compose template"


def test_otel_compose_uses_grafana_image():
    data = _load_compose()
    image = data["services"]["otel-lgtm"]["image"]
    assert str(image).startswith("grafana/otel-lgtm"), f"unexpected image: {image}"


def test_otel_compose_exposes_grafana_ui():
    """Grafana UI on port 3000."""
    data = _load_compose()
    ports = [str(p) for p in data["services"]["otel-lgtm"]["ports"]]
    assert any("3000" in p for p in ports), f"port 3000 not found in {ports}"


def test_otel_compose_exposes_otlp_grpc():
    """OTLP gRPC on port 4317."""
    data = _load_compose()
    ports = [str(p) for p in data["services"]["otel-lgtm"]["ports"]]
    assert any("4317" in p for p in ports), f"port 4317 not found in {ports}"


def test_otel_compose_exposes_otlp_http():
    """OTLP HTTP/protobuf on port 4318."""
    data = _load_compose()
    ports = [str(p) for p in data["services"]["otel-lgtm"]["ports"]]
    assert any("4318" in p for p in ports), f"port 4318 not found in {ports}"


# ---- config accessors -------------------------------------------------------


def test_otel_compose_file_lives_under_bh_home(monkeypatch, tmp_path):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    path = config.otel_compose_file()
    assert path == tmp_path / "docker-compose.otel.yml"


def test_otel_compose_file_default_name():
    """Regardless of home dir, the file is always named docker-compose.otel.yml."""
    path = config.otel_compose_file()
    assert path.name == "docker-compose.otel.yml"


# ---- config example documents the local endpoint ----------------------------


def test_config_example_documents_otlp_endpoint():
    """config.example.yaml must reference the local LGTM OTLP endpoint."""
    text = config.template("config.example.yaml").read_text()
    assert "localhost:4317" in text or "localhost:4318" in text, (
        "config example should document the local OTLP endpoint for otel-lgtm"
    )


def test_config_example_documents_otel_enabled():
    """config.example.yaml must show the otel.enabled flag."""
    text = config.template("config.example.yaml").read_text()
    assert "otel:" in text
    assert "enabled: true" in text


def test_otel_compose_invocations_carry_env_overlay(monkeypatch, tmp_path):
    """Regression (bh-nf1.2): otel_lgtm compose invocations MUST carry the ~/.ws/.env overlay,
    exactly like dolt's — previously the otel stack ran compose with no env, so it could not see
    ports/tokens the env file defines. Drive a real compose op through the shared helper and assert
    the captured env carries a value from the .env file."""
    from beadhive import compose, otel_lgtm

    envfile = tmp_path / ".env"
    envfile.write_text('OTEL_TEST_TOKEN=sekret\n')
    composefile = tmp_path / "docker-compose.otel.yml"
    composefile.write_text("services: {}\n")  # exists → no template seeding
    monkeypatch.setattr(config, "dolt_cfg", lambda: {})
    monkeypatch.setattr(config, "env_file", lambda: envfile)
    monkeypatch.setattr(config, "otel_compose_file", lambda: composefile)
    monkeypatch.setattr(config, "home", lambda: tmp_path)
    monkeypatch.setattr(compose, "compose_cmd", lambda backend: ["docker", "compose"])
    captured = {}
    monkeypatch.setattr(compose, "run", lambda cmd, **kw: captured.update(kw))

    otel_lgtm.down()

    assert captured.get("env", {}).get("OTEL_TEST_TOKEN") == "sekret"
