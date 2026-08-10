"""The unattended-dispatch supervision backend seam (bh-e7r9q.4).

Proves the abstraction with a SECOND implementation (`RecordingBackend`) rather than asserting
it from `SystemdUserBackend` alone, and proves the lease-absent degradation contract lives one
layer up (in `dispatch_hive_run`, tested separately) by keeping this module's own tests to
exactly what `dispatch_supervisor` owns: install/start/persist/status, config selection, and
the documented-not-implemented backends.
"""

from __future__ import annotations

from collections import namedtuple

import pytest

from beadhive import dispatch_supervisor as ds

Completed = namedtuple("Completed", "returncode stdout stderr")


# ---- config selection ---------------------------------------------------------------------


def test_get_supervisor_backend_defaults_to_systemd():
    backend = ds.get_supervisor_backend({})
    assert isinstance(backend, ds.SystemdUserBackend)
    assert backend.name == "systemd"


def test_get_supervisor_backend_rejects_unknown_name():
    with pytest.raises(ValueError, match="bogus"):
        ds.get_supervisor_backend({"host": {"dispatch": {"backend": "bogus"}}})


@pytest.mark.parametrize("name", [ds.BACKEND_LAUNCHD, ds.BACKEND_CONTAINER])
def test_get_supervisor_backend_documents_unimplemented_backends(name):
    with pytest.raises(NotImplementedError, match=name):
        ds.get_supervisor_backend({"host": {"dispatch": {"backend": name}}})


# ---- the seam is a real Protocol, not an assertion: a second implementation conforms ------


def test_recording_backend_conforms_to_the_same_shape_as_systemd():
    """Both backends answer the same three questions the same way — the whole point of the
    seam. If `RecordingBackend` needed a different call shape, the Protocol would not be one."""
    rec = ds.RecordingBackend()
    before = rec.status("some-hive")
    assert before == ds.SupervisorState(detail="never enabled")

    after_enable = rec.enable("some-hive", exec_argv=[], env={})
    assert after_enable.installed and after_enable.running and after_enable.persisted

    after_disable = rec.disable("some-hive")
    assert after_disable.installed  # disable never uninstalls
    assert not after_disable.running
    assert not after_disable.persisted

    assert rec.calls == [
        ("status", "some-hive"),
        ("enable", "some-hive"),
        ("disable", "some-hive"),
    ]


def test_recording_backend_is_idempotent_like_systemd_enable():
    rec = ds.RecordingBackend()
    rec.enable("h", exec_argv=[], env={})
    state = rec.enable("h", exec_argv=[], env={})
    assert state.running and state.persisted


# ---- SystemdUserBackend: real, but its subprocess calls are faked -------------------------


class _FakeRun:
    """Records every `systemctl --user ...` invocation and answers from a scripted table, so
    these tests exercise the REAL unit-templating + argv-building code without touching a real
    systemd."""

    def __init__(self, answers: dict[tuple, Completed] | None = None):
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv, check=False, capture=False):  # noqa: ARG002 - test double signature
        self.calls.append(list(argv))
        key = tuple(argv)
        return self.answers.get(key, Completed(0, "", ""))


def test_systemd_backend_templates_one_unit_file_per_hive_not_per_hand_edit(tmp_path, monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(ds, "run_cmd", fake)
    backend = ds.SystemdUserBackend(unit_dir=tmp_path, bh_binary="/usr/local/bin/bh")

    backend.enable("hive-a", exec_argv=[], env={})
    backend.enable("hive-b", exec_argv=[], env={})

    unit_files = list(tmp_path.glob("*.service"))
    assert [p.name for p in unit_files] == [ds.SYSTEMD_TEMPLATE_NAME]
    text = unit_files[0].read_text()
    assert "%i" in text  # ONE template, instantiated per hive by systemd itself
    assert "ExecStart=/usr/local/bin/bh host dispatch run --hive %i" in text

    enabled_units = [
        c[-1] for c in fake.calls if c[:2] == ["systemctl", "--user"] and c[2] == "enable"
    ]
    assert "bh-dispatch@hive-a.service" in enabled_units
    assert "bh-dispatch@hive-b.service" in enabled_units


def test_systemd_backend_enable_does_not_rewrite_unchanged_template(tmp_path, monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(ds, "run_cmd", fake)
    backend = ds.SystemdUserBackend(unit_dir=tmp_path, bh_binary="bh")

    backend.enable("hive-a", exec_argv=[], env={})
    mtime_1 = (tmp_path / ds.SYSTEMD_TEMPLATE_NAME).stat().st_mtime_ns
    reload_calls_1 = sum(1 for c in fake.calls if c[-1] == "daemon-reload")

    backend.enable("hive-a", exec_argv=[], env={})
    mtime_2 = (tmp_path / ds.SYSTEMD_TEMPLATE_NAME).stat().st_mtime_ns
    reload_calls_2 = sum(1 for c in fake.calls if c[-1] == "daemon-reload")

    assert mtime_1 == mtime_2
    assert reload_calls_1 == reload_calls_2 == 1  # not re-reloaded on the idempotent re-run


def test_systemd_backend_status_reads_is_active_and_is_enabled(tmp_path, monkeypatch):
    unit = "bh-dispatch@hive-a.service"
    fake = _FakeRun(
        answers={
            ("systemctl", "--user", "is-active", unit): Completed(0, "active\n", ""),
            ("systemctl", "--user", "is-enabled", unit): Completed(0, "enabled\n", ""),
        }
    )
    monkeypatch.setattr(ds, "run_cmd", fake)
    backend = ds.SystemdUserBackend(unit_dir=tmp_path, bh_binary="bh")
    (tmp_path / ds.SYSTEMD_TEMPLATE_NAME).write_text("x")

    state = backend.status("hive-a")
    assert state.installed
    assert state.running
    assert state.persisted


def test_systemd_backend_status_not_installed_when_no_template_written(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "run_cmd", _FakeRun())
    backend = ds.SystemdUserBackend(unit_dir=tmp_path, bh_binary="bh")
    state = backend.status("hive-a")
    assert not state.installed
    assert not state.running


def test_systemd_backend_disable_stops_and_deprists_without_uninstalling(tmp_path, monkeypatch):
    unit = "bh-dispatch@hive-a.service"
    fake = _FakeRun(
        answers={
            ("systemctl", "--user", "is-active", unit): Completed(0, "inactive\n", ""),
            ("systemctl", "--user", "is-enabled", unit): Completed(0, "disabled\n", ""),
        }
    )
    monkeypatch.setattr(ds, "run_cmd", fake)
    backend = ds.SystemdUserBackend(unit_dir=tmp_path, bh_binary="bh")
    backend.enable("hive-a", exec_argv=[], env={})

    state = backend.disable("hive-a")

    assert (tmp_path / ds.SYSTEMD_TEMPLATE_NAME).exists()  # never uninstalled
    assert not state.running
    assert not state.persisted
    disable_calls = [c for c in fake.calls if c[2:4] == ["disable", "--now"]]
    assert disable_calls == [["systemctl", "--user", "disable", "--now", unit]]
