# 0.24.3 Darwin release-upgrade verification

The filesystem probe in `config_paths.statfs_type` passed a 256-byte Linux
`statfs` buffer to other platforms' libc. Apple's
[statfs header](https://github.com/apple/darwin-xnu/blob/main/bsd/sys/mount.h)
defines both a legacy LP64 layout with 90-byte mount-name arrays and a modern
layout with `MAXPATHLEN` arrays. Both exceed 256 bytes and place `f_type` after
the filesystem counters and owner. Calling either ABI with the Linux buffer can
overwrite Python heap memory; interpreting the first field as Linux's filesystem
magic is also invalid. The probe now returns `None` before loading libc on any
non-Linux platform, as its existing contract documents.

The public host CLI reaches this probe through deferred telemetry startup,
`observaloop_env.load_worktree_env`, `worktree.cwd_worktree_dir`, and
`config.worktrees_root`. This happens even when telemetry export is disabled.
Native heap corruption can therefore appear after plan output or during apply.
The installed Python API does not exercise that CLI startup path.

This establishes an unsafe native call and a compatible repair. There is no
operator Mac crash report or Darwin execution result from the Linux workspace;
attribution of the observed exit 139 to this defect remains an inference until
the Mac checks below complete. Python/package versions in the incident were
PyPI Beadhive 0.24.2 and uv-managed Python 3.13. The Linux regression builds a
wheel, resolves its installed `bh` entry point, and runs subprocess
plan/apply/check against isolated Dolt in signed and trusted modes. Only the
fixture secret broker and clock are injected. Every subprocess verifies that
the native filesystem probe ran, including successful process shutdown and CAS
refusals. The test also verifies exact digest, stable identity and hive lease,
readback after apply, and refusal of a replay without another epoch bump. A
separate trusted-mode rotation deliberately exits after SQL publication and
before emitting success JSON; public check identifies its committed epoch and
reviewed archive, and replay refusal proves there is no blind second rotation.

Local regression environment: Linux x86_64, kernel 6.12.111, glibc 2.41,
Python 3.11.15, PyMySQL 1.1.2, Typer 0.26.8, ruamel.yaml 0.19.1,
cryptography 50.0.0, and Pydantic 2.13.4. No native crash occurred in these Linux
subprocesses; the Mac/Python 3.13 result remains pending.

## Post-publication operator Mac checks

Run these after 0.24.3 is published, in Bash (`bash` opens it on macOS). Keep the
existing HQ trust mode. Use the
existing operator settings, admitted frame identity, and lease. The canonical
host manifest must already describe the approved target release digest through
the normal reviewed config publication. Do not edit restore floors, write SQL,
call private Python rotation APIs, or re-adopt the hive.

Install and capture the actual released environment:

```bash
uv tool install --upgrade --force beadhive==0.24.3
bh --version
sw_vers
uname -m
tool_root="$(uv tool dir)"
operator_python="$tool_root/beadhive/bin/python"
"$operator_python" -I -c 'import importlib.metadata as m, platform; print(platform.python_version(), platform.platform()); print({name: m.version(name) for name in ("beadhive", "PyMySQL", "typer", "ruamel.yaml", "cryptography", "pydantic")})'
```

Set `FRAME_ID`, `OPERATOR_SETTINGS`, and the reviewed target/CAS values below
from the current public check and prepared config record. Preserve the full
output and each exit code in a private directory. The settings path names a
credential-bearing file; never copy its contents into reports.

```bash
mkdir -m 700 upgrade-0243-evidence
PYTHONFAULTHANDLER=1 bh host release-upgrade check "$FRAME_ID" \
  --operator-settings "$OPERATOR_SETTINGS" \
  > upgrade-0243-evidence/before.json 2> upgrade-0243-evidence/before.stderr
printf 'check exit: %s\n' "$?"

rotation_args=(
  --operator-settings "$OPERATOR_SETTINGS"
  --expected-revision "$EXPECTED_REVISION"
  --expected-host-id "$EXPECTED_HOST_ID"
  --expected-epoch "$EXPECTED_EPOCH"
  --expected-release "$EXPECTED_RELEASE_DIGEST"
  --expected-config-head "$EXPECTED_CONFIG_HEAD"
  --release-id "$TARGET_RELEASE_ID"
  --release-digest "$TARGET_RELEASE_DIGEST"
  --profile "$TARGET_PROFILE"
  --config-revision "$TARGET_CONFIG_REVISION"
  --candidate-expires-at "$PLAN_EXPIRES_AT"
)
PYTHONFAULTHANDLER=1 bh host release-upgrade plan "$FRAME_ID" \
  "${rotation_args[@]}" \
  > upgrade-0243-evidence/plan.json 2> upgrade-0243-evidence/plan.stderr
printf 'plan exit: %s\n' "$?"
```

Review the plan's digest, old/new epoch, identity, target digest and principal
route. The public provision operation needs the existing administrative
`authority_writer` binding with route provisioning privileges. Set
`FRAME_ADDRESS` to the approved frame's literal source address. Supply the new
principal password over stdin, keeping it out of argv and stored evidence:

```bash
PLAN_SHA256="$("$operator_python" -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["plan_sha256"])' upgrade-0243-evidence/plan.json)"
read -r -s -p 'New frame principal password: ' FRAME_PASSWORD
printf '\n'
printf '%s\n' "$FRAME_PASSWORD" | PYTHONFAULTHANDLER=1 \
  bh host release-upgrade provision "$FRAME_ID" "${rotation_args[@]}" \
  --plan-sha256 "$PLAN_SHA256" --confirm \
  --frame-address "$FRAME_ADDRESS" --password-stdin \
  > upgrade-0243-evidence/provision.json 2> upgrade-0243-evidence/provision.stderr
printf 'provision exit: %s\n' "$?"
unset FRAME_PASSWORD
```

Provisioning creates only the reviewed next-epoch route. It does not rotate the
authority. Its protected schema commits change the runtime revision, so obtain
that returned revision and re-run/review plan before apply. In the Bash array
defined above, element 3 is the value following `--expected-revision`:

```bash
EXPECTED_REVISION="$("$operator_python" -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["revision"])' upgrade-0243-evidence/provision.json)"
rotation_args[3]="$EXPECTED_REVISION"
PYTHONFAULTHANDLER=1 bh host release-upgrade plan "$FRAME_ID" \
  "${rotation_args[@]}" \
  > upgrade-0243-evidence/plan.json 2> upgrade-0243-evidence/replan.stderr
printf 'replan exit: %s\n' "$?"
PLAN_SHA256="$("$operator_python" -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["plan_sha256"])' upgrade-0243-evidence/plan.json)"
signing_args=()
```

Review the replacement plan, including its exact provisioned session/evidence
tables. For signed HQ, set `signing_args=(--operator-key "$OPERATOR_KEY")` with
the existing operator key; trusted HQ keeps the empty array for keyless apply.

```bash
PYTHONFAULTHANDLER=1 bh host release-upgrade apply "$FRAME_ID" \
  "${rotation_args[@]}" "${signing_args[@]}" --plan-sha256 "$PLAN_SHA256" --confirm \
  > upgrade-0243-evidence/apply.json 2> upgrade-0243-evidence/apply.stderr
printf 'apply exit: %s\n' "$?"
PYTHONFAULTHANDLER=1 bh host release-upgrade check "$FRAME_ID" \
  --operator-settings "$OPERATOR_SETTINGS" \
  > upgrade-0243-evidence/after.json 2> upgrade-0243-evidence/after.stderr
printf 'check exit: %s\n' "$?"
```

Require all successful commands to exit 0, including shutdown after JSON
output. Verify the new revision and exactly one epoch advance, active admission,
unchanged frame/host/instance/key/beadyard identity, target release digest, and
the archive's exact reviewed plan digest. Renew the existing frame session and
conformance through the public CLI; verify the hive retains its holder and lease
epoch without re-adoption.

If apply crashes, times out, or gives an ambiguous acknowledgment, run **check
before any further mutation**. A matching new epoch/release/archive means the
rotation committed; do not repeat apply. If the old epoch remains, investigate
before making a newly reviewed plan. Never blindly retry with changed CAS
inputs. If check itself fails, leave the outcome unresolved until protected
readback is available.

On another native crash, preserve faulthandler stderr and the corresponding
macOS report from `~/Library/Logs/DiagnosticReports`. Before sharing, redact
usernames, sensitive paths, endpoints and any credential material. Record the
exception type, faulting native library/stack, macOS architecture, Python and
package versions, and whether JSON output appeared before the crash. These
checks are the pending Darwin/public-PyPI acceptance gate, not evidence already
obtained in Linux CI.
