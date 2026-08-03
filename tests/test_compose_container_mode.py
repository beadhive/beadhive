"""compose.py must not shell out to a container runtime from inside the Beadhive image.

The defect (bh-pc2a.6): `bh dolt up` / the otel equivalent drove docker/podman/colima by name.
In the image there is no runtime and no socket — the host's is deliberately NOT mounted, since
that would hand the container host root — so the user met a bare `docker: not found` instead of
being told where to run the command.

Both halves matter and both are covered here: the refusal in-container, and the UNCHANGED
behaviour on a normal host (the regression the bead asks for).
"""

from __future__ import annotations

import pytest
import typer

from beadhive import compose


@pytest.fixture
def not_in_container(monkeypatch):
    monkeypatch.delenv(compose.CONTAINER_MARKER, raising=False)


@pytest.fixture
def inside_container(monkeypatch):
    monkeypatch.setenv(compose.CONTAINER_MARKER, "1")


# --- detection ------------------------------------------------------------------------------


def test_absent_marker_is_not_a_container(not_in_container):
    assert compose.in_container() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE"])
def test_marker_set_is_a_container(monkeypatch, value):
    monkeypatch.setenv(compose.CONTAINER_MARKER, value)
    assert compose.in_container() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "  "])
def test_explicitly_falsey_marker_is_not_a_container(monkeypatch, value):
    """`docker run -e BH_IN_CONTAINER=0` must be able to turn the behaviour off."""
    monkeypatch.setenv(compose.CONTAINER_MARKER, value)
    assert compose.in_container() is False


# --- the refusal ----------------------------------------------------------------------------


def test_ensure_up_refuses_in_container(inside_container, capsys):
    with pytest.raises(typer.Exit) as exc:
        compose.ensure_up("colima", stack="dolt")
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "bh dolt up" in err, "must name the command the user typed"
    assert "HOST" in err, "must say where to run it instead"
    assert "docker socket" in err, "must explain WHY, not just refuse"


def test_run_compose_refuses_before_touching_the_filesystem(inside_container, tmp_path, capsys):
    """Seeding a compose file that can never be run from here would leave confusing state."""
    target = tmp_path / "docker-compose.yml"
    with pytest.raises(typer.Exit):
        compose.run_compose("colima", target, "docker-compose.yml", "up", "-d", stack="otel")
    assert not target.exists(), "must refuse BEFORE seeding the file"
    assert "bh otel up" in capsys.readouterr().err


def test_the_refusal_names_the_stack_it_was_called_for(inside_container, capsys):
    """A shared message that always said 'dolt' would misdirect otel users."""
    with pytest.raises(typer.Exit):
        compose.ensure_up("colima", stack="otel")
    err = capsys.readouterr().err
    assert "bh otel up" in err
    assert "bh dolt up" not in err


# --- the regression: a normal host is untouched ----------------------------------------------


def test_host_ensure_up_still_drives_the_runtime(not_in_container, monkeypatch):
    """The whole point of gating on an explicit marker: hosts behave exactly as before."""
    calls = []
    monkeypatch.setattr(compose, "ok", lambda *a, **k: False)
    monkeypatch.setattr(compose, "run", lambda cmd, **k: calls.append(cmd))
    compose.ensure_up("colima", stack="dolt")
    assert calls == [["colima", "start"]]


def test_host_backend_default_is_unchanged(not_in_container, monkeypatch):
    monkeypatch.setattr(compose.config, "dolt_cfg", dict)
    assert compose.backend() == "colima"


def test_in_container_backend_defaults_to_none(inside_container, monkeypatch):
    """Nothing in the image to drive, so the honest default is 'someone else manages this'."""
    monkeypatch.setattr(compose.config, "dolt_cfg", dict)
    assert compose.backend() == "none"


def test_explicit_config_still_wins_in_container(inside_container, monkeypatch):
    """An operator who knows better must never be overridden by the default."""
    monkeypatch.setattr(compose.config, "dolt_cfg", lambda: {"backend": "podman"})
    assert compose.backend() == "podman"
