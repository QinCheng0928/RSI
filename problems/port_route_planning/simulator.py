"""Port-route-planning Python simulation integration.

TODO: connect the actual simulator and sandbox using evolution.framework.Simulator.
Keep the coupled road environment, dynamics models, and execution integration in
this problem directory. execution.simulator_version identifies their combined
implementation and dependencies. No synthetic execution records are produced
while integration is pending.
"""
from pathlib import Path

from evolution.config import ExecutionConfig
from evolution.errors import SimulatorNotConfigured
from evolution.models import Scenario, SimulatorResult


class PendingSimulator:
    sandboxed = False
    deterministic = False

    def check_readiness(self, config: ExecutionConfig) -> None:
        raise SimulatorNotConfigured(
            "TODO: port_route_planning simulator interface and execution sandbox are not connected"
        )

    def run(self, code_directory: Path, scenario: Scenario, config: ExecutionConfig) -> SimulatorResult:
        raise SimulatorNotConfigured(
            "TODO: implement port_route_planning Simulator.run; no synthetic execution records are produced"
        )
