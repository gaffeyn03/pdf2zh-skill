from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TypeAlias

from typing_compat import override

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


class JsonLoader(Protocol):
    def __call__(self, source: str, /) -> JsonValue: ...


load_json: Final[JsonLoader] = json.loads

__all__ = ("JsonValue", "load_json")

SCHEMA_VERSION: Final = 1
PLACEHOLDER: Final = re.compile(r"<[^<>\n]+>")


@dataclass(frozen=True, slots=True)
class JobError(Exception):
    code: str
    message: str

    @override
    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class CapturedOccurrence:
    page: int
    source: str


@dataclass(frozen=True, slots=True)
class TranslationUnit:
    unit_id: str
    occurrence_index: int
    page: int
    source: str
    source_hash: str
    placeholders: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrepareRequest:
    source: Path
    job_dir: Path
    source_lang: str
    target_lang: str
    pages: tuple[int, ...] | None
    output_mode: str
    glossary: tuple[str, ...]
    style: str
    max_items: int
    max_characters: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha3_256_file(path: Path) -> str:
    digest = hashlib.sha3_256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Mapping[str, JsonValue]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _ = temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def json_array(values: Iterable[JsonValue]) -> list[JsonValue]:
    return list(values)


def build_units(
    captured: Sequence[CapturedOccurrence],
) -> tuple[TranslationUnit, ...]:
    units: list[TranslationUnit] = []
    for index, occurrence in enumerate(captured, start=1):
        source_hash = hashlib.sha256(occurrence.source.encode()).hexdigest()
        units.append(
            TranslationUnit(
                unit_id=f"u{index:06d}-{source_hash[:12]}",
                occurrence_index=index,
                page=occurrence.page,
                source=occurrence.source,
                source_hash=source_hash,
                placeholders=tuple(PLACEHOLDER.findall(occurrence.source)),
            ),
        )
    return tuple(units)


def split_batches(
    units: Sequence[TranslationUnit],
    max_items: int,
    max_characters: int,
) -> tuple[tuple[TranslationUnit, ...], ...]:
    if max_items < 1 or max_characters < 1:
        raise JobError("invalid_batch_limit", "Batch limits must be positive.")
    batches: list[tuple[TranslationUnit, ...]] = []
    current: list[TranslationUnit] = []
    characters = 0
    for unit in units:
        item_limit = len(current) >= max_items
        character_limit = characters + len(unit.source) > max_characters
        if current and (item_limit or character_limit):
            batches.append(tuple(current))
            current = []
            characters = 0
        current.append(unit)
        characters += len(unit.source)
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def unit_json(unit: TranslationUnit) -> dict[str, JsonValue]:
    return {
        "id": unit.unit_id,
        "occurrence_index": unit.occurrence_index,
        "page": unit.page,
        "source": unit.source,
        "source_hash": unit.source_hash,
        "placeholders": json_array(unit.placeholders),
    }


def canonical_hash(payload: Mapping[str, JsonValue]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def create_job(
    request: PrepareRequest,
    captured: Sequence[CapturedOccurrence],
) -> Path:
    if request.job_dir.exists():
        raise JobError("job_exists", f"Job directory already exists: {request.job_dir}")
    if not captured:
        raise JobError(
            "no_text", "No translatable text was captured in the selected pages."
        )
    request.job_dir.parent.mkdir(parents=True, exist_ok=True)
    parent = request.job_dir.parent.resolve()
    for stale in parent.glob(f".{request.job_dir.name}.prepare-*"):
        if stale.resolve().parent != parent:
            raise JobError(
                "unsafe_stale_stage", f"Refusing unsafe staging path: {stale}"
            )
        if stale.is_dir():
            shutil.rmtree(stale)
    stage = (
        request.job_dir.parent / f".{request.job_dir.name}.prepare-{uuid.uuid4().hex}"
    )
    try:
        stage.mkdir()
        for name in ("batches", "responses", "translations", "output"):
            (stage / name).mkdir()
        _ = shutil.copyfile(request.source, stage / "source.pdf")
        units = build_units(captured)
        batches = split_batches(units, request.max_items, request.max_characters)
        batch_ids = [f"b{index:04d}" for index in range(1, len(batches) + 1)]
        sources = [unit.source for unit in units]
        batch_hashes: dict[str, JsonValue] = {}
        previous: dict[str, JsonValue] = {}
        following: dict[str, JsonValue] = {}
        offset = 0
        for batch_id, batch in zip(batch_ids, batches, strict=True):
            end = offset + len(batch)
            previous[batch_id] = json_array(sources[max(0, offset - 2) : offset])
            following[batch_id] = json_array(sources[end : end + 2])
            body: dict[str, JsonValue] = {
                "schema_version": SCHEMA_VERSION,
                "batch_id": batch_id,
                "policy": "Translate exact units; preserve IDs and placeholders.",
                "glossary": json_array(request.glossary),
                "style": request.style,
                "document_context": json_array(sources[:3]),
                "previous_context": previous[batch_id],
                "next_context": following[batch_id],
                "units": json_array(unit_json(unit) for unit in batch),
            }
            body["batch_hash"] = canonical_hash(body)
            batch_hashes[batch_id] = body["batch_hash"]
            atomic_write_json(stage / "batches" / f"{batch_id}.json", body)
            offset = end
        atomic_write_json(
            stage / "units.json",
            {
                "schema_version": SCHEMA_VERSION,
                "units": json_array(unit_json(unit) for unit in units),
            },
        )
        atomic_write_json(
            stage / "context.json",
            {
                "schema_version": SCHEMA_VERSION,
                "document_context": json_array(sources[:3]),
                "previous_context": previous,
                "next_context": following,
            },
        )
        atomic_write_json(
            stage / "manifest.json",
            {
                "schema_version": SCHEMA_VERSION,
                "state": "PREPARED",
                "source": "source.pdf",
                "source_hash": sha256_file(stage / "source.pdf"),
                "source_lang": request.source_lang,
                "target_lang": request.target_lang,
                "page_range": json_array(request.pages) if request.pages else None,
                "output_mode": request.output_mode,
                "batch_ids": json_array(batch_ids),
                "batch_hashes": batch_hashes,
            },
        )
        os.replace(stage, request.job_dir)
    except (OSError, JobError):
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return request.job_dir
