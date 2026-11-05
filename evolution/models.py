from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Status(str, Enum):
    EXECUTABLE = "EXECUTABLE"
    INVALID = "INVALID"
    INFRA_ERROR = "INFRA_ERROR"


class SourceFile(Model):
    path: str
    content: str


class CodeProposal(Model):
    strategy_tag: str
    files: list[SourceFile] = Field(min_length=1)
    rationale: str


class StrategyPlan(Model):
    strategies: list[str] = Field(min_length=2)


class Individual(Model):
    individual_id: str
    code_hash: str
    artifact_ref: str
    generation_born: int
    parent_ids: list[str]
    operator: Literal["INITIAL", "REPAIR", "CROSSOVER", "MUTATION"]
    strategy_tag: str
    lineage_ref: str
    generation_errors: list[str] = Field(default_factory=list)


class Scenario(Model):
    scenario_id: str
    inputs: dict[str, Any]
    seed: int
    purpose: str

    @property
    def content_hash(self) -> str:
        # Labels and purpose are annotations, not simulator inputs.
        return digest({"inputs": self.inputs, "seed": self.seed})


class ScenarioProposal(Model):
    scenarios: list[Scenario] = Field(min_length=1)


class ScenarioSuite(Model):
    generation: int
    scenarios: list[Scenario] = Field(min_length=1)
    scenario_space_version: str
    rubric_version: str


class ExecutionKey(Model):
    code_hash: str
    scenario_content_hash: str
    env_image_hash: str
    simulator_version: str
    config_hash: str
    seed: int
    harness_hash: str
    observation_schema_version: str
    external_inputs_hash: str

    @property
    def key(self) -> str:
        return digest(self)


class Evidence(Model):
    evidence_id: str
    scenario_hash: str
    step: int
    observation: dict[str, Any]


class SimulatorResult(Model):
    status: Status
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    trajectories: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    failure_category: Optional[str] = None


class ExecutionRecord(Model):
    individual_id: str
    execution_key: ExecutionKey
    run_id: str
    scenario_id: str
    result: SimulatorResult
    evidence: list[Evidence]
    stdout_ref: str
    stderr_ref: str
    provenance: Literal["SIMULATOR", "GENERATION_ERROR"] = "SIMULATOR"


class RunUse(Model):
    individual_id: str
    record: ExecutionRecord
    cache_hit: bool
    record_ref: str


class Coverage(Model):
    individual_id: str
    required_execution_keys: list[str]
    cached_keys: list[str]
    executed_keys: list[str]
    generation_error_keys: list[str]
    missing_keys: list[str]
    environment_failures: list[str]


class Diagnosis(Model):
    individual_id: str
    status: Status
    category: str
    failure_refs: list[str]
    next_generation_feedback: str


class Correctness(Model):
    passed: bool
    explanation: str


class AnomalyClaim(Model):
    claim_id: str
    scenario_id: str
    claim: str
    alternative_explanations: list[str]
    evidence_refs: list[str]
    hypothesis_only: bool


class AnalystOutput(Model):
    claims: list[AnomalyClaim]


class ReviewOutput(Model):
    observations: list[str]
    counter_evidence_refs: list[str]


class SemanticRelations(Model):
    shared_premises: list[str]
    correlated_claims: list[str]
    contradictions: list[str]
    independent_claims: list[str]


class Verdict(Model):
    claim_id: str
    status: Literal["CONFIRMED", "REJECTED", "UNCONFIRMED"]
    evidence_refs: list[str]
    explanation: str
    opposing_explanation: str


class ArbiterOutput(Model):
    verdicts: list[Verdict]
    strengths: list[str]
    tradeoffs: list[str]


class PreferenceOutput(Model):
    winner: Literal["A_BETTER", "B_BETTER", "TIE", "INCOMPARABLE"]
    explanation: str
    evidence_refs: list[str]
    serious_regression: bool


class PairJudgment(Model):
    individual_a: str
    individual_b: str
    winner: Literal["A_BETTER", "B_BETTER", "TIE", "INCOMPARABLE"]
    order_consistent: bool
    serious_regression: bool
    reasoning_refs: list[str]


class Assessment(Model):
    individual: Individual
    status: Status
    correctness_pass: bool
    comparable: bool
    diagnosis: Optional[Diagnosis] = None
    profile_ref: Optional[str] = None

    @property
    def needs_repair(self) -> bool:
        return self.status != Status.EXECUTABLE or not self.correctness_pass


class SelectionReport(Model):
    generation: int
    pool_ids: list[str]
    executable_pool_ids: list[str]
    invalid_pool_ids: list[str]
    elite_ids: list[str]
    selected_invalid_ids: list[str]
    selected_ids: list[str]
    duplicate_code_groups: list[list[str]]
    comparison_graph: list[PairJudgment]
    behavior_quality_available: bool
    rule_version: str = "evaluation-selection-v3"


class Population(Model):
    generation: int
    population_ids: list[str]
    assessments: list[Assessment]
    elite_ids: list[str]
    selection_report_ref: str
    config_hash: str

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.population_ids)) != len(self.population_ids):
            raise ValueError("population IDs must be distinct")
        if [a.individual.individual_id for a in self.assessments] != self.population_ids:
            raise ValueError("assessments must match population order")
        return self
