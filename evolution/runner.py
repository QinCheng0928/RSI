from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from evolution.config import ProblemConfig, config_fingerprint, load_symbol
from evolution.errors import ContractError, EvolutionError
from evolution.evaluation import Evaluator, Selector
from evolution.framework import ProblemSpec
from evolution.generation import Developer
from evolution.llm import LLM, OpenAIClient
from evolution.models import Assessment, Individual, Population, Scenario, ScenarioSuite, Status
from evolution.storage import Store
from evolution.testing import Executor, Tester
from evolution.testing.simulator import Simulator


@contextmanager
def run_lock(store: Store):
    with store.path("run.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ContractError("another process is already writing this run") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Evolution:
    def __init__(self, cfg: ProblemConfig, problem_directory: Path, store: Store,
                 llm: Optional[LLM] = None, simulator: Optional[Simulator] = None,
                 spec: Optional[ProblemSpec] = None):
        self.cfg, self.directory, self.store = cfg, problem_directory.resolve(), store
        if store.root.is_relative_to(self.directory):
            raise ContractError("run storage must be outside the problem directory")
        self.fingerprint = config_fingerprint(cfg, self.directory)
        self.spec = spec if spec is not None else load_symbol(cfg.spec)
        if not isinstance(self.spec, ProblemSpec):
            raise ContractError("problem spec must implement ProblemSpec")
        self.llm = llm if llm is not None else OpenAIClient(cfg.models, store)
        self.simulator = simulator if simulator is not None else load_symbol(cfg.execution.simulator)()
        self.executor = Executor(cfg.execution, self.simulator, store, self.fingerprint)
        self.developer = Developer(cfg, self.directory, self.llm, store)
        self.tester = Tester(cfg.task, cfg.scenario_space, cfg.rubric, cfg.agents.tester, self.spec, self.llm, store)
        self.evaluator = Evaluator(cfg, self.spec, self.llm, store)
        self.selector = Selector(cfg.genetics, cfg.experiment.population_size, cfg.experiment.seed)

    def prepare(self) -> None:
        frozen = {"config_hash": self.fingerprint, "config": self.cfg.model_dump(mode="json")}
        if self.store.path("config/resolved.json").exists():
            if self.store.read("config/resolved.json") != frozen:
                raise ContractError("run configuration/problem files changed; use a new run ID")
        else:
            self.store.save("config/resolved.json", frozen)
            manifest = {}
            for path in sorted(self.directory.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    ref = self.store.object({"bytes_hex": path.read_bytes().hex()})
                    manifest[str(path.relative_to(self.directory))] = ref
            self.store.save("config/problem_files.json", manifest)
            framework = Path(__file__).parent
            core_manifest = {str(path.relative_to(framework)): self.store.object({"bytes_hex": path.read_bytes().hex()})
                             for path in sorted(framework.rglob("*.py"))}
            self.store.save("config/framework_files.json", core_manifest)
        if not self.store.path("experiment_manifest.json").exists():
            self.manifest("PREPARED", None)

    def manifest(self, status: str, population: Optional[Population], reason: Optional[str] = None) -> dict:
        assessments = population.assessments if population else []
        deployable = [a.individual.individual_id for a in assessments
                      if a.status == Status.EXECUTABLE and a.correctness_pass and a.comparable]
        result = {"P": self.cfg.experiment.population_size, "N": self.cfg.experiment.generations,
            "m": self.cfg.evaluation.anomaly_analyst_count,
            "bootstrap_completed": population is not None,
            "generations_completed": population.generation if population else 0,
            "final_population": population.population_ids if population else [],
            "individual_states": [{"individual_id": a.individual.individual_id, "status": a.status.value,
                "correctness_pass": a.correctness_pass,
                "needs_repair": a.needs_repair,
                "comparable": a.comparable} for a in assessments],
            "executable_count": len(deployable),
            "invalid_count": sum(a.status != Status.EXECUTABLE for a in assessments),
            "deployable_ids": deployable, "deployment_message": "评估通过的可执行候选见 deployable_ids" if deployable else "无可部署算法",
            "status": status, "reason": reason, "config_hash": self.fingerprint}
        self.store.pointer("experiment_manifest.json", result)
        return result

    def previous_state(self, population: Optional[Population]) -> dict:
        if population is None:
            return {"suite": [], "core": [], "feedback": [], "coverage": {}}
        report = self.store.read(population.selection_report_ref)
        return self.store.read(report["runtime_state_ref"])

    def offspring(self, generation: int, parents: list[Assessment], prefix: str, feedback: dict) -> list[Individual]:
        # Recover a complete stage after interruption without making new candidates
        # or retrying an algorithm inside the same generation.
        directory = self.store.path(f"generations/gen_{generation:04d}/attempts")
        manifests = sorted(directory.glob("*/generation/offspring_manifest.json"))
        if manifests:
            path = manifests[-1]
            manifest = self.store.read(str(path.relative_to(self.store.root)))
            ids = manifest["produced_ids"]
            if len(ids) == self.cfg.experiment.population_size:
                parent_ids = {a.individual.individual_id for a in parents}
                individuals = [Individual.model_validate(self.store.read(f"individuals/{key}/metadata.json")) for key in ids]
                for individual in individuals:
                    if individual.generation_born != generation or not set(individual.parent_ids) <= parent_ids:
                        raise ContractError("interrupted offspring stage has inconsistent lineage")
                    self.store.source(individual)
                self.store.save(prefix + "/generation/offspring_manifest.json", {
                    **manifest, "reused_from": str(path.relative_to(self.store.root))})
                return individuals
        return self.developer.generate(generation, parents, prefix, feedback)

    def run(self) -> dict:
        with run_lock(self.store):
            self.prepare()
            population = self.store.load_population(self.cfg.experiment.population_size)
            if population and population.config_hash != self.fingerprint:
                raise ContractError("population configuration differs from frozen run")
            # Restore reviewed memory if a crash followed population commit.
            for path in sorted(self.store.path("populations").glob("gen_*.json")):
                committed = self.store.read(str(path.relative_to(self.store.root)))
                report_ref = committed["selection_report_ref"]
                attempt = report_ref.rsplit("/evaluation/", 1)[0]
                self.store.save(f"memory/gen_{committed['generation']:04d}.json",
                                self.store.read(attempt + "/memory_event.json"))
            if population and population.generation == self.cfg.experiment.generations:
                return self.manifest("COMPLETED", population)
            try:
                # TODO adapter blocks before spending model tokens or generating fake traces.
                self.executor.preflight()
                start = population.generation + 1 if population else 0
                for generation in range(start, self.cfg.experiment.generations + 1):
                    prefix = self.store.attempt(generation)
                    previous = self.previous_state(population)
                    parents = population.assessments if population else []
                    memory = [self.store.read(str(p.relative_to(self.store.root)))
                        for p in sorted(self.store.path("memory").glob("gen_*.json"))[-3:]]
                    developer_feedback = {"selected_assessments": [a.model_dump(mode="json") for a in parents],
                        "reviewed_memory": memory, "behavior_feedback": {}}
                    for parent in parents:
                        if parent.profile_ref:
                            profile = self.store.read(parent.profile_ref)
                            developer_feedback["behavior_feedback"][parent.individual.individual_id] = {
                                "confirmed_anomalies": [a for a in profile["anomalies"] if a["status"] == "CONFIRMED"],
                                "strengths": profile["strengths"], "tradeoffs": profile["tradeoffs"],
                                "trace": profile["trace"]}
                    offspring = self.offspring(generation, parents, prefix, developer_feedback)
                    pool = [a.individual for a in parents] + offspring
                    if len(pool) > 2 * self.cfg.experiment.population_size or len({i.individual_id for i in pool}) != len(pool):
                        raise ContractError("pool must contain at most 2P distinct candidate IDs")
                    core = ([Scenario.model_validate(s) for s in previous["core"]] if population
                            else [Scenario.model_validate(s) for s in self.cfg.core_scenarios])
                    old = [Scenario.model_validate(s) for s in previous["suite"]]
                    # Confirmed anomaly scenarios become regressions. Old adaptive cases are
                    # retained too, preserving comparable historical coverage across generations.
                    regression_ids = {f["scenario_id"] for f in previous["feedback"]}
                    regression = [s for s in old if s.scenario_id in regression_ids]
                    existing_suites = sorted(self.store.path(f"generations/gen_{generation:04d}/attempts").glob("*/testing/suite.json"))
                    if existing_suites:
                        previous_suite_ref = str(existing_suites[-1].relative_to(self.store.root))
                        suite = ScenarioSuite.model_validate(self.store.read(previous_suite_ref))
                        for scenario in suite.scenarios:
                            self.spec.validate_scenario(scenario)
                        self.store.save(prefix + "/testing/suite.json", suite)
                        self.store.save(prefix + "/testing/reused_suite.json", {"suite_ref": previous_suite_ref})
                    else:
                        suite = self.tester.build(generation, core, old, regression,
                            previous["coverage"], previous["feedback"], prefix)
                    execution = self.executor.run(pool, suite, prefix)
                    assessments, comparisons, feedback = self.evaluator.evaluate(pool, execution, suite, prefix)
                    selected, report = self.selector.select(generation, assessments, comparisons, bootstrap=generation == 0)
                    covered = [{"scenario_id": s.scenario_id, "content_hash": s.content_hash,
                                "purpose": s.purpose} for s in suite.scenarios]
                    state = {"suite": [s.model_dump() for s in suite.scenarios],
                        "core": previous["core"] if population else [s.model_dump() for s in suite.scenarios],
                        "feedback": feedback, "coverage": {"covered_scenarios": covered}}
                    state_ref = self.store.save(prefix + "/runtime_state.json", state)
                    report_ref = self.store.save(prefix + "/evaluation/selection_report.json",
                        {**report.model_dump(mode="json"), "runtime_state_ref": state_ref})
                    new_population = Population(generation=generation,
                        population_ids=[a.individual.individual_id for a in selected], assessments=selected,
                        elite_ids=report.elite_ids, selection_report_ref=report_ref, config_hash=self.fingerprint)
                    # Memory is an immutable reviewed event and is reconstructible from the
                    # same durable selection transaction after a crash.
                    memory_event = {"generation": generation, "evidence_level": "REVIEWED",
                        "selection_report_ref": report_ref, "sanitized_confirmed_observations": feedback,
                        "assessments": [{"individual_id": a.individual.individual_id,
                            "profile_ref": a.profile_ref,
                            "diagnosis": a.diagnosis.model_dump(mode="json") if a.diagnosis else None}
                            for a in selected],
                        "limits": "Invalid candidates are not behavior-comparable; operators run only in the next generation."}
                    self.store.save(prefix + "/memory_event.json", memory_event)
                    self.store.commit(new_population, self.cfg.experiment.population_size)
                    population = new_population
                    self.store.save(f"memory/gen_{generation:04d}.json", memory_event)
                    self.manifest("RUNNING", population)
                return self.manifest("COMPLETED", population)
            except (EvolutionError, OSError, ValueError) as error:
                # Operational/schema/data failures are explicit aborts, not compatibility
                # fallbacks. Programmer bugs retain their traceback and are never hidden.
                self.store.event("RUN_ABORTED", {"category": type(error).__name__, "reason": str(error)})
                self.manifest("RUN_ABORTED", population, str(error))
                raise

    def generate_only(self) -> str:
        with run_lock(self.store):
            self.prepare()
            prefix = "standalone/" + self.store.attempt(0).split("/")[-1]
            self.developer.generate(0, [], prefix, {})
            self.store.save(prefix + "/status.json", {"status": "GENERATED_UNTESTED",
                "note": "All generation outputs preserved; no simulation or evaluation performed, no population committed."})
            return prefix

    def suite_only(self) -> str:
        with run_lock(self.store):
            self.prepare()
            prefix = "standalone/" + self.store.attempt(0).split("/")[-1]
            core = [Scenario.model_validate(s) for s in self.cfg.core_scenarios]
            self.tester.build(0, core, [], [], {}, [], prefix)
            return prefix
