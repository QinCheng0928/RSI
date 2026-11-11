from __future__ import annotations
from typing import List

import difflib
import random
from pathlib import Path, PurePosixPath

from pydantic import Field, create_model

from evolution.config import ProblemConfig
from evolution.errors import ContractError
from evolution.llm import LLM
from evolution.models import Assessment, CodeProposal, Individual, StrategyPlan, digest
from evolution.storage import Store, is_within


class Developer:
    """One model output per candidate; all outputs proceed to testing/evaluation."""

    def __init__(self, cfg: ProblemConfig, directory: Path, llm: LLM, store: Store):
        self.cfg, self.llm, self.store = cfg, llm, store
        template = (directory / cfg.template).resolve()
        if not is_within(template, directory.resolve()):
            raise ContractError("template must live in the problem directory")
        self.template = template.read_text()

    def generate(self, generation: int, parents: List[Assessment], prefix: str, feedback: dict) -> List[Individual]:
        cfg, p = self.cfg, self.cfg.experiment.population_size
        rng = random.Random(cfg.experiment.seed + generation * 100003)
        if parents and (len(parents) != p or any(a.individual.generation_born >= generation for a in parents)):
            raise ContractError("generation requires P assessed parents from earlier generations")
        repair_parents = [a for a in parents if a.needs_repair]
        variation_parents = [a for a in parents if not a.needs_repair]
        strategies = []
        if not parents:
            plan_schema = create_model(
                f"StrategyPlan{p}", __base__=StrategyPlan,
                strategies=(List[str], Field(min_length=p, max_length=p,
                    description=f"Exactly {p} distinct, self-contained alternative algorithm strategies; one per candidate.")))
            plan = self.llm.ask(cfg.agents.generator,
                f"Plan exactly {p} distinct initial algorithm strategies for {p} separate candidates. "
                f"Return a strategies array containing exactly {p} strings. Each string must describe "
                "one complete, self-contained alternative algorithm that satisfies the full task. "
                "Do not split one algorithm into components, implementation steps, safety filters, "
                "or parameter settings as separate entries. Diversify the main decision approach "
                "across candidates. Do not write code.",
                {"task": cfg.task.model_dump(), "P": p}, plan_schema, prefix + "/strategy_plan")
            self.store.save(prefix + "/strategy_plan.json", plan)
            if len(plan.strategies) != p:
                raise ContractError(f"strategy planner returned {len(plan.strategies)} strategies; expected exactly {p}")
            strategies = plan.strategies
        produced, operations = [], []
        for slot in range(p):
            chosen: List[Assessment] = []
            if parents:
                if slot < len(repair_parents):
                    # Every selected failure gets exactly one child, using only
                    # diagnosis from an already completed earlier generation.
                    operator, chosen = "REPAIR", [repair_parents[slot]]
                    assignment = "previous_generation_failure"
                else:
                    # Binary crossover needs two distinct eligible parents.
                    if len(variation_parents) == 1:
                        operator = "MUTATION"
                        assignment = "single_variation_parent"
                    else:
                        operator = rng.choices(["CROSSOVER", "MUTATION"], weights=[
                            cfg.genetics.crossover_probability, cfg.genetics.mutation_probability], k=1)[0]
                        assignment = "random_variation"
                    chosen = rng.sample(variation_parents, 2 if operator == "CROSSOVER" else 1)
                strategy = {"REPAIR": "failure_log_guided_patch", "CROSSOVER": "semantic_recombination",
                            "MUTATION": "local_variation"}[operator]
            else:
                operator, strategy = "INITIAL", strategies[slot]
                assignment = "initialization"
            location = f"{prefix}/generation/slot_{slot:03d}"
            parent_inputs = []
            for assessment in chosen:
                parent = assessment.individual
                lineage = self.store.read(parent.lineage_ref)
                parent_inputs.append({"individual": parent.model_dump(), "needs_repair": assessment.needs_repair,
                    "files": self.store.source(parent),
                    "diagnosis": assessment.diagnosis.model_dump() if assessment.diagnosis else None,
                    "failure_records": {ref: self.store.read(ref) for ref in
                        (assessment.diagnosis.failure_refs if assessment.diagnosis else [])},
                    "generation_output": self.store.read(lineage["output_ref"])})
            files, errors, rationale, tag = {}, [], "", strategy
            try:
                proposal = self.llm.ask(cfg.agents.generator,
                    "Generate Python source implementing solve(observation, seed) using the assigned operator. "
                    "Use only previous-generation evaluation feedback. REPAIR fixes the assigned failed parent. "
                    "CROSSOVER and MUTATION act only on parents assessed as not needing repair. "
                    "Return complete files. This candidate will be tested and evaluated without regeneration.",
                    {"task": cfg.task.model_dump(), "template": self.template, "operator": operator,
                     "strategy": strategy, "parents": parent_inputs, "feedback": feedback,
                     "seed": rng.randrange(2**31)}, CodeProposal, location)
                output = proposal.model_dump()
                rationale, tag = proposal.rationale, proposal.strategy_tag
                # Preserve unusable submissions; only in-directory .py files can
                # be materialized. Syntax and behavior are left to later stages.
                for source in proposal.files:
                    path = PurePosixPath(source.path)
                    if (path.is_absolute() or ".." in path.parts or "\\" in source.path
                            or path.suffix != ".py" or str(path) != source.path
                            or source.path not in cfg.task.allowed_changes or source.path in files):
                        errors.append("unusable file submission: " + source.path)
                    else:
                        files[source.path] = source.content
                if "algorithm.py" not in files:
                    errors.append("missing algorithm.py")
            except ContractError as error:
                # Output/schema/refusal faults become candidates, never an in-round retry.
                errors.append(str(error))
                output = {"error": str(error), "raw_responses": [self.store.read(str(path.relative_to(self.store.root)))
                    for path in sorted(self.store.path(location).rglob("response.json"))]}
            output_ref = self.store.save(location + "/output.json", output)
            code_hash = digest(files)
            individual_id = "alg_" + digest({"prefix": prefix, "slot": slot, "code_hash": code_hash})
            base = "individuals/" + individual_id
            individual = Individual(individual_id=individual_id, code_hash=code_hash, artifact_ref=base + "/code",
                generation_born=generation, parent_ids=[a.individual.individual_id for a in chosen],
                operator=operator, strategy_tag=tag, lineage_ref=base + "/lineage.json", generation_errors=errors)
            diff = {}
            if chosen:
                original = self.store.source(chosen[0].individual)
                diff = {name: "".join(difflib.unified_diff(original.get(name, "").splitlines(True),
                    content.splitlines(True), fromfile="parent/" + name, tofile="child/" + name))
                    for name, content in files.items()}
            self.store.save_individual(individual, files, {"operator": operator,
                "parent_ids": individual.parent_ids, "patch_diff": diff,
                "recombination_plan": rationale, "output_ref": output_ref})
            produced.append(individual)
            operations.append({"slot": slot, "operator": operator, "individual_id": individual_id,
                               "parent_ids": individual.parent_ids, "assignment": assignment,
                               "generation_errors": errors})
        self.store.save(prefix + "/generation/offspring_manifest.json", {
            "generation": generation, "target_count": p,
            "repair_parent_ids": [a.individual.individual_id for a in repair_parents],
            "variation_parent_ids": [a.individual.individual_id for a in variation_parents],
            "operator_counts": {op: sum(o["operator"] == op for o in operations)
                                for op in ("INITIAL", "REPAIR", "CROSSOVER", "MUTATION")},
            "produced_ids": [i.individual_id for i in produced], "operation_records": operations})
        return produced
