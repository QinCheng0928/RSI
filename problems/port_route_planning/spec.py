from evolution.errors import ContractError
from evolution.framework import ProblemSpec
from evolution.models import Correctness, Scenario, SimulatorResult


class PortRoutePlanning(ProblemSpec):
    def validate_scenario(self, scenario: Scenario) -> None:
        if not scenario.scenario_id or not scenario.inputs:
            raise ValueError("scenario requires a nonempty ID and inputs")
        # TODO: validate actual simulator input schema/ranges once available.

    def check_correctness(self, scenario: Scenario, result: SimulatorResult) -> Correctness:
        # TODO: implement deterministic domain constraints with real simulator evidence.
        # Refuse to call an algorithm correct before the domain contract is known.
        raise ContractError("TODO: implement problem-specific deterministic correctness checks")

    def sanitize_feedback(self, confirmed):
        return [{"scenario_id": item["scenario_id"], "symptom": item["symptom"]} for item in confirmed]


SPEC = PortRoutePlanning()
