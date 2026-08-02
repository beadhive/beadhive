# Extending SlopCodeBench for Software Factories

*Research findings on `SprocketLab/slop-code-bench` (SCBench) — what it measures, how granular it
gets, whether it can benchmark Beadhive-as-orchestrator against a bare harness as-is, and what we
would have to contribute upstream to make it a credible benchmark for a multi-agent factory with an
improvement loop.*

Sources (fetched 2026-08-01):

- Repo: `SprocketLab/slop-code-bench` @ `main` — cloned and read: `README.md`, `docs/` (agents,
  commands, evaluation, execution, metrics, problems), `configs/`, `src/slop_code/`.
- Problems: `gabeorlanski/scb-problems` @ `main`.
- Paper: *SlopCodeBench*, arXiv `2603.24755v2` (HTML edition).
- Site: `scbench.ai`.

> **Naming:** upstream calls the thing under test an **agent** (`--agent claude_code`). In Beadhive
> vocabulary that is a **harness**. This document says *harness* when it means the CLI/orchestrator
> and *agent* only when quoting upstream identifiers.

---

## 1. TL;DR

- **SCBench is an iterative-degradation benchmark, not a task benchmark.** An agent implements a
  spec, then extends its own code across a sequence of checkpoints in the same workspace. It scores
  correctness *three ways* (strict / ISO / core) and code quality *two ways* (structural erosion,
  verbosity), against a human calibration panel of 473 repos.
- **Its granularity is genuinely good** — symbol → file → checkpoint → problem → run, with ~60
  distinct metrics per checkpoint and `MetricStats` rollups per run.
- **The variable matrix is expressive but manual.** Harness × model × prompt × thinking ×
  environment × pass-policy × budget × one-shot are all first-class config axes; there is no sweep
  runner, so a matrix is a shell loop plus `save_template` interpolation.
- **A Beadhive-vs-bare A/B is runnable today with zero upstream code** via a custom
  `configs/agents/*.yaml` + Docker template. **But five specific things will skew it**, one of them
  (worktrees inside the measured workspace) badly enough to invalidate a run outright.
- **What an as-is run would actually measure is the slop of a factory's incremental sub-steps** —
  real and useful, but not the factory. Three blind spots are structural, not configurable: specs
  are revealed one checkpoint at a time so **there is nowhere to put a pre-planning phase**; the
  workspace *is* the submission so **worktrees and parallel execution have no place to live**; and a
  run is one agent/model/credential so **heterogeneous role-skill-model fleets are unrepresentable**.
- **The gap is accounting and framing, not execution.** SCBench's trajectory schema, usage tracker,
  and result schema are all single-agent-shaped. Sub-agent work is invisible to `steps`,
  unattributable by role, and unrepresentable in `result.json`. Fixing that is a small, obviously
  correct set of upstream PRs that benefit every delegating harness, not just ours. The one deeper
  contribution is a **scoped-iteration mode** — full roadmap visible, delivery still incremental —
  which is what would let a planning plane be evaluated at all.

---

## 2. Methodology

### 2.1 The core construct

Single-shot benchmarks miss path dependence. SCBench evaluates **iterative specification
refinement**: the agent must live with its own earlier architectural decisions.

```text
y₁ = πθ(x₁, y₀)      x = spec, y = workspace
y₂ = πθ(x₂, y₁)
yᵢ = πθ(xᵢ, yᵢ₋₁)
```

Each checkpoint carries the workspace forward. Nothing resets the code.

### 2.2 Corpus

| Property | Value |
|---|---|
| Problems | 36, language-agnostic by design (Python track evaluated) |
| Checkpoints | 196 total, 3–5 per problem typical |
| Phases | Start, Early, Mid, Late, Final |
| Authoring | by the paper's authors, two-phase, each problem reviewed by a non-drafter |
| Distribution | separate repo `gabeorlanski/scb-problems`, plus Harbor dataset registry |

Design principles the corpus enforces:

- **No prescribed internal interfaces.** Specs pin the external CLI/API contract only. The agent
  owns all internal structure — which is what makes erosion measurable.
- **Hidden test suites.** The agent never sees the pytest files.
- **Black-box, language-agnostic specs.**

Per-problem layout:

```text
problems/<name>/
├── config.yaml            # entry_file, checkpoint order/version/state, include_prior_tests,
│                          # static_assets, test_dependencies, custom markers, timeout
├── checkpoint_N.md        # the spec text handed to the agent
├── tests/
│   ├── conftest.py        # required fixtures: entrypoint_argv, checkpoint_name
│   ├── test_checkpoint_N.py
│   └── data/              # optional parametrized case dirs (case.yaml + expected.json)
├── static_assets/         # mounted read-only for all tests
└── solutions/checkpoint_N/  # reference implementations (not used in evaluation)
```

Test categorization is by pytest marker:

| Marker | Group | Meaning |
|---|---|---|
| *(none)* | CORE | must pass to count as solved |
| `@pytest.mark.functionality` | FUNCTIONALITY | optional feature coverage |
| `@pytest.mark.error` | ERROR | error handling / edge cases |
| prior checkpoint files | REGRESSION | re-run automatically when `include_prior_tests` |

### 2.3 Execution model

- Docker, default `configs/environments/docker-python3.12-uv.yaml`
  (`ghcr.io/astral-sh/uv:python3.12-trixie-slim`, workdir `/workspace`, workspace bind-mounted).
- Harness CLI installed by a per-agent Jinja Dockerfile (`docker_template`). The `claude_code` one
  `npm install -g @anthropic-ai/claude-code@{{version}}` as a non-root `agent` user.
- Per the paper: fresh container per turn, **only the working directory persists**.
- **Code accumulates across checkpoints; each test case is isolated** via pytest `tmp_path`.
- After each checkpoint the workspace is snapshotted (tar, with ignore-globs); the metrics pipeline
  runs over the snapshot, not the live container.
- 2-hour wall-clock limit per run. **No turn cap and no cost ceiling in the published runs** —
  though the config supports `step_limit` / `cost_limit` / `net_cost_limit`.

### 2.4 Scoring — three correctness dimensions

This is the sharpest design decision in the benchmark.

| Dimension | Definition | Question it answers |
|---|---|---|
| **Strict** | all tests pass, including regression | solved it *and* didn't break prior work |
| **ISO** | non-regression tests only | solved the new requirements |
| **Core** | spec-mandated core tests only | met the minimum spec |

**The gap between strict and ISO is the measurement of self-inflicted regression.** For a factory
with a review gate, that gap is the primary hypothesis to attack.

### 2.5 Scoring — two quality dimensions

Both bounded `[0,1]` for cross-problem comparability, both emitted by a **pinned external
`scb-check` release** (version recorded per checkpoint as `scb_check_version`).

- **Structural erosion** — fraction of total cyclomatic-complexity *mass* residing in callables with
  `CC > 10`. Mass is size-weighted: `mass.cc = Σ (complexity × √symbol_sloc)`. Measures whether
  complexity *concentrates* in already-complex functions rather than merely growing.
- **Verbosity** — fraction of lines either flagged by ast-grep slop patterns or identified as
  structural duplicates by clone analysis.

Plus an **LLM-judge rubric** pass (`slop-code metrics judge --rubric configs/rubrics/llm_judge.jsonl
--model <openrouter model>`) emitting one record per flagged violation, including whether a
violation was **carried over** from the prior checkpoint.

### 2.6 Human calibration panel

| Property | Value |
|---|---|
| Repos | 473 open-source Python, `<100` to `>10k` stars |
| Commits | up to 30 source-modifying per repo, 13,667 total |
| Result | agent code shows **2.3× verbosity**, **2.0× erosion** vs. human commits |
| Trend | erosion *rises* in **77%** of agent trajectories vs **53%** of human repo histories |
| Extra | pre/post-2024 temporal split within repos |

### 2.7 Headline findings

- **No agent solves any problem end-to-end.** Best strict checkpoint pass rate: **14.8%**.
- 13.18B tokens across all experiments (Composer 2 lowest at 0.38B, Kimi K2.6 highest at 1.14B).
- Quality-aware prompting (`anti_slop`, `plan_first`) reduces *initial* verbosity by up to **34.8%**
  and erosion by up to **62.3%** — but **does not stop iterative degradation**, and costs **+12.1%**
  per checkpoint.
- Native provider harnesses were used deliberately over unified frameworks like SWE-agent, on the
  grounds that "frontier models are trained for their provider's harness rather than generalized
  agent loops."

### 2.8 Stated limitations

- Python-only track despite language-agnostic corpus design.
- Measures *visible* quality issues via static analysis, not maintainability consequences.
- The metrics are design choices — the `CC > 10` erosion threshold and the ast-grep ruleset in
  particular.
- Future work explicitly names "whether specific architectural refactoring strategies during
  iteration might mitigate accumulation." **That is our thesis, named by the authors as an open
  question.**

---

## 3. Architecture

### 3.1 Package layout

```text
src/slop_code/
├── agent_runner/
│   ├── agent.py            # Agent ABC + AgentConfigBase
│   ├── registry.py         # two registries: config class, agent class
│   ├── trajectory.py       # AgentStep | ThinkingStep | ToolUseStep
│   ├── trajectory_parsing.py
│   └── agents/{claude_code,codex,cursor_cli,gemini,kimi_cli,miniswe,opencode,openhands,pi}/
├── execution/
│   ├── workspace.py        # isolated dirs, snapshot restore, file reads
│   ├── session.py          # workspace lifecycle + static assets + runtimes
│   ├── snapshot.py         # tar snapshots with ignore_globs / keep_globs
│   ├── docker_runtime/     # container exec, volumes, --network mode, ports
│   └── local_exec.py
├── entrypoints/
│   ├── config/             # RunConfig → ResolvedRunConfig, OmegaConf interpolation
│   ├── problem_runner/     # driver, worker, state, one_shot, renderer
│   ├── evaluation/
│   └── commands/
├── evaluation/             # pytest orchestration, marker grouping
├── metrics/
│   ├── driver.py           # file walk, exclude patterns, per-file metrics
│   ├── languages/python/   # radon CC, symbol tracing, imports
│   ├── checkpoint/, summary/, rubric/
└── dashboard/              # Dash app: pages, graphs, assets
```

### 3.2 The `Agent` ABC — the extension point

```python
class Agent(ABC):
    @classmethod
    @abstractmethod
    def _from_config(cls, config, model, credential, problem_name,
                     verbose, image, thinking_preset, thinking_max_tokens) -> Agent: ...
    @abstractmethod
    def setup(self, session: Session) -> None: ...
    @abstractmethod
    def run(self, task: str) -> None: ...        # does NOT reset state
    @abstractmethod
    def reset(self) -> None: ...
    @abstractmethod
    def save_artifacts(self, path: Path) -> None: ...
    @abstractmethod
    def cleanup(self) -> None: ...

    def run_checkpoint(self, task) -> CheckpointInferenceResult: ...  # timing + error capture
    def finish_checkpoint(self, reset_context=True) -> None: ...      # reset + accumulate cost
    def hit_net_rate_limit(self) -> bool: ...
    def supports_replay(self) -> bool: ...       # only MiniSWE today
```

Lifecycle: `from_config` → `setup(session)` → per-checkpoint `{run_checkpoint, save_artifacts,
finish_checkpoint}` → `cleanup`.

Registration is two-step: the **config** class auto-registers via `__init_subclass__(agent_type=…,
register=True)`; the **agent** class registers manually via `register_agent("my_agent", MyAgent)`.

### 3.3 State lifetime across checkpoints

| State | Persists? | Reset by |
|---|---|---|
| Conversation history | conditional | `reset()` if `reset_context=True` |
| Working directory | **yes** | managed by Session/Workspace |
| `prior_cost` | **yes, always accumulates** | never |
| `usage` tracker | no | `finish_checkpoint()` creates a new one |

`reset_context` is a **runtime** flag, not per-agent. Default `False` — conversation history is
preserved across checkpoints. The `claude_code` adapter implements that as `claude --continue`.

### 3.4 The `claude_code` adapter specifically

CLI invocation (`_build_cli_args`):

```text
claude --output-format stream-json --verbose [--continue]
       [--append-system-prompt …] [--model …] [--max-turns <step_limit>]
       [--allowedTools …] [--disallowedTools …] [--permission-mode …]
       <extra_args…> --print -- <task>
```

Config surface (`ClaudeCodeConfig`): `version`, `binary`, `extra_args`, `env`, `timeout`,
`append_system_prompt`, `allowed_tools`, `disallowed_tools`, `max_turns`, `permission_mode`,
`settings` (JSON blob), `max_output_tokens`, `base_url`, `docker_template`.

Thinking presets map to `CLAUDE_CODE_EFFORT_LEVEL` (`low|medium|high|xhigh`).

Artifacts saved per checkpoint: `prompt.txt`, `stdout.jsonl`, `stderr.log`, `trajectory.jsonl`, plus
copied native Claude trace files.

**The parser is the important detail.** `agents/claude_code/parser.py` walks the stream-json and
emits one `ToolUseStep` per `tool_use` block **in the main loop only**. A `Task` sub-agent spawn is
one step; everything inside it is invisible. See §6.1.

---

## 4. Granularity and dimensions

### 4.1 Five nested levels

| Level | Artifact | Contents |
|---|---|---|
| Symbol | per-file JSONL | per-function CC, SLOC, statements |
| File | `files.jsonl` | LOC, lint, imports, symbol counts |
| **Checkpoint** | `checkpoint_results.jsonl` (one row) | the full metric set below |
| Problem | aggregated | **sum** cost/time/steps/tokens, **mean** pass rates |
| Run | `result.json` | `MetricStats{mean, stddev, min, max, median, count}` |

Quality metrics, deltas, and composites are **checkpoint-level only** — deliberately, since they are
point-in-time.

A **missing row** in `checkpoint_results.jsonl` means the agent errored or timed out before reaching
that checkpoint. A row with `state == "error"` means it attempted and failed; metrics may be
partial. Only `state == "ran"` has a complete metric set. *Relevant to us: a factory loop that blows
the wall clock produces missing rows, not zeros.*

### 4.2 Full dimension inventory

**Identification** — `checkpoint`, `problem`, `path`, `idx`, `version`, `state`, `is_first`,
`is_last`.

**Inference** — `started`, `ended`, `elapsed`, `cost` (USD), `steps`, `input`, `output`,
`cache_read`, `cache_write`, `reasoning`.

**Evaluation** — `total_tests`, `passed_tests`, `pass_rate`, `checkpoint_pass_rate` (ISO),
`core_pass_rate`, `{core,functionality,error,regression}_{total,passed}`, `duration`.

**Code size** — `loc`, `total_lines`, `files`, `lines_added`, `lines_removed`, `single_comments`.

**Symbols & structure** — `symbols_total`, `functions`, `methods`, `classes`, `statements`,
`mean_func_loc`, `lines_per_symbol`.

**Cyclomatic complexity** (radon) — `cc_max`, `cc_mean`, `cc_std`, `cc_high_count` (>10),
`cc_extreme_count` (>30), `high_cc_mean`, `cc_normalized`, `cc_concentration` (Gini of the CC
distribution; 0 = uniform, 1 = concentrated).

**Linting** — `lint_errors`, `lint_fixable`, `lint_per_loc`.

**Waste (`scb-check`)** — `cloned_sloc_lines`, `cloned_pct`, `verbosity_flagged_sloc_lines`,
`verbosity_flagged_pct`, `single_use_functions`, `trivial_wrappers`, `unused_variables`.

**Rubric (LLM judge)** — `rubric_total_flags`, `rubric_carried_over`, `rubric_verbosity_flags`,
`rubric_erosion_flags`, `rubric_per_loc`.

**Dependency graph** (optional, Python only) — `graph_cyclic_dependency_mass` (edge weight in SCCs /
total), `graph_propagation_cost` (average reachability in transitive closure),
`graph_dependency_entropy` (normalized Shannon; lower = deps concentrated on few modules).

**Mass** — `mass.cc`, `mass.high_cc_pct`.

**Deltas** (every checkpoint after the first) — `delta.loc`, `delta.verbosity`,
`delta.churn_ratio` = `(lines_added + lines_removed) / prior_total_lines`.

**Run-level rollups** — `checkpoints_solved`, `checkpoints_iso_solved`, `checkpoints_core_solved`,
`problem_solved`, `problem_partial`, and their `pct_*` forms;
`pass_rates.{checkpoint,problem}.{core,total,error,functionality,regression}`; `costs.{checkpoint,
problem,total}`; `time.*`; `steps.*`; `tokens.*`; `cc.*`; `ratios.{rubric,lint}`; `verbosity`;
`erosion`.

### 4.3 The comparison table upstream publishes

| Question | Metric |
|---|---|
| Solves more? | `pct_problems_solved`, `pct_checkpoints_solved` |
| Meets core spec? | `pct_checkpoints_core_solved` |
| **Breaks prior work?** | `pass_rates.checkpoint.regression` |
| Cleaner code? | `ratios.lint.mean`, `verbosity.mean` |
| **Quality degrades?** | `delta.loc`, `delta.verbosity`, `delta.churn_ratio`, `erosion.mean` |
| Cheaper end-to-end? | `costs.problem.mean`, `costs.total` |
| Fewer steps? | `steps.checkpoint.mean` |

Rows 3 and 5 are the Beadhive hypothesis, stated in the benchmark's own vocabulary.

---

## 5. The variable matrix

### 5.1 Axes

| Axis | Values | Source |
|---|---|---|
| **harness** (`agent`) | `claude_code`, `codex`, `cursor_cli`, `gemini`, `kimi_cli`, `miniswe`, `opencode`, `openhands`, `pi` | `configs/agents/*.yaml` |
| **model** | 38 configs: Opus 4.5–4.7, Sonnet 4.5/4.6, GPT-5→5.5 + Codex/Mini/Spark variants, Gemini 2.5/3/3.1, GLM 4.5–5.1, Kimi K2–K2.6, MiniMax M2.7, Composer-2, Grok-code-fast-1, o3 | `configs/models/*.yaml` |
| **prompt** (scaffold) | `just-solve`, `anti_slop`, `plan_first`, `plan-and-test` | `configs/prompts/*.jinja` |
| **thinking** | `none`/`disabled`/`low`/`medium`/`high` preset, or `{max_tokens: N}` | run config |
| **environment** | `docker-python3.12-uv`, `local-py`, custom | `configs/environments/` |
| **pass_policy** | `any`, `any-case`, `all-cases`, `all-non-error-cases`, `core-cases`, `any-core-cases`, `all-core-cases` | run config |
| **one_shot** | collapse all checkpoint specs into one prompt, evaluate final tests — the single-shot ablation | run config |
| **budget** | `step_limit`, `cost_limit`, `net_cost_limit` | agent config |
| **problems** | subset or auto-discover | run config |
| **harness version** | e.g. `claude_code 2.0.51` | agent config |

### 5.2 How combinations actually form

Precedence: `CLI key=value overrides > CLI flags > config file > built-in defaults`.

**There is no matrix runner.** Each cell is one `slop-code run`. What makes sweeps tractable is
`save_template` OmegaConf interpolation:

```yaml
save_dir: outputs
save_template: ${model.name}/${agent.type}-${agent.version}_${prompt}_${thinking}_${now:%Y%m%dT%H%M}
```

Available: `${model.name}`, `${model.provider}`, `${agent.type}`, `${agent.version}` (cleanly
elided with surrounding separators when unset), `${prompt}`, `${thinking}`, `${env.name}`,
`${now:strftime}`.

Every cell lands in a distinctly-named directory; the analysis layer (`slop-code metrics`, the Dash
dashboard) reads across directories. **The sweep is a shell loop.**
`configs/runs/variance.yaml` is the repeat-runs idiom: identical config, timestamp discriminates
seeds. The paper reports no explicit seed count per configuration.

Other run modes worth knowing: `--resume <dir>` (loads saved config, detects last completed
checkpoint, validates critical fields match, deletes invalidated checkpoint dirs), `--dry-run`,
`--num-workers N`, `--no-evaluate`.

### 5.3 The paper's actual sweep

15 model configurations across 6 providers × 3 prompt scaffolds (`just-solve` baseline, `anti-slop`,
`plan-first`), each on the model's **native** harness. Plus the `one_shot` collapse as a
single-shot control, and the 473-repo human panel as an external reference distribution.

---

## 6. Running Beadhive vs. bare harness, as-is

**Verdict: the A/B is runnable today with zero upstream code changes. Five things will skew it; one
of them invalidates a run outright if not handled.**

### 6.1 The five skews

**① `steps` under-counts delegated work; `cost` does not.**
`parser.py` emits one `ToolUseStep` per main-loop `tool_use`. A `Task` spawn is one step; the
sub-agent's entire inner loop is invisible. Cost and tokens *are* correct — Claude Code's stream-json
`result` event reports session-cumulative usage including sub-agents. **Consequence:** any
`steps`-based comparison between a delegating harness and a flat one is meaningless. Report cost and
elapsed; state the limitation explicitly.

**② Worktrees inside `/workspace` will double-count. This is the run-killer.**
`metrics/driver.py:391` excludes exactly: `__pycache__`, `*.pyc`, `venv`, `.venv`, `virtualenv`,
`.virtualenv`, `.git`, `node_modules`, `.tox`, `.nox`. It then `rglob`s every file matching the
entry-file extension. Beadhive worktrees at `wt/batch/<group>/` are neither dot-prefixed nor in that
set — every `.py` in them counts as submission code, inflating `loc` and **destroying `cloned_pct`
and `verbosity`, since worktrees are literal clones**.
*Mitigation:* place worktrees outside the mounted workspace, or patch the exclude set locally (and
upstream it — see §7.5). `.beads/*.jsonl` and markdown planning docs are safe: wrong extension.

**③ Session continuity is a confound.**
Default `reset_context=False` means the control arm gets `claude --continue` across checkpoints — a
single conversation accumulating context for free. A Beadhive run is many sessions with deliberate
handoffs. Pin `reset_context` identically in both arms or the comparison measures context retention,
not orchestration.

**④ Wall clock and git.**
2h per checkpoint. A full plan → dispatch → review → merge molecule with sub-agents may exceed it;
timeouts surface as *missing rows*, not zeros (§4.1), so they must be counted and reported as a
first-class outcome. Separately, the workspace is **not a git repo** — `git init` plus identity must
happen in the Docker template or an environment `setup` command. Check `--network` mode in the
environment spec too: `bh hq` / `gh` flows either need egress or the hive must run purely local.

**⑤ Equal-checkpoints is the wrong unit of comparison.**
Beadhive spends tokens on planning and review that emit no code. At equal checkpoints it looks
expensive; at equal dollars it may look excellent. SCBench exposes `costs.problem` so normalization
is *possible*, but the benchmark's framing doesn't do it — so the protocol must be stated
explicitly, up front, before seeing results.

### 6.2 Path A — zero-code (recommended first)

Keep `type: claude_code`; vary only the harness *configuration*.

```yaml
# configs/agents/claude_code_beadhive.yaml
type: claude_code
version: 2.0.51
permission_mode: bypassPermissions
docker_template: ./beadhive.j2      # + git, bh, bd, seeded ~/.claude plugins & role skills
env:
  BH_HIVE_MODE: local
append_system_prompt: "Drive all work through `bh work` per the developer role skill."
cost_limits: {cost_limit: 0, step_limit: 100, net_cost_limit: 0}
```

```bash
for AGENT in claude_code claude_code_beadhive; do
  uv run slop-code run --agent $AGENT --model anthropic/opus-4.5 \
    --prompt just-solve thinking=low \
    save_template='${agent.type}-bh/${model.name}/${now:%Y%m%dT%H%M}'
done
```

Then compare `result.json` on the metrics that encode the hypothesis:

| Claim | Metric |
|---|---|
| The review gate stops regressions | `pct_checkpoints_solved` − `pct_checkpoints_iso_solved` |
| The improvement loop holds quality | `erosion.mean`, `delta.verbosity`, `ratios.lint.mean` |
| It's affordable | `costs.problem.mean` |

### 6.3 Path B — a real adapter

A `type: beadhive` implementing the `Agent` ABC, where `run(task)` drives a whole molecule
(plan → dispatch → review → merge) and `save_artifacts` writes the bead lifecycle alongside the
trajectory. Worth doing once Beadhive needs to be measurable independently of which CLI sits
underneath. **Not worth it for a first result** — Path A answers the question faster.

### 6.4 On the "one model at a time" concern

It is not a hard constraint on the harness side. `--model` sets one credential and `base_url` for
the harness *process*; a harness that routes roles to different models internally (which Beadhive
dispatchers do per bead) can already do so. What is missing is **vocabulary**: `result.json` records
a single `model` field, so a heterogeneous fleet is misreported as whatever was passed on the CLI.
That is a reporting gap, not an execution blocker — see §7.2 and §7.6.

### 6.5 What SCBench structurally cannot see about a factory

The skews in §6.1 are fixable with configuration. These three are not — they are consequences of
the benchmark's *shape*, and they bound what an as-is result can claim.

**The honest framing of an as-is run:** SCBench would measure the slop produced by a factory's
**incremental sub-steps**, one checkpoint at a time. That is a real and useful measurement. It is
*not* a measurement of the factory.

#### 6.5.1 There is nowhere to put the pre-planning phase

Specs are revealed **one checkpoint at a time**. The agent learns checkpoint 3 exists only after
finishing checkpoint 2. A planning plane's entire value proposition — decompose a known scope into a
dependency DAG, choose an architecture that survives the whole scope, sequence the work — is
structurally unavailable, because the scope is hidden by construction.

This is not incidental; it is the benchmark's core mechanism. Path dependence is *interesting* to
SCBench precisely because the agent cannot see ahead. So the benchmark measures a factory with its
main lever disconnected, and then reports the result as the factory's performance.

There is also no accounting slot for planning that spans checkpoints. Cost is attributed per
checkpoint; a planning pass that pays off three checkpoints later is billed entirely to checkpoint 1
and looks like pure overhead.

**The missing middle.** SCBench has two modes and needs a third:

| Mode | Scope visible | Delivery | Regression scored |
|---|---|---|---|
| default | one checkpoint at a time | incremental | yes |
| `one_shot` | **all checkpoints at once** | single shot | final tests only |
| *(missing)* | **all checkpoints as a roadmap** | **incremental** | **yes** |

`one_shot` already proves the corpus can expose full scope — it concatenates every checkpoint spec.
But it then collapses delivery to one turn and evaluates only the final checkpoint, so iteration and
regression measurement are both lost. The missing mode — roadmap visible, delivery and scoring still
per-checkpoint — is where a planning plane can actually be evaluated. See §7.9.

#### 6.5.2 Worktrees and parallelism have no place to live

The workspace is a single bind-mounted directory that *is* the submission. There is no notion of a
scratch area, a branch, or concurrent work-in-progress that is not yet part of the measured artifact.
Consequences:

- Worktrees inside the workspace are **counted as submission code** (§6.1②) — a factory is penalized
  for its own isolation mechanism, and penalized hardest on exactly the clone/verbosity metrics.
- Worktrees outside the workspace are invisible to snapshots, so any evaluation of *how* the factory
  worked is lost — only the merged endpoint survives.
- There is no representation of **parallel** bead execution at all. Wall-clock `elapsed` is measured
  per checkpoint, so N developers working concurrently register as one long serial checkpoint, and
  the whole point of fanout — throughput — is unmeasurable.

Fixing this needs both a declared measurement boundary (§7.5) and a concurrency-aware time model:
`elapsed` should distinguish wall-clock from summed agent-time, or parallelism reads as slowness.

#### 6.5.3 Heterogeneous role / skill / model fleets are unrepresentable

A run is `(one agent config, one model, one credential, one prompt)`. A factory is a **fleet**:
a planner on a high-reasoning model, several developers on a cheaper one, a reviewer with a
different skill bundle and a different cost profile, a merger that mostly runs deterministic
commands. Three separate gaps:

- **Cost.** `UsageTracker` is scalar. Per-role spend, and therefore quality-per-dollar by role, is
  not expressible (§7.2). A fleet that saves money by routing implementation to a cheap model
  reports the same single blended number as one that doesn't.
- **Skills.** There is no field describing *what capability* a seat had — which skills, tools, or
  gates were in play. The `prompt` axis is the only scaffold vocabulary, and it is one string.
- **Credentials.** One `ProviderCredential` per run. Routing roles across *providers* — not just
  models — is possible only by baking it inside the harness, where SCBench cannot see or report it.

The net effect: two very different factory configurations produce byte-identical run metadata. There
is no way to attribute a result to a fleet design, which makes fleet design unstudiable.

---

## 7. Proposed upstream contributions

Ranked by value to the field. **1–3 are the ones without which multi-agent results are not
credible.** 4–8 are what would turn this from a *model* benchmark into a *harness* benchmark. **7.9
is the only one that changes what the benchmark can ask** rather than what it can see — it is the
answer to the structural blind spot in §6.5.1, and the highest-value item here if the goal is to
evaluate a factory rather than instrument one.

### 7.1 Orchestrator-aware trajectory schema

`agent_runner/trajectory.py` has a flat `AgentStep | ThinkingStep | ToolUseStep`. Add a
spawned-agent step type plus `agent_id` / `parent_id` / `role`, and teach `claude_code/parser.py` to
descend into `Task` sub-agent streams so `steps` counts the whole tree.

*Why it lands:* fixes §6.1① for **any** delegating harness — Claude Code, OpenHands, anything with
sub-agents. Entirely model-agnostic. Highest-leverage single PR.

### 7.2 Per-role cost and token attribution

`UsageTracker` is scalar (`cost`, `steps`, `net_tokens`, `current_tokens`). Make it a mapping
`role → TokenUsage`.

*Why it lands:* enables "42% of spend went to planning and review", quality-per-dollar broken out by
role, and — the same change — makes heterogeneous model fleets reportable. It is the instrumentation
that answers *does the improvement loop pay for itself*.

### 7.3 Intra-checkpoint quality curves

SCBench measures quality **once**, at the end of each checkpoint. A harness with a review/refine
loop moves quality *within* a checkpoint — the current instrumentation is blind to precisely the
thing we claim. `execution/session.py` already has snapshot machinery; expose a hook to snapshot at
each internal commit and run `scb-check` per snapshot.

*Output:* an erosion/verbosity trace **inside** the checkpoint. That is the plot that proves or
kills the improvement-loop hypothesis.

### 7.4 Git-native evaluation mode

The human calibration panel is measured over **commits** (13,667). Agent trajectories are measured
over **directory snapshots**. Different plumbing, same claimed comparison. Contribute an environment
mode that `git init`s the workspace and derives `lines_added`/`lines_removed`/churn and snapshots
from commits.

*Why it lands:* closes a genuine methodological gap in the paper's own central comparison. That it
is exactly what a git-flow orchestrator needs is a happy coincidence, not the pitch.

### 7.5 A measurement-boundary spec

Add `measure_paths` / `.scbignore` to the problem or environment config, so "what counts as the
submission" is **declared** rather than inferred from a hardcoded exclude list.

*Why it lands:* without it, any orchestrator that keeps worktrees, task databases, planning docs, or
scratch dirs in the workspace is silently mis-scored (§6.1②). Small PR, unblocks a whole class of
harnesses.

### 7.6 Harness as a first-class leaderboard axis, plus a sweep runner

`result.json` records `model`, `agent_type`, `agent_version`, `thinking`, `prompt`. Add an
`orchestration` block (role count, max concurrency, loop policy, per-role model map) and a
`harness_config_hash`. Then add `slop-code sweep --matrix … --repeats N` so the variable matrix is
not a shell loop and variance is not a directory-naming convention.

*Why it lands:* today the dashboard can hold harness fixed and vary model. It should be able to do
the reverse.

### 7.7 Budget-parity run mode

Implement equal-net-cost (or equal-token) comparison as a supported protocol: cap both arms at the
same `net_cost_limit`, report pass rate and erosion **at the cap**.

*Why it lands:* without it any multi-agent harness "wins" by spending more, and the result is
uninterpretable. The authors have half-conceded the need already — their own prompting study reports
the +12.1% cost of quality-aware prompts as a tradeoff rather than normalizing it away.

### 7.8 Path-dependence and recoverability probes

Path dependence is the paper's central claim, and a harness with replanning is precisely the
intervention that should reduce it.

- **Divergence metric** — structural distance between snapshots from repeated runs of the same
  config at checkpoint *k*. Low divergence = the harness converges regardless of early luck.
- **Recoverability probe** — seed checkpoint 1 with a deliberately poor implementation and measure
  whether later checkpoints refactor out of it. SCBench already ships `solutions/checkpoint_N/`
  reference implementations, so the seeding machinery is half-built; add a "seed from
  solution/variant" run mode.

*Why it lands:* it directly serves the authors' own named open question (§2.8), which makes it the
easiest of these to get merged.

### 7.9 Scoped-iteration run mode — the missing middle

Add a third mode alongside default and `one_shot` (§6.5.1): **the full checkpoint roadmap is
revealed up front, but delivery and scoring remain per-checkpoint with regression intact.**

```yaml
scoped_iteration:
  enabled: true
  roadmap_prefix: "Planned scope (future checkpoints, do not implement yet):"
  reveal: all          # or: lookahead N
```

Mechanically this is small — `one_shot.py` already concatenates every checkpoint spec, so the
spec-assembly half exists; what's new is keeping the per-checkpoint run/evaluate loop instead of
collapsing it.

*Why it lands:* it isolates a variable the paper currently confounds. Today "path dependence" mixes
two effects — *the agent chose badly* and *the agent could not have known*. Scoped iteration
separates them, and it turns "does foresight prevent erosion?" into a directly measurable A/B
(`reveal: none` vs `lookahead: 1` vs `reveal: all`). It also gives a planning plane somewhere to
exist, and gives cross-checkpoint planning cost somewhere to be billed. This is the one contribution
that changes what the benchmark can *ask*, not just what it can *see*.

### 7.10 Optional — contribute the adapter

A `beadhive` agent type under `agent_runner/agents/` plus `configs/agents/`, as the reference example
of a harness whose unit of work is a **molecule** rather than a turn. Do this *after* 7.1–7.3 land,
so it demonstrates an instrumented capability rather than asking for one.

---

## 8. Recommended sequence

1. **Run the zero-code A/B first** (§6.2), on a 4–6 problem subset, with skews ①–⑤ controlled.
   Roughly a day of config work. Purpose: find out whether there is a signal worth defending before
   investing in upstream work. **Scope the claim honestly** — this measures incremental sub-step
   slop with the planning plane disconnected (§6.5.1), not the factory.
2. If there is a signal, **land 7.1, 7.2, and 7.5 upstream** — small, obviously correct,
   independently useful, and none of them mention Beadhive.
3. **Land 7.9 (scoped iteration).** Without it, every subsequent factory result is measured with the
   planning plane structurally disabled. It is also the most defensible upstream pitch, because it
   disentangles *chose badly* from *could not have known* in the paper's own headline claim.
4. **Build the real comparison** on 7.3 (intra-checkpoint curves), 7.7 (budget parity), and 7.8
   (path dependence). These are the three that make a factory claim falsifiable.
5. 7.4, 7.6, 7.10 as follow-on once the first result exists.

---

## Appendix A — key file references

| What | Path in `slop-code-bench` |
|---|---|
| Agent ABC & config base | `src/slop_code/agent_runner/agent.py` |
| Registries | `src/slop_code/agent_runner/registry.py` |
| Trajectory step types | `src/slop_code/agent_runner/trajectory.py` |
| Claude Code adapter | `src/slop_code/agent_runner/agents/claude_code/agent.py` |
| Claude Code stream parser | `src/slop_code/agent_runner/agents/claude_code/parser.py` |
| Claude Code Dockerfile template | `src/slop_code/agent_runner/agents/claude_code/docker.j2` |
| **Metrics exclude patterns** | `src/slop_code/metrics/driver.py:391` |
| Snapshot ignore/keep globs | `src/slop_code/execution/snapshot.py` |
| Docker volumes & `--network` | `src/slop_code/execution/docker_runtime/exec.py` |
| Checkpoint loop | `src/slop_code/entrypoints/problem_runner/worker.py` |
| Run config resolution | `src/slop_code/entrypoints/config/` |
| Metric definitions | `docs/metrics-reference.md` |
| Run CLI reference | `docs/commands/run.md` |
| Agent implementation guide | `docs/agents/agent-class.md` |
| Checkpoint design guide | `docs/problems/checkpoints.md` |

## Appendix B — vocabulary map

| SCBench | Beadhive | Note |
|---|---|---|
| agent | **harness** | the CLI under test |
| problem | — | a multi-checkpoint spec sequence |
| checkpoint | ≈ **bead** / molecule step | one spec increment + its tests |
| checkpoint spec (`checkpoint_N.md`) | ≈ bead description | what the harness is asked to do |
| regression tests | ≈ **green line** invariant | prior checkpoints re-run |
| strict − ISO gap | ≈ what the **review gate** should close | self-inflicted regression |
| erosion / verbosity | ≈ what the **improvement loop** should hold | `scb-check` composites |
| run | a full sweep cell | one harness × model × prompt × thinking |
| `one_shot` mode | — | the single-shot control condition |
| *(no equivalent)* | **planning plane** | scope is hidden ahead of the current checkpoint (§6.5.1) |
| *(no equivalent)* | **worktree / fanout** | workspace *is* the submission; no parallelism model (§6.5.2) |
| *(no equivalent)* | **seat / role / skill** | one agent, one model, one credential per run (§6.5.3) |
