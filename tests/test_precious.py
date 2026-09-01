from __future__ import annotations

import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from beadhive import config, precious
from beadhive.config_schema import BeadhiveConfig, iter_schema_fields
from beadhive.precious import PreciousFile, scan_precious


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def test_precious_file_is_frozen() -> None:
    item = PreciousFile(".env", 3, "precious", ".env")
    with pytest.raises(FrozenInstanceError):
        item.bytes = 4  # type: ignore[misc]


def test_scan_precious_classifies_ignored_and_untracked_content(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text(
        ".env\n*.db\nnode_modules/\nignored-large\nignored-small\nunknown-dir/\n"
    )
    _git(repo, "add", ".gitignore")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "test: add ignore taxonomy",
    )
    (repo / ".env").write_text("token=x\n")
    (repo / "state.db").write_bytes(b"db")
    (repo / "ignored-large").write_bytes(b"x" * 64)
    (repo / "ignored-small").write_bytes(b"x")
    (repo / "untracked-large").write_bytes(b"u" * 64)
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "payload.bin").write_bytes(b"n" * 256)
    (repo / "unknown-dir").mkdir()
    (repo / "unknown-dir" / "payload.bin").write_bytes(b"d" * 64)

    real_scandir = precious.os.scandir

    def scandir_without_junk(path):
        assert Path(path).name != "node_modules", "junk directory was walked"
        return real_scandir(path)

    monkeypatch.setattr(precious.os, "scandir", scandir_without_junk)
    found = scan_precious(
        repo,
        precious_globs=[".env", "*.db"],
        junk_globs=["node_modules/**"],
        min_bytes=32,
    )

    by_path = {item.path: item for item in found}
    assert list(by_path) == [".env", "ignored-large", "state.db", "unknown-dir/", "untracked-large"]
    assert by_path[".env"] == PreciousFile(".env", 8, "precious", ".env")
    assert by_path["state.db"] == PreciousFile("state.db", 2, "precious", "*.db")
    assert by_path["ignored-large"].category == "review"
    assert by_path["unknown-dir/"].category == "review"
    assert by_path["untracked-large"].category == "review"
    assert "ignored-small" not in by_path
    assert not [path for path in by_path if path.startswith("node_modules")]


def test_junk_is_rejected_before_any_filesystem_measurement(monkeypatch, tmp_path) -> None:
    result = subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout="!! node_modules/\0!! .env\0", stderr=""
    )
    monkeypatch.setattr(precious, "_run_git", lambda *_args, **_kwargs: result)
    measured: list[Path] = []

    def measure(path: Path, _min_bytes: int) -> int:
        measured.append(path)
        return 1

    monkeypatch.setattr(precious, "_bounded_bytes", measure)

    assert scan_precious(
        tmp_path,
        precious_globs=[".env"],
        junk_globs=["node_modules/**"],
        min_bytes=1024,
    ) == [PreciousFile(".env", 1, "precious", ".env")]
    assert measured == [tmp_path / ".env"]


def test_directory_measurement_is_bounded_and_conservative(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "unknown"
    directory.mkdir()
    (directory / "one").touch()
    (directory / "two").touch()
    monkeypatch.setattr(precious, "_DIRECTORY_ENTRY_LIMIT", 1)

    assert precious._bounded_directory_bytes(directory, 123) == 123


def test_scan_does_not_follow_untracked_symlink(tmp_path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    _git(repo, "init", "-q")
    (outside / "large-secret").write_bytes(b"x" * 2048)
    (repo / "link").symlink_to(outside, target_is_directory=True)

    assert scan_precious(repo, precious_globs=[], junk_globs=[], min_bytes=1024) == []


def test_scan_rejects_negative_threshold_before_running_git(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        precious, "_run_git", lambda *_args, **_kwargs: pytest.fail("git should not run")
    )
    with pytest.raises(ValueError, match="non-negative"):
        scan_precious(tmp_path, precious_globs=[], junk_globs=[], min_bytes=-1)


def test_scan_fails_closed_when_git_cannot_describe_the_path(tmp_path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        scan_precious(tmp_path, precious_globs=[], junk_globs=[], min_bytes=1)


def test_status_parser_accepts_only_untracked_and_ignored_records() -> None:
    output = "x\0!!\0!! ignored name\0?? untracked\0 M tracked\0R  renamed\0old-name\0"
    assert precious._status_paths(output) == ["ignored name", "untracked"]


def test_glob_matching_supports_nested_basenames_and_collapsed_directories() -> None:
    assert precious._glob_match("service/.env", (".env",)) == ".env"
    assert precious._glob_match("cache/item.pyc", ("*.pyc",)) == "*.pyc"
    assert precious._glob_match("node_modules/", ("node_modules/**",)) == "node_modules/**"
    assert precious._glob_match("data/", ("data/**",)) == "data/**"


def test_config_accessors_default_global_and_per_hive_precedence() -> None:
    assert config.precious_globs({}, None) == list(precious.DEFAULT_PRECIOUS_GLOBS)
    assert config.junk_globs({}, None) == list(precious.DEFAULT_JUNK_GLOBS)
    assert config.precious_min_bytes({}, None) == precious.DEFAULT_PRECIOUS_MIN_BYTES

    cfg = {
        "work": {
            "precious_globs": ["global-secret"],
            "junk_globs": ["global-junk/**"],
            "precious_min_bytes": 99,
        }
    }
    entry = {
        "work": {
            "precious_globs": [],
            "junk_globs": ["local-junk/**"],
            "precious_min_bytes": 7,
        }
    }
    assert config.precious_globs(cfg, None) == ["global-secret"]
    assert config.junk_globs(cfg, None) == ["global-junk/**"]
    assert config.precious_min_bytes(cfg, None) == 99
    assert config.precious_globs(cfg, entry) == []
    assert config.junk_globs(cfg, entry) == ["local-junk/**"]
    assert config.precious_min_bytes(cfg, entry) == 7


def test_config_accessors_preserve_facade_patch_seam_and_return_fresh_defaults(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_layered(_cfg, _entry, section, key, default=None):
        calls.append((section, key))
        return default

    monkeypatch.setattr(config, "layered", fake_layered)
    first = config.precious_globs({}, None)
    first.append("mutated")
    assert config.precious_globs({}, None) == list(precious.DEFAULT_PRECIOUS_GLOBS)
    assert config.junk_globs({}, None) == list(precious.DEFAULT_JUNK_GLOBS)
    assert config.precious_min_bytes({}, None) == precious.DEFAULT_PRECIOUS_MIN_BYTES
    assert calls == [
        ("work", "precious_globs"),
        ("work", "precious_globs"),
        ("work", "junk_globs"),
        ("work", "precious_min_bytes"),
    ]


def test_config_schema_and_discovery_publish_precious_contract() -> None:
    model = BeadhiveConfig()
    assert model.work.precious_globs == list(precious.DEFAULT_PRECIOUS_GLOBS)
    assert model.work.junk_globs == list(precious.DEFAULT_JUNK_GLOBS)
    assert model.work.precious_min_bytes == precious.DEFAULT_PRECIOUS_MIN_BYTES
    fields = {field.path for field in iter_schema_fields()}
    assert {
        "work.precious_globs",
        "work.junk_globs",
        "work.precious_min_bytes",
    } <= fields
    with pytest.raises(ValidationError):
        BeadhiveConfig(work={"precious_min_bytes": -1})


def test_schema_defaults_and_shipped_template_are_deterministic() -> None:
    first = BeadhiveConfig()
    first.work.precious_globs.append("mutated")
    second = BeadhiveConfig()
    assert second.work.precious_globs == list(precious.DEFAULT_PRECIOUS_GLOBS)

    template = Path(config.template("config.example.yaml")).read_text()
    assert "#   precious_globs:" in template
    assert "#   junk_globs:" in template
    assert f"#   precious_min_bytes: {precious.DEFAULT_PRECIOUS_MIN_BYTES}" in template
