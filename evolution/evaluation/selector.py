from __future__ import annotations

from collections import Counter, defaultdict

from evolution.config import GeneticsConfig
from evolution.errors import InfrastructureError, PopulationInsufficient
from evolution.models import Assessment, PairJudgment, SelectionReport, Status, digest


class Selector:
    """Evaluation determines survival; generation operators have no survival quota."""

    def __init__(self, genetics: GeneticsConfig, size: int, seed: int):
        self.cfg, self.size, self.seed = genetics, size, seed

    def select(self, generation: int, assessments: list[Assessment], comparisons: list[PairJudgment],
               bootstrap: bool = False) -> tuple[list[Assessment], SelectionReport]:
        if any(a.status == Status.INFRA_ERROR for a in assessments):
            raise InfrastructureError("infrastructure failure is not algorithm fitness")
        if len({a.individual.individual_id for a in assessments}) != len(assessments):
            raise PopulationInsufficient("candidate IDs must be distinct")
        if len(assessments) < self.size:
            raise PopulationInsufficient("POPULATION_INSUFFICIENT: fewer than P candidate records")
        good = [a for a in assessments if not a.needs_repair and a.comparable]
        invalid = [a for a in assessments if a not in good]
        groups = defaultdict(list)
        for a in assessments:
            groups[a.individual.code_hash].append(a.individual.individual_id)
        selected, elites = [], []
        code_counts, strategy_counts, lineage_counts = Counter(), Counter(), Counter()

        def diversity_key(a: Assessment):
            return (code_counts[a.individual.code_hash], strategy_counts[a.individual.strategy_tag],
                lineage_counts[tuple(sorted(a.individual.parent_ids))],
                digest({"seed": self.seed, "generation": generation, "id": a.individual.individual_id}))

        def retain(a: Assessment):
            selected.append(a)
            code_counts[a.individual.code_hash] += 1
            strategy_counts[a.individual.strategy_tag] += 1
            lineage_counts[tuple(sorted(a.individual.parent_ids))] += 1

        if bootstrap:
            if len(assessments) != self.size:
                raise PopulationInsufficient("initial generation must contain P candidate records")
            selected = list(assessments)
        else:
            remaining = {a.individual.individual_id: a for a in good}
            defeats = {key: set() for key in remaining}
            wins = {key: set() for key in remaining}
            for pair in comparisons:
                a, b = pair.individual_a, pair.individual_b
                if a not in remaining or b not in remaining or not pair.order_consistent:
                    continue
                if pair.winner == "A_BETTER":
                    defeats[b].add(a)
                    wins[a].add(b)
                elif pair.winner == "B_BETTER":
                    defeats[a].add(b)
                    wins[b].add(a)
            while remaining and len(selected) < self.size:
                frontier = [a for key, a in remaining.items() if not (defeats[key] & remaining.keys())]
                proven_elites = [a for a in frontier if wins[a.individual.individual_id]
                                 and not defeats[a.individual.individual_id]]
                preferred = proven_elites if len(elites) < self.cfg.elite_count and proven_elites else frontier
                chosen = min(preferred or list(remaining.values()), key=diversity_key)
                key = chosen.individual.individual_id
                if len(elites) < self.cfg.elite_count and wins[key] and not defeats[key]:
                    elites.append(chosen)
                retain(chosen)
                del remaining[key]
            # Invalid candidates only occupy places left by evaluated executable
            # candidates. No repair reservation, priority or fallback switch.
            while len(selected) < self.size:
                chosen = min(invalid, key=lambda a: (a.status == Status.EXECUTION_BLOCKED, *diversity_key(a)))
                retain(chosen)
                invalid.remove(chosen)
        report = SelectionReport(generation=generation,
            pool_ids=[a.individual.individual_id for a in assessments],
            executable_pool_ids=[a.individual.individual_id for a in good],
            invalid_pool_ids=[a.individual.individual_id for a in assessments if a not in good],
            elite_ids=[a.individual.individual_id for a in elites],
            selected_invalid_ids=[a.individual.individual_id for a in selected if a not in good],
            selected_ids=[a.individual.individual_id for a in selected],
            duplicate_code_groups=[ids for ids in groups.values() if len(ids) > 1],
            comparison_graph=comparisons, behavior_quality_available=bool(good))
        return selected, report
