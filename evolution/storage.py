from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Dict

from evolution.errors import ContractError
from evolution.models import ExecutionRecord, Individual, Population, canonical, digest


TRACE_FORMAT = (
    "Tables use columns and rows; each row follows the listed column order. "
    "Every sample and its numeric precision are retained. Evidence source names decisions or trajectories; "
    "row is a zero-based sample index in that table or list. Evidence step is an evidence ordinal, "
    "not a control step; use the referenced sample's episode, step and time for temporal reasoning. "
    "The evidence table shares scenario_hash; context_rows identifies evidence rows that also include "
    "the result context. Cite original evidence_id values. Evidence references in highlights and "
    "verified packets refer to data already supplied in trace."
)


def _table(records: Any) -> Any:
    if not records or not isinstance(records[0], dict):
        return records
    columns = list(records[0])
    if any(not isinstance(row, dict) or set(row) != set(columns) for row in records):
        return records
    return {"columns": columns, "rows": [[row[column] for column in columns] for row in records]}


def _records(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    columns, rows = value["columns"], value["rows"]
    if len(columns) != len(set(columns)) or any(len(row) != len(columns) for row in rows):
        raise ContractError("invalid stored table")
    return [dict(zip(columns, row)) for row in rows]


def artifact_id() -> str:
    """UTC chronological ID with a random suffix for concurrent model calls."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ_") + uuid.uuid4().hex[:12]


def is_within(path: Path, parent: Path) -> bool:
    """Python 3.8 equivalent of Path.is_relative_to for resolved paths."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class Store:
    """One run, one writer. Immutable objects plus atomic named references."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, ref: str) -> Path:
        path = (self.root / ref).resolve()
        if not is_within(path, self.root):
            raise ContractError("artifact path escapes run root")
        return path

    def save(self, ref: str, value: Any) -> str:
        path, content = self.path(ref), canonical(value) + "\n"
        if path.exists():
            if path.read_text() != content:
                raise ContractError("immutable artifact collision: " + ref)
            return ref
        atomic_text(path, content)
        return ref

    def read(self, ref: str) -> Any:
        return json.loads(self.path(ref).read_text())

    def pointer(self, ref: str, value: Any) -> None:
        atomic_text(self.path(ref), canonical(value) + "\n")

    def object(self, value: Any) -> str:
        ref = "objects/" + digest(value) + ".json"
        path = self.path(ref)
        if path.exists() and path.read_text() != canonical(value) + "\n":
            # Preserve corrupt bytes for audit, then restore the addressed content.
            quarantine = self.path("corrupt_objects/" + uuid.uuid4().hex + ".bin")
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, quarantine)
        return self.save(ref, value)

    def verified_object(self, ref: str) -> Any:
        data = self.read(ref)
        if self.path(ref).stem != digest(data):
            raise ContractError("object hash mismatch: " + ref)
        return data

    def save_execution(self, record: ExecutionRecord) -> str:
        data = record.model_dump(mode="json")
        result = data["result"]
        sources = {"decision": "decisions", "trajectory": "trajectories"}
        offsets = {kind: 0 for kind in sources}
        rows, context_rows = [], []
        for index, evidence in enumerate(record.evidence):
            observation = evidence.observation
            kind = observation.get("kind")
            if kind not in sources or evidence.scenario_hash != record.execution_key.scenario_content_hash:
                break
            source, row = sources[kind], offsets[kind]
            samples = result[source]
            expected = {"kind": kind, "data": samples[row]} if row < len(samples) else {}
            if "context" in observation:
                expected["context"] = result["context"]
                context_rows.append(index)
            if canonical(observation) != canonical(expected):
                break
            rows.append({"evidence_id": evidence.evidence_id, "step": evidence.step, "source": source, "row": row})
            offsets[kind] += 1
        else:
            if rows:
                data["evidence"] = {**_table(rows), "scenario_hash": record.execution_key.scenario_content_hash,
                                    "context_rows": context_rows}
        for field in ("decisions", "trajectories"):
            result[field] = _table(result[field])
        return self.object(data)

    def load_execution(self, ref: str) -> ExecutionRecord:
        data = self.verified_object(ref)
        result = data["result"]
        for field in ("decisions", "trajectories"):
            result[field] = _records(result[field])
        table = data["evidence"]
        if isinstance(table, dict):
            evidence = []
            kinds = {"decisions": "decision", "trajectories": "trajectory"}
            for index, row in enumerate(_records(table)):
                source, position = row["source"], row["row"]
                if source not in kinds or not isinstance(position, int) or not 0 <= position < len(result[source]):
                    raise ContractError("invalid stored evidence reference")
                observation = {"kind": kinds[source], "data": result[source][position]}
                if index in table["context_rows"]:
                    observation["context"] = result["context"]
                evidence.append({"evidence_id": row["evidence_id"], "scenario_hash": table["scenario_hash"],
                                 "step": row["step"], "observation": observation})
            data["evidence"] = evidence
        return ExecutionRecord.model_validate(data)

    def execution_trace(self, ref: str) -> dict:
        data = self.verified_object(ref)
        return {"scenario_id": data["scenario_id"],
                **{field: data["result"][field] for field in ("decisions", "trajectories", "context")},
                "evidence": data["evidence"]}

    def attempt(self, generation: int) -> str:
        return f"generations/gen_{generation:04d}/attempts/{artifact_id()}"

    def event(self, kind: str, value: Any) -> str:
        return self.save(f"events/{artifact_id()}.json", {"kind": kind, "data": value})

    def save_individual(self, individual: Individual, files: Dict[str, str], lineage: Any) -> None:
        code_root = self.path(individual.artifact_ref)
        for relative, source in files.items():
            path = (code_root / relative).resolve()
            if not is_within(path, code_root) or path.suffix != ".py":
                raise ContractError("source path must stay in the candidate directory")
            if path.exists() and path.read_text() != source:
                raise ContractError("cannot overwrite parent source")
            if not path.exists():
                atomic_text(path, source)
        self.save(f"individuals/{individual.individual_id}/source_manifest.json", files)
        self.save(f"individuals/{individual.individual_id}/metadata.json", individual)
        self.save(individual.lineage_ref, lineage)

    def source(self, individual: Individual) -> Dict[str, str]:
        files = self.read(f"individuals/{individual.individual_id}/source_manifest.json")
        if digest(files) != individual.code_hash:
            raise ContractError("individual source manifest hash mismatch")
        for relative, source in files.items():
            path = self.path(individual.artifact_ref + "/" + relative)
            if path.read_text() != source:
                raise ContractError("individual source modified: " + individual.individual_id)
        return files

    def commit(self, population: Population, expected_size: int) -> None:
        self.validate_population(population, expected_size)
        ref = f"populations/gen_{population.generation:04d}.json"
        current = self.load_population(expected_size)
        expected_generation = 0 if current is None else current.generation + 1
        if population.generation != expected_generation:
            raise ContractError("population commit must advance exactly one generation")
        self.save(ref, population)
        self.pointer("populations/population_current.json", {"manifest_ref": ref, "hash": digest(population)})

    def validate_population(self, population: Population, expected_size: int) -> None:
        if len(population.population_ids) != expected_size:
            raise ContractError("population must contain exactly P individuals")
        for assessment in population.assessments:
            if assessment.status.value == "INFRA_ERROR":
                raise ContractError("infrastructure failures cannot become algorithm assessments")
            self.source(assessment.individual)
        self.read(population.selection_report_ref)

    def load_population(self, expected_size: int) -> Optional[Population]:
        # A durable immutable manifest is the commit record. Recover the pointer
        # if a crash occurred between manifest creation and pointer replacement.
        paths = sorted(self.path("populations").glob("gen_*.json"))
        if not paths:
            return None
        populations = [Population.model_validate(json.loads(p.read_text())) for p in paths]
        if [p.generation for p in populations] != list(range(len(populations))):
            raise ContractError("non-contiguous population history")
        population = populations[-1]
        self.validate_population(population, expected_size)
        ref = str(paths[-1].relative_to(self.root))
        self.pointer("populations/population_current.json", {"manifest_ref": ref, "hash": digest(population)})
        return population
