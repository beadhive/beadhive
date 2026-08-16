"""`ws bd …` — a workspace-aware passthrough to beads, with optional hive routing.

Plain: forwards to `bd` in the current dir, intercepting `create` (and `import`) to auto-apply
the provider/org/repo triplet (ports bdc). `-a`/`-r` route across hives (requires git_workspace).

This module doubles as the `bd` (Dolt) `Engine` adapter (bh-dw3e.5, see `engine.py`): `run()`
(and `json()`, which is built on it) is the shared bd-invocation helper every other module calls,
and it routes through `engine.get_engine().passthrough` — today always `BdEngine`, so this is
extraction-only, no behavior change.
"""

from __future__ import annotations

import contextlib
import contextvars
import json as _json
import re
import sys
import tempfile
from pathlib import Path

import typer

from . import config, guard, registry, route, validate
from . import run as _runmod
from .identity import resolve_actor, workspace_identity
from .run import run as _run

# Characters that may continue a bead id (`bh-baml-m76.10`, `bh-1vvdp`). Used to anchor id
# matching so an id is only ever matched as a WHOLE token.
_ID_CHARS = r"A-Za-z0-9._-"


def names_bead(desc: str, bead: str) -> bool:
    """True iff `desc` names `bead` as a whole id rather than as a prefix of a longer sibling.

    Gate identity is description-based (`bd gate create --blocks <bead>` writes the id into the
    text), so the match must be anchored: a plain substring test makes every `.1` the owner of
    `.10`/`.11`/`.12` (bh-1vvdp), which is deterministic for any molecule with 10+ children and
    silently resolves a sibling's human review gate — an integrity boundary — as a side effect of
    an ordinary submit. Anchoring both sides is what makes the id a token rather than a prefix.
    Case-insensitive, matching the callers' previous `.lower()` behaviour."""
    return bool(
        re.search(
            rf"(?<![{_ID_CHARS}]){re.escape(str(bead))}(?![{_ID_CHARS}])",
            str(desc or ""),
            re.IGNORECASE,
        )
    )


def run(args, cwd, actor="", capture=False, text_input=None):
    """Run a `bd` subcommand scoped to the hive via `-C <cwd>` (so the right Beads DB is hit
    regardless of the process cwd / `--hive`). Prepends `--actor <name>` for the audit trail;
    `text_input` feeds stdin (e.g. a JSONL record for `bd import -`). The one shared bd-invocation
    helper the work/plan/triage/report layers all call — routed through the configured
    `Engine.passthrough` (bh-dw3e.5; `bd` is the only engine today, so this is extraction-only)."""
    from . import engine  # lazy: engine imports bd, so keep the cycle import-safe

    return engine.get_engine().passthrough(
        args, cwd, actor=actor, capture=capture, text_input=text_input
    )


def err_line(res) -> str:
    """First non-empty output line — bd's `Error: …` headline, never its usage dump."""
    for line in ((res.stdout or "") + (res.stderr or "")).splitlines():
        if line.strip():
            return line.strip()
    return f"exit {res.returncode}"


# Keys a bd/Dolt JSON error payload puts the human-readable message under, most specific first.
_ERR_KEYS = ("error", "message", "msg", "detail", "details")


def _json_err_message(text: str) -> str:
    """The human message inside a JSON error payload in `text`, or '' if there isn't one.
    Tolerates the payload being wrapped in other output by scanning to the outermost braces."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return ""
    try:
        payload = _json.loads(text[start : end + 1])
    except _json.JSONDecodeError:
        return ""
    # Descend at most a few levels into nested payloads (`{"error": {"message": "…"}}`), taking
    # the first key that carries actual text. Iterative, so a hostile 2000-deep object cannot
    # blow the stack. A dict with no error-ish key at all ends the walk: better to fall back to
    # the raw first line than to invent a message.
    for _ in range(8):
        if not isinstance(payload, dict):
            return ""
        for key in _ERR_KEYS:
            if key in payload:
                value = payload[key]
                if isinstance(value, str) and value.strip():
                    return value.strip()
                payload = value
                break
        else:
            return ""
    return ""


def err_detail(res) -> str:
    """A human-actionable failure reason from a completed bd process.

    `err_line` returns the first non-empty line, which is right for bd's one-line `Error: …`
    headline and WRONG for the multi-line JSON object bd emits on a SQL failure: the first line
    is the bare `{`, so a real failure surfaced as `bulk copy from 'bh' failed (issues: {)` and
    told the operator nothing about why (bh-f8rdk). Prefers the JSON payload's own message, falls
    back to the first line carrying information (a structural bracket is not a message), and
    finally to the exit code — so a reason is never empty."""
    text = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
    message = _json_err_message(text)
    if message:
        return message
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped.strip("{}[],"):
            return stripped
    return f"exit {res.returncode}"


def show(bead, cwd, *, strict=False):
    """The bead's JSON object (bd show may return a single object or a 1-list), or None.

    `strict=True` — or being anywhere inside :func:`strict_reads` — raises `BinaryMissing` when bd
    itself is absent, rather than returning the None that a caller cannot tell from "no such bead"
    (bh-8x452)."""
    data = json(["show", bead], cwd, strict=strict)
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def state(bead, dim, cwd) -> str:
    """Current value of a state dimension via `bd state <bead> <dim>` ('' if unset)."""
    res = run(["state", bead, dim], cwd, capture=True)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def store_prefix(cwd) -> str:
    """The issue prefix the STORE at *cwd* declares for itself, or "" when there is no store.

    Read from `bd config list --json`'s `issue_prefix`, which is stored IN the database — so a
    clone that brought a `refs/dolt/data` store down with it can be asked what its beads are
    actually called, instead of that being inferred from the directory name (bh-ezrq9).

    "" covers every "cannot answer" case identically — no store, an unreachable one, an
    unparseable reply — because every one of them means the same thing to the caller: fall back
    to deriving. This must never RAISE: it runs inside onboard's preflight, where a probe that
    threw would turn "this repo has no beads yet", the ordinary case, into a failed onboard.

    Note the key spelling. `bd config get issue-prefix` (hyphen) answers "(not set)" even on a
    store whose prefix is set; the live key is `issue_prefix` (underscore). Reading the JSON map
    sidesteps having to know that, and sidesteps parsing "(not set)" out of prose.
    """
    data = json(["config", "list"], cwd)
    if not isinstance(data, dict):
        return ""
    return str(data.get("issue_prefix") or "").strip()


def triplet_label_args(cwd) -> list[str]:
    """`-l provider:…,org:…,repo:…` for `cwd`'s managed identity, or [] outside one.

    Typer-free core: the identity-triplet labels `ws bd create` auto-applies, shared with
    the future MCP entrypoint so both build the same label set."""
    ident = workspace_identity(cwd)
    if ident is None:
        return []
    provider, org, repo = ident
    return ["-l", f"provider:{provider},org:{org},repo:{repo}"]


class BinaryMissing(RuntimeError):
    """The bd binary itself is absent — raised only for STRICT callers.

    Exists because the None-on-error contract is ambiguous in exactly one way that matters:
    None means "no such bead" to most callers, so an absent binary reads as a fact about the
    operator's data. On the CLI a narrated warning is enough (the operator sees the truth one
    line above the falsehood). On a STRUCTURED surface it is not: `bh mcp serve` hands the agent
    the return value and writes the narration to the server's stderr, which the agent never
    reads — so an agent got `null`, i.e. "bead not found" (bh-8x452). Those callers become strict
    — `strict=True` at the call, or any read made inside :func:`strict_reads` — and get this
    instead."""


#: Strict bd reads for the CURRENT context, set by :func:`strict_reads`. A ContextVar rather than
#: another parameter because the callers that need strictness are SURFACES, not call sites: an MCP
#: resource reaches bd through `triage.intake_payload` / `work_show.show_payload` /
#: `work.schedule_payload` / `worktree.status_rows`, none of which take a `strict=` flag, so
#: per-call plumbing made strict exactly the five resources someone remembered to plumb and left
#: five more returning a plausible empty result (bh-fzh4h). Scoping it to the surface makes
#: strictness a property of "who is going to read this answer", which is what it always was.
_STRICT_READS: contextvars.ContextVar[bool] = contextvars.ContextVar("bh_bd_strict_reads")


@contextlib.contextmanager
def strict_reads():
    """Make every bd read in this context strict, however indirectly it is reached.

    Inside the block `json()` — and so `show()`, and so every helper layered on them, at any call
    depth — raises :class:`BinaryMissing` for an absent binary instead of returning the None its
    caller cannot tell from "no such bead". Wrap a whole STRUCTURED surface in it (`bh mcp serve`
    wraps its resource handlers) so an indirect read cannot quietly reintroduce the null shape,
    including in a resource written later.

    ContextVar-scoped: safe under concurrency (each task/thread sees its own value) and the
    previous value is restored on exit. It does NOT change what a SUCCESSFUL read returns, and it
    does NOT promote an ordinary bd failure (bd ran, exited non-zero) to an exception — only the
    absent-binary case, which is never an answer about the data.
    """
    token = _STRICT_READS.set(True)
    try:
        yield
    finally:
        _STRICT_READS.reset(token)


def json(args, cwd, *, strict=False):
    """Run ``bd -C <cwd> <args> --json`` and return the parsed dict/list, or None on error.

    Appends ``--json`` itself — callers pass args WITHOUT ``--json``. Returns None when the
    process exits non-zero or the output is not valid JSON (matches the None-on-failure contract
    the work/triage/plan layers rely on). Routed through `run()` (so through the Engine seam too)
    — same resulting command as before, just no longer a separate direct `_run` call.

    A MISSING bd still returns None (callers must keep working — `bh doctor`'s whole job is
    reporting on a broken seat), but it SAYS SO first, once per process. None means "no such
    bead" to most callers, so an un-narrated absence became `✗ no such bead: <id>` — a confident,
    false statement about the operator's data, and the same manufactured-finding class bh-7m2h9
    was filed about.

    `strict=True` — or ANY call made inside :func:`strict_reads` — raises `BinaryMissing` for that
    one case instead, for callers whose consumer cannot see the narration; see that exception's
    docstring."""
    res = run(args + ["--json"], cwd, capture=True)
    if res.returncode != 0:
        if (strict or _STRICT_READS.get(False)) and (binary := _runmod.missing_binary(res)):
            raise BinaryMissing(_missing_binary_message(binary, narrating=False))
        _warn_missing_binary(res, cwd)
        return None
    try:
        return _json.loads(res.stdout or "null")
    except _json.JSONDecodeError:
        return None


#: Narrate an absent binary once per (binary, HIVE) — see `_warn_missing_binary` for why the hive
#: is in the key and why it is not narrowed further.
_MISSING_BINARY_WARNED: set[tuple[str, str]] = set()


def _missing_binary_message(binary: str, *, narrating: bool) -> str:
    """The ONE statement of "this binary is absent", for both ways bh reports it.

    bh reports the same fact through two channels on purpose, and bh-fzh4h asked that they be
    converged rather than left to drift: `_warn_missing_binary` narrates to stderr for a human
    watching a CLI run, and `BinaryMissing` is raised to a strict caller whose consumer — an MCP
    client, a subprocess reading JSON — never sees stderr. The CHANNEL differs because the
    audience does; the claim, the reason it matters and the remedy are built HERE once, so a fix
    to the wording lands in both. `narrating` picks only the plural voice the stderr line can use,
    since it prefixes the output the human is about to read; the raised message speaks about the
    single failed lookup it is attached to.
    """
    subject = "every bead read below is" if narrating else "this is"
    return (
        f"`{binary}` is not on PATH — {subject} a FAILED LOOKUP, not an answer about your data. "
        f"Install it, or add its directory to PATH (`{config.BINARY_ALIAS} doctor` names the "
        f"remedy)."
    )


def _warn_missing_binary(res, cwd=None) -> None:
    """Say plainly that the binary is absent, so the caller's own "not found" message cannot be
    read as a fact about the data.

    GRANULARITY, decided on the record (bh-8x452). Keyed on `(binary, hive)`, not on the binary
    alone. A fleet-wide sweep — `bh doctor`, `bh hive survey`, any `-a` fan-out — reads bd once per
    hive, and a binary-only key narrated for the FIRST hive and then went silent, so an operator
    read 40 empty hives with one warning above them all.

    Deliberately NOT narrower than that. Within one hive the narration stays once, because a verb
    that reads bd twenty times would otherwise print twenty copies of the same sentence, and the
    per-verb reset the bead floated buys nothing a per-hive key does not: a CLI process is one
    verb. The long-lived processes the bead worried about — `bh mcp serve`, localloop — no longer
    DEPEND on this channel at all: every MCP resource and tool reads bd inside
    :func:`strict_reads`, so their caller gets `BinaryMissing` in band, on every call, forever.
    stderr is the human's channel and this is the human's cadence; the structured surface has its
    own."""
    binary = _runmod.missing_binary(res)
    key = (binary, str(cwd or ""))
    if not binary or key in _MISSING_BINARY_WARNED:
        return
    _MISSING_BINARY_WARNED.add(key)
    typer.echo(f"✗ {_missing_binary_message(binary, narrating=True)}", err=True)


def children(epic, cwd, extra=None):
    """`bd list --parent <epic>` filtered to rows that are ACTUALLY children by the parent EDGE.

    bd resolves `--parent` by dotted-id PREFIX, not by the edge, so a bead deliberately detached
    from its epic still comes back on the strength of its id alone (bh-89mrf). That made
    re-parenting a no-op against every consumer of this list: `bhui-5mhu.3` reported `parent: None`
    and was absent from the reverse dep tree, yet still blocked `bh work finish bhui-5mhu` as an
    open child. Detaching a bead has to mean it stops gating the molecule.

    Trusting the edge is a strict narrowing of bd's answer — a real child carries it — so a row
    without it was never ours to count. bd states the edge TWO ways in one row: a top-level
    `parent`, and a `parent-child` entry in `dependencies` (the form `bd dep tree` walks). Either
    counts: `parent` is simply ABSENT from a parentless row (not null — measured: 439 of 723 rows
    in this hive carry no such key), so a reader that trusted only one representation would decide
    membership on which field bd happened to emit. Returns None on a bd read failure, keeping
    `json()`'s contract so callers can still tell "cannot list" from "no children".

    `--limit 0` defeats bd's default 50-row window. An epic with more than 50 children would
    otherwise under-report its open ones and `bh work finish` would land an INCOMPLETE molecule
    silently — the same window that already hid an open review gate from approve (bh-pwi2, see
    `work_logic._bead_gates`). Largest molecule in this hive today is 26 children, so this is a
    latent bug closed while the call was being rewritten anyway, not an observed one."""
    rows = json(["list", "--parent", str(epic), "--limit", "0"] + list(extra or []), cwd)
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict) and _has_parent_edge(r, str(epic))]


def _has_parent_edge(row, epic) -> bool:
    """True iff `row` carries a parent edge to `epic` in either representation bd emits."""
    if str(row.get("parent") or "") == epic:
        return True
    deps = row.get("dependencies")
    return isinstance(deps, list) and any(
        isinstance(d, dict)
        and d.get("type") == "parent-child"
        and str(d.get("depends_on_id") or "") == epic
        for d in deps
    )


def _is_help(args) -> bool:
    """True when `args` asks for help/usage — the label gate must not block `--help`."""
    return any(a in ("-h", "--help") for a in args)


def _user_labels(create_args) -> list[str]:
    """The labels a `bd create` invocation passes via `-l`/`--label` (comma-separated),
    flattened — the caller's half of the new bead's label set the write gate validates."""
    labels: list[str] = []
    grab_next = False
    for arg in create_args:
        if grab_next:
            labels.extend(v for v in arg.split(",") if v)
            grab_next = False
        elif arg in ("-l", "--label"):
            grab_next = True
        elif arg.startswith("--label="):
            labels.extend(v for v in arg[len("--label=") :].split(",") if v)
    return labels


def new_bead_problems(cfg, ident, user_labels, iid="") -> list[str]:
    """Label problems for the ONE bead a create/import is about to write — the identity
    triplet auto-applied plus the caller's labels, validated in isolation via
    `validate.bead_violations`. NOT the hive's whole DB: pre-existing label debt must never
    block an unrelated write (the anti-deadlock rationale in bead_violations' docstring).
    Returns [] when `cwd` is outside a managed/registered hive — there is no registry
    identity to validate the new bead against."""
    if ident is None:
        return []
    entry = registry.find_entry(cfg, *ident)
    if entry is None:
        return []
    provider, org, repo = ident
    labels = [f"provider:{provider}", f"org:{org}", f"repo:{repo}", *(user_labels or [])]
    return validate.bead_violations(cfg, iid or f"{entry['prefix']}-new", labels)


def create(create_args, cwd) -> tuple[int, str]:
    """Run `bd create` for `cwd`'s hive with its identity triplet appended. Typer-free core.

    Returns `(exit_code, error)`: when the NEW bead's own labels (identity triplet + `-l`
    labels) have violations, returns `(1, msg)` listing that bead's problems and runs
    nothing; otherwise `(bd's exit code, "")`. The gate is per-bead — pre-existing label
    debt elsewhere in the hive never blocks an unrelated create. Callers render `error`.
    `--help`/`-h` always falls through — usage should print even with label violations."""
    if not _is_help(create_args):
        problems = new_bead_problems(
            config.load(), workspace_identity(cwd), _user_labels(create_args)
        )
        if problems:
            return 1, (
                "new bead has label violations — fix its labels (vocabulary: "
                f"'{config.BINARY_ALIAS} label validate'): " + "; ".join(problems)
            )
    extra = triplet_label_args(cwd)
    return _rc_error(_run(["bd", "create", *create_args, *extra], check=False, cwd=cwd))


def _rc_error(res) -> tuple[int, str]:
    """`(exit code, error)` for a bd WRITE, naming an absent binary instead of leaving a bare 127.

    The read path got this in bh-fzh4h (`strict_reads` → `BinaryMissing`); the write path kept
    reporting only the number. `create_items` renders per-item failures, so over MCP an uninstalled
    bd reached the agent as ``bd_create failed for: #0 'my bead': bd exit 127`` — a statement about
    THAT ITEM, which sends the agent to inspect a bead that is fine. Same class as the null read
    (bh-8x452): the exit code is a failed invocation, not a fact about the request.

    Only the tagged case is translated (`run.missing_binary`, never the number alone — a child may
    exit 127 by choice); every other non-zero keeps the empty `error` its callers already render."""
    if binary := _runmod.missing_binary(res):
        return res.returncode, _missing_binary_message(binary, narrating=False)
    return res.returncode, ""


# ---- whole-payload create (the shell-free transport) ------------------------------------
#
# One bead-shaped dict — the schema the `bd_create` MCP tool takes — is the unit BOTH shell-free
# transports carry: the MCP tool hands it over as a JSON value, and `bh bd create --json` reads it
# from a file or stdin. Shared here so the two cannot drift into two schemas.
#
# WHY THIS EXISTS (2026-08-16). Bead prose is markdown, and markdown marks identifiers with
# backticks. Passed as a double-quoted shell argument, a backtick is COMMAND SUBSTITUTION: bash
# ran the spans before `bh` was ever exec'd and `just push` + `just bump` executed against this
# repo; `just release` — publish to PyPI, recoverable only by yank — was a code span in the same
# argument and did not run only because bash pairs backticks POSITIONALLY (1<->2, 3<->4), so which
# spans fire drifts out of phase with what the author wrote. bh cannot detect this: substitution
# happens before the process starts, so bh receives the *result*. The only fix is a transport where
# prose never becomes a shell token.

#: Fields of one bead item, mapped to the `bd create` flag that carries them. `title` is the
#: positional and is handled separately; `labels`/`deps` are lists and joined on `,`.
_ITEM_FLAGS = {
    "type": "--type",
    "priority": "-p",
    "description": "-d",
    "acceptance": "--acceptance",
    "design": "--design",
    "parent": "--parent",
}


def create_args_from_item(item: dict) -> list[str]:
    """Translate one structured bead item into `bd create` positional/flag args.

    Mirrors the flag taxonomy `plan._create_issue` uses. The identity triplet is NOT added here —
    `create` appends it, so both transports inherit the same triplet + label gate. Values go into
    an argv LIST, which `run` passes to `subprocess` without a shell, so no character in the prose
    is ever interpreted. Assumes `title` is present (`create_items` checks first)."""
    args: list[str] = [str(item["title"]).strip()]
    for field, flag in _ITEM_FLAGS.items():
        value = item.get(field)
        if value not in (None, ""):
            args += [flag, str(value)]
    if labels := (item.get("labels") or []):
        args += ["-l", ",".join(str(label) for label in labels)]
    if deps := (item.get("deps") or []):
        args += ["--deps", ",".join(str(dep) for dep in deps)]
    return args


def create_items(items, cwd) -> tuple[list[str], list[str]]:
    """Create every structured item in `items`, one `create` call each.

    Returns `(created titles, failure messages)`. Each item is gated on ITS OWN labels by
    `create`, so a bad item is refused by itself; the loop runs to the end and reports every
    failure rather than stopping at the first, so a caller sees the whole picture in one pass."""
    created: list[str] = []
    failures: list[str] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            failures.append(f"#{idx}: expected a JSON object, got {type(item).__name__}")
            continue
        if not str(item.get("title") or "").strip():
            failures.append(f"#{idx}: missing 'title'")
            continue
        code, error = create(create_args_from_item(item), cwd)
        if code != 0:
            failures.append(f"#{idx} {item['title']!r}: {error or f'bd exit {code}'}")
        else:
            created.append(str(item["title"]))
    return created, failures


def create_json(source, cwd) -> tuple[int, str]:
    """`bh bd create --json <path>|-` — create beads from a whole-payload JSON document.

    `source` is a file path or `-` for stdin. The document is one bead object or a LIST of them,
    each taking the `bd_create` MCP schema: `{title (required), type, priority, description,
    acceptance, design, parent, labels[], deps[]}`. Returns `(exit_code, error)` like `create`,
    whose triplet injection and per-bead label gate it goes through unchanged.

    Partial success is reported, not hidden: if item 3 of 5 fails, the first two are already
    created and the error names which items failed."""
    try:
        if source == "-":
            text = sys.stdin.read()
        else:
            path = Path(source)
            text = (path if path.is_absolute() else Path(cwd or ".") / path).read_text()
    except OSError as exc:
        return 1, f"cannot read --json payload {source!r}: {exc}"
    try:
        payload = _json.loads(text)
    except ValueError as exc:
        return 1, f"--json payload {source!r} is not valid JSON: {exc}"
    items = payload if isinstance(payload, list) else [payload]
    if not items:
        return 1, f"--json payload {source!r} is empty — nothing to create"
    created, failures = create_items(items, cwd)
    if failures:
        made = f" (created {len(created)} of {len(items)})" if created else ""
        return 1, f"--json create failed for: {'; '.join(failures)}{made}"
    for title in created:
        typer.echo(f"✓ created {title}")
    return 0, ""


def _split_json_flag(create_args) -> tuple[str, list[str]]:
    """Pull `--json <src>` / `--json=<src>` out of `create_args`.

    Returns `(source, leftover args)`; `source` is `""` when the flag is absent. The leftover is
    what makes `--json` refuse to be *combined* with per-field flags rather than silently picking
    a precedence — the payload is the whole bead or it is not the input."""
    rest: list[str] = []
    source = ""
    args = list(create_args)
    while args:
        arg = args.pop(0)
        if arg == "--json":
            source = args.pop(0) if args else ""
        elif arg.startswith("--json="):
            source = arg[len("--json=") :]
        else:
            rest.append(arg)
    return source, rest


def _create(create_args, cwd):
    """CLI wrapper over `create`: echo the violation error to stderr, return the exit code.

    Routes `--json <path>|-` to `create_json` — the shell-free transport. It is an EITHER/OR with
    the flag path, not a default: combining it with per-field flags is refused rather than
    resolved by precedence, because a half-payload/half-flags create is exactly the inconsistent
    transport that produced this bead."""
    if not _is_help(create_args) and any(
        a == "--json" or a.startswith("--json=") for a in create_args
    ):
        source, rest = _split_json_flag(create_args)
        if not source:
            code, error = 1, "--json needs a payload: a file path, or '-' to read stdin"
        elif rest:
            code, error = (
                1,
                (
                    "--json carries the WHOLE bead and cannot be combined with per-field flags "
                    f"({' '.join(rest)}) — move those fields into the payload"
                ),
            )
        else:
            code, error = create_json(source, cwd)
        if error:
            typer.echo(f"✗ {error}", err=True)
        return code
    code, error = create(create_args, cwd)
    if error:
        typer.echo(f"✗ {error}", err=True)
    return code


def augment_labels(records: list[dict], ident: tuple[str, str, str]) -> list[dict]:
    """Merge the identity triplet into each record's ``labels`` (dedup, order-stable).

    Typer-free core, shared idempotency: appends ``provider:``/``org:``/``repo:`` only when
    absent, so re-importing an already-triplet-tagged record is a no-op on labels."""
    provider, org, repo = ident
    triplet = [f"provider:{provider}", f"org:{org}", f"repo:{repo}"]
    out = []
    for rec in records:
        labels = list(rec.get("labels") or [])
        for tag in triplet:
            if tag not in labels:
                labels.append(tag)
        out.append({**rec, "labels": labels})
    return out


def import_labeled(import_args, cwd) -> tuple[int, str]:
    """Run `bd import` for `cwd`'s hive with its identity triplet merged into every record.

    `bd import` is a raw upsert and, unlike `create`, does NOT inject the triplet — so a backfill
    JSONL would land registry-invalid. This reads the source (a file path, or ``-``/none = stdin),
    augments each record's labels, and imports the augmented copy. Idempotent by ``external_ref``.
    Each augmented record is gated on ITS OWN labels (`new_bead_problems`) — a bad record is
    refused with its problems listed; pre-existing label debt in the hive never blocks the
    import. Returns `(exit_code, error)` like `create`; callers render `error`. `--help`/`-h`
    always falls through to plain `bd import --help` — usage should print even with label
    violations, and without touching stdin/the identity triplet."""
    if _is_help(import_args):
        return _run(["bd", "import", *import_args], check=False, cwd=cwd).returncode, ""
    ident = workspace_identity(cwd)
    if ident is None:
        return 1, "not inside a managed hive — cannot resolve the identity triplet for import."
    flags = [a for a in import_args if a.startswith("-") and a != "-"]
    srcs = [a for a in import_args if not a.startswith("-")]
    src = srcs[-1] if srcs else "-"
    try:
        if src == "-":
            text = sys.stdin.read()
        else:
            p = Path(src)
            base = Path(cwd) if cwd else Path.cwd()
            text = (p if p.is_absolute() else base / src).read_text()
    except OSError as e:
        return 1, f"cannot read import source {src!r}: {e}"
    try:
        records = [_json.loads(ln) for ln in text.splitlines() if ln.strip()]
    except _json.JSONDecodeError as e:
        return 1, f"invalid JSONL in {src!r}: {e}"
    augmented = augment_labels(records, ident)
    cfg = config.load()
    problems = []
    for rec in augmented:
        problems.extend(new_bead_problems(cfg, ident, rec.get("labels"), iid=rec.get("id", "")))
    if problems:
        return 1, "import would write beads with label violations: " + "; ".join(problems)
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tf:
        tf.write("\n".join(_json.dumps(r) for r in augmented) + "\n")
        tmp = tf.name
    from . import engine  # lazy: engine imports bd, so keep the cycle import-safe

    try:
        result = engine.get_engine(cfg).import_jsonl(cwd, [*flags, tmp])
    finally:
        Path(tmp).unlink(missing_ok=True)
    combined = (result.stdout or "") + (result.stderr or "")
    # bd errors "nothing to commit" when an import changes nothing — that IS the idempotent no-op
    # a re-run should produce (the upsert created zero duplicates), so treat it as success.
    if result.returncode != 0 and "nothing to commit" in combined:
        typer.echo("nothing to import — already up to date")
        return 0, ""
    if combined.strip():
        typer.echo(combined.rstrip())
    return _rc_error(result)


def _import(import_args, cwd):
    """CLI wrapper over `import_labeled`: echo the error to stderr, return the exit code."""
    code, error = import_labeled(import_args, cwd)
    if error:
        typer.echo(f"✗ {error}", err=True)
    return code


def _run_one(args, cwd, cfg=None):
    # The host-lease gate, PER TARGET (bh-edvs): `bh work claim` refuses when this host is not
    # the hive's leased primary, and `bh bd update --claim` — the same write — must too. Here
    # rather than in `passthrough` below because `-a`/`-r` fan out across hives holding
    # different leases; returning 1 lets `route.fan_out` fail this hive and still run the rest.
    refusal = guard.bd_write_refusal(args, cwd, cfg=cfg)
    if refusal:
        typer.echo(refusal, err=True)
        return 1
    if args and args[0] == "create":
        return _create(args[1:], cwd)
    if args and args[0] == "import":
        return _import(args[1:], cwd)
    return _run(["bd", *args], check=False, cwd=cwd).returncode


def passthrough(mode, target, args):
    route.reject_inline_flags(args)
    guard.guard_bd(args, resolve_actor())  # gate `bd github push/sync` (seat + single-item)
    cfg = config.load() if mode != "cwd" else {}
    tgts = route.targets(cfg, mode, target)
    try:
        route.fan_out(tgts, lambda _label, cwd: _run_one(args, cwd, cfg))
    finally:
        route.invalidate_targets(cfg, tgts)  # a passthrough may have mutated the hive
