from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from typing import Dict, List, Tuple
from tqdm import tqdm

from evolution.config import ProblemConfig
from evolution.context import TraceContext, evidence_refs
from evolution.errors import ContractError, InfrastructureError
from evolution.framework import ProblemSpec
from evolution.llm import LLM
from evolution.models import (ArbiterOutput, Assessment, Diagnosis, Individual,
    PairJudgment, PreferenceOutput, ReviewOutput, RunUse, ScenarioSuite, SemanticRelations, Status, digest)
from evolution.storage import Store


class Evaluator:
    def __init__(self, cfg: ProblemConfig, spec: ProblemSpec, llm: LLM, store: Store):
        self.cfg, self.spec, self.llm, self.store = cfg, spec, llm, store
        self.context = TraceContext(cfg, llm, store)

    def evaluate(self, pool: List[Individual], execution: Dict[str, List[RunUse]],
                 suite: ScenarioSuite, prefix: str) -> Tuple[List[Assessment], List[PairJudgment], List[dict]]:
        assessments, profiles, confirmed = [], {}, []
        conditions = {(s.content_hash, s.seed) for s in suite.scenarios}
        baseline_conditions = None
        aliases = {i.individual_id: "candidate_" + digest({"id": i.individual_id,
            "generation": suite.generation, "seed": self.cfg.experiment.seed})[:12] for i in pool}
        self.store.save(prefix + "/evaluation/aliases.json", aliases)
        for individual in tqdm(pool, desc=f"gen_{suite.generation:04d} 评估", unit="算法"):
            uses = execution[individual.individual_id]
            actual = {(u.record.execution_key.scenario_content_hash, u.record.execution_key.seed) for u in uses}
            if actual != conditions or len(uses) != len(suite.scenarios):
                raise ContractError("comparison prohibited: incomplete or unequal scenario coverage")
            comparable_keys = sorted([{k: v for k, v in u.record.execution_key.model_dump().items()
                                      if k != "code_hash"} for u in uses], key=digest)
            if baseline_conditions is None:
                baseline_conditions = comparable_keys
            if comparable_keys != baseline_conditions:
                raise ContractError("comparison prohibited: unequal environment/configuration fingerprints")
            for scenario, used in zip(suite.scenarios, uses):
                if (used.individual_id != individual.individual_id
                        or used.record.individual_id != individual.individual_id
                        or used.record.execution_key.code_hash != individual.code_hash
                        or used.record.execution_key.scenario_content_hash != scenario.content_hash):
                    raise ContractError("execution record identity/scenario order mismatch")
            if any(u.record.result.status == Status.INFRA_ERROR for u in uses):
                raise InfrastructureError("cannot evaluate infrastructure errors as algorithm failures")
            base = prefix + "/evaluation/" + individual.individual_id
            failed = [u for u in uses if u.record.result.status != Status.EXECUTABLE]
            correctness = [self.spec.check_correctness(scenario, used.record.result)
                for scenario, used in zip(suite.scenarios, uses)] if not failed else []
            self.store.save(base + "/correctness.json", [c.model_dump() for c in correctness])
            enough_behavior = all(u.record.evidence for u in uses)
            if failed or not all(c.passed for c in correctness) or not enough_behavior:
                reasons = [u.record.result.failure_category or u.record.result.status.value for u in failed]
                if not failed:
                    reasons = [c.explanation for c in correctness if not c.passed]
                    if not enough_behavior:
                        reasons.append("missing valid behavior observations")
                diagnosis = Diagnosis(individual_id=individual.individual_id, status=Status.INVALID,
                    category="; ".join(reasons), failure_refs=[u.record_ref for u in uses],
                    next_generation_feedback="Use these observations only during the next generation; do not repair or rerun this candidate now.")
                self.store.save(base + "/failure_diagnosis.json", diagnosis)
                assessments.append(Assessment(individual=individual, status=Status.INVALID, correctness_pass=False,
                    comparable=False, diagnosis=diagnosis))
                continue
            alias = aliases[individual.individual_id]
            profile, symptoms = self.analyze(alias, uses, suite, base)
            profile_ref = self.store.save(base + "/behavior_profile.json", profile)
            profiles[individual.individual_id] = profile
            confirmed.extend(symptoms)
            assessments.append(Assessment(individual=individual, status=Status.EXECUTABLE,
                correctness_pass=True, comparable=True, profile_ref=profile_ref))
        self.store.save(prefix + "/evaluation/failure_diagnoses.json", [
            a.diagnosis.model_dump(mode="json") for a in assessments if a.diagnosis])
        comparisons = self.compare(profiles, suite, prefix)
        self.store.save(prefix + "/evaluation/assessments.json", [a.model_dump(mode="json") for a in assessments])
        # Problem-specific sanitizer sees only confirmed scenario symptoms, never IDs/code.
        sanitized = self.spec.sanitize_feedback(confirmed)
        sanitized = [{"scenario_id": s["scenario_id"], "symptom": s["symptom"]} for s in sanitized]
        self.store.save(prefix + "/evaluation/sanitized_feedback.json", sanitized)
        return assessments, comparisons, sanitized

    def analyze(self, alias: str, uses: List[RunUse], suite: ScenarioSuite, base: str) -> Tuple[dict, List[dict]]:
        m = self.cfg.evaluation.anomaly_analyst_count
        context = self.context
        shared = {"task": self.cfg.task.model_dump(), "rubric": self.cfg.rubric.model_dump(),
            "suite": suite.model_dump(exclude={"scenarios"}), "individual_alias": alias,
            "deterministic_correctness_pass": True}
        focuses = ["temporal stability", "interaction and safety", "task behavior and tradeoffs"]
        names = self.cfg.agents.analysts

        def first_pass(index: int):
            role = ("Independently analyze this scenario and identify evidence-grounded anomaly hypotheses. "
                    "Jointly compare ALL matched episodes and intensity settings in the supplied complete "
                    "scenario observations. Compare condition-dependent behavior and "
                    "terminal outcomes; elapsed time alone does not determine task quality. "
                    "Return a concise summary of normal behavior, strengths, possible problems, tradeoffs and "
                    "alternative explanations, with supporting evidence_refs. Cite only supplied evidence IDs. "
                    "No peer opinions are supplied. Set hypothesis_only=true. An empty claims list is allowed.")
            return [context.analyze(used.record_ref, names[index % len(names)],
                {"task": shared["task"], "rubric": shared["rubric"], "scenario": scenario.model_dump(),
                 "focus": focuses[index % len(focuses)]}, role,
                f"{base}/analyst_originals/session_{index:02d}/scenario_{scenario_index:04d}")
                for scenario_index, (scenario, used) in enumerate(zip(suite.scenarios, uses))]

        # Separate stateless calls; all first passes finish and are sealed before reviews.
        with ThreadPoolExecutor(max_workers=m) as worker:
            batches = list(worker.map(first_pass, range(m)))
        originals = [{"claims": [claim.model_copy(update={"claim_id": f"{i}:{claim.claim_id}"}).model_dump()
            for i, result in enumerate(batch) for claim in result.claims],
            "summaries": [{"scenario_id": s.scenario_id, "summary": result.summary,
                           "evidence_refs": result.evidence_refs} for s, result in zip(suite.scenarios, batch)]}
            for batch in batches]
        claims, owners = [], {}
        for index, original in enumerate(originals):
            self.store.save(f"{base}/analyst_originals/session_{index:02d}.json", original)
            for claim in original["claims"]:
                item = dict(claim)
                # Namespace IDs for audit; peers receive neither author identity nor vote counts.
                item["claim_id"] = digest({"session": index, "claim": item})[:24]
                owners[item["claim_id"]] = index
                claims.append(item)
        self.store.save(base + "/analyst_originals/seal.json", {"hashes": [digest(o) for o in originals]})
        summaries = [item for original in originals for item in original["summaries"]]
        overview = context.summarize(self.cfg.agents.arbiter, summaries,
            {"task": shared["task"], "rubric": shared["rubric"]}, base + "/overview")
        shared["behavior_summary"] = overview
        def scoped(group):
            ids = {claim["scenario_id"] for claim in group}
            return {**shared, "scenarios": [s.model_dump() for s in suite.scenarios if s.scenario_id in ids]}

        claim_order = sorted(claims, key=lambda c: digest(c))
        shared_evidence = {}
        normalized = {}
        for claim in claims:
            normalized.setdefault(" ".join(claim["claim"].lower().split()), []).append(claim["claim_id"])
            for ref in claim["evidence_refs"]:
                shared_evidence.setdefault(ref, []).append(claim["claim_id"])
        role = ("Analyze claim dependence, shared premises, causal chains and contradictions. "
                "Repeated opinions are not independent evidence. Never use voting as proof.")
        build = lambda group: {**scoped(group), "anonymous_claims": group}
        semantic_parts = [context.ask(self.cfg.agents.independence, role, build(group), SemanticRelations,
            f"{base}/independence/batch_{index:04d}") for index, group in enumerate(
                context.groups(claim_order, build, self.cfg.agents.independence, role, SemanticRelations))]
        semantic = SemanticRelations(**{field: [item for part in semantic_parts for item in getattr(part, field)]
                                       for field in SemanticRelations.model_fields})
        independence = {"duplicates": [v for v in normalized.values() if len(v) > 1],
            "shared_evidence": {k: v for k, v in shared_evidence.items() if len(v) > 1},
            "semantic": semantic.model_dump(), "statistical_independence_claimed": False,
            "semantic_batch_count": len(semantic_parts)}
        self.store.save(base + "/independence_report.json", independence)
        reviews = []
        for index in range(m):
            others = [c for c in claim_order if owners[c["claim_id"]] != index]
            role = ("Cross-review sealed anonymous peer claims using summaries and raw evidence excerpts. "
                    "Seek counterexamples, alternative explanations and missing conditions. "
                    "Acknowledge uninspected intervals; cite only supplied raw evidence IDs.")
            profile = names[index % len(names)]
            build = lambda group: context.with_evidence({**scoped(group), "anonymous_claims": group}, uses,
                evidence_refs(group) if group else overview["evidence_refs"], profile, role, ReviewOutput)
            parts = []
            for batch_index, group in enumerate(context.groups(others, build, profile, role, ReviewOutput)):
                payload = build(group)
                part = context.ask(profile, role, payload, ReviewOutput,
                    f"{base}/cross_reviews/session_{index:02d}/batch_{batch_index:04d}")
                if not set(part.counter_evidence_refs) <= {e["evidence_id"] for e in payload["evidence"]}:
                    raise ContractError("cross-review cites evidence outside its input")
                reviews.append({"claim_ids": [c["claim_id"] for c in group], **part.model_dump()})
                parts.append(part)
            output = ReviewOutput(observations=[o for part in parts for o in part.observations],
                counter_evidence_refs=list(dict.fromkeys(ref for part in parts for ref in part.counter_evidence_refs)))
            self.store.save(f"{base}/cross_reviews/session_{index:02d}.json", output)
        # Re-read immutable raw objects and verify provenance before building packets.
        evidence_index = {}
        for scenario, used in zip(suite.scenarios, uses):
            raw = self.store.load_execution(used.record_ref)
            if digest(raw) != digest(used.record):
                raise ContractError("raw execution record changed during evidence retrieval")
            for evidence in used.record.evidence:
                evidence_index[evidence.evidence_id] = {"run_id": used.record.run_id, "individual_alias": alias,
                    "scenario_id": scenario.scenario_id, "record_ref": used.record_ref}
        packets = []
        for claim in claim_order:
            valid = [ref for ref in claim["evidence_refs"] if ref in evidence_index
                     and evidence_index[ref]["scenario_id"] == claim["scenario_id"]]
            packets.append({"claim_id": claim["claim_id"], "requested_refs": claim["evidence_refs"],
                "provenance_status": "VERIFIED" if valid and len(valid) == len(claim["evidence_refs"]) else "UNVERIFIED",
                "records": [{"evidence_id": ref, **evidence_index[ref]} for ref in valid],
                "checker_results": "Existence and provenance verified; semantic support remains for arbiter review."})
        self.store.save(base + "/evidence_packets.json", packets)
        # Internal provenance (run IDs, record paths) stays out of anonymous judging prompts.
        anonymous_packets = [{**{key: value for key, value in p.items() if key != "records"},
                              "evidence_refs": [record["evidence_id"] for record in p["records"]]}
                             for p in packets]
        role = ("You are the single anomaly arbiter. Decide every supplied claim exactly once using verified "
                "raw excerpts, counterarguments and the frozen rubric. Summaries are interpretations. "
                "UNVERIFIED evidence cannot confirm a claim. Acknowledge uninspected intervals. "
                "Do not rank algorithms, select populations, change code or stop evolution.")
        def build(group):
            ids = {c["claim_id"] for c in group}
            selected_reviews = [r for r in reviews if not r["claim_ids"] or ids.intersection(r["claim_ids"])]
            return context.with_evidence({**scoped(group), "claims": group, "independence": independence,
                "cross_reviews": selected_reviews,
                "evidence_packets": [p for p in anonymous_packets if p["claim_id"] in ids]}, uses,
                evidence_refs(group) | {ref for r in selected_reviews for ref in r["counter_evidence_refs"]}
                | (set(overview["evidence_refs"]) if not group else set()),
                self.cfg.agents.arbiter, role, ArbiterOutput)
        verdict_parts = []
        for index, group in enumerate(context.groups(claim_order, build, self.cfg.agents.arbiter, role, ArbiterOutput)):
            payload = build(group)
            part = context.ask(self.cfg.agents.arbiter, role, payload, ArbiterOutput,
                               f"{base}/arbiter/batch_{index:04d}")
            if (len(part.verdicts) != len(group)
                    or {v.claim_id for v in part.verdicts} != {c["claim_id"] for c in group}):
                raise ContractError("arbiter must decide exactly the claims in its input batch")
            supplied = {e["evidence_id"] for e in payload["evidence"]}
            verdict_parts.append(part.model_copy(update={"verdicts": [v.model_copy(update={"status": "UNCONFIRMED",
                "explanation": "Cited evidence was not supplied as a raw excerpt in this batch."})
                if v.status == "CONFIRMED" and not set(v.evidence_refs) <= supplied else v for v in part.verdicts]}))
        verdicts = ArbiterOutput(verdicts=[v for part in verdict_parts for v in part.verdicts],
            strengths=list(dict.fromkeys(s for part in verdict_parts for s in part.strengths)),
            tradeoffs=list(dict.fromkeys(t for part in verdict_parts for t in part.tradeoffs)))
        self.store.save(base + "/arbiter/verdicts.json", verdicts)
        expected = {c["claim_id"] for c in claims}
        if len(verdicts.verdicts) != len(expected) or {v.claim_id for v in verdicts.verdicts} != expected:
            raise ContractError("arbiter must return exactly one verdict per proposed claim")
        packet_index = {p["claim_id"]: p for p in packets}
        claim_index = {c["claim_id"]: c for c in claims}
        checked, symptoms = [], []
        for verdict in verdicts.verdicts:
            data = verdict.model_dump()
            scenario_id = claim_index[verdict.claim_id]["scenario_id"]
            data["scenario_id"] = scenario_id
            refs_ok = bool(verdict.evidence_refs) and all(r in evidence_index and
                evidence_index[r]["scenario_id"] == scenario_id for r in verdict.evidence_refs)
            if verdict.status == "CONFIRMED" and (packet_index[verdict.claim_id]["provenance_status"] != "VERIFIED" or not refs_ok):
                data["status"] = "UNCONFIRMED"
                data["explanation"] = "Evidence verification failed. Arbiter response retained in raw audit."
            checked.append(data)
            if data["status"] == "CONFIRMED":
                symptoms.append({"scenario_id": scenario_id, "symptom": claim_index[verdict.claim_id]["claim"]})
        self.store.save(base + "/anomaly_verdicts.json", checked)
        trace = []
        for index, (scenario, used) in enumerate(zip(suite.scenarios, uses)):
            summary = context.summarize(self.cfg.agents.arbiter,
                [original["summaries"][index] for original in originals]
                + [{"reviewed_finding": item} for item in checked if item["scenario_id"] == scenario.scenario_id],
                {"task": shared["task"], "rubric": shared["rubric"], "scenario": scenario.model_dump(),
                 "result_context": used.record.result.context}, f"{base}/reviewed_summaries/{index:04d}")
            trace.append({"scenario_id": scenario.scenario_id, "summary": summary,
                "context": used.record.result.context,
                "evidence": context.snippets([used], set(summary["evidence_refs"]) |
                    evidence_refs([item for item in checked if item["scenario_id"] == scenario.scenario_id]), radius=0)})
        profile = {"individual_alias": alias, "execution_status": "EXECUTABLE", "correctness_pass": True,
            "anomalies": checked, "strengths": verdicts.strengths, "tradeoffs": verdicts.tradeoffs,
            "trace": trace, "evidence_refs": sorted(evidence_index)}
        return profile, symptoms

    def compare(self, profiles: Dict[str, dict], suite: ScenarioSuite, prefix: str) -> List[PairJudgment]:
        comparisons, calls = [], 0
        maximum_candidates = 2 * self.cfg.experiment.population_size
        if len(profiles) > maximum_candidates:
            raise ContractError("comparison pool must not exceed 2P candidates")
        # All eligible pairs are compared; retain a deterministic artifact order.
        pairs = sorted(combinations(profiles, 2), key=lambda p: digest({"pair": sorted(p),
            "seed": self.cfg.experiment.seed, "generation": suite.generation}))
        inverse = {"A_BETTER": "B_BETTER", "B_BETTER": "A_BETTER", "TIE": "TIE", "INCOMPARABLE": "INCOMPARABLE"}
        for index, (left, right) in enumerate(tqdm(pairs, desc=f"gen_{suite.generation:04d} 比较", unit="对")):
            outputs, refs = [], []
            valid_evidence = set(profiles[left]["evidence_refs"]) | set(profiles[right]["evidence_refs"])
            for order, (a, b) in enumerate(((left, right), (right, left))):
                location = f"{prefix}/evaluation/preferences/pair_{index:04d}/order_{order}"
                role = ("Compare anonymous A and B using the fixed rubric and matched scenarios. "
                    "Use reviewed behavior summaries and raw evidence excerpts, including strengths, "
                    "confirmed problems, tradeoffs and opposing evidence. Jointly compare intensity settings. "
                    "Partial judgments cover different conditions: weigh their substance, never count wins "
                    "or anomalies as a score. Do not assume a total order. Cite supplied raw evidence for "
                    "a strict preference. Keep the explanation concise and cite at most four evidence IDs.")
                traces = [{t["scenario_id"]: {**t, "evidence": [e for e in t["evidence"]
                    if e["evidence_id"] in t["summary"]["evidence_refs"]]}
                    for t in profiles[key]["trace"]} for key in (a, b)]
                items = [{"scenario": scenario.model_dump(), "A": traces[0][scenario.scenario_id],
                          "B": traces[1][scenario.scenario_id]} for scenario in suite.scenarios]
                raw = {e["evidence_id"]: e for trace in traces for item in trace.values() for e in item["evidence"]}
                common = {"task": self.cfg.task.model_dump(), "rubric": self.cfg.rubric.model_dump(),
                    "A": {key: profiles[a][key] for key in ("correctness_pass", "strengths", "tradeoffs")},
                    "B": {key: profiles[b][key] for key in ("correctness_pass", "strengths", "tradeoffs")}}
                def build(group):
                    inline = {e["evidence_id"] for part in group for name in ("A", "B")
                              for e in part.get(name, {}).get("evidence", [])}
                    return {**common, "matched_observations_or_partial_judgments": group,
                            "supporting_evidence": [raw[ref] for ref in sorted(evidence_refs(group) - inline) if ref in raw]}
                for level in range(12):
                    groups = self.context.groups(items, build, self.cfg.agents.preference, role, PreferenceOutput)
                    parts = []
                    for batch_index, group in enumerate(groups):
                        payload = build(group)
                        result = self.context.ask(self.cfg.agents.preference, role, payload, PreferenceOutput,
                            f"{location}/level_{level:02d}/batch_{batch_index:04d}")
                        calls += 1
                        supplied = {e["evidence_id"] for e in payload["supporting_evidence"]}
                        supplied.update(e["evidence_id"] for item in group for name in ("A", "B")
                                        for e in item.get(name, {}).get("evidence", []))
                        if (not set(result.evidence_refs) <= supplied or
                                result.winner in ("A_BETTER", "B_BETTER") and not result.evidence_refs):
                            result = result.model_copy(update={"winner": "INCOMPARABLE", "evidence_refs": []})
                        parts.append({"scenario_ids": [sid for item in group for sid in
                            (item["scenario_ids"] if "scenario_ids" in item else [item["scenario"]["scenario_id"]])],
                            **result.model_dump()})
                    if len(parts) == 1:
                        break
                    if level and len(parts) >= len(items):
                        raise InfrastructureError("partial comparisons cannot fit within max_input_bytes")
                    items = parts
                else:
                    raise InfrastructureError("comparison reduction exceeded its depth limit")
                ref = self.store.save(location + "/judgment.json", result)
                refs.append(ref)
                valid = all(e in valid_evidence for e in result.evidence_refs)
                if result.winner in ("A_BETTER", "B_BETTER") and not result.evidence_refs:
                    valid = False
                outputs.append(result if valid else result.model_copy(update={"winner": "INCOMPARABLE"}))
            consistent = outputs[0].winner == inverse[outputs[1].winner]
            comparisons.append(PairJudgment(individual_a=left, individual_b=right,
                winner=outputs[0].winner if consistent else "INCOMPARABLE", order_consistent=consistent,
                serious_regression=any(o.serious_regression for o in outputs), reasoning_refs=refs))
        self.store.save(prefix + "/evaluation/preferences.json", [c.model_dump() for c in comparisons])
        self.store.save(prefix + "/evaluation/comparison_summary.json", {
            "population_size": self.cfg.experiment.population_size,
            "maximum_candidates": maximum_candidates,
            "comparison_budget_per_generation": self.cfg.comparison_budget_per_generation,
            "eligible_candidates": len(profiles), "compared_pairs": len(comparisons),
            "final_order_judgments": 2 * len(comparisons),
            "comparison_calls": calls, "all_pairs_covered": True})
        return comparisons
