"""``.bdlg`` archive writer and reader (format ADR section 1): reproducibility, round trip and
refusal of hostile input, each with its own specific error."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

import beadlog
from beadlog import testing

V01 = Path(__file__).resolve().parents[3] / "docs" / "schemas" / "beadlog" / "v0.1"
EXAMPLE = V01 / "examples" / "archive"
GROUP = testing.GROUP
GENERATOR = testing.GENERATOR


# -- helpers -------------------------------------------------------------------------------


def _events() -> tuple[beadlog.BeadEvent, ...]:
    return testing.conformance_events()


def _write(events: Sequence[beadlog.BeadEvent] | None = None, **options: object) -> bytes:
    buffer = io.BytesIO()
    beadlog.write_archive(
        buffer,
        _events() if events is None else events,
        groups=[GROUP],
        generator=GENERATOR,
        **options,  # type: ignore[arg-type]
    )
    return buffer.getvalue()


def _members(archive: bytes) -> list[tuple[zipfile.ZipInfo, bytes]]:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        return [(info, zf.read(info)) for info in zf.infolist()]


def _pack(
    members: Sequence[tuple[str, bytes]],
    *,
    stored: Sequence[str] = ("mimetype",),
    zip64: bool = False,
) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED
            with zf.open(info, "w", force_zip64=zip64) as handle:
                handle.write(data)
    return out.getvalue()


def _edit(
    archive: bytes,
    change: Callable[[dict[str, bytes]], dict[str, bytes] | None] | None = None,
    *,
    manifest: Callable[[dict], None] | None = None,
    refresh: bool = True,
    **pack: object,
) -> bytes:
    """Re-pack ``archive`` after ``change`` edited its members by name; ``refresh`` recomputes
    each listed member's sha256/bytes (and JSONL record count) in the manifest so only the
    edit under test is wrong. ``manifest`` edits the manifest object last."""
    members = {info.filename: data for info, data in _members(archive)}
    if change is not None:
        members = change(members) or members
    meta = json.loads(members["MANIFEST.json"])
    if refresh:
        for row in meta["files"]:
            data = members.get(row["path"])
            if data is None:
                continue
            row["sha256"], row["bytes"] = hashlib.sha256(data).hexdigest(), len(data)
            if "records" in row:
                row["records"] = data.count(b"\n")
    if manifest is not None:
        manifest(meta)
    members["MANIFEST.json"] = (json.dumps(meta, indent=2) + "\n").encode()
    order = ["mimetype", "MANIFEST.json", *sorted(set(members) - {"mimetype", "MANIFEST.json"})]
    return _pack([(name, members[name]) for name in order if name in members], **pack)  # type: ignore[arg-type]


def _open(archive: bytes, **options: object) -> beadlog.ArchiveReader:
    return beadlog.ArchiveReader(io.BytesIO(archive), **options)  # type: ignore[arg-type]


PART = "events/conformance/part-0000.jsonl"


# -- writer: reproducibility and layout ----------------------------------------------------


def test_same_events_written_twice_are_byte_identical() -> None:
    assert _write() == _write()
    assert _write(index=True) == _write(index=True)


def test_input_order_and_duplicates_do_not_change_the_bytes() -> None:
    events = list(_events())
    assert _write(list(reversed(events))) == _write(events) == _write(events + events[:5])


def test_path_destinations_are_byte_identical_too(tmp_path: Path) -> None:
    for name in ("one.bdlg", "two.bdlg"):
        beadlog.write_archive(tmp_path / name, _events(), groups=[GROUP], generator=GENERATOR)
    assert (tmp_path / "one.bdlg").read_bytes() == (tmp_path / "two.bdlg").read_bytes()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["one.bdlg", "two.bdlg"]
    assert (tmp_path / "one.bdlg").stat().st_mode & 0o777 == 0o644


def test_failed_write_to_a_path_leaves_nothing_behind(tmp_path: Path) -> None:
    ghost = beadlog.BeadRef("urn:uuid:undeclared", "x-1")
    bad = beadlog.mint_event(
        "create",
        ghost,
        {"type": "task", "fields": {"title": "x"}},
        occurred_at=None,
        recorded_at=None,
        extracted_at="2026-01-01T00:00:00Z",
        source=("t", "r"),
        provenance="observed",
    )
    with pytest.raises(beadlog.ArchiveWriteError, match="not declared"):
        beadlog.write_archive(tmp_path / "x.bdlg", [bad], groups=[GROUP], generator=GENERATOR)
    assert list(tmp_path.iterdir()) == []


def test_entries_are_ordered_stored_first_and_normalised() -> None:
    members = _members(_write(index=True))
    names = [info.filename for info, _ in members]
    assert names[:2] == ["mimetype", "MANIFEST.json"]
    assert names[2:] == sorted(names[2:])
    assert names[2:] == [
        "checkpoints/conformance/part-0000.jsonl",
        "events/conformance/part-0000.jsonl",
        "index/recorded.jsonl",
        "schema/event.schema.json",
        "schema/manifest.schema.json",
    ]
    for info, data in members:
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
        assert info.external_attr >> 16 == 0o644
        assert not info.is_dir()
        assert info.extra == b""  # no ZIP64 field on small members: ZIP64 only when needed
        assert info.compress_type == (
            zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
        )
        if info.filename == "mimetype":
            assert data == beadlog.MEDIA_TYPE.encode("ascii")


def test_the_adr_worked_example_packs_to_the_committed_vectors() -> None:
    manifest_json = json.loads((EXAMPLE / "MANIFEST.json").read_text(encoding="utf-8"))
    events = [
        beadlog.BeadEvent.from_json(json.loads(line))
        for part in ("events/bh/part-0000.jsonl", "checkpoints/bh/part-0000.jsonl")
        for line in (EXAMPLE / part).read_text(encoding="utf-8").splitlines()
    ]
    buffer = io.BytesIO()
    written = beadlog.write_archive(
        buffer,
        events,
        groups=[beadlog.Group.from_json(group) for group in manifest_json["groups"]],
        generator=beadlog.Generator.from_json(manifest_json["generator"]),
        capabilities=beadlog.Capabilities.from_json(manifest_json["capabilities"]),
        created_at=manifest_json["created_at"],
        description=manifest_json["description"],
        index=True,
    )
    assert written.to_json() == manifest_json
    packed = {info.filename: data for info, data in _members(buffer.getvalue())}
    assert packed.pop("mimetype") == beadlog.MEDIA_TYPE.encode("ascii")
    assert packed.pop("MANIFEST.json") == (EXAMPLE / "MANIFEST.json").read_bytes()
    assert sorted(packed) == sorted(row["path"] for row in manifest_json["files"])
    for path, data in packed.items():
        assert data == (EXAMPLE / path).read_bytes(), path

    with beadlog.ArchiveReader(io.BytesIO(buffer.getvalue())) as reader:
        reader.verify(ids=True)
        assert [event.id for event in reader.events()] == [
            event.id for event in beadlog.sort_events(events)
        ]
        assert reader.has_index
        assert len(list(reader.index())) == 22


def test_packaged_schemas_are_byte_copies_of_the_published_ones() -> None:
    packaged = beadlog.archive.packaged_schemas()
    assert sorted(packaged) == ["schema/event.schema.json", "schema/manifest.schema.json"]
    for member, data in packaged.items():
        assert data == (V01 / member.split("/", 1)[1]).read_bytes()


def test_parts_are_size_partitioned_numbered_and_never_split_an_event() -> None:
    archive = _write(max_part_bytes=1200)
    with _open(archive) as reader:
        parts = [row["path"] for row in reader.manifest.files if row["role"] == "events"]
        assert len(parts) > 2
        assert parts == [f"events/conformance/part-{n:04d}.jsonl" for n in range(len(parts))]
        reader.verify(ids=True)
        assert tuple(reader.events()) == beadlog.sort_events(_events_without_erased())
    for info, data in _members(archive):
        if info.filename.startswith("events/"):
            assert data.endswith(b"\n")
            assert len(data) <= 1200 or data.count(b"\n") == 1
            for line in data.splitlines():
                json.loads(line)


def _events_without_erased() -> tuple[beadlog.BeadEvent, ...]:
    return beadlog.union_merge([_events()])


def test_an_oversized_single_event_still_gets_a_part_of_its_own() -> None:
    archive = _write(max_part_bytes=1)
    with _open(archive) as reader:
        counts = [row["records"] for row in reader.manifest.files if row["role"] == "events"]
        assert set(counts) == {1}
        reader.verify()


def test_too_many_parts_for_the_naming_scheme_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from beadlog.archive import writer

    monkeypatch.setattr(writer, "MAX_PARTS_PER_GROUP", 2)
    with pytest.raises(beadlog.ArchiveWriteError, match="parts"):
        _write(max_part_bytes=1)


def test_erased_content_never_reaches_a_part() -> None:
    erased = next(e for e in _events() if e.data.get("body", "").startswith("content that must"))
    archive = _write(index=True)
    for _, data in _members(archive):
        assert b"must not survive" not in data
        assert erased.id.encode() not in data.replace(b'"events":["' + erased.id.encode(), b"")
    with _open(archive) as reader:
        assert erased.id not in {event.id for event in reader.events()}


def test_capabilities_follow_the_content() -> None:
    with _open(_write()) as reader:
        caps = reader.manifest.capabilities
        assert caps.required == ("tombstones",)
        assert caps.optional == ("checkpoints",)
    with _open(_write(index=True)) as reader:
        assert "recorded-time-monotone-load" in reader.manifest.capabilities.optional
        assert reader.has_index
    with _open(_write(_events()[:3])) as reader:
        assert reader.manifest.capabilities == beadlog.Capabilities()
        assert not reader.has_index
        with pytest.raises(beadlog.ArchiveError, match="no recorded-time index"):
            list(reader.index())


def _seed_only_log() -> list[beadlog.BeadEvent]:
    events = [e for e in _events() if e.bead.id == "conf-a"]
    checkpoint = next(e for e in events if e.kind is beadlog.EventKind.CHECKPOINT)
    return [checkpoint]


def test_a_checkpoint_that_starts_a_beads_history_makes_checkpoint_seeded_required() -> None:
    with _open(_write(_seed_only_log())) as reader:
        assert reader.manifest.capabilities.required == ("checkpoint-seeded",)
        assert reader.manifest.capabilities.optional == ()
    # ... and a reader that cannot fold from a seed refuses it.
    with pytest.raises(beadlog.UnsupportedCapabilityError):
        _open(_write(_seed_only_log()), implemented_capabilities=["tombstones"])


def test_caller_capabilities_merge_and_correctness_ones_are_never_optional() -> None:
    caps = beadlog.Capabilities(required=("plan",), optional=("tombstones", "x-new-thing"))
    with _open(_write(capabilities=caps)) as reader:
        out = reader.manifest.capabilities
        assert out.required == ("plan", "tombstones")
        assert out.optional == ("checkpoints", "x-new-thing")


def test_plan_lamport_and_redaction_are_declared() -> None:
    plan = {"pass": "complexity", "schema_version": "1", "corpus_hash": "sha256:" + "0" * 64,
            "plan_hash": "sha256:" + "1" * 64}  # fmt: skip
    with _open(_write(plan=plan)) as reader:
        assert "plan" in reader.manifest.capabilities.optional
        assert reader.manifest.plan == plan
    events = [
        beadlog.mint_event(
            "comment",
            beadlog.BeadRef(GROUP.id, "b-1"),
            {"body": "hi"},
            occurred_at="2026-01-01T00:00:00Z",
            recorded_at=None,
            extracted_at="2026-01-02T00:00:00Z",
            source=("t", "r"),
            provenance="observed",
            lamport=3,
        )
    ]
    with _open(_write(events)) as reader:
        assert "lamport" in reader.manifest.capabilities.optional
    redacted = events[0].to_json() | {"redacted": ["/data/body"], "data": {}}
    redacted_event = beadlog.BeadEvent.from_json(redacted)
    with pytest.raises(beadlog.ArchiveWriteError, match="redaction.applied"):
        _write([redacted_event])
    policy = {"applied": True, "policy": "public", "withheld": {"members": ["/data/body"]}}
    with _open(_write([redacted_event], redaction=policy)) as reader:
        assert "redaction" in reader.manifest.capabilities.required


def test_writer_refuses_bad_inputs() -> None:
    with pytest.raises(beadlog.ArchiveWriteError, match="dir"):
        beadlog.write_archive(
            io.BytesIO(), [], groups=[beadlog.Group("g", "../x")], generator=GENERATOR
        )
    with pytest.raises(beadlog.ArchiveWriteError, match="two groups"):
        beadlog.write_archive(
            io.BytesIO(),
            [],
            groups=[beadlog.Group("g1", "same"), beadlog.Group("g2", "same")],
            generator=GENERATOR,
        )
    with pytest.raises(beadlog.ArchiveWriteError, match="at least one group"):
        beadlog.write_archive(io.BytesIO(), [], groups=[], generator=GENERATOR)
    with pytest.raises(beadlog.ArchiveWriteError, match="no index"):
        _write(capabilities=beadlog.Capabilities(optional=("recorded-time-monotone-load",)))
    with pytest.raises(beadlog.ArchiveWriteError, match="max_part_bytes"):
        _write(max_part_bytes=0)
    with pytest.raises(beadlog.ArchiveWriteError, match="schema"):
        _write(schemas={"../evil": b"{}"})
    with pytest.raises(beadlog.TimestampError):
        _write(created_at="2026-01-01 00:00:00")


def test_an_archive_with_no_events_is_valid() -> None:
    with _open(_write([])) as reader:
        reader.verify()
        assert list(reader.events()) == []
        assert reader.manifest.counts == {"events": 0, "beads": 0, "by_kind": {}}


# -- round trip -----------------------------------------------------------------------------


def test_round_trip_returns_identical_events_and_manifest() -> None:
    archive = _write(index=True)
    manifest, events = beadlog.read_archive(io.BytesIO(archive))
    assert events == beadlog.sort_events(_events_without_erased())
    assert [event.to_json() for event in events] == [
        event.to_json() for event in beadlog.sort_events(_events_without_erased())
    ]
    assert manifest.counts["events"] == len(events)
    assert set(manifest.counts["by_kind"]) == {str(kind) for kind in beadlog.EventKind}
    # A second write of what was read is the same archive.
    again = io.BytesIO()
    beadlog.write_archive(
        again,
        events,
        groups=list(manifest.groups),
        generator=manifest.generator,
        capabilities=manifest.capabilities,
        index=True,
    )
    assert again.getvalue() == archive


def test_round_trip_through_a_path_and_multiple_groups(tmp_path: Path) -> None:
    other = beadlog.Group("urn:uuid:other", "other")
    moved = [
        beadlog.BeadEvent.from_json(e.to_json() | {"bead": {"group": other.id, "id": e.bead.id}})
        for e in _events()
        if e.bead.id == "conf-c"  # a create and a retire: no edges, nothing group-specific
    ]
    own = [e for e in _events() if e.bead.id == "conf-a"]
    path = tmp_path / "multi.bdlg"
    beadlog.write_archive(path, own + moved, groups=[GROUP, other], generator=GENERATOR)
    with beadlog.ArchiveReader(path) as reader:
        reader.verify()
        assert {g for g in (e.bead.group for e in reader.events())} == {GROUP.id, other.id}
        mine = list(reader.events(group=GROUP.id))
        assert mine and all(e.bead.group == GROUP.id for e in mine)
        assert list(reader.events(checkpoints=False)) == [
            e for e in reader.events() if e.kind is not beadlog.EventKind.CHECKPOINT
        ]
    assert [info.filename for info, _ in _members(path.read_bytes())][2:] == [
        "checkpoints/conformance/part-0000.jsonl",
        "events/conformance/part-0000.jsonl",
        "events/other/part-0000.jsonl",
        "schema/event.schema.json",
        "schema/manifest.schema.json",
    ]


def test_events_are_streamed_and_a_tampered_part_raises_before_its_events() -> None:
    archive = _write(max_part_bytes=1500)
    parts = [i.filename for i, _ in _members(archive) if i.filename.startswith("events/")]
    assert len(parts) >= 2
    manifest = json.loads(_members(archive)[1][1])
    first_count = next(r for r in manifest["files"] if r["path"] == parts[0])["records"]
    names = [info.filename for info, _ in _members(archive)]
    members = {info.filename: data for info, data in _members(archive)}
    members[parts[-1]] = members[parts[-1]].replace(b"conf-", b"conf+", 1)  # same length
    broken = _pack([(name, members[name]) for name in names])
    with _open(broken) as reader:
        stream = reader.events(checkpoints=False)
        assert iter(stream) is stream  # a generator, not a list
        taken = [next(stream) for _ in range(first_count)]  # earlier parts stream out
        assert len(taken) == first_count
        with pytest.raises(beadlog.IntegrityError):
            list(stream)


# -- reader: refusals -----------------------------------------------------------------------


def _flip_same_length(members: dict[str, bytes]) -> None:
    members[PART] = members[PART].replace(b"Alpha", b"Omega", 1)


def test_tampered_part_is_refused_with_a_hash_mismatch() -> None:
    members = {i.filename: d for i, d in _members(_write())}
    _flip_same_length(members)
    archive = _pack([(n, members[n]) for n in [i.filename for i, _ in _members(_write())]])
    with _open(archive) as reader:
        with pytest.raises(beadlog.PartHashMismatchError, match=PART):
            list(reader.events())
        with pytest.raises(beadlog.PartHashMismatchError):
            reader.verify()
    assert issubclass(beadlog.PartHashMismatchError, beadlog.IntegrityError)


def test_part_with_a_different_length_is_a_size_mismatch() -> None:
    with pytest.raises(beadlog.MemberSizeMismatchError):
        _open(_edit(_write(), lambda m: {**m, PART: m[PART] + b"\n"}, refresh=False))


def test_record_count_must_match_the_manifest() -> None:
    def bump(meta: dict) -> None:
        next(row for row in meta["files"] if row["path"] == PART)["records"] += 1

    archive = _edit(_write(), manifest=bump)
    with _open(archive) as reader, pytest.raises(beadlog.RecordCountMismatchError):
        list(reader.events())


def test_manifest_counts_must_match_the_events() -> None:
    def lie(meta: dict) -> None:
        meta["counts"]["events"] += 1

    with _open(_edit(_write(), manifest=lie)) as reader:
        with pytest.raises(beadlog.RecordCountMismatchError, match="counts"):
            reader.verify()


def test_missing_mimetype_is_refused() -> None:
    members = [(i.filename, d) for i, d in _members(_write()) if i.filename != "mimetype"]
    with pytest.raises(beadlog.MissingMimetypeError):
        _open(_pack(members))
    # Present but not first is the same refusal.
    reordered = [m for m in members] + [("mimetype", beadlog.MEDIA_TYPE.encode())]
    with pytest.raises(beadlog.MissingMimetypeError, match="not the first"):
        _open(_pack(reordered))
    with pytest.raises(beadlog.MissingMimetypeError):
        _open(_pack([]))


@pytest.mark.parametrize(
    ("content", "stored"),
    [(b"application/zip", True), (beadlog.MEDIA_TYPE.encode() + b"\n", True), (b"x" * 60, True),
     (beadlog.MEDIA_TYPE.encode(), False)],
)  # fmt: skip
def test_wrong_mimetype_is_refused(content: bytes, stored: bool) -> None:
    members = [(i.filename, d) for i, d in _members(_write())]
    members[0] = ("mimetype", content)
    with pytest.raises(beadlog.InvalidMimetypeError):
        _open(_pack(members, stored=("mimetype",) if stored else ()))


def test_manifest_must_be_the_second_entry() -> None:
    members = [(i.filename, d) for i, d in _members(_write())]
    swapped = [members[0], members[2], members[1], *members[3:]]
    with pytest.raises(beadlog.MalformedArchiveError, match="second"):
        _open(_pack(swapped))


@pytest.mark.parametrize(
    "name",
    ["../evil.txt", "/etc/passwd", "events/../../x", "a//b", "./x", "C:/windows", "a\\b",
     "events/", "nested/./x"],
)  # fmt: skip
def test_zip_slip_names_are_refused(name: str) -> None:
    members = [(i.filename, d) for i, d in _members(_write())] + [(name, b"owned")]
    with pytest.raises(beadlog.UnsafePathError):
        _open(_pack(members))


def test_duplicate_member_names_are_refused() -> None:
    members = [(i.filename, d) for i, d in _members(_write())]
    with pytest.warns(UserWarning, match="Duplicate name"):
        archive = _pack([*members, members[2]])
    with pytest.raises(beadlog.UnsafePathError, match="duplicate"):
        _open(archive)


def test_check_member_name_accepts_ordinary_names() -> None:
    for name in ("mimetype", "events/bh/part-0000.jsonl", "a.b/c-d_e"):
        assert beadlog.check_member_name(name) == name


def test_oversized_entry_is_refused_by_its_declared_size() -> None:
    bomb = b"0" * (3 * 1024 * 1024)  # compresses to a few KiB
    archive = _edit(_write(), lambda m: {**m, PART: bomb}, refresh=False)
    limits = beadlog.ReadLimits(max_entry_bytes=1024 * 1024)
    with pytest.raises(beadlog.EntryTooLargeError, match=PART):
        _open(archive, limits=limits)
    # Under the default limit the same archive fails instead on the manifest's byte count.
    with pytest.raises(beadlog.MemberSizeMismatchError):
        _open(archive)


def test_total_decompressed_size_is_limited() -> None:
    limits = beadlog.ReadLimits(max_total_bytes=2000)
    with pytest.raises(beadlog.ArchiveTooLargeError):
        _open(_write(), limits=limits)


def test_oversized_manifest_is_refused() -> None:
    with pytest.raises(beadlog.EntryTooLargeError, match="MANIFEST"):
        _open(_write(), limits=beadlog.ReadLimits(max_manifest_bytes=100))


def test_entry_count_limit_is_enforced_from_the_end_record() -> None:
    archive = _write()
    with pytest.raises(beadlog.TooManyEntriesError):
        _open(archive, limits=beadlog.ReadLimits(max_entries=5))
    assert beadlog.archive.declared_entry_count(io.BytesIO(archive)) == 6
    assert beadlog.archive.declared_entry_count(io.BytesIO(b"not a zip")) is None


def test_overlong_lines_are_refused() -> None:
    with _open(_write(), limits=beadlog.ReadLimits(max_line_bytes=50)) as reader:
        with pytest.raises(beadlog.PartFormatError, match="over 50 bytes"):
            list(reader.events())


def test_unknown_required_capability_is_refused_before_anything_else() -> None:
    archive = _edit(_write(), manifest=lambda m: m["capabilities"]["required"].append("kind:alias"))
    with pytest.raises(beadlog.UnsupportedCapabilityError) as excinfo:
        _open(archive)
    assert excinfo.value.missing == ("kind:alias",)
    # It wins even over a stale manifest: nothing else about the archive is trusted.
    stale = _edit(
        _write(),
        lambda m: {**m, PART: b"garbage"},
        manifest=lambda m: m["capabilities"]["required"].append("kind:alias"),
        refresh=False,
    )
    with pytest.raises(beadlog.UnsupportedCapabilityError):
        _open(stale)
    # Optional unknown capabilities never refuse.
    optional = _edit(_write(), manifest=lambda m: m["capabilities"]["optional"].append("x-later"))
    _open(optional).close()
    # A reader may implement fewer capabilities and then refuses a known one it lacks.
    with pytest.raises(beadlog.UnsupportedCapabilityError):
        _open(_write(), implemented_capabilities=[])


def test_unknown_format_versions_and_malformed_manifests_are_refused() -> None:
    with pytest.raises(beadlog.ManifestError):
        _open(_edit(_write(), manifest=lambda m: m.update(format_version="0.2.0")))
    with pytest.raises(beadlog.ManifestError):
        _open(_edit(_write(), manifest=lambda m: m.pop("counts")))
    members = [(i.filename, d) for i, d in _members(_write())]
    members[1] = ("MANIFEST.json", b"{not json")
    with pytest.raises(beadlog.MalformedArchiveError, match="JSON"):
        _open(_pack(members))
    members[1] = ("MANIFEST.json", b'{"a": NaN}')
    with pytest.raises(beadlog.MalformedArchiveError):
        _open(_pack(members))


def test_not_a_zip_is_malformed() -> None:
    with pytest.raises(beadlog.MalformedArchiveError):
        _open(b"definitely not a zip file")
    with pytest.raises(beadlog.MalformedArchiveError):
        _open(b"")


def test_member_set_must_equal_the_manifest() -> None:
    with pytest.raises(beadlog.MalformedArchiveError, match="not in the manifest"):
        _open(_edit(_write(), lambda m: {**m, "events/conformance/extra.txt": b"x"}))
    with pytest.raises(beadlog.MalformedArchiveError, match="missing from the archive"):
        _open(_edit(_write(), lambda m: {k: v for k, v in m.items() if k != PART}, refresh=False))
    with pytest.raises(beadlog.MalformedArchiveError, match="required member"):
        _open(
            _edit(
                _write(),
                lambda m: {k: v for k, v in m.items() if k != "schema/event.schema.json"},
                manifest=lambda meta: meta["files"].pop(),
                refresh=False,
            )
        )


def test_manifest_file_rows_must_fit_their_role() -> None:
    def role(meta: dict) -> None:
        next(r for r in meta["files"] if r["path"] == PART)["role"] = "checkpoints"

    with pytest.raises(beadlog.MalformedArchiveError, match="does not fit"):
        _open(_edit(_write(), manifest=role))

    def undeclared_dir(meta: dict) -> None:
        meta["groups"][0]["dir"] = "elsewhere"

    with pytest.raises(beadlog.MalformedArchiveError, match="no declared group"):
        _open(_edit(_write(), manifest=undeclared_dir))


def test_part_numbers_must_not_have_gaps() -> None:
    archive = _write(max_part_bytes=1200)

    def renumber(members: dict[str, bytes]) -> dict[str, bytes]:
        moved = members.pop("events/conformance/part-0001.jsonl")
        members["events/conformance/part-9999.jsonl"] = moved
        return members

    def rename(meta: dict) -> None:
        for row in meta["files"]:
            if row["path"].endswith("part-0001.jsonl") and row["path"].startswith("events"):
                row["path"] = "events/conformance/part-9999.jsonl"

    with pytest.raises(beadlog.MalformedArchiveError, match="gaps"):
        _open(_edit(archive, renumber, manifest=rename))


def test_index_and_its_capability_must_agree() -> None:
    archive = _write(index=True)
    drop = lambda m: m["capabilities"]["optional"].remove("recorded-time-monotone-load")  # noqa: E731
    with pytest.raises(beadlog.MalformedArchiveError, match="index"):
        _open(_edit(archive, manifest=drop))


def _bad_line(change: Callable[[bytes], bytes]) -> beadlog.ArchiveReader:
    return _open(_edit(_write(), lambda m: {**m, PART: change(m[PART])}))


def test_malformed_event_lines_are_part_format_errors() -> None:
    first = lambda data: data.split(b"\n", 1)  # noqa: E731
    cases = {
        "not json": lambda d: b"{oops\n" + first(d)[1],
        "bom": lambda d: b"\xef\xbb\xbf" + d,
        "blank line": lambda d: b"\n" + d,
        "not utf8": lambda d: b"\xff\xfe\n" + first(d)[1],
        "not an object": lambda d: b"[1]\n" + first(d)[1],
        "missing members": lambda d: b'{"id": "sha256:' + b"0" * 64 + b'"}\n' + first(d)[1],
        "bare nan": lambda d: d.replace(b'"priority":2', b'"priority":NaN', 1),
    }
    for name, change in cases.items():
        with _bad_line(change) as reader, pytest.raises(beadlog.PartFormatError):
            list(reader.events())
        assert name


def test_a_final_line_without_lf_is_refused() -> None:
    with _bad_line(lambda d: d[:-1]) as reader:
        with pytest.raises(beadlog.PartFormatError, match="LF"):
            list(reader.events())


def test_event_in_the_wrong_group_or_directory_is_refused() -> None:
    def other_group(data: bytes) -> bytes:
        first, rest = data.split(b"\n", 1)
        event = json.loads(first)
        event["bead"]["group"] = "urn:uuid:elsewhere"
        return json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n" + rest

    with _bad_line(other_group) as reader, pytest.raises(beadlog.PartFormatError, match="group"):
        list(reader.events())

    checkpoint_row = "checkpoints/conformance/part-0000.jsonl"

    def swap(members: dict[str, bytes]) -> dict[str, bytes]:
        return {**members, checkpoint_row: members[PART].split(b"\n", 1)[0] + b"\n"}

    with _open(_edit(_write(), swap)) as reader:
        with pytest.raises(beadlog.PartFormatError, match="does not belong"):
            list(reader.events())


def test_events_out_of_part_order_are_refused() -> None:
    def reverse(data: bytes) -> bytes:
        return b"".join(reversed(data.splitlines(keepends=True)))

    with _bad_line(reverse) as reader, pytest.raises(beadlog.PartOrderError):
        list(reader.events())


def test_duplicate_event_ids_fail_verification() -> None:
    def doubled(data: bytes) -> bytes:
        lines = data.splitlines(keepends=True)
        return b"".join([lines[0], *lines])

    with (
        _bad_line(doubled) as reader,
        pytest.raises(beadlog.PartFormatError, match="more than once"),
    ):
        reader.verify()


def test_event_ids_can_be_verified() -> None:
    def alter(data: bytes) -> bytes:
        return data.replace(b'"title":"Alpha"', b'"title":"Alpho"', 1)

    with _bad_line(alter) as reader:
        reader.verify()  # shape, order and hashes are fine ...
        with pytest.raises(beadlog.EventIdMismatchError):
            reader.verify(ids=True)  # ... but the id no longer matches the content


def test_a_stale_or_lying_index_is_refused() -> None:
    index = "index/recorded.jsonl"
    archive = _write(index=True)

    def reorder(members: dict[str, bytes]) -> dict[str, bytes]:
        lines = members[index].splitlines(keepends=True)
        return {**members, index: b"".join(reversed(lines))}

    with _open(_edit(archive, reorder)) as reader, pytest.raises(beadlog.PartOrderError):
        list(reader.index())

    def wrong_line(members: dict[str, bytes]) -> dict[str, bytes]:
        lines = [json.loads(line) for line in members[index].splitlines()]
        lines[0]["line"] += 1
        return {
            **members,
            index: b"".join(json.dumps(x, sort_keys=True).encode() + b"\n" for x in lines),
        }

    with _open(_edit(archive, wrong_line)) as reader:
        with pytest.raises(beadlog.IndexMismatchError):
            reader.verify()

    def short(members: dict[str, bytes]) -> dict[str, bytes]:
        return {**members, index: members[index].split(b"\n", 1)[1]}

    with _open(_edit(archive, short)) as reader:
        with pytest.raises(beadlog.IndexMismatchError, match="entries"):
            reader.verify()


def test_a_damaged_compressed_stream_is_a_corrupt_member() -> None:
    archive = bytearray(_write())
    with zipfile.ZipFile(io.BytesIO(bytes(archive))) as zf:
        info = zf.getinfo(PART)
    # Flip a byte in the middle of the deflate stream, past the local header.
    start = info.header_offset + 30 + len(info.filename) + len(info.extra)
    archive[start + info.compress_size // 2] ^= 0xFF
    with _open(bytes(archive)) as reader, pytest.raises(beadlog.IntegrityError):
        list(reader.events())


def test_reader_use_after_close_is_an_error() -> None:
    reader = _open(_write())
    reader.close()
    with pytest.raises(beadlog.ArchiveError, match="closed"):
        list(reader.events())
    reader.close()


def test_specific_errors_are_distinct_types() -> None:
    errors = {
        beadlog.PartHashMismatchError,
        beadlog.UnsafePathError,
        beadlog.EntryTooLargeError,
        beadlog.UnsupportedCapabilityError,
        beadlog.MissingMimetypeError,
    }
    assert len(errors) == 5
    for first in errors:
        for second in errors - {first}:
            assert not issubclass(first, second)


# -- ZIP64 ----------------------------------------------------------------------------------


def _zip64_extras(archive: bytes) -> dict[str, bool]:
    return {info.filename: b"\x01\x00" == info.extra[:2] for info, _ in _members(archive)}


def test_writer_uses_zip64_exactly_when_a_member_needs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not any(_zip64_extras(_write()).values())
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1000)  # a "4 GiB" member is now 1000 bytes
    archive = _write(index=True)
    extras = _zip64_extras(archive)
    sizes = {info.filename: info.file_size for info, _ in _members(archive)}
    assert extras["events/conformance/part-0000.jsonl"]
    assert extras["schema/event.schema.json"]
    assert not extras["mimetype"]
    for name, needs in extras.items():
        assert needs == (sizes[name] * 1.05 > 1000), name
    with _open(archive) as reader:  # readers support ZIP64 transparently
        reader.verify(ids=True)
        assert tuple(reader.events()) == beadlog.sort_events(_events_without_erased())


def test_reader_supports_zip64_archives_from_other_writers() -> None:
    members = [(i.filename, d) for i, d in _members(_write(index=True))]
    forced = _pack(members, zip64=True)
    assert forced != _pack(members)
    with _open(forced) as reader:
        reader.verify(ids=True)
        assert len(list(reader.events())) == len(_events_without_erased())


def test_entry_count_is_read_from_the_zip64_end_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 2)  # 6 entries now need ZIP64 records
    archive = bytearray(_write())
    assert b"PK\x06\x06" in archive and b"PK\x06\x07" in archive[-120:]
    # A real archive with > 65535 entries marks the 16-bit counts of the classic end record
    # as 0xFFFF and keeps the truth in the ZIP64 record; mark them the same way.
    end = archive.rfind(b"PK\x05\x06")
    archive[end + 8 : end + 12] = b"\xff\xff\xff\xff"
    marked = bytes(archive)
    assert beadlog.archive.declared_entry_count(io.BytesIO(marked)) == 6
    with pytest.raises(beadlog.TooManyEntriesError):
        _open(marked, limits=beadlog.ReadLimits(max_entries=5))
    with _open(marked, limits=beadlog.ReadLimits(max_entries=6)) as reader:
        reader.verify()


def test_a_forged_multi_gigabyte_zip64_entry_is_refused_by_its_declared_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic large-entry fixture: no 5 GiB data, just a central-directory ZIP64 extra that
    claims it, as a decompression bomb or a genuinely huge part would."""
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1000)
    archive = bytearray(_write())
    forged, at = 5 * 1024**3, 0
    while (at := archive.find(b"PK\x01\x02", at)) >= 0:
        name_length, extra_length = struct.unpack_from("<HH", archive, at + 28)
        name = bytes(archive[at + 46 : at + 46 + name_length]).decode()
        if name == PART:
            extra = at + 46 + name_length
            header_id, _ = struct.unpack_from("<HH", archive, extra)
            assert header_id == 1 and extra_length >= 12  # a ZIP64 extra field
            struct.pack_into("<Q", archive, extra + 4, forged)  # uncompressed size
            break
        at += 4
    else:  # pragma: no cover
        raise AssertionError("central directory entry not found")
    with pytest.raises(beadlog.EntryTooLargeError, match=str(forged)):
        _open(bytes(archive))
    roomy = beadlog.ReadLimits(max_entry_bytes=8 * 1024**3, max_total_bytes=16 * 1024**3)
    with pytest.raises(beadlog.MemberSizeMismatchError, match=str(forged)):
        _open(bytes(archive), limits=roomy)
