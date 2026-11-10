"""Load generated algorithms inside the simulator's execution process."""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
from pathlib import Path
from typing import Any, Callable, Dict

from evolution.errors import ContractError


def load_algorithm(path: Path) -> Callable[[Dict[str, Any], int], Dict[str, Any]]:
    """Import an immutable Python source file and return solve(observation, seed).

    Simulator adapters call this inside their sandbox/worker, then call solve
    with actual observations. Import and algorithm exceptions propagate to the
    adapter for real failure classification. No simulator is fabricated here.
    """
    path = Path(path).resolve(strict=True)
    if path.suffix != ".py":
        raise ContractError("algorithm source must be a Python .py file")
    module_name = "generated_algorithm_" + hashlib.sha256(str(path).encode()).hexdigest()
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ContractError("cannot construct algorithm module spec: " + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    solve = getattr(module, "solve", None)
    if not callable(solve) or inspect.iscoroutinefunction(solve):
        raise ContractError("algorithm must define a synchronous callable solve(observation, seed)")
    inspect.signature(solve).bind({}, 0)
    return solve
