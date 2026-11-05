from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from typing import Any

from evolution.config import ProblemConfig
from evolution.errors import ContractError, InfrastructureError
from evolution.framework import ProblemSpec
from evolution.llm import LLM
from evolution.models import (AnalystOutput, ArbiterOutput, Assessment, Diagnosis, Individual,
    PairJudgment, PreferenceOutput, ReviewOutput, RunUse, ScenarioSuite, SemanticRelations, Status, digest)
from evolution.storage import Store


class Evaluator:
    def __init__(self, cfg: ProblemConfig, spec: ProblemSpec, llm: LLM, store: Store):
        self.cfg, self.spec, self.llm, self.store = cfg, spec, llm, store

    def evaluate(self, pool: list[Individual], execution: dict[str, list[RunUse]],
                 suite: ScenarioSuite, prefix: str) -> tuple[list[Assessment], list[PairJudgment], list[dict]]:
        assessments, profiles, confirmed = [], {}, []
        conditions = {(s.content_hash, s.seed) for s in suite.scenarios}
        baseline_conditions = None
        aliases = {i.individual_id: "candidate_" + digest({"id": i.individual_id,
            "generation": suite.generation, "seed": self.cfg.experiment.seed})[:12] for i in pool}
        self.store.save(prefix + "/evaluation/aliases.json", aliases)
        for individual in pool:
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
                blocked = any(u.record.result.status == Status.EXECUTION_BLOCKED for u in uses)
                status = Status.EXECUTION_BLOCKED if blocked else Status.INVALID
                reasons = [u.record.result.failure_category or u.record.result.status.value for u in failed]
                if not failed:
                    reasons = [c.explanation for c in correctness if not c.passed]
                    if not enough_behavior:
                        reasons.append("missing valid behavior observations")
                diagnosis = Diagnosis(individual_id=individual.individual_id, status=status,
                    category="; ".join(reasons), failure_refs=[u.record_ref for u in uses],
                    next_generation_feedback="Use these observations only during the next generation; do not repair or rerun this candidate now.")
                self.store.save(base + "/failure_diagnosis.json", diagnosis)
                assessments.append(Assessment(individual=individual, status=status, correctness_pass=False,
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

    def analyze(self, alias: str, uses: list[RunUse], suite: ScenarioSuite, base: str) -> tuple[dict, list[dict]]:
        m = self.cfg.evaluation.anomaly_analyst_count
        trace = [{"scenario_id": scenario.scenario_id,
                  "decisions": used.record.result.decisions, "trajectories": used.record.result.trajectories,
                  "context": used.record.result.context,
                  "evidence": [e.model_dump() for e in used.record.evidence]}
                 for scenario, used in zip(suite.scenarios, uses)]
        shared = {"task": self.cfg.task.model_dump(), "rubric": self.cfg.rubric.model_dump(),
            "suite": suite.model_dump(), "individual_alias": alias, "trace": trace,
            "deterministic_correctness_pass": True}
        focuses = ["temporal stability", "interaction and safety", "task behavior and tradeoffs"]
        names = self.cfg.agents.analysts

        def first_pass(index: int):
            return self.llm.ask(names[index % len(names)],
                "Independently identify evidence-grounded behavioral anomaly hypotheses. "
                "No peer opinions exist in this conversation. Share all necessary facts and constraints. "
                "An empty claims list is allowed. Cite actual evidence IDs; consider alternative explanations.",
                {**shared, "focus": focuses[index % len(focuses)]}, AnalystOutput,
                f"{base}/analyst_originals/session_{index:02d}")

        # Separate stateless calls; all first passes finish and are sealed before reviews.
        with ThreadPoolExecutor(max_workers=m) as worker:
            originals = list(worker.map(first_pass, range(m)))
        claims, owners = [], {}
        for index, original in enumerate(originals):
            self.store.save(f"{base}/analyst_originals/session_{index:02d}.json", original)
            if len({c.claim_id for c in original.claims}) != len(original.claims):
                raise ContractError("duplicate analyst claim ID")
            for claim in original.claims:
                item = claim.model_dump()
                # Namespace IDs for audit; peers receive neither author identity nor vote counts.
                item["claim_id"] = digest({"session": index, "claim": item})[:24]
                owners[item["claim_id"]] = index
                claims.append(item)
        self.store.save(base + "/analyst_originals/seal.json", {"hashes": [digest(o) for o in originals]})
        claim_order = sorted(claims, key=lambda c: digest(c))
        shared_evidence = {}
        normalized = {}
        for claim in claims:
            normalized.setdefault(" ".join(claim["claim"].lower().split()), []).append(claim["claim_id"])
            for ref in claim["evidence_refs"]:
                shared_evidence.setdefault(ref, []).append(claim["claim_id"])
        semantic = self.llm.ask(self.cfg.agents.independence,
            "Analyze claim dependence, shared premises, causal chains and contradictions. "
            "Repeated opinions are not independent evidence. Never use voting as proof.",
            {**shared, "anonymous_claims": claim_order}, SemanticRelations, base + "/independence")
        independence = {"duplicates": [v for v in normalized.values() if len(v) > 1],
            "shared_evidence": {k: v for k, v in shared_evidence.items() if len(v) > 1},
            "semantic": semantic.model_dump(), "statistical_independence_claimed": False}
        self.store.save(base + "/independence_report.json", independence)
        reviews = []
        all_evidence = [e.model_dump() for u in uses for e in u.record.evidence]
        for index in range(m):
            others = [c for c in claim_order if owners[c["claim_id"]] != index]
            output = self.llm.ask(names[index % len(names)],
                "Cross-review sealed anonymous peer claims. Seek counterexamples and missing conditions. "
                "Complementary evidence is highlighted; all hard constraints and necessary context are available. "
                "Do not invent observations. Original first passes are immutable.",
                {**shared, "anonymous_claims": others,
                 "highlighted_evidence": all_evidence[index::m]}, ReviewOutput,
                f"{base}/cross_reviews/session_{index:02d}")
            reviews.append(output.model_dump())
            self.store.save(f"{base}/cross_reviews/session_{index:02d}.json", output)
        # Re-read immutable raw objects and verify provenance before building packets.
        evidence_index = {}
        for scenario, used in zip(suite.scenarios, uses):
            raw = self.store.verified_object(used.record_ref)
            if digest(raw) != digest(used.record):
                raise ContractError("raw execution record changed during evidence retrieval")
            for evidence in used.record.evidence:
                evidence_index[evidence.evidence_id] = {"evidence": evidence.model_dump(),
                    "run_id": used.record.run_id, "individual_alias": alias,
                    "scenario_id": scenario.scenario_id, "record_ref": used.record_ref}
        packets = []
        for claim in claim_order:
            valid = [ref for ref in claim["evidence_refs"] if ref in evidence_index
                     and evidence_index[ref]["scenario_id"] == claim["scenario_id"]]
            packets.append({"claim_id": claim["claim_id"], "requested_refs": claim["evidence_refs"],
                "provenance_status": "VERIFIED" if valid and len(valid) == len(claim["evidence_refs"]) else "UNVERIFIED",
                "records": [evidence_index[ref] for ref in valid],
                "checker_results": "Existence and provenance verified; semantic support remains for arbiter review."})
        self.store.save(base + "/evidence_packets.json", packets)
        # Internal provenance (run IDs, record paths) stays out of anonymous judging prompts.
        anonymous_packets = [{**p, "records": [{"evidence": r["evidence"], "scenario_id": r["scenario_id"]}
                            for r in p["records"]]} for p in packets]
        verdicts = self.llm.ask(self.cfg.agents.arbiter,
            "You are the single anomaly arbiter. Decide every claim exactly once using verified evidence, "
            "counterarguments and frozen rubric. UNVERIFIED evidence cannot confirm a claim. "
            "Do not rank algorithms, select populations, change code or stop evolution.",
            {**shared, "claims": claim_order, "independence": independence,
             "cross_reviews": reviews, "evidence_packets": anonymous_packets}, ArbiterOutput,
            base + "/arbiter")
        expected = {c["claim_id"] for c in claims}
        if len(verdicts.verdicts) != len(expected) or {v.claim_id for v in verdicts.verdicts} != expected:
            raise ContractError("arbiter must return exactly one verdict per proposed claim")
        packet_index = {p["claim_id"]: p for p in packets}
        claim_index = {c["claim_id"]: c for c in claims}
        checked, symptoms = [], []
        for verdict in verdicts.verdicts:
            data = verdict.model_dump()
            scenario_id = claim_index[verdict.claim_id]["scenario_id"]
            refs_ok = bool(verdict.evidence_refs) and all(r in evidence_index and
                evidence_index[r]["scenario_id"] == scenario_id for r in verdict.evidence_refs)
            if verdict.status == "CONFIRMED" and (packet_index[verdict.claim_id]["provenance_status"] != "VERIFIED" or not refs_ok):
                data["status"] = "UNCONFIRMED"
                data["explanation"] = "Evidence verification failed. Arbiter response retained in raw audit."
            checked.append(data)
            if data["status"] == "CONFIRMED":
                symptoms.append({"scenario_id": scenario_id, "symptom": claim_index[verdict.claim_id]["claim"]})
        self.store.save(base + "/anomaly_verdicts.json", checked)
        profile = {"individual_alias": alias, "execution_status": "EXECUTABLE", "correctness_pass": True,
            "anomalies": checked, "strengths": verdicts.strengths, "tradeoffs": verdicts.tradeoffs,
            "trace": trace, "evidence_refs": sorted(evidence_index)}
        return profile, symptoms

    def compare(self, profiles: dict[str, dict], suite: ScenarioSuite, prefix: str) -> list[PairJudgment]:
        comparisons = []
        maximum_candidates = 2 * self.cfg.experiment.population_size
        if len(profiles) > maximum_candidates:
            raise ContractError("comparison pool must not exceed 2P candidates")
        # All eligible pairs are compared; retain a deterministic artifact order.
        pairs = sorted(combinations(profiles, 2), key=lambda p: digest({"pair": sorted(p),
            "seed": self.cfg.experiment.seed, "generation": suite.generation}))
        inverse = {"A_BETTER": "B_BETTER", "B_BETTER": "A_BETTER", "TIE": "TIE", "INCOMPARABLE": "INCOMPARABLE"}
        for index, (left, right) in enumerate(pairs):
            outputs, refs = [], []
            valid_evidence = set(profiles[left]["evidence_refs"]) | set(profiles[right]["evidence_refs"])
            for order, (a, b) in enumerate(((left, right), (right, left))):
                location = f"{prefix}/evaluation/preferences/pair_{index:04d}/order_{order}"
                result = self.llm.ask(self.cfg.agents.preference,
                    "Compare anonymous A and B using the fixed rubric, same scenarios and real traces. "
                    "Consider strengths, correctness, confirmed anomalies and tradeoffs, not anomaly counts. "
                    "Cite evidence for a strict preference. Do not assume a total order.",
                    {"task": self.cfg.task.model_dump(), "rubric": self.cfg.rubric.model_dump(),
                     "suite": suite.model_dump(), "A": profiles[a], "B": profiles[b]},
                    PreferenceOutput, location)
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
            "comparison_calls": 2 * len(comparisons), "all_pairs_covered": True})
        return comparisons
