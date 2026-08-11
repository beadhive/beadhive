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


# ---- per-instance --dry-run / --seat-binary override (bh-3xl60) --------------------------


def test_enable_with_exec_argv_writes_a_per_instance_override_drop_in(tmp_path, monkeypatch):
    """`bh host dispatch enable --dry-run --seat-binary <path>` — carried onto the systemd
    instance via a drop-in (`ExecStart=` cannot vary per-instance in the shared template, whose
    `%i` substitution already covers everything else)."""
    fake = _FakeRun()
    monkeypatch.setattr(ds, "run_cmd", fake)
    backend = ds.SystemdUserBackend(unit_dir=tmp_path, bh_binary="/usr/local/bin/bh")

    backend.enable("hive-a", exec_argv=["--dry-run", "--seat-binary", "/x/stub.py"], env={})

    override = tmp_path / "bh-dispatch@hive-a.service.d" / "override.conf"
    assert override.exists()
    text = override.read_text()
    assert "ExecStart=\n" in text  # the blank line that clears the inherited ExecStart first
    assert (
        "ExecStart=/usr/local/bin/bh host dispatch run --hive hive-a --dry-run "
        "--seat-binary /x/stub.py\n" in text
    )
    # A different hive's override is untouched — this is per-INSTANCE, not shared.
    assert not (tmp_path / "bh-dispatch@hive-b.service.d").exists()


def test_enable_without_exec_argv_never_writes_an_override(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "run_cmd", _FakeRun())
    backend = ds.SystemdUserBackend(unit_dir=tmp_path, bh_binary="bh")
    backend.enable("hive-a", exec_argv=[], env={})
    assert not (tmp_path / "bh-dispatch@hive-a.service.d").exists()


def test_a_plain_enable_converges_away_a_previous_dry_run_override(tmp_path, monkeypatch):
    """Re-running `enable` with neither flag must converge the override away, the same
    idempotent-convergence contract `enable` already promises for the unit itself."""
    fake = _FakeRun()
    monkeypatch.setattr(ds, "run_cmd", fake)
    backend = ds.SystemdUserBackend(unit_dir=tmp_path, bh_binary="bh")
    backend.enable("hive-a", exec_argv=["--dry-run"], env={})
    override = tmp_path / "bh-dispatch@hive-a.service.d" / "override.conf"
    assert override.exists()

    backend.enable("hive-a", exec_argv=[], env={})

    assert not override.exists()


def test_enable_does_not_rewrite_an_unchanged_override(tmp_path, monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(ds, "run_cmd", fake)
    backend = ds.SystemdUserBackend(unit_dir=tmp_path, bh_binary="bh")
    override = tmp_path / "bh-dispatch@hive-a.service.d" / "override.conf"

    backend.enable("hive-a", exec_argv=["--dry-run"], env={})
    mtime_1 = override.stat().st_mtime_ns
    reloads_1 = sum(1 for c in fake.calls if c[-1] == "daemon-reload")

    backend.enable("hive-a", exec_argv=["--dry-run"], env={})
    mtime_2 = override.stat().st_mtime_ns
    reloads_2 = sum(1 for c in fake.calls if c[-1] == "daemon-reload")

    assert mtime_1 == mtime_2
    assert reloads_1 == reloads_2


def test_recording_backend_still_conforms_with_exec_argv_set():
    """The seam holds for the OTHER implementation too, exec_argv and all."""
    rec = ds.RecordingBackend()
    state = rec.enable("hive-a", exec_argv=["--dry-run"], env={})
    assert state.installed and state.running and state.persisted


# ---- `installed` is PER INSTANCE, never the shared template ------------------------------


def test_installed_is_per_instance_not_the_shared_template(tmp_path, monkeypatch):
    """THE bug bh-e7r9q.6 exists to prevent, previously baked into its own status read.

    `installed` used to be `(unit_dir / "bh-dispatch@.service").exists()` — ONE file shared by
    every hive on the host. So enabling hive A made every OTHER hive report
    `installed=True, running=False`, which classifies as `enabled_stopped`: "supervised but not
    running", the dead-loop alarm. The distinction between a dead loop and a hive that was never
    enabled is exactly what `.6` is for, and this inverted it into a permanent false positive on
    every hive but one.
    """
    a, b = "bh-dispatch@hive-a.service", "bh-dispatch@hive-b.service"
    fake = _FakeRun(
        answers={
            # hive-a is enabled and running; hive-b was never enabled on this host.
            ("systemctl", "--user", "is-active", a): Completed(0, "active\n", ""),
            ("systemctl", "--user", "is-enabled", a): Completed(0, "enabled\n", ""),
            ("systemctl", "--user", "is-active", b): Completed(3, "inactive\n", ""),
            ("systemctl", "--user", "is-enabled", b): Completed(1, "", "No such file"),
        }
    )
    monkeypatch.setattr(ds, "run_cmd", fake)
    backend = ds.SystemdUserBackend(unit_dir=tmp_path, bh_binary="bh")

    backend.enable("hive-a", exec_argv=[], env={})  # writes the SHARED template
    assert (tmp_path / ds.SYSTEMD_TEMPLATE_NAME).exists()

    assert backend.status("hive-a").installed is True
    assert backend.status("hive-b").installed is False, (
        "hive-b was never enabled — the shared template file must not make it look supervised"
    )


def test_installed_falls_back_to_the_wants_symlink_when_systemctl_is_unavailable(
    tmp_path, monkeypatch
):
    """`systemctl --user is-enabled` is authoritative, but the `default.target.wants/` symlink
    is the same fact on disk — and it is per instance, which the template file is not."""
    monkeypatch.setattr(ds, "run_cmd", _FakeRun())  # every call answers exit 0, empty stdout
    backend = ds.SystemdUserBackend(unit_dir=tmp_path, bh_binary="bh")
    assert backend.status("hive-a").installed is False

    wants = tmp_path / "default.target.wants"
    wants.mkdir()
    (wants / "bh-dispatch@hive-a.service").write_text("")
    assert backend.status("hive-a").installed is True
    assert backend.status("hive-b").installed is False


def test_a_started_but_not_yet_persisted_instance_still_reads_as_installed(tmp_path, monkeypatch):
    """The half-state `enable` converges: the unit is running but `is-enabled` has not caught
    up. Running implies installed — otherwise `status` would report `not_enabled` for a loop
    that is demonstrably alive."""
    unit = "bh-dispatch@hive-a.service"
    fake = _FakeRun(
        answers={
            ("systemctl", "--user", "is-active", unit): Completed(0, "active\n", ""),
            ("systemctl", "--user", "is-enabled", unit): Completed(1, "disabled\n", ""),
        }
    )
    monkeypatch.setattr(ds, "run_cmd", fake)
    backend = ds.SystemdUserBackend(unit_dir=tmp_path, bh_binary="bh")

    state = backend.status("hive-a")
    assert state.installed is True
    assert state.running is True
    assert state.persisted is False
