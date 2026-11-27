"""Bounded views of complete execution records for model requests."""

from evolution.errors import ContractError, InfrastructureError
from evolution.llm import build_request
from evolution.models import AnalystOutput, TraceSummary, canonical


def evidence_refs(value):
    if isinstance(value, dict):
        refs = set(value.get("evidence_refs", []))
        if "evidence_id" in value:
            refs.add(value["evidence_id"])
        for item in value.values():
            refs.update(evidence_refs(item))
        return refs
    if isinstance(value, list):
        return set().union(*(evidence_refs(item) for item in value)) if value else set()
    return set()


class TraceContext:
    def __init__(self, cfg, llm, store):
        self.cfg, self.llm, self.store = cfg, llm, store

    def fits(self, profile, role, payload, schema):
        settings = self.cfg.models[profile]
        return len(canonical(build_request(settings, role, payload, schema)).encode("utf-8")) <= settings.max_input_bytes

    def ask(self, profile, role, payload, schema, prefix, validate=None):
        if not self.fits(profile, role, payload, schema):
            size = len(canonical(build_request(self.cfg.models[profile], role, payload, schema)).encode("utf-8"))
            raise InfrastructureError(f"model input budget exceeded: {size} > "
                                      f"{self.cfg.models[profile].max_input_bytes} bytes at {prefix}")
        if validate is None:
            return self.llm.ask(profile, role, payload, schema, prefix)
        return self.llm.ask(profile, role, payload, schema, prefix, validate=validate)

    def groups(self, items, build, profile, role, schema):
        groups, current = [], []
        for item in items:
            if not self.fits(profile, role, build(current + [item]), schema):
                if not current:
                    raise InfrastructureError("one input item exceeds the model input budget; "
                                              "increase max_input_bytes or reduce fixed request context")
                groups.append(current)
                current = []
                if not self.fits(profile, role, build(current + [item]), schema):
                    raise InfrastructureError("one input item exceeds the model input budget")
            current.append(item)
        if current or not groups:
            groups.append(current)
        return groups

    def summarize(self, profile, items, common, prefix):
        role = ("Summarize all supplied observations concisely, retaining normal behavior, strengths, "
                "possible problems, alternative explanations and limitations. Preserve scenario and episode "
                "identities, condition/intensity differences, terminal outcomes and material opposing evidence. "
                "A shorter time to a different terminal outcome does not by itself imply better task performance. "
                "Keep confirmed findings distinct from hypotheses. Do not invent metrics or observations. "
                "Cite only supplied evidence IDs. Later summaries must preserve material differences across "
                "all supplied parts, not count opinions or select only one part. Output at most 1000 characters.")
        build = lambda parts: {**common, "parts": parts}
        for level in range(12):
            groups = self.groups(items, build, profile, role, TraceSummary)
            notes = []
            for index, group in enumerate(groups):
                available = evidence_refs(group)
                def validate(note):
                    invalid = set(note.evidence_refs) - available
                    if invalid or available and not note.evidence_refs:
                        raise ContractError("summary must cite available evidence: "
                            f"invalid_refs={sorted(invalid)}, at_least_one_required={bool(available)}; "
                            "copy evidence IDs exactly from the supplied parts")
                note = self.ask(profile, role, build(group), TraceSummary,
                                f"{prefix}/level_{level:02d}/part_{index:04d}", validate=validate)
                self.store.save(f"{prefix}/level_{level:02d}/part_{index:04d}.json", note)
                notes.append(note.model_dump())
            if len(notes) == 1:
                return notes[0]
            if level and len(notes) >= len(items):
                raise InfrastructureError("summaries cannot fit together within max_input_bytes")
            items = notes
        raise InfrastructureError("summary reduction exceeded its depth limit")

    def analyze(self, ref, profile, common, role, prefix, schema=AnalystOutput):
        record = self.store.load_execution(ref)
        aliases = {f"E{index}": e.evidence_id for index, e in enumerate(record.evidence)}
        common = {**common, "scenario_id": record.scenario_id, "seed": record.execution_key.seed,
                  "execution_status": record.result.status.value, "failure_category": record.result.failure_category,
                  "result_context": record.result.context}
        payload = {**common, "observations": [{"evidence_id": alias,
            "observation": {k: v for k, v in e.observation.items() if k != "context"}}
            for alias, e in zip(aliases, record.evidence)],
            "stdout": record.result.stdout, "stderr": record.result.stderr}
        def validate(result):
            refs = set(result.evidence_refs)
            if isinstance(result, AnalystOutput):
                if len({c.claim_id for c in result.claims}) != len(result.claims):
                    raise ContractError("duplicate analyst claim ID within a scenario; assign unique claim_id values")
                for claim in result.claims:
                    if claim.scenario_id != record.scenario_id or not claim.hypothesis_only:
                        raise ContractError(f"claim {claim.claim_id} must have scenario_id={record.scenario_id!r} "
                                            "and hypothesis_only=true")
                    refs.update(claim.evidence_refs)
            invalid = refs - set(aliases)
            if invalid or record.evidence and not result.evidence_refs:
                allowed = f"E0 through E{len(aliases) - 1}" if aliases else "none (no observations)"
                raise ContractError("analysis must cite supplied evidence without inventing references: "
                    f"invalid_refs={sorted(invalid)}, allowed_ids={allowed}, "
                    f"nonempty_summary_evidence_refs_required={bool(record.evidence)}; "
                    "correct both structured citations and citations in the summary and claims")
        result = self.ask(profile, role, payload, schema, prefix + "/joint_analysis", validate=validate)
        self.store.save(prefix + "/evidence_aliases.json", {"record_ref": ref, "aliases": aliases})
        updates = {"evidence_refs": [aliases[ref] for ref in result.evidence_refs]}
        if isinstance(result, AnalystOutput):
            updates["claims"] = [claim.model_copy(update={"evidence_refs": [aliases[ref] for ref in claim.evidence_refs]})
                                 for claim in result.claims]
        return result.model_copy(update=updates)

    @staticmethod
    def snippets(uses, refs, radius=1):
        refs, output = set(refs), []
        for used in uses:
            evidence = used.record.evidence
            positions = set()
            for index, item in enumerate(evidence):
                if item.evidence_id not in refs:
                    continue
                data = item.observation.get("data", {})
                episode = data.get("episode") if isinstance(data, dict) else None
                step = data.get("step") if isinstance(data, dict) else None
                for neighbor in range(max(0, index - radius), min(len(evidence), index + radius + 1)):
                    other = evidence[neighbor].observation
                    other_data = other.get("data", {})
                    other_episode = other_data.get("episode") if isinstance(other_data, dict) else None
                    if other.get("kind") == item.observation.get("kind") and other_episode == episode:
                        positions.add(neighbor)
                if radius and isinstance(step, (int, float)):
                    for neighbor, other_item in enumerate(evidence):
                        other_data = other_item.observation.get("data", {})
                        if (isinstance(other_data, dict) and other_data.get("episode") == episode
                                and isinstance(other_data.get("step"), (int, float))
                                and abs(other_data["step"] - step) <= radius):
                            positions.add(neighbor)
            output.extend({"scenario_id": used.record.scenario_id, "evidence_id": evidence[index].evidence_id,
                "observation": {k: v for k, v in evidence[index].observation.items() if k != "context"}}
                for index in sorted(positions))
        return output

    def with_evidence(self, payload, uses, refs, profile, role, schema):
        for radius in (1, 0):
            result = {**payload, "evidence": self.snippets(uses, refs, radius)}
            if self.fits(profile, role, result, schema):
                return result
        return result
