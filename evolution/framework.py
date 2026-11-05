"""Trusted problem contract; candidate code is never imported in the host."""
from abc import ABC, abstractmethod
from typing import Any

from evolution.models import Correctness, Scenario, SimulatorResult


class ProblemSpec(ABC):
    @abstractmethod
    def validate_scenario(self, scenario: Scenario) -> None:
        """Reject inputs outside the configured domain (raise ValueError)."""

    @abstractmethod
    def check_correctness(self, scenario: Scenario, result: SimulatorResult) -> Correctness:
        """Deterministic domain checks, independent of LLM preferences."""

    @abstractmethod
    def sanitize_feedback(self, confirmed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Only task/scenario/observed symptom; no candidate identity or lineage."""
