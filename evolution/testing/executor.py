from __future__ import annotations

import uuid

from pydantic import ValidationError

from evolution.config import ExecutionConfig
from evolution.errors import AlgorithmExecutionError, ContractError, InfrastructureError
from evolution.framework import Simulator
from evolution.models import (Coverage, Evidence, ExecutionKey, ExecutionRecord, Individual,
                              RunUse, Scenario, ScenarioSuite, SimulatorResult, Status, digest)
from evolution.storage import Store


class Executor:
    """Execution and raw-record caching are internal services of Testing."""

    def __init__(self, cfg: ExecutionConfig, simulator: Simulator, store: Store, contract_hash: str):
        self.cfg, self.simulator, self.store, self.contract_hash = cfg, simulator, store, contract_hash

    def preflight(self) -> None:
        self.simulator.preflight(self.cfg)
        if not self.simulator.sandboxed:
            raise InfrastructureError("simulator adapter must provide a real execution sandbox")
        if any(value == "TODO" for value in (self.cfg.simulator_version, self.cfg.harness_hash)):
            raise InfrastructureError("simulator version and harness fingerprint must be configured")
        if self.cfg.cache_enabled and self.cfg.deterministic and not self.simulator.deterministic:
            raise InfrastructureError("adapter cannot guarantee the configured deterministic cache semantics")

    def key(self, individual: Individual, scenario: Scenario) -> ExecutionKey:
        return ExecutionKey(code_hash=individual.code_hash, scenario_content_hash=scenario.content_hash,
            simulator_version=self.cfg.simulator_version,
            config_hash=digest({"environment": self.cfg.environment, "contract": self.contract_hash,
                "timeout_seconds": self.cfg.timeout_seconds, "memory_mb": self.cfg.memory_mb}),
            seed=scenario.seed, harness_hash=self.cfg.harness_hash,
            observation_schema_version=self.cfg.observation_schema_version,
            external_inputs_hash=self.cfg.external_inputs_hash)

    @staticmethod
    def evidence(result: SimulatorResult, scenario: Scenario, run_id: str) -> list[Evidence]:
        # Both decisions and trajectories are retained; evidence binds content to run.
        items = ([{"kind": "decision", "data": d, "context": result.context} for d in result.decisions]
                 + [{"kind": "trajectory", "data": t, "context": result.context} for t in result.trajectories])
        return [Evidence(evidence_id=digest({"run_id": run_id, "step": n, "observation": item}),
            scenario_hash=scenario.content_hash, step=n, observation=item) for n, item in enumerate(items)]

    def load_cached(self, key: ExecutionKey, individual: Individual, scenario: Scenario):
        cached = self.store.cached(key.key)
        if cached is None:
            return None
        try:
            record = ExecutionRecord.model_validate(cached["data"])
            if (record.execution_key != key or record.provenance != "SIMULATOR"
                    or record.result.status == Status.INFRA_ERROR):
                raise ContractError("cache execution metadata mismatch")
            if self.store.verified_object(record.stdout_ref) != {"text": record.result.stdout}:
                raise ContractError("cache stdout mismatch")
            if self.store.verified_object(record.stderr_ref) != {"text": record.result.stderr}:
                raise ContractError("cache stderr mismatch")
            if self.evidence(record.result, scenario, record.run_id) != record.evidence:
                raise ContractError("cache evidence content/provenance mismatch")
        except (OSError, ValueError, KeyError, ContractError, ValidationError) as error:
            self.store.event("CACHE_INVALIDATED", {"key": key.key, "reason": str(error)})
            return None
        return RunUse(individual_id=individual.individual_id, record=record, cache_hit=True, record_ref=cached["record_ref"])

    def run(self, pool: list[Individual], suite: ScenarioSuite, prefix: str) -> dict[str, list[RunUse]]:
        output, coverage, real_calls = {}, [], 0
        use_cache = self.cfg.cache_enabled and self.cfg.deterministic and self.simulator.deterministic
        for individual in pool:
            self.store.source(individual)
            uses, cached_keys, executed_keys, generation_error_keys, required, environment_failures = [], [], [], [], [], []
            for scenario in suite.scenarios:
                key = self.key(individual, scenario)
                required.append(key.key)
                cacheable = use_cache and not individual.generation_errors
                used = self.load_cached(key, individual, scenario) if cacheable else None
                if used is not None:
                    cached_keys.append(key.key)
                    uses.append(used)
                    continue
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
                                "cached_keys": cached_keys, "executed_keys": executed_keys,
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
                if cacheable:
                    self.store.pointer(f"execution_cache/{key.key}.json", {"record_ref": record_ref})
                uses.append(RunUse(individual_id=individual.individual_id, record=record, cache_hit=False, record_ref=record_ref))
                if individual.generation_errors:
                    generation_error_keys.append(key.key)
                else:
                    executed_keys.append(key.key)
            output[individual.individual_id] = uses
            coverage.append(Coverage(individual_id=individual.individual_id, required_execution_keys=required,
                cached_keys=cached_keys, executed_keys=executed_keys, generation_error_keys=generation_error_keys,
                missing_keys=[], environment_failures=environment_failures))
        self.store.save(prefix + "/testing/execution_uses.json", {k: [u.model_dump(mode="json") for u in v] for k, v in output.items()})
        self.store.save(prefix + "/testing/coverage_report.json", {
            "coverage": [c.model_dump() for c in coverage], "real_simulator_calls": real_calls,
            "cache_hits": sum(len(c.cached_keys) for c in coverage),
            "cache_policy": "exact_execution_key" if use_cache else "disabled_rerun_all_candidates"})
        return output
