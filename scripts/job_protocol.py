from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from job_files import (
    PLACEHOLDER,
    SCHEMA_VERSION,
    JobError,
    JsonValue,
    canonical_hash,
    load_json,
    sha256_file,
)


@dataclass(frozen=True, slots=True)
class Unit:
    unit_id: str
    source: str
    source_hash: str
    placeholders: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Batch:
    batch_id: str
    batch_hash: str
    units: tuple[Unit, ...]


@dataclass(frozen=True, slots=True)
class Job:
    job_dir: Path
    manifest: dict[str, JsonValue]
    batch_ids: tuple[str, ...]
    batches: dict[str, Batch]
    accepted: tuple[str, ...]


def _read_json(path: Path, code: str) -> dict[str, JsonValue]:
    try:
        payload = load_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JobError(code, f"Cannot read UTF-8 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise JobError(code, f"JSON mapping required: {path}")
    return payload


def _string(value: JsonValue, code: str) -> str:
    if not isinstance(value, str):
        raise JobError(code, "Expected a JSON string.")
    return value


def _list(value: JsonValue, code: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise JobError(code, "Expected a JSON array.")
    return value


def _schema(payload: Mapping[str, JsonValue], code: str) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise JobError(code, "Unsupported or missing schema_version.")


def _recover_temps(job_dir: Path) -> None:
    for name in ("", "batches", "responses", "translations"):
        directory = job_dir / name
        if not directory.is_dir():
            continue
        for temporary in directory.glob("*.tmp"):
            if temporary.is_file():
                temporary.unlink()


def _unit(value: JsonValue) -> Unit:
    if not isinstance(value, dict):
        raise JobError("batch_invalid", "Batch unit must be a mapping.")
    required = {
        "id",
        "occurrence_index",
        "page",
        "source",
        "source_hash",
        "placeholders",
    }
    if set(value) != required:
        raise JobError("batch_invalid", "Batch unit fields do not match the schema.")
    source = _string(value["source"], "batch_invalid")
    source_hash = _string(value["source_hash"], "batch_invalid")
    if sha256(source.encode()).hexdigest() != source_hash:
        raise JobError("batch_invalid", "Batch unit source hash changed.")
    placeholders = tuple(
        _string(item, "batch_invalid")
        for item in _list(value["placeholders"], "batch_invalid")
    )
    if placeholders != tuple(PLACEHOLDER.findall(source)):
        raise JobError("batch_invalid", "Batch unit placeholders changed.")
    return Unit(
        _string(value["id"], "batch_invalid"), source, source_hash, placeholders
    )


def _batch(job_dir: Path, batch_id: str, expected_hash: str) -> Batch:
    payload = _read_json(job_dir / "batches" / f"{batch_id}.json", "batch_invalid")
    required = {
        "schema_version",
        "batch_id",
        "batch_hash",
        "policy",
        "glossary",
        "style",
        "document_context",
        "previous_context",
        "next_context",
        "units",
    }
    if set(payload) != required:
        raise JobError("batch_invalid", "Batch fields do not match the schema.")
    _schema(payload, "batch_invalid")
    actual_hash = _string(payload["batch_hash"], "batch_invalid")
    body = {key: value for key, value in payload.items() if key != "batch_hash"}
    if actual_hash != canonical_hash(body) or actual_hash != expected_hash:
        raise JobError(
            "batch_hash_mismatch", "Batch content no longer matches its immutable hash."
        )
    if _string(payload["batch_id"], "batch_invalid") != batch_id:
        raise JobError("batch_invalid", "Batch ID does not match its filename.")
    units = tuple(_unit(value) for value in _list(payload["units"], "batch_invalid"))
    if not units or len({unit.unit_id for unit in units}) != len(units):
        raise JobError("batch_invalid", "Batch unit IDs must be unique and nonempty.")
    return Batch(batch_id, actual_hash, units)


def _translation(
    batch: Batch, payload: Mapping[str, JsonValue]
) -> tuple[tuple[str, str], ...]:
    required = {"schema_version", "batch_id", "batch_hash", "translations"}
    if set(payload) != required:
        raise JobError(
            "translation_invalid", "Translation fields do not match the schema."
        )
    _schema(payload, "translation_invalid")
    if _string(payload["batch_id"], "translation_invalid") != batch.batch_id:
        raise JobError("translation_invalid", "Translation batch ID mismatch.")
    if _string(payload["batch_hash"], "translation_invalid") != batch.batch_hash:
        raise JobError("translation_invalid", "Translation batch hash mismatch.")
    rows = _list(payload["translations"], "translation_invalid")
    parsed: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "source_hash", "text"}:
            raise JobError(
                "translation_invalid", "Translation row fields do not match the schema."
            )
        parsed.append(
            (
                _string(row["id"], "translation_invalid"),
                _string(row["text"], "translation_invalid"),
            )
        )
    expected = tuple(unit.unit_id for unit in batch.units)
    if tuple(row[0] for row in parsed) != expected:
        raise JobError(
            "translation_invalid", "Translation IDs must exactly match batch order."
        )
    for unit, row, raw in zip(batch.units, parsed, rows, strict=True):
        if (
            not isinstance(raw, dict)
            or _string(raw["source_hash"], "translation_invalid") != unit.source_hash
        ):
            raise JobError("translation_invalid", "Translation source hash mismatch.")
        if not row[1].strip() or Counter(PLACEHOLDER.findall(row[1])) != Counter(
            unit.placeholders
        ):
            raise JobError(
                "translation_invalid",
                "Translation text is empty or changes placeholders.",
            )
    return tuple(parsed)


def _load_job(job_dir: Path) -> Job:
    root = job_dir.resolve()
    if not root.is_dir():
        raise JobError("job_missing", f"Job directory does not exist: {root}")
    _recover_temps(root)
    manifest = _read_json(root / "manifest.json", "manifest_invalid")
    required = {
        "schema_version",
        "state",
        "source",
        "source_hash",
        "source_lang",
        "target_lang",
        "page_range",
        "output_mode",
        "batch_ids",
        "batch_hashes",
    }
    if set(manifest) != required:
        raise JobError("manifest_invalid", "Manifest fields do not match the schema.")
    _schema(manifest, "manifest_invalid")
    if sha256_file(root / _string(manifest["source"], "manifest_invalid")) != _string(
        manifest["source_hash"], "manifest_invalid"
    ):
        raise JobError("manifest_invalid", "Source PDF hash changed.")
    batch_ids = tuple(
        _string(value, "manifest_invalid")
        for value in _list(manifest["batch_ids"], "manifest_invalid")
    )
    hashes = manifest["batch_hashes"]
    if (
        not batch_ids
        or len(set(batch_ids)) != len(batch_ids)
        or not isinstance(hashes, dict)
        or set(hashes) != set(batch_ids)
    ):
        raise JobError("manifest_invalid", "Manifest batch ownership is invalid.")
    batches = {
        batch_id: _batch(root, batch_id, _string(hashes[batch_id], "manifest_invalid"))
        for batch_id in batch_ids
    }
    accepted_paths = tuple(
        (root / "translations" / f"{batch_id}.json") for batch_id in batch_ids
    )
    found = {path.stem for path in (root / "translations").glob("*.json")}
    if found - set(batch_ids):
        raise JobError("translation_invalid", "Unexpected accepted translation file.")
    accepted = tuple(
        batch_id
        for batch_id, path in zip(batch_ids, accepted_paths, strict=True)
        if path.is_file()
    )
    if accepted != batch_ids[: len(accepted)]:
        raise JobError(
            "translation_order_invalid",
            "Accepted translations must form a batch-order prefix.",
        )
    for batch_id in accepted:
        _ = _translation(
            batches[batch_id],
            _read_json(
                root / "translations" / f"{batch_id}.json",
                "translation_invalid",
            ),
        )
    return Job(root, manifest, batch_ids, batches, accepted)


json_list = _list
load_job = _load_job
parse_job_unit = _unit
parse_translation = _translation
read_job_json = _read_json
require_schema = _schema
string_value = _string
