"""Only extension point for real simulator/sandbox integration.

TODO: implement Simulator with the user's future interface. The adapter must
execute candidate files in an OS/container sandbox (no network, read-only code,
isolated scratch, CPU/memory/wall-clock limits), capture actual logs and classify
algorithm faults separately from transport/service failures. The execution process uses testing.loader.load_algorithm
(importlib.util) to load candidate code, then calls solve(observation, seed).
"""
from pathlib import Path
from typing import Protocol

from evolution.config import ExecutionConfig
from evolution.errors import SimulatorNotConfigured
from evolution.models import Scenario, SimulatorResult


class Simulator(Protocol):
    sandboxed: bool
    deterministic: bool

    def preflight(self, config: ExecutionConfig) -> None:
        """Verify health, actual image/harness/dependency hashes and resource isolation.

        Must raise InfrastructureError on unavailable services or fingerprint
        mismatch. Populate all result-affecting external hashes in the config.
        """
        ...

    def run(self, code_directory: Path, scenario: Scenario, config: ExecutionConfig) -> SimulatorResult:
        """Return real behavior/errors; raise InfrastructureError for service failures.

        In the execution worker, load code_directory / 'algorithm.py' with
        evolution.testing.loader.load_algorithm and call the returned solve.
        Return INVALID for syntax/interface/runtime/timeout faults and
        EXECUTION_BLOCKED for a blocked operation, preserving real errors.
        Alternatively raise AlgorithmExecutionError with the original worker
        traceback and category; the executor records it and moves on.
        Never repair or regenerate code inside this call.
        """
        ...


class PendingSimulator:
    sandboxed = False
    deterministic = False

    def preflight(self, config: ExecutionConfig) -> None:
        raise SimulatorNotConfigured("TODO: real simulator interface and execution sandbox are not connected")

    def run(self, code_directory: Path, scenario: Scenario, config: ExecutionConfig) -> SimulatorResult:
        raise SimulatorNotConfigured("TODO: implement Simulator.run; no synthetic execution records are produced")
