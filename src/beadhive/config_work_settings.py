"""Typed work, routing, validation, dispatch, and identity policy accessors."""

from __future__ import annotations

import datetime

from pydantic import TypeAdapter

from . import config as _config

_UNSET = _config._UNSET


def load():
    return _config.load()


def layered(cfg, entry, section, key, default=None):
    return _config.layered(cfg, entry, section, key, default)


def asset(name):
    return _config.asset(name)


def work_cfg(cfg=None):
    """The global `work` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("work", {}) or {}


def work_value(cfg, entry, key, default=None):
    """A work setting: per-hive `entry['work'][key]` > global `work[key]` > default."""
    return layered(cfg, entry, "work", key, default)


def routing_policy(cfg, entry) -> str:
    """Routing resolution policy, per-hive > global > ``loose``.

    Config validation admits only ``loose`` and ``strict``. This read-side fallback keeps an
    unrelated command usable after a hand-edited typo; the resolver that consumes this value
    owns the actual strict/loose behavior.
    """
    value = str(layered(cfg, entry, "work.routing", "policy", "loose"))
    return value if value in ("loose", "strict") else "loose"


def routing_tiers(cfg, entry):
    """Normalized model routes, per-hive > global > an empty list.

    Each returned :class:`config_schema.RoutingTierConfig` has explicit inclusive bounds, so an
    omitted floor is ``SIMPLE`` and an omitted ceiling is ``REASONING``. Invalid hand-edited
    data degrades to no routes here; ``bh config validate`` remains the loud diagnostic gate.
    """
    from .config_schema import RoutingTierConfig

    raw = layered(cfg, entry, "work.routing", "tiers", []) or []
    try:
        return TypeAdapter(list[RoutingTierConfig]).validate_python(raw)
    except ValueError:
        return []


def validate_cmd(cfg, entry, phase=None, main_gate=False):
    """How `ws work check/submit/merge` validates a worktree (default `just check`).

    With a ``phase`` (submit | merge | molecule | postland | union), a per-point override at
    ``work.validate.<phase>`` (per-hive > global) wins, else falls back to ``work.validate_cmd``.
    ``phase=None`` keeps the legacy single-command behavior. When ``main_gate`` (the operation
    targets the shared integration branch), a ``<phase>-main`` override is preferred over
    ``<phase>`` — so an ad-hoc bead landing on main can run the full suite while a molecule member's
    merge into ``mol/<epic>`` stays fast. Lets a hive run a fast subset at the frequent intermediate
    points and the full suite only at the main-merge boundary.

    A declared toolchain (bh-d0kb) is knowledge-only and is NEVER consulted here — its
    ``suggested_validate_cmd`` is something an agent proposes to the operator, who sets
    ``work.validate_cmd`` explicitly."""
    per = work_value(cfg, entry, "validate", {}) or {}
    keys = [f"{phase}-main", phase] if (phase and main_gate) else [phase]
    for key in keys:
        if key and key in per:
            return str(per[key])
    return str(work_value(cfg, entry, "validate_cmd", "just check"))


def validate_cmd_is_configured(cfg, entry) -> bool:
    """Whether the operator has explicitly set ``work.validate_cmd`` (per-hive or global),
    as opposed to silently riding the built-in ``just check`` default. Feeds the
    ``bh doctor`` / ``bh hive ready`` nudge (bh-l44i): a *named* weak gate (the operator
    chose it, even if it's compile-only) is fine; an *unnamed* one — nobody ever looked —
    is what quietly lets test regressions merge clean.

    Whether an unconfigured default actually looks test-free is a separate question — see
    ``validate_probe.probe_validate_cmd``, which resolves (rather than pattern-matches) the
    command against the hive's own justfile."""
    return layered(cfg, entry, "work", "validate_cmd", _UNSET) is not _UNSET


def validation_mode(cfg, entry):
    """Which merge boundaries re-validate the integration tip:
    relaxed (default — today: submit + assembled-mol pre-land only) |
    conservative (also re-test the tip after every per-bead merge AND post-land) |
    loose (trust per-bead submits — skip even the assembled-mol pre-land check).
    Unknown values fall back to relaxed."""
    mode = str(work_value(cfg, entry, "validation", "relaxed"))
    return mode if mode in ("relaxed", "conservative", "loose") else "relaxed"


def demo_cmd(cfg, entry):
    """How `ws work review --demo` exercises the feature with the real app (default none)."""
    return str(work_value(cfg, entry, "demo_cmd", ""))


def review_gate(cfg, entry):
    """bd gate type opened at submit: human | timer | gh:run | gh:pr (default human)."""
    return str(work_value(cfg, entry, "review_gate", "human"))


def work_runtime(cfg, entry):
    """Which scheduler wakes a role binary for a ready bead: claude (Task-tool sub-agent
    fanout, documented not developed) | local (poll loop, harness-agnostic default) |
    temporal (Temporal workers). Config key `work.runtime`, default `local`. Unknown values
    fall back to `local` here (the runtime seam's own `get_runtime` raises loudly instead —
    this getter mirrors `work_landing`/`review_gate`'s tolerant-getter shape so a hand-edited
    bad value never crashes an unrelated `bh` invocation that only reads config)."""
    mode = str(work_value(cfg, entry, "runtime", "local"))
    return mode if mode in ("claude", "local", "temporal") else "local"


def work_landing(cfg, entry):
    """How merge/finish land onto the SHARED integration branch: local (default — a --no-ff
    merge in the clone) | pr (PR-only-main repos: push the branch + open a GitHub PR; CI and
    the PR merge take over the postland role, `work land` completes the close). Unknown values
    fall back to local. Only the shared-branch boundary is PR-governed — a bead landing into
    its molecule container (`wt/bead/epic/<epic>`) always merges locally."""
    mode = str(work_value(cfg, entry, "landing", "local"))
    return mode if mode in ("local", "pr") else "local"


def push_remote(cfg, entry):
    """The git remote branch pushes target: submit's out-of-process (`gh:*`) publish and the
    `landing: pr` push. Config key `work.push_remote`, default origin.

    A `kind=external` (contribution) hive always resolves to `origin` — that's the fork
    onboarding forked+cloned us write access to (bh-uxam.1); `work.push_remote` is a
    same-repo-family knob (e.g. `landing: pr`) and must never redirect a contribution push
    at `upstream`, which is pull-only."""
    if str((entry or {}).get("kind", "")) == "external":
        return "origin"
    return str(work_value(cfg, entry, "push_remote", "origin"))


def integration_branch(cfg, entry):
    """The branch a bead branch merges back to / is measured against (default main)."""
    return str(work_value(cfg, entry, "integration_branch", "main"))


def pr_base(cfg, entry):
    """The PR base branch NAME for a `kind=external` (contribution) hive — the branch on
    `upstream` a contribution ultimately lands on. Reuses `integration_branch` (default
    "main"): for a contribution hive that config key stops meaning "the local branch we
    merge onto" and instead names the upstream branch a worktree bases off of / a PR
    targets (`worktree.pr_base_ref` resolves the actual `upstream/<name>` git ref)."""
    return integration_branch(cfg, entry)


def max_commits(cfg, entry):
    """submit rejects a branch with more than this many commits over the base (default 10)."""
    return int(work_value(cfg, entry, "max_commits", 10))


#: How long a recorded validation verdict stays reusable, as an ISO-8601 duration. P1D is
#: exactly the 24h `validation_ledger` has shipped since bh-dfx0 — a re-expression of the
#: existing default as a duration, not a behavior change.
DEFAULT_LEDGER_TTL = "P1D"
_DURATION = TypeAdapter(datetime.timedelta)


def duration_seconds(value: str, default: str = DEFAULT_LEDGER_TTL) -> int:
    """An ISO-8601 duration (`PT30M` / `PT4H` / `P1D` / `P1DT2H`) as whole seconds, falling back
    to `default` when the string doesn't parse. Parsing is pydantic's — already a core dep, so
    no hand-rolled grammar and no new dependency. A bad value is *also* rejected up front by
    `config_schema.WorkConfig.ledger_ttl`, so this fallback only catches a hand-edited config
    that never went through validation: a typo must not fail an unrelated `bh` command."""
    for candidate in (value, default):
        try:
            return int(_DURATION.validate_python(candidate).total_seconds())
        except (ValueError, TypeError):
            continue
    return 24 * 60 * 60


def ledger_ttl(cfg, entry) -> int:
    """Seconds a green validation verdict stays reusable — `work.ledger_ttl`, per-hive over
    global, default `P1D` (attested-green ADR, Decision 3; `validation_ledger`). The realistic
    reuse window is minutes-to-hours: operators are expected to tune this **DOWN**, not up. A
    pass from this morning is stronger evidence about an identical tree than a pass from last
    week, and a short TTL is cheap insurance against environmental rot — a green recorded before
    a toolchain upgrade says nothing about after it."""
    return duration_seconds(str(work_value(cfg, entry, "ledger_ttl", DEFAULT_LEDGER_TTL)))


def enforce_signing(cfg, entry) -> bool:
    """Whether the merge path refuses a branch carrying any commit git cannot verify as TRUSTED
    (default False). See `config_schema.WorkConfig.enforce_signing` for what it gates, why it is
    off by default, and why it has no grandfathering clause."""
    return bool(work_value(cfg, entry, "enforce_signing", False))


def batch_max_size(cfg, entry):
    """Max issues a planner-declared `batch:<group>` may hold (handled+validated+merged as one
    unit). Default 5 — keeps a batch bubble small enough to stay reviewable / bisectable."""
    return int(work_value(cfg, entry, "batch_max_size", 5))


def dispatch_value(cfg, entry, key, default=None):
    """A work.dispatch setting: per-hive `entry['work']['dispatch'][key]` >
    global `work.dispatch[key]` > default (work_value, one level deeper)."""
    return layered(cfg, entry, "work.dispatch", key, default)


def dispatch_mode(cfg, entry):
    """How the coordinator dispatches ready beads: fanout (one bead per developer
    sub-agent) | collapsed (batch beads into a shared session) | auto (choose by budget).
    Config key `work.dispatch.mode`, default fanout. Unknown values fall back to fanout."""
    mode = str(dispatch_value(cfg, entry, "mode", "fanout"))
    return mode if mode in ("fanout", "collapsed", "auto") else "fanout"


def dispatch_max_depth(cfg, entry):
    """How deep the coordinator may nest sub-agent dispatch: 0 (no sub-agents) |
    1 | 2. Config key `work.dispatch.max_depth`, default 2. Out-of-range values clamp to 2."""
    depth = int(dispatch_value(cfg, entry, "max_depth", 2))
    return depth if depth in (0, 1, 2) else 2


def dispatch_max_beads_per_session(cfg, entry):
    """Max beads a single collapsed dispatch session may hold before the coordinator
    fans out instead. Config key `work.dispatch.max_beads_per_session`, default 8."""
    return int(dispatch_value(cfg, entry, "max_beads_per_session", 8))


def dispatch_auto_budget(cfg, entry):
    """Budget (in m-sized-beads worth of work) an `auto`-mode session may absorb before
    the coordinator splits it. Config key `work.dispatch.auto_budget`, default 8."""
    return int(dispatch_value(cfg, entry, "auto_budget", 8))


def dispatch_max_action_retries(cfg, entry):
    """The `bh work next` loop-breaker threshold: escalate once a bead's own event record already
    shows N identical failed attempts of the same action. Config key
    `work.dispatch.max_action_retries`, default 2 (so the third attempt escalates).

    The count is DERIVED by counting event beads (`work_next.attempt_count`) — this knob sets a
    threshold, never a stored counter. Values below 1 clamp to 1: a threshold of 0 would escalate
    before anything had been tried."""
    return max(int(dispatch_value(cfg, entry, "max_action_retries", 2)), 1)


def dispatch_review_mode(cfg, entry):
    """Who reviews a dispatched bead: self (the developer self-reviews) | fresh (a
    separate reviewer seat). Config key `work.dispatch.review_mode`, default self.
    Unknown values fall back to self.

    `paired` (two seats sign off) depends on the resumable-agent spike and is not yet
    wired; selecting it does NOT silently no-op — it falls back to `fresh` with a
    warning so the bead still gets an independent reviewer rather than an unreviewed
    gate."""
    mode = str(dispatch_value(cfg, entry, "review_mode", "self"))
    if mode == "paired":
        from . import log  # lazy: keep config free of the log↔config import cycle

        log.get_logger(__name__).warning(
            "review_mode_paired_fallback",
            requested="paired",
            effective="fresh",
            reason="paired review depends on the resumable-agent spike; not yet wired",
        )
        return "fresh"
    return mode if mode in ("self", "fresh") else "self"


def dispatch_reviewer_cross_seat(cfg, entry):
    """The reviewer cross-seat policy (roles/RBAC matrix §3): what happens when the seat approving
    a review gate is the same person who authored the bead (a rubber-stamp risk — including an
    agent self-approving its own dispatched work). `hard` (default, bh-e5kv) BLOCKS the
    self-approval so a `type:human` review gate always gets an independent sign-off; `advise` WARNS
    but lets the approval through — an explicit opt-out for a hive that knowingly runs a live-human-
    watching collapsed session (`review_mode: self`) and wants the shortcut back. Config key
    `work.dispatch.reviewer_cross_seat`; unknown values fall back to `hard` (fail closed — the
    review gate is a security boundary, not a UX nicety; was `advise` before bh-e5kv, which let the
    same self-approval action land sometimes blocked, sometimes merely warned, depending on
    whether the calling agent chose to heed an advisory message)."""
    mode = str(dispatch_value(cfg, entry, "reviewer_cross_seat", "hard"))
    return mode if mode in ("advise", "hard") else "hard"


def _dispatch_positive_float(cfg, entry, key, default, *, allow_zero=False):
    """A `work.dispatch.<key>` float that must not be negative (and, unless *allow_zero*, must
    not be zero either). A hand-edited `poll_interval: -1` would busy-spin the local runtime and
    a `terminate_grace: 0` would SIGKILL before SIGTERM could ever be honored, so both fall back
    to the default rather than being obeyed — the same tolerant-getter shape the other dispatch
    accessors use for an unknown enum value."""
    try:
        value = float(dispatch_value(cfg, entry, key, default))
    except (TypeError, ValueError):
        return float(default)
    if value < 0 or (value == 0 and not allow_zero):
        return float(default)
    return value


def dispatch_poll_interval(cfg, entry):
    """Seconds the `local` runtime sleeps between poll passes. Config key
    `work.dispatch.poll_interval`, default 5.0. Gate latency is bounded by this
    (work-runtime-tiers-adr.md Limitation 1) — a push doorbell is explicitly out of scope."""
    return _dispatch_positive_float(cfg, entry, "poll_interval", 5.0)


def dispatch_max_concurrency(cfg, entry):
    """How many seat processes the `local` runtime may hold in flight at once. Config key
    `work.dispatch.max_concurrency`, default 2; values below 1 clamp to 1 (a cap of 0 would be a
    loop that can never dispatch, which is a silent stall rather than a bound).

    IN-PROCESS ONLY, and it resets on restart by design: it describes this loop process's own
    children, and a restarted process has none (loop-ownership-and-execution-memory-adr.md
    Decision 2)."""
    try:
        return max(int(dispatch_value(cfg, entry, "max_concurrency", 2)), 1)
    except (TypeError, ValueError):
        return 2


def dispatch_max_run_seconds(cfg, entry):
    """Per-run wall-time cap for one seat process. Config key `work.dispatch.max_run_seconds`,
    default 1800.0; `0` disables the cap. In-process, reset on restart — the sibling of
    `dispatch_max_concurrency` and the second half of v1's caps (token-budget enforcement is
    explicitly out and defers to bh-3yoh)."""
    return _dispatch_positive_float(cfg, entry, "max_run_seconds", 1800.0, allow_zero=True)


def dispatch_terminate_grace(cfg, entry):
    """Seconds between the reaper's group SIGTERM and its group SIGKILL. Config key
    `work.dispatch.terminate_grace`, default 5.0. The loop polls until the group is actually
    gone rather than assuming either signal worked (bh-a7so.2 §3)."""
    return _dispatch_positive_float(cfg, entry, "terminate_grace", 5.0)


def dispatch_envelope_grace(cfg, entry):
    """Seconds the loop holds the child's stdout pipe after signalling it, waiting for the priced
    envelope BEFORE reaping the group. Config key `work.dispatch.envelope_grace`, default 3.0
    (the envelope was measured at ~0.63s, bh-a7so.7 §4). That patience is the whole difference
    between a priced, attributed cancel and a silent one."""
    return _dispatch_positive_float(cfg, entry, "envelope_grace", 3.0)


def dispatch_seat_command(cfg, entry):
    """The command template the `local` runtime spawns for a seat. Config key
    `work.dispatch.seat_command`, default `bh-{role}` — the argv head of the settled role-binary
    contract (`bh-<seat> --workspace … --bead … --instructions … --session_id …`). Shell-split,
    with `{role}` substituted; a hive that installs its seat binaries elsewhere (or a demo that
    points at the reference stub) overrides this rather than patching the runtime."""
    return str(dispatch_value(cfg, entry, "seat_command", "") or "bh-{role}")


def dispatch_seat_bundle(cfg, entry) -> str:
    """The seat bundle `bh work loop` hands every seat it spawns, as `--bundle <path>`.

    Config key `work.dispatch.seat_bundle`. Default: the bundle shipped in this package
    (`assets/seat-bundle.json`) — NOT empty (bh-xrg1f).

    WHY THERE IS A DEFAULT AT ALL. A seat spawned with no bundle resolves baml-harness's
    `bare_seat`: `permission_mode: "plan"` plus a closed roster whose `ask: ["Bash(*)"]` is a
    refusal under headless `-p`. That is the right default for an unknown caller and the wrong
    one for this caller — a dispatched write seat that can reason but never act costs a full
    model turn per bead to produce `run_blocked`, then re-dispatches into the same denial until
    the loop-breaker fires on attempt count. It presents as an epic that spends money and never
    progresses rather than as an error.

    Set it to a path to use your own bundle. Set it to `"-"` to pass NO bundle and get the
    default-closed seat back — spelled explicitly, because "" cannot mean both "unset, give me
    the default" and "deliberately none".
    """
    declared = str(dispatch_value(cfg, entry, "seat_bundle", "") or "")
    if declared == "-":
        return ""
    return declared or str(asset("seat-bundle.json"))


def union_globs(cfg, entry) -> list:
    """Globs naming append-only files eligible for union conflict resolution.

    Resolved: per-hive ``entry['work']['conflict']['union_globs']`` > global
    ``work.conflict.union_globs`` > default ``[]`` (union disabled).
    """
    hive_conflict = ((entry or {}).get("work") or {}).get("conflict") or {}
    if "union_globs" in hive_conflict:
        return list(hive_conflict["union_globs"])
    glob_conflict = work_cfg(cfg).get("conflict") or {}
    if "union_globs" in glob_conflict:
        return list(glob_conflict["union_globs"])
    return []


def work_identity(cfg, entry, actor=""):
    """Merged agent identity profile (per-hive work.identity over global), normalized to
    {mode, name, email, signing_key, sign}. mode defaults to 'agent' when any field is set,
    else 'supervised' (inherit the human's git/signing config — stamp nothing).

    Per-developer attribution: when `actor` (a dev/<name>) names an entry in the `devs` mapping
    (`work.identity.devs[dev/<name>]` → {email, signing_key, sign, optional name}), that
    developer's overrides layer over the base identity so each developer's commits are authored +
    SSH-signed as its own seat — real ledger attribution, distinct from the human and from
    sibling developers. Default behavior is unchanged when no devs are configured or `actor` is
    empty.

    Key decision (bead .28): the mapping key is `devs` (matching the `dev/` seat prefix per the
    roles/RBAC matrix). The legacy key `crews` is still honored as a DEPRECATED alias — `devs`
    entries win on collision — so existing configs keep resolving through the migration window
    (removed later per limn/kkke sequencing)."""
    glob = dict(work_cfg(cfg).get("identity", {}) or {})
    hive = dict(((entry or {}).get("work", {}) or {}).get("identity", {}) or {})
    merged = {**glob, **hive}
    # `devs` is the canonical key; `crews` is the deprecated legacy alias (devs wins on collision).
    devs = {
        **(glob.get("crews") or {}),
        **(hive.get("crews") or {}),
        **(glob.get("devs") or {}),
        **(hive.get("devs") or {}),
    }
    merged.pop("crews", None)
    merged.pop("devs", None)
    if actor and actor in devs:
        merged = {**merged, **(dict(devs[actor] or {}))}
    mode = merged.get("mode") or ("agent" if merged else "supervised")
    return {
        "mode": mode,
        "name": merged.get("name"),
        "email": merged.get("email"),
        "signing_key": merged.get("signing_key"),
        "sign": bool(merged.get("sign", False)),
    }


def claim_authority(cfg, entry) -> str:
    """Named `ClaimAuthority` (claim_authority.py) `bh work claim`/`submit` use to mint + resolve
    the acting seat: default `local` (Tier 0, `LocalTrustAuthority` — LOCAL-TRUST ONLY, see that
    module's docstring). Config key `work.identity.authority`, layered per-hive over global."""
    glob = dict(work_cfg(cfg).get("identity", {}) or {})
    hive = dict(((entry or {}).get("work", {}) or {}).get("identity", {}) or {})
    merged = {**glob, **hive}
    return str(merged.get("authority") or "local")


# ---- release (release-order planning, bh-k2j8) -------------------------------
# Advisory release-order policy consulted by the dispatcher's start-verdict and the
# merger's merge-order (release_order.py, sibling beads) — never obeyed blindly, and a
# no-op when unset (falls back to today's FCFS behavior).
# Precedence: per-hive entry['release'][key] > global release[key] > built-in default.


__all__ = [
    "work_cfg",
    "work_value",
    "routing_policy",
    "routing_tiers",
    "validate_cmd",
    "validate_cmd_is_configured",
    "validation_mode",
    "demo_cmd",
    "review_gate",
    "work_runtime",
    "work_landing",
    "push_remote",
    "integration_branch",
    "pr_base",
    "max_commits",
    "DEFAULT_LEDGER_TTL",
    "_DURATION",
    "duration_seconds",
    "ledger_ttl",
    "enforce_signing",
    "batch_max_size",
    "dispatch_value",
    "dispatch_mode",
    "dispatch_max_depth",
    "dispatch_max_beads_per_session",
    "dispatch_auto_budget",
    "dispatch_max_action_retries",
    "dispatch_review_mode",
    "dispatch_reviewer_cross_seat",
    "_dispatch_positive_float",
    "dispatch_poll_interval",
    "dispatch_max_concurrency",
    "dispatch_max_run_seconds",
    "dispatch_terminate_grace",
    "dispatch_envelope_grace",
    "dispatch_seat_command",
    "dispatch_seat_bundle",
    "union_globs",
    "work_identity",
    "claim_authority",
]
