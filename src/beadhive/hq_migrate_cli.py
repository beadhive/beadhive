"""Explicit HQ config-authority migration command.

The command never discovers a credential or changes Beads attachments. It only
uses the selected HOST binding and caller-reviewed intent/evidence files.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import typer


def _sql_store(host):
    from .hq_control_plane import SqlControlPlane

    hq = host.get("hq") or {}
    sql = hq.get("sql") or {}
    if not sql:
        raise ValueError("prepared SQL HOST binding missing")
    return SqlControlPlane(sql).config_store()


def _git_store(host, *, operator_key: Path | None):
    from . import config
    from .hq_control_plane import attach_fleet_config

    hq = host.get("hq") or {}
    return attach_fleet_config(
        config.hq_dir(),
        bootstrap={"mode": "git", "authority_anchor": hq.get("authority_anchor")},
        operator_key=str(operator_key) if operator_key is not None else None,
    )


def _suspension(artifact: Path, signature: Path):
    from .hq_transition import WriterSuspensionEvidence

    try:
        body = artifact.read_bytes()
        if len(body) > 4096:
            raise ValueError()
        record = json.loads(body)
        if not isinstance(record, dict):
            raise ValueError()
        return WriterSuspensionEvidence(
            record["backend_identity"],
            record["generation"],
            record["original_head"],
            record["principal"],
            hashlib.sha256(body).hexdigest(),
            record["observed_at"],
            record["valid_until"],
            artifact,
            signature,
        )
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("trusted writer suspension artifact invalid") from None


def _to_sql(*, dry_run: bool, confirm: bool, intent: Path | None) -> dict:
    from . import config, hq_seed

    host = config.load_host()
    if config.fleet_sql_selected():
        raise ValueError("SQL configuration authority is already selected")
    store = _sql_store(host)
    plan = hq_seed.plan_current(destination=store)
    if dry_run:
        return {"to": "dolt-server", "phase": "dry-run", **plan.report()}
    if not confirm or intent is None:
        raise ValueError("seed apply requires --confirm and a durable --intent path")
    blocking = tuple(
        issue for issue in plan.issues if issue != "target config database is already initialized"
    )
    if blocking:
        raise ValueError("seed plan has unresolved identity, schema or source conflicts")
    receipt = hq_seed.apply(
        plan, intent, store, fresh_plan=lambda: hq_seed.plan_current(destination=store)
    )
    if receipt is None:
        raise ValueError("seed acknowledgment unknown; reconcile the same durable intent")
    if hq_seed.plan_current().source_manifest_id != plan.source_manifest_id:
        raise ValueError("seed source changed before HOST selector switch")
    proposed = deepcopy(dict(host))
    proposed.setdefault("hq", {}).setdefault("sql", {})["enabled"] = True
    config.save_after_verified_hq_seed(
        proposed, revision=receipt.committed_revision, beadyard_id=receipt.beadyard_id
    )
    return {
        "to": "dolt-server",
        "phase": "CONFIG_READY",
        "revision": receipt.committed_revision,
        "beadyard_id": receipt.beadyard_id,
        "source_manifest_id": plan.source_manifest_id,
    }


def _to_git(
    *,
    dry_run: bool,
    confirm: bool,
    intent: Path | None,
    mirror_journal: Path | None,
    operator_key: Path | None,
    suspension_artifact: Path | None,
    suspension_signature: Path | None,
    expected_sql_revision: str,
    expected_git_revision: str,
) -> dict:
    from . import config, hq_seed
    from .identity import workspace_root
    from .modules.config.domain.ports import ordered_documents_digest

    host = config.load_host()
    if not config.fleet_sql_selected():
        raise ValueError("SQL configuration authority is not selected")
    sql_store = _sql_store(host)
    git_store, git_snapshot = _git_store(host, operator_key=operator_key)
    sql_snapshot = sql_store.load_snapshot()
    if dry_run:
        return {
            "to": "git",
            "phase": "dry-run",
            "sql_revision": sql_snapshot.commit_revision,
            "git_revision": git_snapshot.commit_revision,
            "sql_documents_sha256": ordered_documents_digest(sql_snapshot.documents),
            "git_documents_sha256": ordered_documents_digest(git_snapshot.documents),
        }
    if not all(
        (confirm, intent, mirror_journal, operator_key, suspension_artifact, suspension_signature)
    ):
        raise ValueError(
            "rollback requires --confirm, intent, mirror journal, "
            "operator key and signed suspension"
        )
    if sql_snapshot.commit_revision != expected_sql_revision:
        raise ValueError("rollback expected SQL revision changed")
    original_host_bytes = config.config_path().read_bytes()
    proposed = deepcopy(dict(host))
    proposed.setdefault("hq", {}).setdefault("sql", {})["enabled"] = False
    if "mode" in proposed["hq"]:
        proposed["hq"]["mode"] = "git"
    receipt = hq_seed.load_rollback_receipt(intent)
    if receipt is None:
        receipt = hq_seed.export_latest_to_signed_git(
            sql_store=sql_store,
            git_store=git_store,
            expected_sql_revision=expected_sql_revision,
            expected_git_revision=expected_git_revision,
            original_host_bytes=original_host_bytes,
            proposed_host=proposed,
            suspension=_suspension(suspension_artifact, suspension_signature),
            intent_path=intent,
        )
    elif (receipt.source_revision, receipt.export_expected_parent, receipt.suspension) != (
        expected_sql_revision,
        expected_git_revision,
        _suspension(suspension_artifact, suspension_signature),
    ):
        raise ValueError("rollback intent does not match original heads or suspension")
    if git_store.load_snapshot().commit_revision != receipt.export_revision:
        raise ValueError("signed Git export changed after original intent")
    git_snapshot = git_store.load_snapshot(revision=receipt.export_revision)
    root = Path(workspace_root())
    if mirror_journal.exists():
        mirror = hq_seed.recover_git_mirror_plan(
            mirror_journal,
            receipt=receipt,
            git_snapshot=git_snapshot,
            hq_dir=config.hq_dir(),
            workspace_root=root,
            proposed_host=proposed,
        )
    else:
        mirror = hq_seed.plan_git_mirror(
            receipt=receipt,
            git_snapshot=git_snapshot,
            hq_dir=config.hq_dir(),
            workspace_root=root,
            proposed_host=proposed,
        )
    hq_seed.install_git_mirror(
        mirror,
        mirror_journal,
        sql_store=sql_store,
        git_store=git_store,
        receipt=receipt,
        original_host_bytes=original_host_bytes,
        proposed_host=proposed,
    )
    config.save_after_verified_hq_export(proposed, receipt, mirror)
    return {
        "to": "git",
        "phase": "CONFIG_READY",
        "sql_revision": receipt.source_revision,
        "git_revision": receipt.export_revision,
        "beadyard_id": receipt.beadyard_id,
    }


def migrate_cmd(
    to: str,
    *,
    dry_run: bool,
    confirm: bool,
    intent: Path | None,
    mirror_journal: Path | None,
    operator_key: Path | None,
    suspension_artifact: Path | None,
    suspension_signature: Path | None,
    expected_sql_revision: str,
    expected_git_revision: str,
) -> None:
    """Report a safe plan or run one explicitly reviewed authority transition."""
    dry_run = dry_run or not confirm
    try:
        if to == "dolt-server":
            result = _to_sql(dry_run=dry_run, confirm=confirm, intent=intent)
        elif to == "git":
            result = _to_git(
                dry_run=dry_run,
                confirm=confirm,
                intent=intent,
                mirror_journal=mirror_journal,
                operator_key=operator_key,
                suspension_artifact=suspension_artifact,
                suspension_signature=suspension_signature,
                expected_sql_revision=expected_sql_revision,
                expected_git_revision=expected_git_revision,
            )
        else:
            raise ValueError("--to must be dolt-server or git")
    except ValueError:
        # A typed parser may include rejected raw input in ValueError text.
        typer.echo(
            "HQ configuration migration refused; inspect the plan and original intent", err=True
        )
        raise typer.Exit(1) from None
    except Exception:
        typer.echo("HQ configuration migration authority unavailable", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result, sort_keys=True))
