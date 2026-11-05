"""Port-route-planning adapter for the external simulation environment.

TODO: connect the actual simulator and sandbox using evolution.framework.Simulator.
Keep task-specific transport, input/action translation, and execution integration
here. No synthetic execution records are produced while integration is pending.
"""
from pathlib import Path

from evolution.config import ExecutionConfig
from evolution.errors import SimulatorNotConfigured
from evolution.models import Scenario, SimulatorResult


class PendingSimulator:
    sandboxed = False
    deterministic = False

    def preflight(self, config: ExecutionConfig) -> None:
        raise SimulatorNotConfigured(
            "TODO: port_route_planning simulator interface and execution sandbox are not connected"
        )

    def run(self, code_directory: Path, scenario: Scenario, config: ExecutionConfig) -> SimulatorResult:
        raise SimulatorNotConfigured(
            "TODO: implement port_route_planning Simulator.run; no synthetic execution records are produced"
        )
