from __future__ import annotations

from typing import Any, Dict, List

from evolution.config import Rubric, ScenarioSpace, TaskSpec
from evolution.errors import ContractError
from evolution.framework import ProblemSpec
from evolution.llm import LLM
from evolution.models import Scenario, ScenarioProposal, ScenarioSuite
from evolution.storage import Store


class Tester:
    """Black-box prompt builder: takes no individual, source, operator or lineage."""
    __test__ = False

    def __init__(self, task: TaskSpec, space: ScenarioSpace, rubric: Rubric,
                 profile: str, spec: ProblemSpec, llm: LLM, store: Store):
        self.task, self.space, self.rubric = task, space, rubric
        self.profile, self.spec, self.llm, self.store = profile, spec, llm, store

    def build(self, generation: int, core: List[Scenario], previous: List[Scenario],
              regression: List[Scenario], coverage: Dict[str, Any],
              sanitized_feedback: List[Dict[str, Any]], prefix: str) -> ScenarioSuite:
        # Caller supplies only scenario-level coverage and domain-sanitized symptoms.
        feedback = [{"scenario_id": item["scenario_id"], "symptom": item["symptom"]}
                    for item in sanitized_feedback]
        payload = {"task": self.task.model_dump(), "scenario_space": self.space.model_dump(),
            "generation": generation, "coverage": coverage, "sanitized_feedback": feedback,
            "known_scenarios": [s.model_dump() for s in core + previous + regression]}
        proposal = self.llm.ask(self.profile,
            "Design black-box test scenarios within the task's scenario space. "
            "Include boundary/coverage, regression, issue-driven and exploration cases. "
            "No algorithm implementation or lineage is available. Use stable scenario IDs.",
            payload, ScenarioProposal, prefix + "/testing/scenario_generation")
        self.store.save(prefix + "/testing/delta_suite.json", proposal)
        unique, names = {}, {}
        for scenario in core + previous + regression + proposal.scenarios:
            self.spec.validate_scenario(scenario)
            if scenario.scenario_id in names and names[scenario.scenario_id] != scenario.content_hash:
                raise ContractError("scenario ID cannot be reused with different inputs/seed")
            names[scenario.scenario_id] = scenario.content_hash
            unique.setdefault(scenario.content_hash, scenario)
        suite = ScenarioSuite(generation=generation, scenarios=list(unique.values()),
            scenario_space_version=self.space.version, rubric_version=self.rubric.rubric_version)
        self.store.save(prefix + "/testing/suite.json", suite)
        self.store.save(prefix + "/testing/regression_suite.json", [s.model_dump() for s in regression])
        return suite
