"""Task contracts only; behavioral quality is judged from flight observations."""
from typing import List, Union
from itertools import combinations
import math

from pydantic import Field, StrictFloat, StrictInt, model_validator

from evolution.framework import ProblemSpec
from evolution.models import Correctness, Model, Scenario, SimulatorResult, Status


POS_MIN = (-5.0, -15.0, -7.0)
POS_MAX = (20.0, -5.0, -1.0)
Vector = List[Union[StrictFloat, StrictInt]]


def vector(value):
    if len(value) != 3 or any(not math.isfinite(x) for x in value):
        raise ValueError("positions must be finite NED vectors of length three")
    return value


def within(position):
    return all(lo <= x <= hi for x, lo, hi in zip(vector(position), POS_MIN, POS_MAX))


def validate_positions(positions):
    if not all(within(p) for p in positions):
        raise ValueError("all initial UAV positions must be inside the flight airspace")
    if any(math.dist(a, b) <= 1.0 for a, b in combinations(positions, 2)):
        raise ValueError("every pair of initial UAV positions must be strictly more than 1 m apart")


class FlightScenario(Model):
    num_adversaries: int = Field(strict=True, ge=1, le=4)
    initial_positions: List[Vector]
    goal: Vector = Field(default_factory=lambda: [15.0, -10.0, -4.0])
    intensities: List[Union[StrictFloat, StrictInt]] = Field(default_factory=lambda: [0.0, 0.5, 1.0])
    max_steps: int = Field(default=500, strict=True, ge=100, le=500)

    @model_validator(mode="after")
    def valid(self):
        if len(self.initial_positions) != self.num_adversaries + 1:
            raise ValueError("initial_positions must contain the tested UAV followed by N adversaries")
        validate_positions(self.initial_positions)
        if not within(self.goal):
            raise ValueError("goal must be inside the flight airspace")
        if math.dist(self.goal, self.initial_positions[0]) <= 3.0:
            raise ValueError("tested UAV must start outside the 3 m goal tolerance")
        if (not 2 <= len(self.intensities) <= 5 or self.intensities[0] != 0
                or self.intensities[-1] != 1
                or any(not math.isfinite(x) or not 0 <= x <= 1 for x in self.intensities)
                or any(a >= b for a, b in zip(self.intensities, self.intensities[1:]))):
            raise ValueError("intensities must be 2–5 strictly increasing values from 0 to 1, including both endpoints")
        return self


class UAVAdversarial(ProblemSpec):
    def validate_scenario(self, scenario: Scenario) -> None:
        if not scenario.scenario_id:
            raise ValueError("scenario_id must be nonempty")
        FlightScenario.model_validate(scenario.inputs)

    def check_correctness(self, scenario: Scenario, result: SimulatorResult) -> Correctness:
        """Check execution and physical constraints; never score obstruction or intensity."""
        try:
            expected = FlightScenario.model_validate(scenario.inputs)
            if result.status != Status.EXECUTABLE:
                raise ValueError(result.failure_category or "candidate did not execute")
            episodes = result.context["episodes"]
            if [e["intensity"] for e in episodes] != expected.intensities:
                raise ValueError("missing paired intensity flights")
            for episode in episodes:
                validate_positions(episode["initial_positions"])
                if len(episode["initial_positions"]) != expected.num_adversaries + 1:
                    raise ValueError("wrong number of initialized UAVs")
                if any(math.dist(a, b) > 0.05 for a, b in zip(
                        episode["initial_positions"], expected.initial_positions)):
                    raise ValueError("measured initial positions differ from requested positions")
                if not episode["complete"] or episode["checked_steps"] < 1:
                    raise ValueError("missing complete monitored flight")
                if episode["violations"]:
                    raise ValueError(str(episode["violations"]))
                frames = [t for t in result.trajectories if t["episode"] == episode["episode"]]
                if not frames or frames[0]["step"] != 0 or frames[-1]["step"] != episode["checked_steps"]:
                    raise ValueError("missing initial or terminal flight evidence")
                for frame in frames:
                    positions = frame["positions"]
                    if len(positions) != expected.num_adversaries + 1:
                        raise ValueError("wrong number of observed UAVs")
                    if any(not within(p) for p in positions[1:]):
                        raise ValueError("adversarial UAV left airspace")
                    if any(math.dist(a, b) <= 1.0 for a, b in combinations(positions, 2)):
                        raise ValueError("UAV separation at or below 1 m")
            return Correctness(passed=True, explanation="Initialization and monitored physical constraints passed; flight quality requires behavioral evaluation.")
        except (ValueError, KeyError, TypeError) as error:
            return Correctness(passed=False, explanation=str(error))

    def sanitize_feedback(self, confirmed):
        return [{"scenario_id": item["scenario_id"], "symptom": item["symptom"]} for item in confirmed]


SPEC = UAVAdversarial()
