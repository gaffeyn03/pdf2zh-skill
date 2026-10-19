from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf
from job_files import JobError, JsonValue


@dataclass(frozen=True, slots=True)
class PageFacts:
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    has_text: bool


@dataclass(frozen=True, slots=True)
class PdfFacts:
    pages: tuple[PageFacts, ...]


@dataclass(frozen=True, slots=True)
class PreviewPair:
    page: int
    source: Path
    output: Path
    dimensions_match: bool
    source_nonblank: bool
    output_nonblank: bool


def _box(rect: pymupdf.Rect) -> tuple[float, float, float, float]:
    return rect.x0, rect.y0, rect.x1, rect.y1


def inspect_pdf(path: Path) -> PdfFacts:
    if not path.is_file():
        raise JobError("pdf_missing", f"PDF is missing: {path}")
    if path.read_bytes()[:5] != b"%PDF-":
        raise JobError("pdf_signature", f"PDF signature is invalid: {path}")
    try:
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise JobError("pdf_encrypted", f"Encrypted PDF is not allowed: {path}")
            if document.is_repaired:
                raise JobError(
                    "pdf_parseability",
                    f"Repaired or truncated PDF is not allowed: {path}",
                )
            pages = tuple(
                PageFacts(
                    media_box=_box(page.mediabox),
                    crop_box=_box(page.cropbox),
                    has_text=bool(page.get_text().strip()),
                )
                for page in document
            )
    except (RuntimeError, ValueError) as exc:
        raise JobError("pdf_parseability", f"PDF cannot be parsed: {path}") from exc
    if not pages:
        raise JobError("page_count", f"PDF has no pages: {path}")
    return PdfFacts(pages)


def selected_pages(
    raw_pages: JsonValue,
    source_count: int,
) -> tuple[int, ...]:
    if raw_pages is None:
        return tuple(range(1, source_count + 1))
    if not isinstance(raw_pages, list):
        raise JobError("manifest_invalid", "page_range must be an array or null.")
    pages: list[int] = []
    for value in raw_pages:
        if not isinstance(value, int) or isinstance(value, bool):
            raise JobError("manifest_invalid", "page_range contains a non-integer.")
        pages.append(value)
    if (
        not pages
        or pages != sorted(set(pages))
        or pages[0] < 1
        or pages[-1] > source_count
    ):
        raise JobError("manifest_invalid", "page_range is outside the source PDF.")
    return tuple(pages)


def structural_errors(
    source: PdfFacts,
    output: PdfFacts,
    pages: tuple[int, ...],
    *,
    bilingual: bool,
) -> list[str]:
    expected_count = len(source.pages) * (2 if bilingual else 1)
    errors: list[str] = []
    if len(output.pages) != expected_count:
        errors.append(
            f"page_count: expected {expected_count}, found {len(output.pages)}",
        )
        return errors
    for page_number in pages:
        source_page = source.pages[page_number - 1]
        output_index = 2 * page_number - 1 if bilingual else page_number - 1
        output_page = output.pages[output_index]
        if (
            source_page.media_box != output_page.media_box
            or source_page.crop_box != output_page.crop_box
        ):
            errors.append(f"page_geometry: page {page_number} boxes changed")
        if not output_page.has_text:
            errors.append(f"nonblank_text: page {page_number} has no text")
    return errors


def preview_page_numbers(pages: tuple[int, ...]) -> tuple[int, ...]:
    candidates = (pages[0], pages[len(pages) // 2], pages[-1])
    return tuple(dict.fromkeys(candidates))


def _pixmap_nonblank(pixmap: pymupdf.Pixmap) -> bool:
    return any(value < 250 for value in pixmap.samples)


def render_preview_pairs(
    source_path: Path,
    output_path: Path,
    pages: tuple[int, ...],
    preview_dir: Path,
    *,
    bilingual: bool,
) -> tuple[PreviewPair, ...]:
    preview_dir.mkdir(parents=True, exist_ok=True)
    pairs: list[PreviewPair] = []
    try:
        with (
            pymupdf.open(source_path) as source,
            pymupdf.open(output_path) as output,
        ):
            for page_number in preview_page_numbers(pages):
                output_index = 2 * page_number - 1 if bilingual else page_number - 1
                source_pixmap = source[page_number - 1].get_pixmap(alpha=False)
                output_pixmap = output[output_index].get_pixmap(alpha=False)
                source_png = preview_dir / f"source-page-{page_number:04d}.png"
                output_png = preview_dir / f"output-page-{page_number:04d}.png"
                source_pixmap.save(source_png)
                output_pixmap.save(output_png)
                pairs.append(
                    PreviewPair(
                        page=page_number,
                        source=source_png,
                        output=output_png,
                        dimensions_match=(
                            source_pixmap.width == output_pixmap.width
                            and source_pixmap.height == output_pixmap.height
                        ),
                        source_nonblank=_pixmap_nonblank(source_pixmap),
                        output_nonblank=_pixmap_nonblank(output_pixmap),
                    ),
                )
    except (RuntimeError, ValueError) as exc:
        raise JobError("preview_render", "Preview rendering failed.") from exc
    return tuple(pairs)


def preview_json(pair: PreviewPair) -> dict[str, JsonValue]:
    return {
        "page": pair.page,
        "source": str(pair.source),
        "output": str(pair.output),
        "dimensions_match": pair.dimensions_match,
        "source_nonblank": pair.source_nonblank,
        "output_nonblank": pair.output_nonblank,
    }
