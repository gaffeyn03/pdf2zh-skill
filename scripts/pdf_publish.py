from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from job_files import SCHEMA_VERSION, JobError, JsonValue, atomic_write_json
from job_loop import Job, string_value
from pdf_replay import load_ready, mark_state
from pdf_validation import (
    PdfFacts,
    inspect_pdf,
    preview_json,
    render_preview_pairs,
    selected_pages,
    structural_errors,
)


def _facts(path: Path, errors: list[str]) -> PdfFacts | None:
    try:
        return inspect_pdf(path)
    except JobError as exc:
        errors.append(f"{exc.code}: {exc.message}")
        return None


def _final_path(job: Job, mode: str, counter: int) -> Path:
    source_stem = job.job_dir.name
    target = string_value(job.manifest["target_lang"], "manifest_invalid")
    if target.startswith("zh"):
        label = "中英双语" if mode == "bilingual" else "中文译文"
    else:
        label = f"{target}-bilingual" if mode == "bilingual" else target
    base = f"{source_stem}-{label}"
    suffix_number = "" if counter == 1 else f"-{counter}"
    return job.job_dir / "output" / f"{base}{suffix_number}.pdf"


def _publish(job: Job, mode: str, chosen: Path) -> Path:
    temporary = job.job_dir / "output" / f".publish-{uuid.uuid4().hex}.tmp"
    try:
        _ = shutil.copyfile(chosen, temporary)
        counter = 1
        while True:
            final_path = _final_path(job, mode, counter)
            try:
                os.link(temporary, final_path)
            except FileExistsError:
                counter += 1
                continue
            return final_path
    finally:
        temporary.unlink(missing_ok=True)


def _preview_payload(
    job: Job,
    source_path: Path,
    chosen: Path,
    pages: tuple[int, ...],
    *,
    bilingual: bool,
) -> tuple[list[JsonValue], list[str]]:
    preview_dir = job.job_dir / "output" / "previews" / (
        "bilingual" if bilingual else "translated"
    )
    shutil.rmtree(preview_dir, ignore_errors=True)
    errors: list[str] = []
    try:
        pairs = render_preview_pairs(
            source_path,
            chosen,
            pages,
            preview_dir,
            bilingual=bilingual,
        )
    except JobError as exc:
        return [], [f"{exc.code}: {exc.message}"]
    for pair in pairs:
        if not pair.source.is_file() or not pair.output.is_file():
            errors.append(f"preview_missing: page {pair.page}")
        if not pair.dimensions_match:
            errors.append(f"preview_dimensions: page {pair.page}")
        if not pair.source_nonblank or not pair.output_nonblank:
            errors.append(f"preview_blank: page {pair.page}")
    return [preview_json(pair) for pair in pairs], errors


def validate_job(job_dir: Path) -> dict[str, JsonValue]:
    job, _rows, linguistic_warnings, mode = load_ready(job_dir)
    source_path = job.job_dir / string_value(job.manifest["source"], "manifest_invalid")
    staged_mono = job.job_dir / "output" / "staged-translated.pdf"
    staged_bilingual = job.job_dir / "output" / "staged-bilingual.pdf"
    chosen = staged_bilingual if mode == "bilingual" else staged_mono
    errors: list[str] = []
    source = _facts(source_path, errors)
    mono = _facts(staged_mono, errors)
    bilingual = _facts(staged_bilingual, errors) if mode == "bilingual" else None
    pages: tuple[int, ...] = ()
    if source is not None:
        pages = selected_pages(job.manifest["page_range"], len(source.pages))
        if mono is not None:
            errors.extend(structural_errors(source, mono, pages, bilingual=False))
        if bilingual is not None:
            errors.extend(structural_errors(source, bilingual, pages, bilingual=True))
    previews: list[JsonValue] = []
    chosen_facts = bilingual if mode == "bilingual" else mono
    if source is not None and chosen_facts is not None and pages and not errors:
        previews, preview_errors = _preview_payload(
            job,
            source_path,
            chosen,
            pages,
            bilingual=mode == "bilingual",
        )
        errors.extend(preview_errors)
    structural_ok = not errors
    mono_previews: list[JsonValue] = []
    if mode == "bilingual" and structural_ok and source is not None and mono is not None:
        mono_previews, mono_errors = _preview_payload(
            job, source_path, staged_mono, pages, bilingual=False
        )
        errors.extend(mono_errors)
        structural_ok = not errors
    error_values: list[JsonValue] = [*errors]
    checks: list[JsonValue] = [
        "signature",
        "parseability",
        "encryption",
        "page_count",
        "selected_page_geometry",
        "selected_page_nonblank_text",
        "preview_pairs",
    ]
    payload: dict[str, JsonValue] = {
        "schema_version": SCHEMA_VERSION,
        "state": "FAILED" if errors else "READY_TO_RENDER",
        "structural_ok": structural_ok,
        "structural_errors": error_values,
        "linguistic_warnings": linguistic_warnings,
        "checks": checks,
        "preview_pairs": previews,
        "translated_preview_pairs": mono_previews if mode == "bilingual" else previews,
        "staged_pdf": str(chosen),
        "final_pdf": None,
        "final_pdfs": [],
    }
    validation_path = job.job_dir / "validation.json"
    atomic_write_json(validation_path, payload)
    if structural_ok:
        final_path = _publish(job, mode, chosen)
        published: list[JsonValue] = [str(final_path)]
        if mode == "bilingual":
            published.insert(0, str(_publish(job, "translated", staged_mono)))
        payload["state"] = "COMPLETED"
        payload["final_pdf"] = str(final_path)
        payload["final_pdfs"] = published
        atomic_write_json(validation_path, payload)
        mark_state(job, "COMPLETED")
    else:
        mark_state(job, "FAILED")
    return {
        "ok": structural_ok,
        **payload,
        "validation_path": str(validation_path),
    }
