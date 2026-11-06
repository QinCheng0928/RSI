import argparse
import json
import re
import uuid
from pathlib import Path

from evolution.config import load_config
from evolution.errors import EvolutionError
from evolution.runner import Evolution
from evolution.storage import Store


def main() -> int:
    parser = argparse.ArgumentParser(description="Generation → testing (execution/caching) → evaluation (selection)")
    parser.add_argument("command", choices=["prepare", "generate", "build-suite", "run"])
    parser.add_argument("--problem", default="port_route_planning", help="Problem directory name under problems/")
    parser.add_argument("--run-id", default=None, help="Reuse the same ID to resume from the last committed generation")
    parser.add_argument("--experiment", help="YAML configuration overlay name under the problem's experiments/ directory")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.problem):
        parser.error("invalid problem directory name")
    directory = Path("problems") / args.problem
    cfg = load_config(directory / "problem.yaml", args.experiment)
    run_id = args.run_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        parser.error("invalid run ID")
    root = Path(cfg.storage_root) / cfg.problem_id / run_id
    engine = Evolution(cfg, directory, Store(root))
    try:
        if args.command == "prepare":
            from evolution.runner import run_lock
            with run_lock(engine.store):
                engine.prepare()
            result = {"status": "PREPARED", "run_directory": str(root)}
        elif args.command == "generate":
            result = {"status": "GENERATED_UNTESTED", "artifacts": str(root / engine.generate_only())}
        elif args.command == "build-suite":
            result = {"status": "SUITE_BUILT_UNEXECUTED", "artifacts": str(root / engine.suite_only())}
        else:
            result = engine.run()
    except EvolutionError as error:
        print(json.dumps({"status": "RUN_ABORTED", "reason": str(error), "run_directory": str(root)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
