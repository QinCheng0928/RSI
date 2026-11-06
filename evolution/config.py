from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from evolution.models import Model, digest


class ExperimentConfig(Model):
    population_size: int = Field(default=10, ge=2)
    generations: int = Field(default=8, ge=0)
    seed: int = 42


class GeneticsConfig(Model):
    crossover_probability: float = Field(default=0.5, ge=0, le=1)
    mutation_probability: float = Field(default=0.5, ge=0, le=1)
    elite_count: int = Field(default=2, ge=0)


class EvaluationConfig(Model):
    anomaly_analyst_count: int = Field(default=3, ge=2)


class ExecutionConfig(Model):
    simulator: str
    cache_enabled: bool = True
    deterministic: bool = True
    environment: dict[str, Any] = Field(default_factory=dict)
    # Version of the complete Python simulation: environment, dynamics, and dependencies.
    simulator_version: str = "TODO"
    harness_hash: str = "TODO"
    observation_schema_version: str = "v1"
    external_inputs_hash: str = "none"
    infra_retries: int = Field(default=1, ge=0, le=5)
    timeout_seconds: float = Field(default=30, gt=0)
    memory_mb: int = Field(default=512, gt=0)


class ModelProfile(Model):
    base_url: str
    model: str
    timeout_seconds: float = Field(default=120, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)
    structured_output: Literal["json_object", "json_schema"] = "json_object"
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reserved(self):
        reserved = {"model", "messages", "response_format", "stream", "api_key", "api_key_env", "base_url", "n"}
        if reserved.intersection(self.parameters):
            raise ValueError("model.parameters contains reserved request fields")
        return self


class AgentConfig(Model):
    generator: str
    tester: str
    analysts: list[str]
    independence: str
    arbiter: str
    preference: str


class TaskSpec(Model):
    task_id: str
    goal: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    hard_constraints: list[str]
    allowed_changes: list[str]


class ScenarioSpace(Model):
    version: str
    types: list[str]
    parameter_ranges: dict[str, Any]
    validity_rules: list[str]


class Rubric(Model):
    rubric_version: str
    principles: list[str]
    tradeoff_guidance: str


class ProblemConfig(Model):
    problem_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    spec: str
    template: str = "template/algorithm.py.txt"
    experiment: ExperimentConfig = Field(default_factory=ExperimentConfig)
    genetics: GeneticsConfig = Field(default_factory=GeneticsConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    execution: ExecutionConfig
    task: TaskSpec
    scenario_space: ScenarioSpace
    rubric: Rubric
    models: dict[str, ModelProfile]
    agents: AgentConfig
    core_scenarios: list[dict[str, Any]] = Field(default_factory=list)
    storage_root: str = "runtime_data"

    @property
    def comparison_budget_per_generation(self) -> int:
        """Two orders for every pair in the largest possible parent/child pool."""
        maximum_candidates = 2 * self.experiment.population_size
        return maximum_candidates * (maximum_candidates - 1)

    @model_validator(mode="after")
    def invariants(self):
        p, g = self.experiment.population_size, self.genetics
        if g.elite_count >= p:
            raise ValueError("elite_count must be smaller than P")
        if abs(g.crossover_probability + g.mutation_probability - 1) > 1e-9:
            raise ValueError("crossover_probability + mutation_probability must equal 1")
        names = [self.agents.generator, self.agents.tester, self.agents.independence,
                 self.agents.arbiter, self.agents.preference, *self.agents.analysts]
        if not self.agents.analysts or any(n not in self.models for n in names):
            raise ValueError("every agent must reference an explicit model profile")
        if len(self.agents.analysts) not in (1, self.evaluation.anomaly_analyst_count):
            raise ValueError("analysts must contain one shared profile or exactly m profiles")
        if self.problem_id != self.task.task_id:
            raise ValueError("problem_id must match task.task_id")
        return self


def load_config(path: Path, experiment: str = None) -> ProblemConfig:
    raw = yaml.safe_load(path.read_text())
    if experiment:
        import re
        if not re.fullmatch(r"[a-z][a-z0-9_]*", experiment):
            raise ValueError("invalid experiment name")
        override = yaml.safe_load((path.parent / "experiments" / (experiment + ".yaml")).read_text())

        def merge(base: dict, delta: dict) -> dict:
            result = dict(base)
            for key, value in delta.items():
                result[key] = merge(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
            return result

        raw = merge(raw, override)
    return ProblemConfig.model_validate(raw)


def load_symbol(reference: str):
    module, name = reference.split(":")
    return getattr(importlib.import_module(module), name)


def config_fingerprint(cfg: ProblemConfig, directory: Path) -> str:
    # Trusted per-problem code, templates, instance data and ablations are frozen.
    files = {str(p.relative_to(directory)): digest(p.read_bytes().hex())
             for p in sorted(directory.rglob("*")) if p.is_file()
             and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    core = Path(__file__).parent
    framework_files = {str(p.relative_to(core)): digest(p.read_bytes().hex()) for p in sorted(core.rglob("*.py"))}
    return digest({"config": cfg.model_dump(mode="json"), "problem_files": files, "framework_files": framework_files})
