from __future__ import annotations
from typing import List
import difflib
import random
from pathlib import Path, PurePosixPath
from pydantic import Field, create_model
from tqdm import tqdm

from evolution.config import ProblemConfig
from evolution.context import TraceContext
from evolution.errors import ContractError
from evolution.llm import LLM
from evolution.models import Assessment, CodeProposal, Individual, RunUse, StrategyPlan, TraceSummary, digest
from evolution.storage import Store, is_within


class Developer:
    """One model output per candidate; all outputs proceed to testing/evaluation."""

    def __init__(self, cfg: ProblemConfig, directory: Path, llm: LLM, store: Store):
        self.cfg, self.llm, self.store = cfg, llm, store
        self.context = TraceContext(cfg, llm, store)
        template = (directory / cfg.template).resolve()
        if not is_within(template, directory.resolve()):
            raise ContractError("template must live in the problem directory")
        self.template = template.read_text()

    @staticmethod
    def parent_feedback(chosen: List[Assessment], feedback: dict) -> dict:
        """Keep chosen-parent feedback; omit memory without individual attribution."""
        if not chosen:
            return {}
        parent_ids = {a.individual.individual_id for a in chosen}
        memory = []
        for event in feedback.get("reviewed_memory", []):
            assessments = [a for a in event.get("assessments", []) if a["individual_id"] in parent_ids]
            if assessments:
                memory.append({**{key: event[key] for key in ("generation", "evidence_level", "limits")
                                  if key in event}, "assessments": assessments})
        return {"selected_assessments": [a.model_dump(mode="json") for a in chosen],
                "reviewed_memory": memory,
                "behavior_feedback": {key: profile for key, profile in feedback.get("behavior_feedback", {}).items()
                                      if key in parent_ids}}

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
        for slot in tqdm(range(p), desc=f"gen_{generation:04d} 生成", unit="算法"):
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
            request_feedback = self.parent_feedback(chosen, feedback)
            for parent_id, profile in request_feedback.get("behavior_feedback", {}).items():
                items = [{key: value for key, value in trace.items() if key != "evidence"}
                         for trace in profile.get("trace", [])]
                items += [{"confirmed_finding": finding} for finding in profile.get("confirmed_anomalies", [])]
                note = self.context.summarize(cfg.agents.generator, items,
                    {"task": cfg.task.model_dump(), "strengths": profile.get("strengths", []),
                     "tradeoffs": profile.get("tradeoffs", [])}, location + "/feedback/" + parent_id)
                raw = {e["evidence_id"]: e for trace in profile.get("trace", []) for e in trace.get("evidence", [])}
                request_feedback["behavior_feedback"][parent_id] = {"summary": note,
                    "evidence": [raw[ref] for ref in note["evidence_refs"] if ref in raw]}
            parent_inputs = []
            for assessment in chosen:
                parent = assessment.individual
                lineage = self.store.read(parent.lineage_ref)
                diagnosis = assessment.diagnosis.model_dump(exclude={"failure_refs"}) if assessment.diagnosis else None
                failure_records, failure_uses = [], []
                for index, ref in enumerate(assessment.diagnosis.failure_refs if assessment.diagnosis else []):
                    record = self.store.load_execution(ref)
                    if record.individual_id != parent.individual_id or record.execution_key.code_hash != parent.code_hash:
                        raise ContractError("failure record does not belong to the chosen parent")
                    failure_uses.append(RunUse(individual_id=parent.individual_id, record=record, record_ref=ref))
                    note = self.context.analyze(ref, cfg.agents.generator,
                        {"task": cfg.task.model_dump(), "diagnosis": diagnosis},
                        "Summarize this parent's observed failure and normal behavior for a later repair. "
                        "Jointly compare all supplied conditions and intensities. Preserve alternative "
                        "explanations and terminal outcomes. The parent diagnosis covers multiple scenarios; "
                        "this input contains only the current scenario. Return evidence_refs copied exactly "
                        "from observations[].evidence_id, never file paths or other identifiers. If observations "
                        "are present, cite at least one. Do not propose code yet.",
                        f"{location}/failures/{parent.individual_id}/scenario_{index:04d}", TraceSummary)
                    failure_records.append({"scenario_id": record.scenario_id, **note.model_dump()})
                if failure_records:
                    note = self.context.summarize(cfg.agents.generator, failure_records,
                        {"task": cfg.task.model_dump()}, location + "/failures/" + parent.individual_id + "/combined")
                    failure_records = {"summary": note,
                        "evidence": self.context.snippets(failure_uses, note["evidence_refs"], radius=0)}
                parent_inputs.append({"individual": parent.model_dump(), "needs_repair": assessment.needs_repair,
                    "files": self.store.source(parent),
                    "diagnosis": diagnosis,
                    "failure_records": failure_records,
                    "generation_output": {"strategy_tag": parent.strategy_tag,
                        "rationale": lineage["recombination_plan"], "generation_errors": parent.generation_errors}})
            files, errors, rationale, tag = {}, [], "", strategy
            try:
                proposal = self.context.ask(cfg.agents.generator,
                    "Generate Python source implementing solve(observation, seed) using the assigned operator. "
                    "Use only previous-generation evaluation feedback. REPAIR fixes the assigned failed parent. "
                    "CROSSOVER and MUTATION act only on parents assessed as not needing repair. "
                    "Return complete files. This candidate will be tested and evaluated without regeneration.",
                    {"task": cfg.task.model_dump(), "template": self.template, "operator": operator,
                     "trace_format": "Behavior summaries are interpretations; evidence contains original raw excerpts. "
                         "Full per-step records remain on disk. Do not assume uninspected intervals are safe.",
                     "strategy": strategy, "parents": parent_inputs, "feedback": request_feedback,
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
            code_hash = digest(files)
            individual_id = "alg_" + digest({"prefix": prefix, "slot": slot, "code_hash": code_hash})
            base = "individuals/" + individual_id
            if not errors:
                output = {key: value for key, value in output.items() if key != "files"}
                output["source_ref"] = base + "/source_manifest.json"
            output_ref = self.store.save(location + "/output.json", output)
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
