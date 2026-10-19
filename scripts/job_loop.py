from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Final

from job_files import (
    PLACEHOLDER,
    SCHEMA_VERSION,
    JobError,
    JsonValue,
    atomic_write_json,
)
from job_protocol import (
    Batch,
    Job,
    Unit,
    json_list,
    load_job,
    parse_job_unit,
    parse_translation,
    read_job_json,
    require_schema,
    string_value,
)

__all__ = [
    "Job",
    "Unit",
    "json_list",
    "load_job",
    "next_batch",
    "parse_job_unit",
    "parse_translation",
    "read_job_json",
    "require_schema",
    "status",
    "string_value",
    "submit_batch",
    "translation_warnings",
]

CITATION: Final = re.compile(r"\[[^\[\]\n]+\]|\([^()\n]*\d{4}[a-z]?[^()\n]*\)")
TOKEN: Final = re.compile(r"https?://\S+|`[^`\n]+`|\b\d+(?:\.\d+)?\b")


def _state(batch_ids: tuple[str, ...], accepted: tuple[str, ...]) -> str:
    if len(accepted) == len(batch_ids):
        return "READY_TO_RENDER"
    if accepted:
        return "TRANSLATING"
    return "PREPARED"


def _status(job: Job) -> dict[str, JsonValue]:
    persisted = job.manifest["state"]
    state = (
        persisted
        if persisted in {"COMPLETED", "FAILED"}
        else _state(job.batch_ids, job.accepted)
    )
    if job.manifest["state"] != state:
        job.manifest["state"] = state
        atomic_write_json(job.job_dir / "manifest.json", job.manifest)
    next_action = {
        "COMPLETED": "return",
        "FAILED": "inspect",
        "READY_TO_RENDER": "render",
    }.get(state, "next")
    return {
        "ok": True,
        "state": state,
        "accepted_batches": len(job.accepted),
        "total_batches": len(job.batch_ids),
        "next_action": next_action,
    }


def next_batch(job_dir: Path) -> dict[str, JsonValue]:
    job = load_job(job_dir)
    status = _status(job)
    if len(job.accepted) == len(job.batch_ids):
        return status
    batch_id = job.batch_ids[len(job.accepted)]
    response_path = job.job_dir / "responses" / f"{batch_id}.json"
    return {
        **status,
        "batch_path": str(job.job_dir / "batches" / f"{batch_id}.json"),
        "response_path": str(response_path),
    }


def status(job_dir: Path) -> dict[str, JsonValue]:
    return _status(load_job(job_dir))


def _response(batch: Batch, path: Path) -> tuple[tuple[str, str], ...]:
    payload = read_job_json(path, "response_invalid_json")
    if set(payload) != {"translations"}:
        raise JobError(
            "response_invalid_json", "Response must contain only translations."
        )
    rows = json_list(payload["translations"], "response_invalid_json")
    parsed: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "text"}:
            raise JobError(
                "response_invalid_json", "Response translation row is invalid."
            )
        parsed.append(
            (
                string_value(row["id"], "response_invalid_json"),
                string_value(row["text"], "response_invalid_json"),
            )
        )
    received = tuple(row[0] for row in parsed)
    expected = tuple(unit.unit_id for unit in batch.units)
    if len(received) != len(set(received)):
        raise JobError("response_duplicate_id", "Response contains duplicate IDs.")
    if received != expected:
        raise JobError(
            "response_ids_mismatch", "Response IDs must exactly match batch order."
        )
    for unit, row in zip(batch.units, parsed, strict=True):
        if not row[1].strip():
            raise JobError(
                "response_empty_text", "Response contains an empty translation."
            )
        if Counter(PLACEHOLDER.findall(row[1])) != Counter(unit.placeholders):
            raise JobError(
                "response_placeholder_mismatch",
                "Response changes protected placeholders.",
            )
    return tuple(parsed)


def _warnings(
    batch: Batch, translations: tuple[tuple[str, str], ...]
) -> list[JsonValue]:
    warnings: list[JsonValue] = []
    for unit, (_, text) in zip(batch.units, translations, strict=True):
        if Counter(CITATION.findall(unit.source)) != Counter(CITATION.findall(text)):
            warnings.append(f"{unit.unit_id}: citation tokens changed")
        if Counter(TOKEN.findall(unit.source)) != Counter(TOKEN.findall(text)):
            warnings.append(f"{unit.unit_id}: numeric or protected tokens changed")
    return warnings


def submit_batch(job_dir: Path, input_path: Path) -> dict[str, JsonValue]:
    job = load_job(job_dir)
    received_path = input_path.resolve()
    response_dir = (job.job_dir / "responses").resolve()
    if received_path.parent != response_dir or received_path.suffix != ".json":
        raise JobError(
            "response_path_mismatch", "Submit the response_path returned by next."
        )
    batch_id = received_path.stem
    if batch_id not in job.batches:
        raise JobError(
            "response_path_mismatch", "Response path is not owned by this job."
        )
    batch = job.batches[batch_id]
    response = _response(batch, received_path)
    accepted_path = job.job_dir / "translations" / f"{batch_id}.json"
    if batch_id in job.accepted:
        if (
            parse_translation(
                batch, read_job_json(accepted_path, "translation_invalid")
            )
            != response
        ):
            raise JobError("duplicate_submit", "An accepted batch cannot be replaced.")
        return {
            **_status(job),
            "batch_id": batch_id,
            "warnings": _warnings(batch, response),
        }
    pending = job.batch_ids[len(job.accepted)]
    if batch_id != pending:
        raise JobError(
            "response_path_mismatch", "Response does not belong to the pending batch."
        )
    atomic_write_json(
        accepted_path,
        {
            "schema_version": SCHEMA_VERSION,
            "batch_id": batch_id,
            "batch_hash": batch.batch_hash,
            "translations": [
                {"id": unit.unit_id, "source_hash": unit.source_hash, "text": text}
                for unit, (_, text) in zip(batch.units, response, strict=True)
            ],
        },
    )
    updated = load_job(job.job_dir)
    return {
        **_status(updated),
        "batch_id": batch_id,
        "warnings": _warnings(batch, response),
    }


translation_warnings = _warnings
