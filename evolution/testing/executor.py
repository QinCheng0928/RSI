from __future__ import annotations
from contextlib import ExitStack
from typing import Dict, List

import uuid

from evolution.config import ExecutionConfig
from evolution.errors import AlgorithmExecutionError, InfrastructureError
from evolution.framework import Simulator
from evolution.models import (Coverage, Evidence, ExecutionKey, ExecutionRecord, Individual,
                              RunUse, Scenario, ScenarioSuite, SimulatorResult, Status, digest)
from evolution.storage import Store


class Executor:
    """Execute every candidate/scenario and retain its actual execution record."""

    def __init__(self, cfg: ExecutionConfig, simulator: Simulator, store: Store, contract_hash: str):
        self.cfg, self.simulator, self.store, self.contract_hash = cfg, simulator, store, contract_hash

    def check_readiness(self) -> None:
        self.simulator.check_readiness(self.cfg)
        if not self.simulator.sandboxed:
            raise InfrastructureError("simulator adapter must provide a real execution sandbox")
        if any(value == "TODO" for value in (self.cfg.simulator_version, self.cfg.harness_hash)):
            raise InfrastructureError("simulator version and harness fingerprint must be configured")

    def key(self, individual: Individual, scenario: Scenario) -> ExecutionKey:
        return ExecutionKey(code_hash=individual.code_hash, scenario_content_hash=scenario.content_hash,
            simulator_version=self.cfg.simulator_version,
            config_hash=digest({"environment": self.cfg.environment, "contract": self.contract_hash,
                "timeout_seconds": self.cfg.timeout_seconds, "memory_mb": self.cfg.memory_mb}),
            seed=scenario.seed, harness_hash=self.cfg.harness_hash,
            observation_schema_version=self.cfg.observation_schema_version,
            external_inputs_hash=self.cfg.external_inputs_hash)

    @staticmethod
    def evidence(result: SimulatorResult, scenario: Scenario, run_id: str) -> List[Evidence]:
        # Both decisions and trajectories are retained; evidence binds content to run.
        items = ([{"kind": "decision", "data": d} for d in result.decisions]
                 + [{"kind": "trajectory", "data": t} for t in result.trajectories])
        # One run-scoped context is sufficient. Repeating a multi-flight context
        # in every control sample makes long external simulations quadratic.
        if items:
            items[0]["context"] = result.context
        return [Evidence(evidence_id=digest({"run_id": run_id, "step": n, "observation": item}),
            scenario_hash=scenario.content_hash, step=n, observation=item) for n, item in enumerate(items)]

    def run(self, pool: List[Individual], suite: ScenarioSuite, prefix: str) -> Dict[str, List[RunUse]]:
        # One call covers a generation's entire candidate pool and scenario suite.
        # Adapters may retain resources within this batch and release them before
        # evaluation starts, including when execution aborts.
        with ExitStack() as resources:
            testing_batch = getattr(self.simulator, "testing_batch", None)
            if testing_batch is not None:
                resources.enter_context(testing_batch(self.cfg))
            return self._run(pool, suite, prefix)

    def _run(self, pool: List[Individual], suite: ScenarioSuite, prefix: str) -> Dict[str, List[RunUse]]:
        output, coverage, real_calls = {}, [], 0
        for individual in pool:
            self.store.source(individual)
            uses, executed_keys, generation_error_keys, required, environment_failures = [], [], [], [], []
            for scenario in suite.scenarios:
                key = self.key(individual, scenario)
                required.append(key.key)
                for retry in range(self.cfg.infra_retries + 1):
                    if individual.generation_errors:
                        result = SimulatorResult(status=Status.INVALID,
                            failure_category="GENERATION_ERROR", stderr="\n".join(individual.generation_errors))
                        break
                    real_calls += 1
                    try:
                        result = SimulatorResult.model_validate(self.simulator.run(
                            self.store.path(individual.artifact_ref), scenario, self.cfg))
                        if result.status == Status.INFRA_ERROR:
                            raise InfrastructureError(result.stderr or "simulator infrastructure error")
                    except AlgorithmExecutionError as error:
                        result = SimulatorResult(status=Status.INVALID,
                            failure_category=error.category, stderr=str(error))
                        break
                    except InfrastructureError as error:
                        ref = self.store.save(f"{prefix}/testing/infrastructure/{uuid.uuid4().hex}.json", {
                            "individual_id": individual.individual_id, "execution_key": key.key,
                            "retry": retry, "reason": str(error), "status": "INFRA_ERROR"})
                        environment_failures.append(ref)
                        if retry == self.cfg.infra_retries:
                            self.store.save(prefix + "/testing/incomplete_coverage.json", {
                                "individual_id": individual.individual_id, "required_execution_keys": required,
                                "executed_keys": executed_keys,
                                "missing_keys": [key.key], "environment_failures": environment_failures})
                            raise
                        continue
                    break
                run_id = uuid.uuid4().hex
                record = ExecutionRecord(individual_id=individual.individual_id, execution_key=key,
                    run_id=run_id, scenario_id=scenario.scenario_id, result=result,
                    provenance="GENERATION_ERROR" if individual.generation_errors else "SIMULATOR",
                    evidence=self.evidence(result, scenario, run_id),
                    stdout_ref=self.store.object({"text": result.stdout}),
                    stderr_ref=self.store.object({"text": result.stderr}))
                record_ref = self.store.object(record)
                uses.append(RunUse(individual_id=individual.individual_id, record=record, record_ref=record_ref))
                if individual.generation_errors:
                    generation_error_keys.append(key.key)
                else:
                    executed_keys.append(key.key)
            output[individual.individual_id] = uses
            coverage.append(Coverage(individual_id=individual.individual_id, required_execution_keys=required,
                executed_keys=executed_keys, generation_error_keys=generation_error_keys,
                missing_keys=[], environment_failures=environment_failures))
        self.store.save(prefix + "/testing/execution_uses.json", {k: [u.model_dump(mode="json") for u in v] for k, v in output.items()})
        self.store.save(prefix + "/testing/coverage_report.json", {
            "coverage": [c.model_dump() for c in coverage], "real_simulator_calls": real_calls})
        return output
