from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import final

from job_files import JobError, JsonValue, atomic_write_json, sha256_file
from job_loop import (
    Job,
    Unit,
    json_list,
    load_job,
    parse_job_unit,
    parse_translation,
    read_job_json,
    require_schema,
    string_value,
    translation_warnings,
)
from pdf_backend import RenderedPdfs, render_pdf


@dataclass(frozen=True, slots=True)
class ReplayRow:
    unit: Unit
    text: str


@dataclass(frozen=True, slots=True)
class RenderRequest:
    source: Path
    stage_dir: Path
    pages: tuple[int, ...] | None
    source_lang: str
    target_lang: str
    include_bilingual: bool


@final
class OccurrenceReplay:
    __slots__ = ("_index", "_rows")

    def __init__(self, rows: tuple[ReplayRow, ...]) -> None:
        self._rows = rows
        self._index = 0

    def translate(self, source: str) -> str:
        if self._index >= len(self._rows):
            raise JobError("replay_drift", "Render requested an extra occurrence.")
        row = self._rows[self._index]
        source_hash = sha256(source.encode()).hexdigest()
        if source != row.unit.source or source_hash != row.unit.source_hash:
            raise JobError(
                "replay_drift",
                f"Occurrence {self._index + 1} changed during render.",
            )
        self._index += 1
        return row.text

    def finish(self) -> None:
        if self._index != len(self._rows):
            raise JobError(
                "replay_drift",
                f"Render consumed {self._index} of {len(self._rows)} occurrences.",
            )


Renderer = Callable[[RenderRequest, OccurrenceReplay], RenderedPdfs]


def mark_state(job: Job, state: str) -> None:
    job.manifest["state"] = state
    atomic_write_json(job.job_dir / "manifest.json", job.manifest)


def _persisted_units(job: Job) -> tuple[Unit, ...]:
    payload = read_job_json(job.job_dir / "units.json", "units_invalid")
    if set(payload) != {"schema_version", "units"}:
        raise JobError("units_invalid", "Unit fields do not match the schema.")
    require_schema(payload, "units_invalid")
    units: list[Unit] = []
    try:
        values = json_list(payload["units"], "units_invalid")
        for expected_index, value in enumerate(values, start=1):
            if not isinstance(value, dict):
                raise JobError("units_invalid", "Unit row must be a mapping.")
            occurrence = value.get("occurrence_index")
            page = value.get("page")
            if (
                occurrence != expected_index
                or not isinstance(page, int)
                or isinstance(page, bool)
                or page < 1
            ):
                raise JobError(
                    "units_invalid",
                    "Occurrence order or page changed.",
                )
            units.append(parse_job_unit(value))
    except JobError as exc:
        raise JobError("units_invalid", exc.message) from exc
    return tuple(units)


def load_ready(
    job_dir: Path,
) -> tuple[Job, tuple[ReplayRow, ...], list[JsonValue], str]:
    job = load_job(job_dir)
    if job.accepted != job.batch_ids:
        raise JobError("job_not_ready", "Every batch must be accepted before render.")
    persisted = _persisted_units(job)
    batched = tuple(
        unit for batch_id in job.batch_ids for unit in job.batches[batch_id].units
    )
    if persisted != batched:
        raise JobError("units_invalid", "Units no longer match immutable batch order.")
    rows: list[ReplayRow] = []
    warnings: list[JsonValue] = []
    for batch_id in job.batch_ids:
        batch = job.batches[batch_id]
        translated = parse_translation(
            batch,
            read_job_json(
                job.job_dir / "translations" / f"{batch_id}.json",
                "translation_invalid",
            ),
        )
        rows.extend(
            ReplayRow(unit, text)
            for unit, (_, text) in zip(batch.units, translated, strict=True)
        )
        warnings.extend(translation_warnings(batch, translated))
    mode = string_value(job.manifest["output_mode"], "manifest_invalid")
    if mode not in {"translated", "bilingual"}:
        raise JobError("manifest_invalid", f"Unsupported output_mode: {mode}")
    return job, tuple(rows), warnings, mode


def _page_range(raw: JsonValue) -> tuple[int, ...] | None:
    if raw is None:
        return None
    values = json_list(raw, "manifest_invalid")
    pages: list[int] = []
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool):
            raise JobError("manifest_invalid", "page_range contains a non-integer.")
        pages.append(value)
    if not pages or pages != sorted(set(pages)) or pages[0] < 1:
        raise JobError("manifest_invalid", "page_range is invalid.")
    return tuple(pages)


def _clean_stages(output_dir: Path) -> None:
    for stage in output_dir.glob(".render-*"):
        if stage.is_dir() and stage.resolve().parent == output_dir.resolve():
            shutil.rmtree(stage)


def render_job(job_dir: Path, renderer: Renderer = render_pdf) -> dict[str, JsonValue]:
    job, rows, _warnings_found, mode = load_ready(job_dir)
    output_dir = job.job_dir / "output"
    output_dir.mkdir(exist_ok=True)
    _clean_stages(output_dir)
    stage = output_dir / f".render-{uuid.uuid4().hex}"
    staged_mono = output_dir / "staged-translated.pdf"
    staged_bilingual = output_dir / "staged-bilingual.pdf"
    for stale in (staged_mono, staged_bilingual):
        stale.unlink(missing_ok=True)
    stage.mkdir()
    replay = OccurrenceReplay(rows)
    try:
        source = job.job_dir / string_value(job.manifest["source"], "manifest_invalid")
        request = RenderRequest(
            source=source,
            stage_dir=stage,
            pages=_page_range(job.manifest["page_range"]),
            source_lang=string_value(job.manifest["source_lang"], "manifest_invalid"),
            target_lang=string_value(job.manifest["target_lang"], "manifest_invalid"),
            include_bilingual=mode == "bilingual",
        )
        rendered = renderer(request, replay)
        replay.finish()
        candidates = (rendered.mono, rendered.bilingual)
        if any(
            path is not None
            and (not path.is_file() or path.resolve().parent != stage.resolve())
            for path in candidates
        ):
            raise JobError("render_output_invalid", "Backend output escaped its stage.")
        if mode == "bilingual" and rendered.bilingual is None:
            raise JobError("render_output_invalid", "Bilingual output is missing.")
        if sha256_file(source) != string_value(
            job.manifest["source_hash"],
            "manifest_invalid",
        ):
            raise JobError("source_hash_mismatch", "Source PDF changed during render.")
        os.replace(rendered.mono, staged_mono)
        if rendered.bilingual is not None:
            os.replace(rendered.bilingual, staged_bilingual)
        mark_state(job, "READY_TO_RENDER")
    except JobError:
        mark_state(job, "FAILED")
        staged_mono.unlink(missing_ok=True)
        staged_bilingual.unlink(missing_ok=True)
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        mark_state(job, "FAILED")
        staged_mono.unlink(missing_ok=True)
        staged_bilingual.unlink(missing_ok=True)
        raise JobError("render_failed", str(exc)) from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    chosen = staged_bilingual if mode == "bilingual" else staged_mono
    return {
        "ok": True,
        "state": "READY_TO_RENDER",
        "staged_pdf": str(chosen),
        "staged_pdfs": [
            str(path) for path in (staged_mono, staged_bilingual) if path.is_file()
        ],
    }
