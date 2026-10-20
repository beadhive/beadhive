"""Restore-attested native backups: take, list, verify, restore (bh-ksmt0.4).

Implements ``remote-migration-transaction-adr.md`` P2 (``snapshot``) and P3 (``attest-backup``) as
reusable primitives, plus the operator verbs ``bh backup snapshot|list|verify|restore``.

A snapshot is ONE dated set in the ``migrate`` backup root (bh-5009a) holding:

``issues.jsonl``    the JSONL floor (``bd export --all``), count-consistent with the store
``dolt-native/``    a NATIVE Dolt backup taken over the connection (never a tar)
``registry.json``   every Dolt-level remote and backup registration (0600, real URLs)
``manifest.json``   mandatory: real schema version, strong source fingerprint, tool versions,
                    checksums, the restore attestation

The backup is *attested* by restoring it into a uniquely named scratch database and requiring the
complete (strong-tier) fingerprint to equal the source's; the scratch is dropped afterwards and
the ``.dolt_dropped_databases`` copy that leaves is reported (bh-2hvmd). A backup that was not
restored is unverified.

bd has a SINGLE ``default`` backup slot per database (bh-1sug). The snapshot registers its own
unique per-transaction destination in that slot, and :func:`preserved_backup_slot` puts the prior
registration back -- name, address and bd's two bookkeeping files -- on EVERY exit path, proving
it by read-back through :meth:`Engine.restore_registry`. A failed restore of the slot is a
failure of the whole snapshot, never a warning.

A success report is never produced from a best-effort manifest: :func:`validate_manifest` fails on
any missing or placeholder field, and the manifest is re-read from disk before ``ok`` is set.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import backup as backup_mod
from . import corpus_fingerprint as cf
from . import registry_snapshot
from .backup_scratch import (
    DroppedCopy,
    ScratchError,
    ScratchStore,
    make_scratch,
    record_counts,
    scratch_facts,
    scratch_name,
    scrub,
    source_facts,
)
from .migration_result import HiveMigrationOutcome
from .run import run

SCHEMA = "bh.native-backup-manifest/1"
KIND = "native-snapshot"
JSONL_NAME = "issues.jsonl"
NATIVE_NAME = "dolt-native"
REGISTRY_NAME = "registry.json"
#: Volatile advisory files inside a Dolt backup directory that never carry data.
_TREE_EXCLUDE = frozenset({"LOCK"})
#: bd's two bookkeeping files for its single backup slot.
_SLOT_FILES = ("dolt-backup.json", "dolt-backup-state.json")

REQUIRED_FIELDS = (
    "schema",
    "kind",
    "hive",
    "prefix",
    "taken_at",
    "bh_version",
    "verified",
    "artifacts",
    "txn",
    "dolt_mode",
    "schema_version",
    "tool_versions",
    "fingerprint",
    "checksums",
    "jsonl",
    "prior_backup",
)
REQUIRED_ARTIFACTS = (JSONL_NAME, NATIVE_NAME, REGISTRY_NAME)
REQUIRED_TOOLS = ("bh", "bd", "dolt")
_PLACEHOLDERS = ("", "unknown", None)


def _now() -> datetime:
    return datetime.now(UTC)


def _token() -> str:
    return secrets.token_hex(3)


# ---- checksums -----------------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def tree_sha256(root: Path) -> str:
    """Deterministic digest of a directory: sorted relative paths with each file's sha256."""
    root = Path(root)
    lines = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink() and p.name not in _TREE_EXCLUDE:
            lines.append(f"{p.relative_to(root).as_posix()}\0{file_sha256(p)}")
    return "sha256:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()


def artifact_checksum(path: Path) -> str:
    return tree_sha256(path) if path.is_dir() else file_sha256(path)


def _artifact_size(path: Path) -> int:
    return backup_mod._dir_size(path) if path.is_dir() else path.stat().st_size


def checksums_of(set_dir: Path, names: Sequence[str] = REQUIRED_ARTIFACTS) -> dict[str, str]:
    return {n: artifact_checksum(Path(set_dir) / n) for n in names}


# ---- tool versions -------------------------------------------------------------------------


def _version_of(argv: list[str], pattern: str) -> str:
    try:
        res = run(argv, check=False, capture=True, timeout=30)
    except Exception:  # noqa: BLE001 - absence is reported as an unknown tool, not a crash
        return ""
    m = re.search(pattern, f"{res.stdout or ''}\n{res.stderr or ''}")
    return m.group(1) if m else ""


def detect_tool_versions(engine=None) -> dict[str, str]:
    """``bh``, ``bd`` and ``dolt`` versions; an undetectable tool is empty, which the manifest
    validator rejects (a success is never reported with a guessed version)."""
    from . import engine as engine_mod

    eng = engine or engine_mod.get_engine({})
    try:
        res = eng.invoke(["--version"], capture=True, timeout=30, hive_aware=False)
        text = f"{res.stdout or ''}\n{res.stderr or ''}"
    except Exception:  # noqa: BLE001 - absence is reported as an unknown tool, not a crash
        text = ""
    m = re.search(r"bd version (\S+)", text)
    return {
        "bh": backup_mod._bh_version(),
        "bd": m.group(1) if m else "",
        "dolt": _version_of(["dolt", "version"], r"dolt version (\S+)"),
    }


# ---- manifest ------------------------------------------------------------------------------


class ManifestError(RuntimeError):
    pass


def write_manifest_strict(set_dir: Path, manifest: Mapping) -> Path:
    """Atomic, fsynced, and RAISING: unlike :func:`backup.write_manifest` this is the writer a
    reported success depends on, so an unwritable manifest is a failure."""
    path = Path(set_dir) / backup_mod.MANIFEST_NAME
    fd, tmp = tempfile.mkstemp(dir=set_dir, prefix=".manifest-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(manifest), fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def load_manifest(set_dir: Path) -> dict:
    """The manifest, or ``ManifestError`` (missing, unreadable, or not an object)."""
    path = Path(set_dir) / backup_mod.MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(
            f"{path} is missing: this set has no manifest, so it is unverified"
        ) from exc
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path} is not a JSON object")
    return data


def validate_manifest(m: Mapping) -> list[str]:
    """Every problem that makes a manifest unfit to back a reported success (empty = fit)."""
    problems: list[str] = []
    for key in REQUIRED_FIELDS:
        if key not in m or m[key] in _PLACEHOLDERS:
            problems.append(f"missing field {key!r}")
    if problems:
        return problems
    if m["schema"] != SCHEMA:
        problems.append(f"unsupported manifest schema {m['schema']!r}")
    if m["kind"] != KIND:
        problems.append(f"unexpected manifest kind {m['kind']!r}")
    if not isinstance(m["verified"], bool):
        problems.append("'verified' is not a boolean")
    sv = m["schema_version"]
    if isinstance(sv, bool) or not isinstance(sv, int) or sv < 1:
        problems.append(f"schema_version {sv!r} is not a real store schema version")
    tools = m["tool_versions"]
    if not isinstance(tools, Mapping):
        problems.append("tool_versions is not an object")
    else:
        for tool in REQUIRED_TOOLS:
            if tools.get(tool) in _PLACEHOLDERS:
                problems.append(f"tool version for {tool!r} is missing")
    arts = m["artifacts"]
    sums = m["checksums"]
    for name in REQUIRED_ARTIFACTS:
        if not isinstance(arts, Mapping) or name not in arts:
            problems.append(f"artifact {name!r} is not recorded")
        if not isinstance(sums, Mapping) or not str(sums.get(name, "")).startswith("sha256:"):
            problems.append(f"checksum for {name!r} is missing")
    try:
        fp = cf.Fingerprint.from_dict(m["fingerprint"])
    except (cf.FingerprintError, KeyError, TypeError) as exc:
        problems.append(f"fingerprint is invalid: {exc}")
    else:
        if fp.tier != cf.TIER_STRONG:
            problems.append(f"fingerprint tier is {fp.tier!r}, not strong")
        if fp.components.get("schema_version") != sv:
            problems.append("manifest schema_version differs from the fingerprint's")
    jsonl = m["jsonl"]
    if not isinstance(jsonl, Mapping) or jsonl.get("issue_records") != jsonl.get(
        "expected_records"
    ):
        problems.append("JSONL record count is not consistent with the store")
    if m["verified"] is True:
        att = m.get("attestation")
        if not isinstance(att, Mapping) or att.get("matched") is not True:
            problems.append("verified is true without a matching restore attestation")
        else:
            try:
                if att.get("restored_digest") != cf.Fingerprint.from_dict(m["fingerprint"]).digest:
                    problems.append("attestation digest differs from the source fingerprint")
            except (cf.FingerprintError, KeyError, TypeError):
                pass
    return problems


# ---- the single backup slot ----------------------------------------------------------------


@dataclass
class SlotReport:
    """Outcome of :func:`preserved_backup_slot`; filled in by its ``finally``."""

    prior: object = None  # the RegistrySnapshot saved before the slot was touched
    restored: bool = False
    detail: str = ""
    files_restored: bool = True
    saved_files: dict | None = None


def _read_slot_files(hive_dir: Path) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for name in _SLOT_FILES:
        try:
            out[name] = (backup_mod.hive_backup_dir(hive_dir).parent / name).read_bytes()
        except OSError:
            out[name] = None
    return out


def _restore_slot_files(hive_dir: Path, saved: Mapping[str, bytes | None]) -> bool:
    ok = True
    for name, data in saved.items():
        path = backup_mod.hive_backup_dir(hive_dir).parent / name
        try:
            if data is None:
                path.unlink(missing_ok=True)
            elif not path.is_file() or path.read_bytes() != data:
                path.write_bytes(data)
            current = path.read_bytes() if path.is_file() else None
        except OSError:
            ok = False
            continue
        ok = ok and current == data
    return ok


@contextlib.contextmanager
def preserved_backup_slot(engine, hive_dir: Path, snapshot, report: SlotReport):
    """Guarantee bd's single ``default`` backup slot is exactly as it was on exit.

    ``snapshot`` is the registry read BEFORE the slot is touched. The ``finally`` re-applies it
    through :meth:`Engine.restore_registry` (which reads back and diffs) and puts bd's bookkeeping
    files back byte-for-byte. It never raises: the verdict is on ``report`` (``restored``) and the
    caller turns a False into a failure of the whole operation. It runs on success, on any
    exception, and on ``KeyboardInterrupt``.
    """
    report.prior = snapshot
    report.saved_files = saved = _read_slot_files(hive_dir)
    try:
        yield snapshot
    finally:
        try:
            outcome = engine.restore_registry(hive_dir, snapshot)
            report.restored = bool(outcome.ok)
            report.detail = (
                "prior backup registration restored and read back"
                if outcome.ok
                else f"restore of the prior registration failed: {outcome.error}"
            )
        except Exception as exc:  # noqa: BLE001 - the verdict goes on the report
            report.restored = False
            report.detail = f"restore of the prior registration raised: {type(exc).__name__}"
        if saved is not None:
            report.files_restored = _restore_slot_files(hive_dir, saved)
            if not report.files_restored:
                report.restored = False
                report.detail += "; bd's backup bookkeeping files could not be restored"


# ---- results -------------------------------------------------------------------------------


@dataclass
class SnapshotResult:
    ok: bool = False
    hive: str = ""
    txn: str = ""
    set_dir: Path | None = None
    manifest: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    slot: SlotReport = field(default_factory=SlotReport)
    dropped: DroppedCopy | None = None
    scratch_database: str = ""
    phase_reached: str = ""
    attested: bool = False

    def outcome(self) -> HiveMigrationOutcome:
        state = "backup-attested" if self.ok else "needs-operator" if self.set_dir else "aborted"
        return HiveMigrationOutcome(
            hive=self.hive,
            txn=self.txn,
            phase_reached=self.phase_reached,
            state=state,
            ok=self.ok,
            evidence_paths=(str(self.set_dir),) if self.set_dir else (),
            detail="; ".join(self.errors),
        )

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "hive": self.hive,
            "txn": self.txn,
            "set": str(self.set_dir) if self.set_dir else "",
            "phase_reached": self.phase_reached,
            "attested": self.attested,
            "scratch_database": self.scratch_database,
            "slot_restored": self.slot.restored,
            "slot_detail": self.slot.detail,
            "dropped_copy": self.dropped.to_dict() if self.dropped else None,
            "errors": list(self.errors),
            "notes": list(self.notes),
        }


def _fail(res: SnapshotResult, msg: str) -> SnapshotResult:
    res.errors.append(scrub(msg))
    return res


def _jsonl_counts(path: Path) -> tuple[int, int]:
    """``(issue records, other records)`` in an export."""
    issues = other = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                kind = json.loads(line).get("_type", "issue")
            except (ValueError, AttributeError):
                kind = "corrupt"
            if kind == "issue":
                issues += 1
            else:
                other += 1
    return issues, other


# ---- P2 + P3 -------------------------------------------------------------------------------


def take_snapshot(
    cfg,
    hive_dir: Path,
    *,
    slug: str,
    prefix: str,
    engine=None,
    scratch: ScratchStore | None = None,
    clock: Callable[[], datetime] = _now,
    token: Callable[[], str] = _token,
    tables: Sequence[str] = cf.DEFAULT_TABLES,
    tool_versions: Callable[[], dict] | None = None,
    hq_descriptor: Mapping | None = None,
    dry_run: bool = False,
    attest_restore: bool = True,
    purpose: str = "",
) -> SnapshotResult:
    """Take a snapshot set and attest it. Never raises on an operational failure: the result
    carries ``ok`` and ``errors``. ``dry_run`` plans only (zero mutation)."""
    from . import engine as engine_mod

    hive_dir = Path(hive_dir)
    engine = engine or engine_mod.get_engine(cfg)
    now = clock().astimezone(UTC)
    txn = f"{backup_mod.stamp(now)}-{token()}"
    res = SnapshotResult(hive=slug, txn=txn)

    # -- preflight: nothing is written until the registry is readable and the source can be
    # fingerprinted; the slot's prior registration is saved before anything touches it.
    try:
        cap = registry_snapshot.capture(
            hive_dir, hive=slug, engine=engine, hq_descriptor=hq_descriptor, clock=lambda: now
        )
    except registry_snapshot.RegistryUnreadable as exc:
        return _fail(res, str(exc))
    if dry_run:
        res.notes.append("dry run: nothing was written")
        res.ok = True
        return res
    try:
        tools = tool_versions() if tool_versions else detect_tool_versions(engine)
    except Exception as exc:  # noqa: BLE001
        return _fail(res, f"tool versions unavailable: {type(exc).__name__}")
    base = backup_mod.migrate_hive_root(slug, cfg)
    set_dir = base / backup_mod.stamp(now)
    try:
        base.mkdir(parents=True, exist_ok=True)
        try:
            set_dir.mkdir()
        except FileExistsError:
            set_dir = base / f"{backup_mod.stamp(now)}-{txn.rsplit('-', 1)[-1]}"
            set_dir.mkdir()
    except OSError as exc:
        return _fail(res, f"cannot create the backup set: {exc}")
    res.set_dir = set_dir

    def abort(msg: str) -> SnapshotResult:
        # P2 rollback: this transaction's set is new and unique; remove only it.
        _fail(res, msg)
        if res.phase_reached == "":
            with contextlib.suppress(OSError):
                shutil.rmtree(set_dir)
            res.set_dir = None
        return res

    try:
        cap.write(set_dir / REGISTRY_NAME)
    except OSError as exc:
        return abort(f"cannot persist the registry capture: {exc}")

    dolt_mode = "embedded" if _is_embedded(hive_dir) else "server"
    native = set_dir / NATIVE_NAME
    native_url = f"file://{native}"
    jsonl = set_dir / JSONL_NAME

    # -- JSONL floor
    exp = engine.invoke(
        ["export", "-o", str(jsonl), "--all"], cwd=hive_dir, capture=True, timeout=600
    )
    if exp.returncode != 0 or not jsonl.is_file():
        return abort(f"JSONL export failed: {(getattr(exp, 'stderr', '') or '').strip()[-200:]}")
    issue_records, _other = _jsonl_counts(jsonl)
    if issue_records == 0 and jsonl.stat().st_size == 0:
        return abort("JSONL export is empty")

    try:
        fp0 = cf.compute(
            source_facts(engine, hive_dir, tables),
            cf.TIER_STRONG,
            tables=tables,
            scope=cf.SCOPE_WORKING,
        )
        expected = record_counts(
            lambda q: engine.query_rows(hive_dir, q), lambda t: f"`{t}`", database=None
        )
    except (cf.FingerprintError, RuntimeError) as exc:
        return abort(f"source fingerprint unavailable: {exc}")
    if issue_records != expected:
        return abort(
            f"JSONL holds {issue_records} issue records but the store holds {expected}; "
            "refusing an inconsistent floor"
        )

    # -- native backup in the (single) default slot, which is ALWAYS put back
    res.phase_reached = "snapshot-started"
    backup_error = ""
    with preserved_backup_slot(engine, hive_dir, cap.snapshot, res.slot):
        added = engine.add_backup(hive_dir, native_url, replace=True)
        if added.returncode != 0:
            backup_error = f"cannot register the snapshot destination: {_tail(added)}"
        else:
            synced = engine.backup(hive_dir, native_url)
            if synced.returncode != 0:
                backup_error = f"native backup failed: {_tail(synced)}"
    if not res.slot.restored:
        res.errors.append(scrub(f"backup slot NOT restored: {res.slot.detail}"))
        res.errors.append(
            f"needs-operator: reapply {set_dir / REGISTRY_NAME} (the saved registration)"
        )
        return res
    if backup_error:
        res.phase_reached = ""
        return abort(backup_error)
    if not native.is_dir() or not any(native.iterdir()):
        res.phase_reached = ""
        return abort("native backup directory is empty")
    res.phase_reached = "snapshot"

    # -- the source must not have moved while we copied it
    try:
        fp1 = cf.compute(
            source_facts(engine, hive_dir, tables),
            cf.TIER_STRONG,
            tables=tables,
            scope=cf.SCOPE_WORKING,
        )
    except (cf.FingerprintError, RuntimeError) as exc:
        return _fail(res, f"source fingerprint unavailable after backup: {exc}")
    moved = cf.compare(fp0, fp1)
    if not moved.equal:
        return _fail(res, f"source changed during the snapshot: {', '.join(moved.differing)}")

    sums = checksums_of(set_dir)
    manifest = {
        "schema": SCHEMA,
        "kind": KIND,
        "hive": slug,
        "prefix": prefix,
        "taken_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bh_version": tools.get("bh"),
        "verified": False,
        "artifacts": {n: _artifact_size(set_dir / n) for n in REQUIRED_ARTIFACTS},
        "txn": txn,
        "dolt_mode": dolt_mode,
        "schema_version": fp0.components["schema_version"],
        "tool_versions": dict(tools),
        "fingerprint": fp0.to_dict(),
        "checksums": sums,
        "jsonl": {"issue_records": issue_records, "expected_records": expected},
        "prior_backup": _prior_description(cap.snapshot),
        **({"purpose": purpose} if purpose else {}),
    }
    try:
        write_manifest_strict(set_dir, manifest)
    except OSError as exc:
        return _fail(res, f"cannot write the manifest: {exc}")
    res.manifest = manifest

    if not attest_restore:
        # A safety copy (pre-restore): the native backup completed, was checksummed and the
        # manifest re-reads valid, but it was NOT restore-attested and says so (verified=false).
        try:
            if checksums_of(set_dir) != sums:
                return _fail(res, "the backup set changed while it was being written")
            problems = validate_manifest(load_manifest(set_dir))
        except (OSError, ManifestError) as exc:
            return _fail(res, f"cannot re-read the manifest: {exc}")
        if problems:
            return _fail(res, "manifest is not valid: " + "; ".join(problems))
        res.manifest = manifest
        res.phase_reached = "snapshot"
        res.ok = res.slot.restored and not res.errors
        return res

    # -- P3: restore into scratch, compare the COMPLETE fingerprint, drop the scratch
    res = attest(
        res,
        cfg,
        hive_dir,
        manifest,
        engine=engine,
        scratch=scratch,
        token=token,
        tables=tables,
        clock=clock,
        prefix=prefix,
    )
    return res


def _tail(proc) -> str:
    text = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
    return scrub(text.splitlines()[-1] if text else f"exit {proc.returncode}")


def _is_embedded(hive_dir: Path) -> bool:
    from . import store_locator

    return store_locator.is_embedded_mode(hive_dir)


def _prior_description(snapshot) -> dict:
    slot = snapshot.backup("default")
    return {
        "registered": slot is not None,
        "name": "default",
        "url": slot.redacted if slot is not None else "",
    }


def attest(
    res: SnapshotResult,
    cfg,
    hive_dir: Path,
    manifest: dict,
    *,
    engine,
    scratch: ScratchStore | None,
    token: Callable[[], str],
    tables: Sequence[str],
    clock: Callable[[], datetime],
    prefix: str,
) -> SnapshotResult:
    """Restore ``res.set_dir``'s native backup into a scratch database and finish the manifest."""
    set_dir = Path(res.set_dir)
    native_url = f"file://{set_dir / NATIVE_NAME}"
    source = cf.Fingerprint.from_dict(manifest["fingerprint"])
    store = scratch or make_scratch(engine, hive_dir, set_dir, res.txn)
    name = scratch_name(prefix, token())
    res.scratch_database = name
    attestation: dict = {
        "scratch_database": name,
        "attested_at": clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tier": cf.TIER_STRONG,
        "source_digest": source.digest,
        "matched": False,
        "scratch_dropped": False,
    }
    handle = None
    try:
        handle = store.restore(native_url, name)
        facts, restored_records = scratch_facts(store, handle, tables)
        restored = cf.compute_like(facts, source, tables=tables)
        diff = cf.compare(source, restored)
        attestation["restored_digest"] = restored.digest
        attestation["matched"] = (
            bool(diff.equal) and restored_records == manifest["jsonl"]["expected_records"]
        )
        if not diff.equal:
            res.errors.append(
                "restored fingerprint differs from the source in: " + ", ".join(diff.differing)
            )
        elif not attestation["matched"]:
            res.errors.append("restored record count differs from the source's")
    except (ScratchError, cf.FingerprintError) as exc:
        res.errors.append(scrub(f"restore attestation failed: {exc}"))
    finally:
        if handle is not None:
            try:
                res.dropped = store.drop(handle)
                attestation["scratch_dropped"] = True
                attestation["dropped_copy"] = res.dropped.to_dict()
            except ScratchError as exc:
                res.errors.append(scrub(f"scratch database NOT dropped: {exc}"))
        with contextlib.suppress(OSError, AttributeError):
            from .backup_scratch import cleanup_embedded_root

            cleanup_embedded_root(set_dir, res.txn)
    res.attested = bool(attestation["matched"])

    # restoring must not have changed what we are vouching for
    try:
        after = checksums_of(set_dir)
    except OSError as exc:
        res.errors.append(f"cannot re-checksum the set: {exc}")
        after = {}
    if after != manifest["checksums"]:
        res.errors.append("the backup set changed while it was being restored")
        res.attested = False

    final = dict(manifest, verified=res.attested, attestation=attestation)
    try:
        write_manifest_strict(set_dir, final)
        reread = load_manifest(set_dir)
    except (OSError, ManifestError) as exc:
        res.errors.append(f"cannot write the final manifest: {exc}")
        return res
    problems = validate_manifest(reread)
    if problems:
        res.errors.append("manifest is not fit to back a success: " + "; ".join(problems))
    res.manifest = reread
    res.phase_reached = "attest-backup" if res.attested else res.phase_reached
    res.ok = (
        res.attested
        and attestation["scratch_dropped"]
        and res.slot.restored
        and not problems
        and not res.errors
    )
    return res


# ---- list ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SetInfo:
    kind: str
    hive: str
    path: Path
    name: str
    taken_at: str
    verified: bool | None  # None: no manifest, so unknown
    problems: tuple[str, ...] = ()
    levels: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "hive": self.hive,
            "path": str(self.path),
            "name": self.name,
            "taken_at": self.taken_at,
            "verified": self.verified,
            "problems": list(self.problems),
            "levels": list(self.levels),
        }


def _info_for_migrate_set(slug: str, d: Path) -> SetInfo:
    try:
        m = load_manifest(d)
    except ManifestError:
        legacy = backup_mod.read_manifest(d)
        return SetInfo(
            "migrate",
            slug,
            d,
            d.name,
            str(legacy.get("taken_at", "")),
            None if not legacy else bool(legacy.get("verified")),
            () if legacy else ("no manifest",),
            _levels(d),
        )
    if m.get("kind") != KIND:
        return SetInfo(
            "migrate",
            slug,
            d,
            d.name,
            str(m.get("taken_at", "")),
            bool(m.get("verified")),
            (),
            _levels(d),
        )
    problems = tuple(validate_manifest(m))
    return SetInfo(
        KIND,
        slug,
        d,
        d.name,
        str(m.get("taken_at", "")),
        bool(m.get("verified")) and not problems,
        problems,
        _levels(d),
    )


def _levels(d: Path) -> tuple[str, ...]:
    out = []
    if (d / JSONL_NAME).is_file():
        out.append("jsonl")
    if (d / NATIVE_NAME).is_dir():
        out.append("native")
    return tuple(out)


def list_sets(cfg, *, hive: str = "") -> list[SetInfo]:
    """Every dated set in the migrate root (optionally one hive's), newest first per hive."""
    root = backup_mod.migrate_root(cfg)
    out: list[SetInfo] = []
    for hive_root in backup_mod._hive_dirs(root):
        slug = hive_root.relative_to(root).as_posix()
        if hive and slug != hive:
            continue
        out.extend(_info_for_migrate_set(slug, d) for d in backup_mod._dated_dirs(hive_root))
    return out


# ---- verify --------------------------------------------------------------------------------


@dataclass
class VerifyReport:
    ok: bool = False
    set_dir: Path | None = None
    problems: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    attested: bool | None = None  # None: restore test not requested
    dropped: DroppedCopy | None = None
    scratch_database: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "set": str(self.set_dir) if self.set_dir else "",
            "problems": list(self.problems),
            "checked": list(self.checked),
            "attested": self.attested,
            "scratch_database": self.scratch_database,
            "dropped_copy": self.dropped.to_dict() if self.dropped else None,
        }


def verify_set(
    set_dir: Path,
    *,
    cfg=None,
    hive_dir: Path | None = None,
    engine=None,
    scratch: ScratchStore | None = None,
    restore_test: bool = False,
    token: Callable[[], str] = _token,
    tables: Sequence[str] | None = None,
) -> VerifyReport:
    """Structural verification (manifest fitness + checksums + JSONL count), and with
    ``restore_test`` a fresh restore into scratch compared with the manifest's fingerprint."""
    set_dir = Path(set_dir)
    rep = VerifyReport(set_dir=set_dir)
    try:
        m = load_manifest(set_dir)
    except ManifestError as exc:
        rep.problems.append(str(exc))
        return rep
    rep.problems.extend(validate_manifest(m))
    rep.checked.append("manifest")
    if rep.problems:
        return rep
    for name, want in m["checksums"].items():
        path = set_dir / name
        if not path.exists():
            rep.problems.append(f"artifact {name!r} is missing")
            continue
        if artifact_checksum(path) != want:
            rep.problems.append(f"checksum of {name!r} differs from the manifest")
        else:
            rep.checked.append(f"checksum:{name}")
    issues, _ = _jsonl_counts(set_dir / JSONL_NAME) if (set_dir / JSONL_NAME).is_file() else (-1, 0)
    if issues != m["jsonl"]["issue_records"]:
        rep.problems.append(
            f"{JSONL_NAME} holds {issues} issue records; the manifest recorded "
            f"{m['jsonl']['issue_records']}"
        )
    else:
        rep.checked.append("jsonl-count")
    if m.get("verified") is not True and m.get("purpose") != "pre-restore":
        rep.problems.append("the set was never restore-attested (verified is false)")
    if rep.problems or not restore_test:
        rep.ok = not rep.problems
        return rep
    # fresh restore test
    if hive_dir is None or cfg is None:
        rep.problems.append("a restore test needs the hive it belongs to")
        return rep
    from . import engine as engine_mod

    engine = engine or engine_mod.get_engine(cfg)
    stub = SnapshotResult(hive=m["hive"], txn=f"verify-{token()}", set_dir=set_dir)
    stub.slot.restored = True
    store = scratch or make_scratch(engine, hive_dir, set_dir, stub.txn)
    name = scratch_name(m["prefix"], token())
    rep.scratch_database = name
    handle = None
    try:
        handle = store.restore(f"file://{set_dir / NATIVE_NAME}", name)
        want_fp = cf.Fingerprint.from_dict(m["fingerprint"])
        use_tables = tuple(tables or want_fp.tables)
        facts, _records = scratch_facts(store, handle, use_tables)
        got = cf.compute_like(facts, want_fp, tables=use_tables)
        diff = cf.compare(want_fp, got)
        rep.attested = diff.equal
        if not diff.equal:
            rep.problems.append("restored fingerprint differs in: " + ", ".join(diff.differing))
    except (ScratchError, cf.FingerprintError) as exc:
        rep.attested = False
        rep.problems.append(scrub(f"restore test failed: {exc}"))
    finally:
        if handle is not None:
            try:
                rep.dropped = store.drop(handle)
            except ScratchError as exc:
                rep.problems.append(scrub(f"scratch database NOT dropped: {exc}"))
        with contextlib.suppress(OSError, AttributeError):
            from .backup_scratch import cleanup_embedded_root

            cleanup_embedded_root(set_dir, stub.txn)
    rep.ok = not rep.problems
    return rep


# ---- restore -------------------------------------------------------------------------------


@dataclass
class RestoreOutcome:
    ok: bool = False
    dry_run: bool = True
    actions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    registry_restored: bool | None = None
    fingerprint_matched: bool | None = None
    safety_set: str = ""  # the pre-restore snapshot of the live store (dry run: where it will go)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "actions": list(self.actions),
            "problems": list(self.problems),
            "registry_restored": self.registry_restored,
            "fingerprint_matched": self.fingerprint_matched,
            "safety_set": self.safety_set,
        }


def restore_set(
    cfg,
    hive_dir: Path,
    set_dir: Path,
    *,
    slug: str,
    engine=None,
    dry_run: bool = True,
    confirm: str = "",
    clock: Callable[[], datetime] = _now,
    tables: Sequence[str] | None = None,
    skip_pre_restore_snapshot: bool = False,
    tool_versions: Callable[[], dict] | None = None,
) -> RestoreOutcome:
    """Restore a verified snapshot over ``hive_dir``'s LIVE database.

    Never implicit: a real run needs ``confirm == slug`` (the operator types the hive's name).
    ``dry_run`` validates the set and prints the plan with zero mutation. The registry is
    captured first and re-applied (read-back verified) after the restore, because replacing a
    database drops what is registered against it.

    The overwrite destroys every bead and Dolt commit written since the set was taken, so a
    pre-restore snapshot of the LIVE store (same :func:`take_snapshot` path, restore-attestation
    skipped) is taken first and the restore is refused, with zero mutation, if it fails. The one
    escape is a live store that cannot be read at all (nothing to save), which needs the explicit
    ``skip_pre_restore_snapshot``."""
    from . import engine as engine_mod

    hive_dir = Path(hive_dir)
    set_dir = Path(set_dir)
    out = RestoreOutcome(dry_run=dry_run)
    report = verify_set(set_dir)
    if not report.ok:
        out.problems.extend(f"set is not restorable: {p}" for p in report.problems)
        return out
    m = load_manifest(set_dir)
    if m["hive"] != slug:
        out.problems.append(
            f"set belongs to {m['hive']!r}, not {slug!r}; refusing a cross-hive restore"
        )
        return out
    engine = engine or engine_mod.get_engine(cfg)
    want = cf.Fingerprint.from_dict(m["fingerprint"])
    use_tables = tuple(tables or want.tables)
    live_note = ""
    live_readable = False
    try:
        live = cf.compute_like(source_facts(engine, hive_dir, use_tables), want, tables=use_tables)
        if live.components.get("project_id") not in ("", want.components.get("project_id")):
            out.problems.append(
                "PROJECT IDENTITY MISMATCH: the live store belongs to a different project than "
                "this set; refusing"
            )
            return out
        live_note = f"live store currently {live.digest[:12]} (set holds {want.digest[:12]})"
        live_readable = True
    except (cf.FingerprintError, RuntimeError):
        live_note = "live store unreadable; its identity could not be compared"
    planned = backup_mod.migrate_hive_root(slug, cfg) / backup_mod.stamp(clock())
    if live_readable and skip_pre_restore_snapshot:
        out.problems.append(
            "--no-pre-restore-snapshot is only for a live store that cannot be read; this one "
            "can, so its pre-restore snapshot is mandatory"
        )
        return out
    if not live_readable and not skip_pre_restore_snapshot:
        out.problems.append(
            "the live store is unreadable, so there is nothing to snapshot first; pass "
            "--no-pre-restore-snapshot to restore without a safety copy"
        )
        return out
    out.actions.append(f"verified set {set_dir} ({', '.join(report.checked)})")
    out.actions.append(live_note)
    if live_readable:
        out.safety_set = str(planned)
        out.actions.append(
            f"a pre-restore snapshot of the live store will be taken at {planned} first "
            "(the restore is refused if it fails)"
        )
    else:
        out.actions.append(
            "NO pre-restore snapshot: the live store is unreadable and "
            "--no-pre-restore-snapshot was given"
        )
    out.actions.append(
        f"bd backup restore {set_dir / NATIVE_NAME} --force: OVERWRITES the live database of "
        f"{slug} in place (no move-aside)"
    )
    out.actions.append("capture the registry first; re-apply it and verify by read-back after")
    out.actions.append("fingerprint the restored live store against the set's manifest")
    if dry_run:
        out.ok = True
        return out
    if confirm != slug:
        out.problems.append(
            f"refusing to overwrite the live database without --confirm {slug}"
            + (" (the value did not match)" if confirm else "")
        )
        return out

    if live_readable:
        safety = take_snapshot(
            cfg,
            hive_dir,
            slug=slug,
            prefix=str(want.components.get("prefix") or ""),
            engine=engine,
            clock=clock,
            tables=use_tables,
            tool_versions=tool_versions,
            attest_restore=False,
            purpose="pre-restore",
        )
        if not safety.ok:
            out.safety_set = ""
            out.problems.append(
                "pre-restore snapshot of the live store failed; nothing was changed: "
                + "; ".join(safety.errors)
            )
            return out
        out.safety_set = str(safety.set_dir)
        out.actions.append(f"pre-restore snapshot of the live store taken at {safety.set_dir}")

    try:
        cap = registry_snapshot.capture(hive_dir, hive=slug, engine=engine, clock=clock)
    except registry_snapshot.RegistryUnreadable as exc:
        out.problems.append(str(exc))
        return out
    keep = set_dir.parent / f"registry-before-restore-{backup_mod.stamp(clock())}.json"
    try:
        cap.write(keep)
    except OSError as exc:
        out.problems.append(f"cannot save the registry before restoring: {exc}")
        return out
    saved_files = _read_slot_files(hive_dir)
    res = engine.backup_restore(hive_dir, set_dir / NATIVE_NAME)
    if res.returncode != 0:
        out.problems.append(f"bd backup restore failed: {_tail(res)}")
    again = engine.restore_registry(hive_dir, cap.snapshot)
    out.registry_restored = bool(again.ok)
    files_ok = _restore_slot_files(hive_dir, saved_files)
    if not again.ok or not files_ok:
        out.problems.append(
            f"registry NOT restored after the restore ({again.error or 'bookkeeping files'}); "
            f"reapply {keep}"
        )
    if res.returncode == 0:
        try:
            got = cf.compute_like(
                source_facts(engine, hive_dir, use_tables), want, tables=use_tables
            )
            diff = cf.compare(want, got)
            out.fingerprint_matched = diff.equal
            if not diff.equal:
                out.problems.append(
                    "restored live store differs from the set in: " + ", ".join(diff.differing)
                )
        except (cf.FingerprintError, RuntimeError) as exc:
            out.fingerprint_matched = False
            out.problems.append(f"cannot fingerprint the restored store: {exc}")
    out.ok = not out.problems
    if out.ok:
        out.actions.append("restored, registry re-applied, fingerprint matches the set")
    return out


__all__ = [
    "SnapshotResult",
    "take_snapshot",
    "verify_set",
    "restore_set",
    "list_sets",
    "validate_manifest",
    "preserved_backup_slot",
]
