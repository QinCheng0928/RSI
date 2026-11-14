# Population-Based Algorithm Evolution

<p align="center">
  <strong>English</strong>
  &nbsp;·&nbsp;
  <a href="docs/README.zh-CN.md">简体中文</a>
</p>

A research framework for generating, testing, and evolving algorithms with large language models. Models judge behavior from task descriptions and evidence collected through actual interaction; the framework manages candidate code, test scenarios, evaluation feedback, and population evolution.

The project targets algorithm optimization tasks that require interaction with external environments and lack an explicit numerical fitness function. Each problem is a self-contained package defining the task, environment interaction, and evaluation contract.

## Features

- Population-based algorithm search with repair, crossover, and mutation operators.
- Separate execution and constraint failures from weak behavioral performance, with diagnoses used for repair in the next generation.
- Test parents and offspring on a common scenario suite, then select using multiple analysts and pairwise comparisons.
- Archive candidate code, model interactions, execution evidence, and evaluation records; resume from committed generations.
- Integrate problems through shared interfaces while keeping environment interaction and candidate isolation inside each problem's simulator.

## Repository layout

```text
evolution/
  generation/       # Code generation and evolution operators
  testing/          # Scenario generation, execution, retries, and records
  evaluation/       # Diagnosis, behavioral analysis, comparison, and selection
  framework.py      # Shared problem and simulator interfaces
  runner.py         # Generation orchestration and recovery
  storage.py        # Experiment artifacts and population commits
problems/
  <problem_id>/     # One self-contained problem package
requirements.txt
```

Each problem owns its `problem.yaml`, `spec.py`, `simulator.py`, and algorithm template. The top-level `problem_id` is the sole problem identifier; `task` defines the goal, inputs, outputs, and constraints.

## Installation

Use Linux and Python 3.8 or a compatible later version. Run commands from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The framework uses Linux file locking. Each problem's adapter must also provide its own environment dependencies, candidate isolation, and readiness checks; these requirements depend on the environment being integrated.

## Model configuration

Configure these sections in the problem's `problem.yaml`:

- `models`: service endpoints, model names, timeouts, and request parameters.
- `agents`: model profiles assigned to generation, testing, analysis, and judging roles.

The framework uses the OpenAI-compatible Chat Completions interface with remote services or a local vLLM server. The service must support the configured `json_object` or `json_schema` output mode.

Create `.env` in the repository root:

```dotenv
LLM_CLOUD_API_KEY_ENV=OPENAI_API_KEY
OPENAI_API_KEY=replace-with-your-cloud-key

LLM_LOCAL_API_KEY_ENV=VLLM_API_KEY
VLLM_API_KEY=local-development
```

`LLM_CLOUD_API_KEY_ENV` and `LLM_LOCAL_API_KEY_ENV` name the environment variables containing the corresponding keys. Configure credentials for the profiles you actually use. Existing process environment variables take precedence over `.env`; a local service's key must match its server configuration.

Git ignores `.env`. Keep credentials in environment variables or the root `.env`, outside the archived `problem.yaml`.

## Run an experiment

Configure model access and the selected problem's environment first. Replace `your_problem` with an existing directory name under `problems/`:

```bash
source .venv/bin/activate
python -m evolution run --problem your_problem --run-id experiment_001
```

Set the experiment size in the problem configuration. This is a small example; the corresponding `problem.yaml` determines the actual settings:

```yaml
experiment:
  population_size: 4
  generations: 2
  seed: 42

genetics:
  crossover_probability: 0.5
  mutation_probability: 0.5
  elite_count: 1
```

`population_size` is the number of retained candidates per generation. `generations: 2` runs two evolution rounds after the initial generation, `gen_0000`. Each subsequent round generates P offspring, tests and evaluates them alongside P parents, and retains P candidates. `elite_count` must be smaller than P, and crossover and mutation probabilities must sum to 1.

Other commands accept the same `--problem` and `--run-id` arguments:

| Command | Purpose |
| --- | --- |
| `python -m evolution prepare` | Save configuration and source snapshots without calling models or checking simulator readiness |
| `python -m evolution generate` | Generate candidates without executing tests |
| `python -m evolution build-suite` | Construct a scenario suite without executing candidates |
| `python -m evolution run` | Run the complete optimization process |

`generate` and `build-suite` require a working model service. Pass `--problem` explicitly to select the intended problem package.

Reuse the same `run-id` with unchanged configuration and source code to resume from committed generations. Use a new `run-id` after changing problem configuration, problem implementation, or framework code.

## Evolution and evaluation

Each generation slot produces one candidate. Failures discovered in a generation do not trigger repair and rerun within that generation. Each failed parent retained from the previous generation receives one repair offspring; remaining slots use crossover or mutation among healthy parents. With only one healthy parent, these slots use mutation.

Execution failures, invalid outputs, hard-constraint violations, and missing evidence receive failure diagnoses. Candidates that pass checks proceed to behavioral analysis, cross-review, and pairwise preference comparison. Infrastructure failures receive bounded retries; exhausted retries abort the run rather than becoming algorithm performance judgments.

Selection prioritizes candidates that pass checks. If fewer than P qualify, failed candidates may fill the remaining places for repair in the next generation. Each eligible pair is compared in both orders, so evaluation requests grow approximately quadratically with the eligible population. Choose the experiment size with model cost and simulation time in mind.

The final output is a recommendation supported by the current test suite. Ties, incomparability, and preference cycles remain explicit; the framework does not guarantee a unique or globally optimal algorithm.

## Problem package structure

Keep each problem's configuration, contracts, environment adapter, and algorithm template together:

```text
problems/<problem_id>/
  problem.yaml                  # Component references and experiment configuration
  spec.py                       # Scenario validation and correctness rules
  simulator.py                  # Environment interaction and isolated execution
  template/
    algorithm.py.txt            # Candidate interface and starting implementation
  sim_config/                   # Optional environment configuration or fixed assets
  experiments/                  # Optional YAML configuration overlays
```

Only the first four files form the standard package layout. Additional modules and assets are optional and belong inside the problem directory when needed. The framework loads components from configuration rather than importing a specific environment directly.

### Configuration: `problem.yaml`

The configuration combines task semantics with the settings needed to run an experiment:

| Section | Responsibility |
| --- | --- |
| `problem_id` | Sole problem identifier, also used in the run storage path |
| `spec` | Import reference for a `ProblemSpec` instance, such as `problems.your_problem.spec:SPEC` |
| `template` | Algorithm template path relative to the problem directory |
| `storage_root` | Root directory for experiment artifacts |
| `task` | Goal, inputs, outputs, hard constraints, and allowed code changes |
| `scenario_space` | Scenario types, parameter ranges, and validity rules |
| `core_scenarios` | Predefined baseline cases; may be empty |
| `rubric` | Behavioral evaluation principles and guidance for conflicting evidence |
| `experiment` | Population size, evolution rounds, and framework random seed |
| `genetics` | Crossover and mutation probabilities and elite count |
| `evaluation` | Number of independent initial analysts |
| `models` / `agents` | Model profiles and their role assignments |
| `execution` | Simulator reference, versions, resource limits, retries, and environment settings |

`task.goal` describes the intended behavior. `task.inputs` and `task.outputs` define the algorithm's observation and action contracts. `task.hard_constraints` specifies mandatory rules, while `task.allowed_changes` restricts the generated files. There is no separate task identifier.

`execution.environment` contains adapter-specific settings; per-scenario variation belongs in `Scenario.inputs`. `simulator_version`, `harness_hash`, `observation_schema_version`, and `external_inputs_hash` identify the execution conditions. The adapter verifies the relevant versions and fingerprints in `check_readiness()`.

Optional `experiments/<name>.yaml` files override base configuration with `--experiment <name>`. Dictionaries merge recursively; lists replace the corresponding base lists.

### Scenarios and correctness: `spec.py`

A scenario contains a label, problem-specific inputs, a seed, and a purpose:

```yaml
scenario_id: baseline_001
inputs:
  case: baseline
seed: 42
purpose: Baseline coverage
```

The `inputs` above are illustrative; each problem defines and validates its own schema. The scenario content hash depends on inputs and seed. Labels and purposes are annotations.

Implement the [ProblemSpec interface](evolution/framework.py):

| Method | Responsibility |
| --- | --- |
| `validate_scenario(scenario)` | Reject invalid inputs, unsupported parameters, and impossible initial conditions |
| `check_correctness(scenario, result)` | Check execution evidence against mandatory domain constraints and return `Correctness` |
| `sanitize_feedback(confirmed)` | Expose only scenario identifiers and behavioral symptoms to subsequent scenario generation |

Correctness checks establish whether a candidate satisfies mandatory requirements. Behavioral evaluation judges how well an eligible candidate achieves the goal. The two responsibilities remain separate.

The tester combines predefined, historical, regression, and newly proposed scenarios. Matching inputs and seeds are deduplicated; reusing a scenario ID with different contents is rejected. Parents and offspring use the same current suite.

### Environment and execution: `simulator.py`

Implement the [Simulator interface](evolution/framework.py):

| Member | Responsibility |
| --- | --- |
| `sandboxed` | Declare whether the adapter provides actual candidate execution isolation |
| `check_readiness(config)` | Check environment availability, dependencies, versions, fingerprints, and isolation prerequisites |
| `run(code_directory, scenario, config)` | Execute one candidate in one scenario and return a `SimulatorResult` with actual observations and errors |
| `testing_batch(config)` | Optional context manager for resources shared across a generation's entire testing batch |

The adapter loads the generated algorithm inside its execution boundary, collects observations, calls the decision function, applies actions, and records behavior. For step-based environments, record the required observation–action–result sequence at each step, including initial and terminal states. Resource limits and dependencies must be enforced by the actual execution environment.

`SimulatorResult` contains execution status, decisions, trajectories, context, standard output, standard error, and an optional failure category. Return algorithm failures as `INVALID` or raise `AlgorithmExecutionError`; use `InfrastructureError` for environment or service failures. The adapter must not repair or regenerate candidates during execution.

The executor traverses candidates and scenarios, manages infrastructure retries, archives records, and checks coverage. Each `executor.run()` covers one generation's testing batch. If `testing_batch()` exists, its context wraps the entire batch and cleans up on normal completion or exceptional exit. The adapter decides whether to reuse or recreate resources within that scope.

External environments may be nondeterministic: state differences, update timing, and asynchronous communication can change observed behavior even with identical code, scenario inputs, and seed. The framework therefore reruns parents and offspring on the current common suite in every generation. Evaluation uses the current execution evidence instead of reusing historical trajectories as new results.

### Candidate contract: `template/algorithm.py.txt`

The template defines the synchronous `solve(observation, seed)` entry point, expected observation fields, action schema, available dependencies, and any restrictions. It can provide an initial implementation that generation operators may replace.

The simulator collects observations outside the candidate and supplies them as arguments. Generated files are restricted by `task.allowed_changes`, including the required `algorithm.py`. Environment setup, correctness checks, and evaluator internals stay outside generated code.

## Inspect results

Artifacts are saved under `runtime_data/<problem_id>/<run_id>/`:

```text
config/                    # Configuration and source snapshot indexes
individuals/<id>/code/      # Each candidate's algorithm.py
individuals/<id>/           # Metadata, parent relationships, and changes
generations/               # Per-generation production, testing, and evaluation
populations/               # Committed populations and recovery pointers
memory/                    # Reviewed feedback for subsequent generations
objects/                   # Execution evidence, logs, and snapshot objects
final/best.json            # Final recommendation and its basis
final/algorithm.py         # Exported code when an eligible candidate exists
experiment_manifest.json   # Overall status, progress, eligible IDs, abort reason
```

Model requests and responses, failure diagnoses, test trajectories, and comparisons are archived in the experiment directory. `COMPLETED` means that the process finished, not that a usable algorithm was found. If no final candidate qualifies, `final/best.json` records `NO_VALID_CANDIDATE` and no recommendation code is exported.

The framework's module organization draws on [LACE](https://github.com/PJ-NTU/LACE).
