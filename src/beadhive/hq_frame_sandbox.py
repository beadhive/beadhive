"""Optional Linux HQ frame-command isolation using explicit util-linux tools.

The operator supplies a separate frame home and complete credential/IPC exclusions.
The command receives a clean environment, private user/PID/network/mount namespaces,
read-only host filesystems except explicit frame paths, and no capabilities.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

FRAME_INPUTS = {
    "BH_HOME",
    "BH_CONFIG",
    "BH_HQ",
    "GIT_WORKSPACE",
    "GITHUB_TOKEN",
    "GITLAB_TOKEN",
    "PYTHONPATH",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-root", type=Path, required=True)
    parser.add_argument("--broker-dir", type=Path, required=True)
    parser.add_argument("--frame-home", type=Path, required=True)
    parser.add_argument("--deny-file", type=Path, action="append", required=True)
    parser.add_argument("--hide-dir", type=Path, action="append", default=[])
    parser.add_argument("--writable-path", type=Path, action="append", default=[])
    parser.add_argument("--frame-env-file", type=Path)
    parser.add_argument("--frame-path", default=os.environ.get("PATH", os.defpath))
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a frame command is required after --")
    if sys.platform != "linux":
        parser.error(
            "HQ namespace launcher is Linux-only; supply an enforced container boundary elsewhere"
        )
    missing = [tool for tool in ("unshare", "mount", "setpriv") if shutil.which(tool) is None]
    if missing:
        parser.error("missing deployment prerequisite util-linux programs: " + ", ".join(missing))
    if not args.inside:
        # Avoid forwarding an operator agent even when its path was not explicitly
        # listed. Dedicated IPC directories can be hidden without hiding frame data.
        inherited_agent = os.environ.get("SSH_AUTH_SOCK")
        if inherited_agent and Path(inherited_agent).parent.exists():
            arguments = ["--hide-dir", str(Path(inherited_agent).parent), *arguments]
        os.execvp(
            "unshare",
            [
                "unshare",
                "--user",
                "--map-root-user",
                "--mount",
                "--pid",
                "--net",
                "--fork",
                "--mount-proc",
                sys.executable,
                str(Path(__file__).resolve()),
                "--inside",
                *arguments,
            ],
        )
    mapping = Path("/proc/self/uid_map").read_text().split()
    if os.getpid() != 1 or len(mapping) != 3 or mapping[0] != "0" or mapping[1] == "0":
        raise ValueError("launcher mounting requires an unprivileged private user/PID namespace")
    frame_home = args.frame_home.resolve(strict=True)
    writable = [frame_home, *(p.resolve(strict=True) for p in args.writable_path)]
    protected = [
        args.server_root.resolve(strict=True),
        args.broker_dir.resolve(strict=True),
        *(p.resolve(strict=True) for p in args.deny_file),
        *(p.resolve(strict=True) for p in args.hide_dir),
    ]
    if any(
        p == w or p.is_relative_to(w) or w.is_relative_to(p) for p in protected for w in writable
    ):
        raise ValueError("frame writable paths overlap operator/server custody")
    inputs = json.loads(args.frame_env_file.read_text()) if args.frame_env_file else {}
    if not isinstance(inputs, dict) or any(
        (k not in FRAME_INPUTS and not k.startswith("FRAME_")) or not isinstance(v, str)
        for k, v in inputs.items()
    ):
        raise ValueError(
            "frame environment contains an unapproved variable; operator agents/env are excluded"
        )
    temporary = frame_home / "tmp"
    temporary.mkdir(exist_ok=True)
    environment = {
        "PATH": args.frame_path,
        "HOME": str(frame_home),
        "TMPDIR": str(temporary),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        **inputs,
    }
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    subprocess.run(["mount", "--rbind", "/", "/"], check=True)
    for path in writable:
        subprocess.run(["mount", "--bind", str(path), str(path)], check=True)
    # Bind copies are owned by this namespace. Every host mount is made read-only;
    # frame data mounts are the only exceptions. Fresh proc sees no operator PIDs.
    mounts = {}
    for row in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = row.split()
        encoded = fields[4]
        target = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), encoded))
        try:
            actual_stat = os.stat(target)
            actual_device = actual_stat.st_dev
        except (PermissionError, FileNotFoundError):
            # An inherited mount may be hidden/absent in the enclosing hermetic
            # /dev. Unreachable targets cannot expose a writable host mount.
            actual_stat = None
            actual_device = None
        if (
            actual_device is not None
            and f"{os.major(actual_device)}:{os.minor(actual_device)}" != fields[2]
        ):
            # A fresh proc or later bind obscures this inherited mount.
            continue
        # Directory traversal requires execute; readable file/device bind mounts
        # such as hermetic /dev/null still enter the checked read-only remount.
        access = os.R_OK | (os.X_OK if actual_stat and stat.S_ISDIR(actual_stat.st_mode) else 0)
        if not os.access(target, access):
            if os.access(target, os.W_OK):
                raise ValueError(f"inaccessible writable host mount: {target}")
            continue
        if "ro" not in fields[5].split(",") and target not in writable and target not in mounts:
            mounts[target] = [
                flag
                for flag in fields[5].split(",")
                if flag in {"nosuid", "nodev", "noexec", "noatime", "nodiratime", "relatime"}
            ]
    for path in sorted(mounts, key=lambda p: len(p.parts), reverse=True):
        subprocess.run(
            ["mount", "-o", ",".join(["remount", "bind", "ro", *mounts[path]]), str(path)],
            check=True,
        )
    for path in args.hide_dir:
        subprocess.run(
            ["mount", "-t", "tmpfs", "-o", "size=1m,mode=000,ro", "tmpfs", str(path.resolve())],
            check=True,
        )
    for path in args.deny_file:
        subprocess.run(["mount", "--bind", "/dev/null", str(path.resolve())], check=True)
    os.execvpe(
        "setpriv",
        [
            "setpriv",
            "--bounding-set=-all",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--no-new-privs",
            *command,
        ],
        environment,
    )
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"frame isolation refused: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
