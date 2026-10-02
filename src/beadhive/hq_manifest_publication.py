"""Lower Git publication adapter for an exact own host manifest."""

from __future__ import annotations

import tempfile
from pathlib import Path

from beadhive_bd_cli import err_line

from .gitref import GIT_TIMEOUT
from .run import run


def _git(args: list[str], cwd: Path):
    return run(["git", *args], cwd=str(cwd), check=False, capture=True, timeout=GIT_TIMEOUT)


class HostPublicationError(Exception):
    """A host registration could not be published without touching unrelated HQ work."""


def _publish_host_manifest_git(
    hq_dir: Path,
    host_id: str,
    *,
    attempts: int = 3,
    remote_override: str | None = None,
    signing_key: str | None = None,
    ssh_keygen: str | None = None,
) -> bool:
    """Publish only this registration; return False when the remote already has it.

    The HQ publication boundary owns Git here. A remote-based disposable checkout avoids
    publishing unrelated local commits, staging someone else's edits, or stashing HQ dirt.
    Non-fast-forward races rebase only our manifest commit, then validate it again.
    """
    from . import hosts

    if Path(host_id).name != host_id or not host_id or host_id in {".", ".."}:
        raise HostPublicationError("host_id must be a manifest filename")
    manifest = hosts.load(hq_dir, host_id)
    if manifest.host_id != host_id:
        raise HostPublicationError("manifest host_id does not match this host")
    relative = hosts.manifest_path(hq_dir, host_id).relative_to(hq_dir)
    content = (hq_dir / relative).read_bytes()

    protected_transport = ["-c", "protocol.ext.allow=always"] if remote_override else []

    def checked(args: list[str], cwd: Path):
        result = _git([*protected_transport, *args], cwd)
        if result.returncode:
            raise HostPublicationError(f"git {args[0]} failed: {err_line(result)}")
        return result

    remote = (
        remote_override or checked(["remote", "get-url", "--push", "origin"], hq_dir).stdout.strip()
    )
    settings = {}
    for key in (
        "user.name",
        "user.email",
        "user.signingkey",
        "gpg.format",
        "gpg.ssh.program",
        "commit.gpgsign",
        "core.sshcommand",
        "protocol.ext.allow",
    ):
        value = _git(["config", "--get", key], hq_dir)
        if value.returncode == 0:
            settings[key] = value.stdout.strip()
    if signing_key is not None:
        settings.update(
            {"gpg.format": "ssh", "user.signingkey": signing_key, "commit.gpgsign": "true"}
        )
        if ssh_keygen is not None:
            settings["gpg.ssh.program"] = ssh_keygen
    with tempfile.TemporaryDirectory(prefix="bh-host-publish-") as directory:
        checkout = Path(directory) / "hq"
        transport = (
            ["-c", f"core.sshcommand={settings['core.sshcommand']}"]
            if "core.sshcommand" in settings
            else []
        )
        if "protocol.ext.allow" in settings:
            transport += ["-c", f"protocol.ext.allow={settings['protocol.ext.allow']}"]
        checked(
            [
                *transport,
                "clone",
                "--single-branch",
                "--branch",
                "main",
                "--",
                remote,
                str(checkout),
            ],
            hq_dir,
        )
        # Clone loses HQ-local settings; preserve effective signing and transport policy.
        for key, value in settings.items():
            checked(["config", key, value], checkout)
        target = checkout / relative
        if target.is_symlink() or target.parent.is_symlink():
            raise HostPublicationError("remote registration path must not be a symlink")
        if target.exists() and target.read_bytes() == content:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        checked(["add", "--", str(relative)], checkout)
        checked(["commit", "-m", f"feat(host): register {host_id}"], checkout)
        for attempt in range(attempts):
            verified = hosts.load(checkout, host_id)
            if verified != manifest or target.read_bytes() != content:
                raise HostPublicationError("registration changed during publication retry")
            pushed = _git(
                [*protected_transport, "push", "origin", "HEAD:refs/heads/main"], checkout
            )
            if pushed.returncode == 0:
                return True
            diagnostic = pushed.stderr + pushed.stdout
            if attempt + 1 == attempts or not any(
                marker in diagnostic for marker in ("non-fast-forward", "fetch first")
            ):
                raise HostPublicationError(f"git push failed: {err_line(pushed)}")
            checked(["fetch", "origin", "main"], checkout)
            checked(["rebase", "origin/main"], checkout)
        raise HostPublicationError("registration publication retry limit exhausted")
