class EvolutionError(RuntimeError):
    """An explicit experiment failure; never a compatibility fallback."""


class InfrastructureError(EvolutionError):
    pass


class AlgorithmExecutionError(EvolutionError):
    """Actual candidate failure reported by the simulator execution worker."""

    def __init__(self, message: str, category: str = "RUNTIME_ERROR"):
        super().__init__(message)
        self.category = category


class SimulatorNotConfigured(InfrastructureError):
    pass


class PopulationInsufficient(EvolutionError):
    pass


class ContractError(EvolutionError):
    pass
