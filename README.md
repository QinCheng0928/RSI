# Population-Based Algorithm Evolution

A framework organized around **generation → testing → evaluation and selection → next-generation generation**. Each generation slot produces one candidate. Invalid output, syntax errors, interface errors, and execution failures are preserved for testing and evaluation; they do not trigger repair within the same generation. Feedback becomes available to generation operators in the following generation.

The included problem is `port_route_planning`. Its real simulator, execution sandbox, observation/action contracts, and domain correctness checks remain **TODO**. With the current configuration, a full `run` records `RUN_ABORTED` during simulator preflight. Preparation, standalone generation, and scenario construction are available separately.

## Repository layout

```text
evolution/
  generation/developer.py    # Generation and repair/crossover/mutation operators
  testing/                  # Black-box scenarios, loading, execution, and caching
  evaluation/               # Diagnosis, analysis, arbitration, preferences, selection
  framework.py              # Shared ProblemSpec and Simulator contracts
  llm.py                    # OpenAI-compatible requests and dotenv credential loading
  runner.py                 # Generation orchestration and recovery
  storage.py                # Immutable artifacts and atomic population commits
problems/port_route_planning/
  problem.yaml              # Task, scenarios, rubric, models, and experiment settings
  spec.py                   # Scenario validation, correctness, and feedback sanitization
  simulator.py              # Problem-specific adapter for the external simulator (TODO)
  template/algorithm.py.txt  # Problem-specific algorithm template
  experiments/              # cloud and no_cache configuration overlays
requirements.txt            # Pinned core runtime dependencies
```

The separation of framework services and self-contained problem directories follows the organization of [LACE](https://github.com/PJ-NTU/LACE). Testing orchestrates execution and caching through a shared simulator interface; each problem owns its concrete simulator adapter. Selection belongs to evaluation. There is currently no `tests/` directory in this repository.

## Installation

Use Python 3.9 or later on Linux. The runner uses Unix file locking and directory synchronization. Run commands from the repository root; installing the project as a package is unnecessary.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m evolution prepare --problem port_route_planning --run-id experiment_001
```

`requirements.txt` pins the four core libraries: `openai`, `pydantic`, `PyYAML`, and `python-dotenv`. Install vLLM separately in the environment serving the local model.

All commands check configuration fields, parameter constraints, and component loading during initialization. `prepare` saves configuration and source snapshots without calling a model or running the simulator. It does not verify credentials, endpoint availability, or simulator readiness.

## Models and credentials

Create `.env` in the **repository root**. Both the profile-to-variable mapping and the actual key variable are required:

```dotenv
LLM_LOCAL_API_KEY_ENV=VLLM_API_KEY
VLLM_API_KEY=local-development

LLM_CLOUD_API_KEY_ENV=OPENAI_API_KEY
OPENAI_API_KEY=replace-with-your-cloud-key
```

For profile `local`, `OpenAIClient` first reads `LLM_LOCAL_API_KEY_ENV`, then uses its value (`VLLM_API_KEY`) as the name of the environment variable containing the key. The mapping contains a variable name, not the secret itself. For a new profile `judge`, provide both `LLM_JUDGE_API_KEY_ENV=JUDGE_API_KEY` and `JUDGE_API_KEY=...`.

The client calls `load_dotenv` on the repository-root `.env` with `override=False`: existing process environment variables take precedence at both lookup steps. A missing mapping or empty referenced key produces an explicit infrastructure error. Defining `OPENAI_API_KEY` or `VLLM_API_KEY` alone does not select a profile or create a mapping.

Keep credentials and their variable-name mappings out of `problem.yaml`. The configuration schema does not accept `api_key` or `api_key_env` fields. Root `.env` is ignored by Git and excluded from run configuration snapshots. Files inside a problem directory are snapshotted, so do not place secrets there. Raw model responses are archived as experiment artifacts; treat their contents accordingly.

The current `problem.yaml` contains these model profiles:

```yaml
models:
  local:
    base_url: http://127.0.0.1:8000/v1
    model: Qwen3.5-9B
    structured_output: json_object
    parameters: {temperature: 0.7}
  cloud:
    base_url: https://agi.tongji.edu.cn/v1
    model: DeepSeek-V4.1-Flash
    structured_output: json_object
    parameters: {}
```

`base_url` selects the endpoint; `model` is the model identifier exposed by that endpoint. These are the configured values, not a guarantee of endpoint or model availability. Optional profile settings include `timeout_seconds` (default 120) and `max_retries` (default 2). `parameters` supplies additional request arguments such as temperature; reserved fields are rejected.

All requests use the OpenAI SDK's Chat Completions interface. A local vLLM server must expose the same protocol, for example:

```bash
vllm serve /path/to/model \
  --served-model-name Qwen3.5-9B \
  --host 127.0.0.1 --port 8000 \
  --api-key local-development
```

The served name must match `models.local.model`, and the server key must match the key referenced by the local profile. `VLLM_API_KEY` is simply the credential variable used in this example.

`agents` selects a profile explicitly for each role. All roles currently use `local`; configuring both profiles does not create automatic priority or fallback. `--experiment cloud` applies the supplied overlay to select `cloud` for all roles. `agents.analysts` accepts one shared profile or exactly `anomaly_analyst_count` profiles.

`structured_output` is used in `evolution/llm.py` to build the request's `response_format`:

- `json_object` requests a JSON object; the required schema is also included in the prompt.
- `json_schema` sends the corresponding Pydantic JSON schema with `strict: false`; the endpoint must support it.

Both modes validate the returned JSON against the expected Pydantic model. There is no automatic fallback between modes or endpoints.

## Population evolution

The current problem settings are:

```yaml
experiment:
  population_size: 10
  generations: 8
  seed: 42

genetics:
  crossover_probability: 0.5
  mutation_probability: 0.5
  elite_count: 2

evaluation:
  anomaly_analyst_count: 3
```

`population_size` is the number of retained individuals and offspring per generation. Generation 0 creates and evaluates 10 candidates and retains the initial population. `generations: 8` then performs eight additional evolution rounds. Each round generates 10 offspring, tests and evaluates the 20 parent/offspring candidates on the same scenario suite, and selects 10 survivors. `seed` controls framework randomness; it does not guarantee reproducibility of remote model responses.

### Generation operators

Only parents retained by the previous selection participate in the next generation. Each retained parent whose assessment is non-executable or fails correctness receives exactly one `REPAIR` offspring. Remaining offspring slots use healthy parents:

- `CROSSOVER` selects two distinct healthy parents and generates a recombination.
- `MUTATION` selects one healthy parent and generates a variation.

The crossover and mutation probabilities must sum to 1 and apply only to non-repair slots. With one healthy parent, these slots use mutation and record the reason. If every parent needs repair, all offspring are repair attempts. There is no `repair_probability`, fixed repair quota, or guaranteed survival for failed candidates.

For example, if retained parents A and B fail, the next generation creates one repair offspring for A and one for B; the other eight slots use crossover or mutation among healthy parents. A failed repair must complete testing, evaluation, and selection before it can receive another repair in a later generation. Failures discovered in the current generation never trigger a repair in that generation.

Each slot makes one generation request and receives an independent candidate ID. Duplicate source code is allowed. Malformed output, missing `algorithm.py`, and invalid proposed file paths are archived as generation errors and passed to evaluation without fabricated execution traces. Infrastructure failures can abort the run; bounded transport retries do not modify candidate source code. Initial strategy planning is a separate model request and must succeed before a complete initial batch can be produced.

### Testing and scenarios

The tester constructs black-box scenarios using the task, scenario space, coverage, and sanitized behavioral feedback. It does not receive candidate source code or lineage.

`core_scenarios` contains predefined scenarios. An empty list means no predefined scenarios; scenario construction still calls the tester model. A scenario has this shape:

```yaml
core_scenarios:
  - scenario_id: baseline_001
    inputs:
      replace_with_real_input_field: 1
    seed: 42
    purpose: Baseline coverage
```

The example input is illustrative: replace it with the real simulator inputs and implement domain validation in `spec.py`. The current specification only checks basic scenario structure and retains TODOs for domain rules.

The suite combines predefined, historical, regression, and newly generated scenarios. Identical inputs and seeds are deduplicated; reusing a scenario ID with different contents is rejected. The entire generation-0 suite becomes the frozen core for subsequent rounds, and later adaptive scenarios accumulate. Parents and offspring are evaluated against the same current suite.

### Evaluation and comparison budget

Evaluation first checks execution evidence and hard correctness constraints:

| Status | Meaning |
|---|---|
| `EXECUTABLE` | Valid execution and behavior evidence that passes correctness checks. |
| `INVALID` | Generation, syntax, interface, runtime, timeout, correctness, or required-evidence failure. |
| `INFRA_ERROR` | Infrastructure failure; aborts evaluation rather than becoming algorithm fitness. |

Failed candidates receive error diagnoses and feedback for the next generation. They do not receive behavioral preference evaluations. Eligible candidates receive `anomaly_analyst_count` independent initial analyses, followed by dependence checking, cross-review, evidence verification, and arbitration. Independent initial contexts do not guarantee statistical independence.

`comparison_budget_per_generation` is a **derived property**, used by the evaluation preference-comparison stage. Do not put it in YAML. For population size P, its upper limit is:

```text
maximum pool = 2P
comparison calls = 2P × (2P - 1)
```

Every eligible pair is compared twice, with the order swapped. For K eligible candidates, the stage performs `K × (K - 1)` logical comparison calls, covering all pairs. With P=10, the maximum is 380 calls for 190 pairs. With K=10, it is 90 calls for 45 pairs. Generation 0 has only P candidates. Inconsistent swapped-order judgments become incomparable.

This limit covers preference comparisons only. Analysis, review, arbitration, generation, scenario construction, and SDK retries consume additional requests. `evaluation/comparison_summary.json` records the limit, eligible count, pair count, and actual comparison calls.

### Selection and elites

Selection prefers correct, comparable executable candidates using the preference graph and code, strategy, and lineage diversity. `elite_count: 2` allows up to two elite survivors. An elite must have at least one consistent preference win and no recorded preference loss; if more qualify, diversity helps choose among them. Parents and offspring can qualify. If fewer than two qualify, selection does not invent additional elites; remaining population slots use the ordinary selection rules. `elite_count` must be smaller than P.

If fewer than P candidates qualify for behavioral selection, the implementation fills remaining slots from failed candidates using code, strategy, and lineage diversity, with a seeded tie-breaker. All algorithm failures use `INVALID`; failure categories are preserved for diagnosis without establishing a selection priority. Only retained failures receive repair offspring next round. An entirely failed population can continue evolving, but is not reported as deployable.

## Commands and recovery

```bash
# Save candidates without executing or committing a population.
python -m evolution generate --problem port_route_planning --run-id generation_001

# Construct scenarios without executing candidates.
python -m evolution build-suite --problem port_route_planning --run-id scenarios_001

# After implementing the simulator, sandbox, and domain contracts:
python -m evolution run --problem port_route_planning --run-id experiment_001
python -m evolution run --problem port_route_planning --experiment cloud --run-id cloud_001
python -m evolution run --problem port_route_planning --experiment no_cache --run-id no_cache_001
```

Standalone generation reports `GENERATED_UNTESTED`; standalone scenario construction reports `SUITE_BUILT_UNEXECUTED`. Both require working model credentials. A full experiment reports `COMPLETED` only after generation 0 and all configured evolution rounds finish.

Reuse the same run ID and configuration to resume a full run. Committed populations are recovered atomically. Complete saved offspring and scenario stages can be reused; partially written stages may be regenerated in a new attempt. Attempt directories record recovery attempts, not same-generation algorithm repair loops.

Resolved configuration, problem files, and framework Python source are frozen for a run. Use a new run ID after changing these inputs. Root `.env` and README are outside that fingerprint. Recently reviewed memory events provide feedback to subsequent generation requests.

## Saved artifacts

Artifacts are organized under `runtime_data/<problem_id>/<run_id>/`:

```text
config/                         # Resolved configuration and source snapshots
individuals/<id>/               # Code, source manifest, metadata, lineage, patches
generations/gen_0000/attempts/<attempt_id>/
  strategy_plan.json            # Initial strategy planning
  generation/slot_000/           # Model requests, responses, validation, output
  generation/offspring_manifest.json
  testing/                      # Scenarios, execution references, coverage, errors
  evaluation/                   # Diagnoses, analyses, reviews, preferences, selection
  runtime_state.json
  memory_event.json
populations/                    # Immutable population commits and current pointer
objects/                        # Content-addressed runs, logs, evidence, snapshots
execution_cache/                # Exact execution-key index
memory/                         # Reviewed events
events/                         # Abort and cache-corruption events
standalone/                     # Standalone generation and scenario artifacts
experiment_manifest.json        # Progress, statuses, deployable IDs, abort reason
```

Raw model responses and generation failures are preserved. The offspring manifest records repair parents, eligible variation parents, and actual operator counts.

Execution caching binds source code, scenario, seed, environment and execution settings, and version fingerprints. It requires caching and deterministic mode to be enabled in configuration and a deterministic adapter. Identical source code can share an immutable raw execution record across candidate IDs; `RunUse.individual_id` identifies its consumer while the original record retains its execution owner. Generation errors are marked `GENERATION_ERROR`, do not count as simulator calls, and are not execution-cache entries. Corrupt cache entries trigger execution again. Nondeterministic mode resamples both parents and offspring. Subjective model evaluations are not execution-cache results.

## Adding problems and connecting the simulator

Keep each problem's configuration, template, domain implementation, and experiment overlays together under `problems/<problem_id>/`:

| Component | Responsibility |
|---|---|
| `problem.yaml` | Problem ID, task contracts, scenarios, rubric, profiles, role assignments, experiment settings. |
| `spec` reference | Importable `ProblemSpec` object, such as `problems.port_route_planning.spec:SPEC`. |
| `simulator.py` | Problem-specific adapter implementing the shared `Simulator` interface for an external simulation environment. |
| `validate_scenario` | Validate real scenario inputs and ranges. |
| `check_correctness` | Check hard domain constraints against actual execution results. |
| `sanitize_feedback` | Expose only scenario identifiers and sanitized behavioral symptoms to testing. |
| `template/algorithm.py.txt` | Define `solve(observation, seed) -> dict`. |
| `experiments/<name>.yaml` | Override defaults; dictionaries merge recursively and lists replace existing lists. |

`execution.simulator` is required and uses an import reference of the form `module:Class`. Each problem explicitly selects its own adapter; there is no framework-default simulator. The current port-route-planning setting is:

```yaml
execution:
  simulator: problems.port_route_planning.simulator:PendingSimulator
```

This placeholder lives in the problem directory and intentionally aborts preflight. When connecting another task, place its adapter in `problems/<problem_id>/simulator.py` and reference that class in its own `problem.yaml`. Experiment overlays can select different adapters for the same problem. Configured classes are instantiated without arguments; execution settings are passed to their methods.

Every adapter implements `Simulator` from `evolution/framework.py`: `sandboxed` and `deterministic` capability attributes, `preflight(config) -> None`, and `run(code_directory, scenario, config) -> SimulatorResult`. The actual simulation environment may run outside this repository; the adapter owns task-specific transport and input/action translation. Testing consumes this common interface and handles execution records, infrastructure retries, evidence, and caching without importing any concrete problem adapter directly.

Generated algorithms are loaded with `importlib.util` through the provided loader:

```python
from evolution.testing import load_algorithm

solve = load_algorithm(code_directory / "algorithm.py")
action = solve(observation, scenario.seed)
# TODO: send the action to the real simulator and record actual behavior/errors.
```

The loader uses `spec_from_file_location`, `module_from_spec`, and `exec_module`; importing candidate code executes its top-level statements. The future execution worker must perform this loading inside its actual sandbox. The loader itself does not provide process isolation or resource enforcement.

The adapter must return real algorithm failures as `INVALID`, or raise `AlgorithmExecutionError` with the original error details. Service failures use `InfrastructureError`. It must never repair or regenerate an algorithm during execution.

The `execution` settings describe the intended execution contract:

- `env_image_hash`, `simulator_version`, `harness_hash`, `observation_schema_version`, and `external_inputs_hash` identify result-affecting versions and inputs. Replace the `TODO` fingerprints before running a real experiment.
- `deterministic` and `cache_enabled` control deterministic execution and cache eligibility.
- `infra_retries: 1` permits one retry after an execution infrastructure failure. Algorithm failures are not retried as repairs.
- `timeout_seconds` and `memory_mb` specify limits that the real sandbox must enforce.
- `environment` supplies adapter-specific environment configuration; it is snapshotted and must not contain secrets.

**Remaining TODOs:** connect the real simulator and sandbox, define observation/action schemas and valid scenario ranges, implement domain correctness checks, and replace placeholder execution fingerprints. The repository currently provides the framework and adapter contracts, not a completed simulation environment.
