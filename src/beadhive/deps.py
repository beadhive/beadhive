"""deps.py — the ONE table of external tools bh depends on (bh-hsus.2 / bh-hsus.3).

bh had SEVEN overlapping registries of "external things bh depends on" (plus an eighth
hand-mirrored in `flake.nix` comments), and nothing reconciled them — the compiler could not
tell you when you forgot one. This module is the single declarative table they derive from::

    setup.PROBE_TABLE     = [d for d in DEPS if d.required == "always"]
    setup.RUNTIME_PROBES  = group "store-runtime", selector `dolt.backend`
    harness.HARNESSES     = [d for d in DEPS if d.install]
    role.KNOWN_HARNESSES  = [d for d in DEPS if d.runs_seats]
    credential probes     = [d for d in DEPS if d.auth]

**A DECLARED ROUTE IS NOT A bh-DRIVEN INSTALL.** ``d.install`` says bh knows HOW this tool
arrives; ``d.install.cmd`` says bh will run it. bh-hsus.1 split those: codex declares a route
(brew / a GitHub release / ``nixpkgs#codex``) with ``cmd=None``, because no single command
works on every plane and this table deliberately does not branch on plane. So
`harness.HARNESSES` is every row with a route (:func:`has_install_route`), while
:func:`installable` — the strictly smaller set bh can actually execute — is claude alone.

**REQUIRED vs OPTIONAL IS THE TYPE BOUNDARY, not a column.** A `Dep` is *required for this
version of bh*; a `plugins.Plugin` is an *optional integration*. `cli` / `readiness` /
`binary` / `version_cmd` would belong to both, but `required` belongs only to deps and
`enabled` only to plugins — and they stop competing once they sit on different types. That is
why this module has no `enabled` field and `plugins.py` has no `required` field.

**`required` HAS EXACTLY TWO VALUES.** ``"always"`` (unconditional) and ``"group:<name>"``
(config selects the member). :func:`is_required` is therefore two branches, not a predicate
DSL. "At least one of" collapses the model rather than complicating it: the container runtime
and the agent harness are the SAME shape — a group whose selector is a config value.
``dolt.backend: jsonl`` selects nothing and nothing in that group is required; that falls out
with no special case, which is the signal the shape is right.

**ONE ROW BENDS THAT, AND IT IS NAMED RATHER THAN HIDDEN.** ``codex`` is a member of the
``agent`` group whose selector can never name it (`config_schema` types the field
``Literal["claude", "opencode"]``), so ``is_required(codex)`` is False *by construction*, not
merely False today. That is NOT the ``backend: jsonl`` case — jsonl is a selector VALUE
outside the member set, and every member of that group remains reachable. For codex, group
membership carries no requirement semantics at all; what it actually encodes is the CATEGORY
``kind="harness"`` already encodes. Two values still partition the table as written, but
membership is quietly doing duty as a third, "never required". Left alone deliberately —
nothing branches on it (every derivation above reads `install` / `runs_seats` / `auth`, none
reads `required`) and bh-hsus.5 owns the fix. See
``docs/spikes/bh-hsus.2-dependency-table.md`` § "Q1 follow-on".

**setup.probe_one() STAYS THE ONE DETECTION MECHANISM.** :func:`present` delegates to it
rather than adding a second `shutil.which()`. Detection is two SEPARATE stages —
:func:`present` (stage 1, "is it here") and the auth probe (stage 2, "is it usable") — because
`bh setup check`'s in-image manifest path is contractually zero-subprocess
(`test_setup_manifest.py`) and every auth probe shells out. The gates stay separate; the TABLE
is one.

**NOT A PLUGIN SYSTEM.** No dynamic loading, no registry protocol, no third-party extension
points — ~10 known tools in a hand-written list, exactly like `plugins.registry()`.

Import-cheap on purpose: `setup` is imported early in the CLI's start-up path, so nothing here
imports `typer`, `config`, or `setup` at module level. The two selectors and :func:`present`
import lazily inside the call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# ---- record types ---------------------------------------------------------------

#: The unconditional value of :attr:`Dep.required`.
ALWAYS = "always"

#: Prefix of the only other legal :attr:`Dep.required` value: ``group:<name>``.
GROUP_PREFIX = "group:"


@dataclass(frozen=True)
class Auth:
    """Stage 2: how a dep's CREDENTIAL arrives, and what to run when it hasn't.

    ``env_vars`` are read in order and reported by NAME, never by value — the whole point of
    naming the provenance rather than the secret (see `harness_auth`, which owns the probing;
    this is only the declaration it reads).
    """

    env_vars: tuple[str, ...] = ()
    login: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallRoute:
    """How one dep gets onto PATH — remedy text with a command attached only where bh genuinely
    drives the install.

    Field-for-field `harness.InstallRoute` (bh-hsus.1), which is the authority on this shape:
    ``cmd`` of ``None`` means bh does not install this tool and ``note`` is the whole remedy;
    a ``cmd`` is a COMPLETE argv, optionally taking one appended version argument — not a
    prefix awaiting a package name. Deliberately NOT branched by platform: where a route
    differs by plane (macOS vs Linux vs Nix) that lives in ``note`` as text, per ADR
    Decision 5 (bh-q160.12).

    HAND-MIRRORED, with a gate instead of an import (bh-hsus.3). `harness` imports `typer`,
    and this module is on `setup`'s import-cheap start-up path, so the rows below restate
    `harness.HARNESSES`'s values rather than importing them;
    `test_deps_characterization.py::test_harness_records_are_reproduced_field_for_field`
    fails the moment the two disagree. Same posture as `flake.nix`'s toolchain list
    (bh-hsus.2 Q4): mirror plus drift test beats codegen at this size. bh-hsus.5 collapses the
    mirror by making `harness.HARNESSES` derive from here.
    """

    cmd: tuple[str, ...] | None = None
    note: str = ""
    proprietary: bool = False


@dataclass(frozen=True)
class Dep:
    """One external tool bh requires — always, or when config selects it from a group.

    ``binary`` is the basename `shutil.which` resolves (``git-workspace``); ``version_cmd`` is
    how you ask it its version (``git workspace --version``), and the two genuinely differ.

    ``runs_seats`` is a CAPABILITY, deliberately independent of group membership: a tool can
    have a known install route and a credential probe without being able to exec a seat. That
    disagreement is the bug the word "harness" was hiding (codex is authenticatable and has a
    documented route but cannot run a seat; opencode can run a seat but bh can neither install
    nor authenticate it), and it is only expressible because these are separate fields.

    ``license`` and ``version_env`` sit HERE rather than on :class:`InstallRoute` because
    that is where `harness.Harness` puts them (bh-hsus.1) — the licence is a fact about the
    tool, the route is a fact about one way of getting it. Both are empty for rows that are
    neither proprietary nor version-pinnable, which is every infra row today.
    """

    name: str
    binary: str
    version_cmd: tuple[str, ...]
    required: str
    kind: str = "infra"
    runs_seats: bool = False
    license: str = ""
    #: Environment variable naming a BOOTSTRAP-target override, consulted only when the tool is
    #: absent — see `harness.Harness.version_env`, whose semantics this mirrors.
    version_env: str = ""
    auth: Auth | None = None
    install: InstallRoute | None = None

    @property
    def group(self) -> str:
        """The group this row belongs to, or ``""`` when it is required unconditionally."""
        return self.required[len(GROUP_PREFIX) :] if self.required != ALWAYS else ""


@dataclass(frozen=True)
class Group:
    """A set of interchangeable deps of which CONFIG selects at most one.

    ``select(cfg)`` returns the selected member's name — or any other string, which simply
    matches no member and makes nothing in the group required. ``selector`` is the config key
    to name in a diagnostic, so an operator is told WHERE the choice was made.
    """

    name: str
    select: Callable[[Any], str]
    selector: str


# ---- selectors ------------------------------------------------------------------


def store_runtime_selection(cfg: dict | None = None) -> str:
    """``dolt.backend``: ``colima`` / ``docker`` / ``podman`` name a runtime; ``none`` and the
    absent-section default ``jsonl`` name none of them, so nothing in the group is required.

    Exception-tolerant by design and by precedent — this is the same body `setup._backend_tag`
    has always had, moved here so the group has ONE selector rather than two. A hive whose
    config cannot be loaded must still be able to run `bh setup check`, which is frequently
    the very command that diagnoses why.
    """
    from . import config

    try:
        c = cfg if cfg is not None else config.load()
        backend = config.dolt_cfg(c).get("backend", "jsonl")
        return str(backend) if backend else "jsonl"
    except Exception:
        return "jsonl"


def agent_selection(cfg: dict | None = None) -> str:
    """``config.harness_name()``: which harness `bh role <seat>` execs. Defaults to ``claude``.

    Note what this CANNOT return: the config schema types this field
    ``Literal["claude", "opencode"]``, so ``codex`` is a declared member of the agent group
    that config can never select — and per bh-hsus.2's evidence (codex 0.146.0 has no
    ``--agent``-equivalent flag) *excluding* it is correct, not an oversight.

    Its group MEMBERSHIP is a different question, and the answer is not the one bh-hsus.2
    first gave: this is not the same shape as ``dolt.backend: jsonl``. jsonl is a selector
    value outside the member set, with every member still reachable; codex is a member outside
    the selector's range, reachable by nothing. See the module docstring — behaviour is
    unchanged and bh-hsus.5 owns it.
    """
    from . import config

    try:
        return config.harness_name(cfg)
    except Exception:
        return "claude"


GROUPS: dict[str, Group] = {
    "store-runtime": Group("store-runtime", store_runtime_selection, "dolt.backend"),
    "agent": Group("agent", agent_selection, "harness"),
}


# ---- the table ------------------------------------------------------------------

# Order is load-bearing: `setup.PROBE_TABLE` derives from it, and `bh setup check` prints in
# table order. The four unconditional rows come first, in their historical order.
DEPS: tuple[Dep, ...] = (
    # -- required always ----------------------------------------------------------
    Dep(
        name="git-workspace",
        binary="git-workspace",
        version_cmd=("git", "workspace", "--version"),
        required=ALWAYS,
    ),
    Dep(
        name="gh",
        binary="gh",
        version_cmd=("gh", "--version"),
        required=ALWAYS,
        # gh reads these in order and either takes precedence over any stored login, which is
        # why a headless factory host needs no `gh auth login` at all.
        auth=Auth(env_vars=("GH_TOKEN", "GITHUB_TOKEN"), login=("gh", "auth", "login", "--web")),
    ),
    Dep(
        name="bd",
        binary="bd",
        version_cmd=("bd", "--version"),
        required=ALWAYS,
    ),
    Dep(
        name="dolt",
        binary="dolt",
        version_cmd=("dolt", "version"),
        required=ALWAYS,
    ),
    # -- group: store-runtime (selector `dolt.backend`) ---------------------------
    # colima is a macOS affordance (a VM to get a docker daemon); a Linux seat uses native
    # docker; a seat that never hosts the dolt sql-server needs no runtime at all.
    Dep(
        name="colima",
        binary="colima",
        version_cmd=("colima", "--version"),
        required=f"{GROUP_PREFIX}store-runtime",
    ),
    Dep(
        name="docker",
        binary="docker",
        version_cmd=("docker", "--version"),
        required=f"{GROUP_PREFIX}store-runtime",
    ),
    Dep(
        name="podman",
        binary="podman",
        version_cmd=("podman", "--version"),
        required=f"{GROUP_PREFIX}store-runtime",
    ),
    # -- group: agent (selector `config.harness_name()`) --------------------------
    Dep(
        name="claude",
        binary="claude",
        version_cmd=("claude", "--version"),
        required=f"{GROUP_PREFIX}agent",
        kind="harness",
        runs_seats=True,
        license="SEE LICENSE IN README.md (proprietary — Anthropic's commercial terms)",
        version_env="BH_CLAUDE_CODE_VERSION",
        # CLAUDE_CODE_OAUTH_TOKEN is the account's own credential — revocable, no API-billing
        # path; ANTHROPIC_API_KEY is the billing fallback.
        auth=Auth(
            env_vars=("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
            login=("claude", "setup-token"),
        ),
        # The NATIVE bootstrap, not npm (bh-hsus.1): an npm copy installed alongside claude's
        # own installer shadows it on PATH by luck of ordering — a live bug, found and fixed on
        # the Linux test-bed. bh only ever bootstraps a host that has no claude yet; after that
        # claude owns its own version.
        install=InstallRoute(
            cmd=("bash", "-c", 'curl -fsSL https://claude.ai/install.sh | bash -s -- "$@"', "bash"),
            note=(
                "bh only bootstraps a host with no claude on it yet. After that: "
                "`claude install <stable|latest|X.Y.Z>` to change version, `claude update` to "
                "check for updates — background auto-update is on by default (`claude doctor`)."
            ),
            proprietary=True,
        ),
    ),
    Dep(
        name="codex",
        binary="codex",
        version_cmd=("codex", "--version"),
        required=f"{GROUP_PREFIX}agent",
        kind="harness",
        # runs_seats=False: codex 0.146.0 has no `--agent`-equivalent flag — `codex --agent X`
        # exits "unexpected argument '--agent' found", and `-p/--profile` layers TOML config,
        # not an agent definition. Evidence: docs/spikes/bh-hsus.2-dependency-table.md.
        license="Apache-2.0",
        auth=Auth(env_vars=("OPENAI_API_KEY",), login=("codex", "login")),
        # A ROUTE bh knows but does not drive (`cmd=None`, bh-hsus.1). Three plane-specific
        # routes and no universal one; naming them in prose is the alternative to branching on
        # platform inside the table, which ADR Decision 5 (bh-q160.12) forbids.
        install=InstallRoute(
            cmd=None,
            note=(
                "brew install --cask codex (macOS) · a release binary from "
                "https://github.com/openai/codex/releases (Linux) · `nixpkgs#codex` where Nix is "
                "the plane (bh-q160.12) — bh does not drive any of these itself."
            ),
        ),
    ),
    Dep(
        name="opencode",
        binary="opencode",
        version_cmd=("opencode", "--version"),
        required=f"{GROUP_PREFIX}agent",
        kind="harness",
        runs_seats=True,
        # No `install` and no `auth`: bh can neither install nor authenticate opencode. That
        # asymmetry against codex (installable + authable, cannot run a seat) is exactly what
        # the single word "harness" used to hide.
    ),
)


# ---- lookups + predicates -------------------------------------------------------


def by_name(name: str) -> Dep:
    """The row called *name*. Raises ``KeyError`` — an unknown dep is a bug in bh, not input."""
    for dep in DEPS:
        if dep.name == name:
            return dep
    raise KeyError(f"unknown dep {name!r}. Known: {', '.join(d.name for d in DEPS)}")


def always_required() -> list[Dep]:
    """Rows required unconditionally, in table order."""
    return [d for d in DEPS if d.required == ALWAYS]


def group_members(group: str) -> list[Dep]:
    """Rows in *group*, in table order."""
    return [d for d in DEPS if d.group == group]


def seat_runners() -> list[Dep]:
    """Rows that can actually exec a seat (`bh role <seat>`)."""
    return [d for d in DEPS if d.runs_seats]


def has_install_route() -> list[Dep]:
    """Rows with a documented way in, whether or not bh drives it — `harness.HARNESSES`."""
    return [d for d in DEPS if d.install is not None]


def installable() -> list[Dep]:
    """The strictly smaller set bh will actually run an install command for.

    Was the same query as :func:`has_install_route` until bh-hsus.1 gave codex ``cmd=None``;
    they are now different sets and conflating them is what would make `bh dep install codex`
    promise something that exits 1 (`harness.missing_hint`'s bug, one hop later).
    """
    return [d for d in DEPS if d.install and d.install.cmd]


def authenticated_deps() -> list[Dep]:
    """Rows that need a credential, i.e. that have a stage-2 gate at all."""
    return [d for d in DEPS if d.auth]


def is_required(dep: Dep, cfg: dict | None = None) -> bool:
    """Is *dep* required under *cfg*? Two branches, deliberately — see the module docstring."""
    if dep.required == ALWAYS:
        return True
    return GROUPS[dep.group].select(cfg) == dep.name


def required_deps(cfg: dict | None = None) -> list[Dep]:
    """Every row required under *cfg*, in table order."""
    return [d for d in DEPS if is_required(d, cfg)]


# ---- the two detection stages ---------------------------------------------------


def present(dep: Dep) -> bool:
    """Stage 1: is *dep*'s binary on PATH? Delegates to `setup.probe_one`, the ONE detection
    mechanism — this never grows a second `shutil.which()`."""
    from . import setup  # lazy: setup derives its probe tables from this module

    return bool(setup.probe_one(dep.name, dep.binary, list(dep.version_cmd))["found"])


def satisfied(dep: Dep, *, authenticated: bool | None = None) -> bool:
    """``present AND (no auth OR authenticated)``.

    *authenticated* is passed IN rather than probed here: stage 2 shells out, and folding it
    into a function `setup check` could reach would break the in-image manifest path's
    zero-subprocess contract. A row that needs a credential with no stage-2 answer supplied is
    reported unsatisfied — the conservative direction for a gate.
    """
    if not present(dep):
        return False
    if dep.auth is None:
        return True
    return bool(authenticated)
