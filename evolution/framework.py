"""Shared contracts for problem specifications and problem-owned simulators."""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol

from evolution.config import ExecutionConfig
from evolution.models import Correctness, Scenario, SimulatorResult


class Simulator(Protocol):
    """Uniform interface implemented by each problem's simulator adapter.

    Concrete adapters belong in problems/<problem_id>/, alongside domain code.
    They may implement a coupled Python environment/dynamics model in a local
    worker or connect to an external service. simulator_version identifies the
    complete simulation, including its environment, dynamics, and dependencies.
    """

    sandboxed: bool
    deterministic: bool

    def preflight(self, config: ExecutionConfig) -> None:
        """Verify simulation readiness, version fingerprints, and resource isolation.

        Check simulator_version against the complete environment/dynamics
        implementation and dependencies, not just the adapter's source version.
        Raise InfrastructureError if the environment is unavailable or invalid.
        """
        ...

    def run(self, code_directory: Path, scenario: Scenario, config: ExecutionConfig) -> SimulatorResult:
        """Execute one candidate/scenario and return actual evidence and errors.

        Load algorithm.py with evolution.testing.loader.load_algorithm inside
        the sandbox worker, then call solve(observation, seed). Enforce network,
        filesystem, CPU, memory, and wall-clock isolation in the real sandbox.
        Return INVALID for algorithm failures, or raise AlgorithmExecutionError
        with actual error details.
        Raise InfrastructureError for service failures. Never repair candidate
        code during execution.
        """
        ...


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
