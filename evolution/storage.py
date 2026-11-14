from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Dict

from evolution.errors import ContractError
from evolution.models import Individual, Population, canonical, digest


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
