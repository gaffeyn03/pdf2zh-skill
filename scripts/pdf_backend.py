from __future__ import annotations

import shutil
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, final, runtime_checkable

from job_files import JobError
from pdf_lexical import repair_dash_arrays
from pymupdf import Document


class Replay(Protocol):
    def translate(self, source: str) -> str: ...


class RenderInput(Protocol):
    @property
    def source(self) -> Path: ...

    @property
    def stage_dir(self) -> Path: ...

    @property
    def pages(self) -> tuple[int, ...] | None: ...

    @property
    def source_lang(self) -> str: ...

    @property
    def target_lang(self) -> str: ...

    @property
    def include_bilingual(self) -> bool: ...


@runtime_checkable
class RepairPage(Protocol):
    def get_contents(self) -> list[int]: ...


@runtime_checkable
class RepairDocument(Protocol):
    @property
    def page_count(self) -> int: ...

    def load_page(self, page_id: int) -> RepairPage: ...

    def xref_stream(self, xref: int) -> bytes | None: ...

    def update_stream(self, xref: int, stream: bytes) -> None: ...

    def saveIncr(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RenderedPdfs:
    mono: Path
    bilingual: Path | None


def _repair_dash_arrays(path: Path) -> None:
    with Document(path) as document:
        if not isinstance(document, RepairDocument):
            raise JobError(
                "render_failed",
                "PyMuPDF page-content stream editing is unavailable.",
            )
        changed = False
        content_xrefs = {
            xref
            for page_id in range(document.page_count)
            for xref in document.load_page(page_id).get_contents()
        }
        for xref in content_xrefs:
            try:
                stream = document.xref_stream(xref)
            except ValueError as exc:
                raise JobError(
                    "render_failed",
                    f"Page content stream {xref} is unreadable.",
                ) from exc
            if stream is None:
                raise JobError(
                    "render_failed",
                    f"Page content stream {xref} is missing.",
                )
            repaired = repair_dash_arrays(stream)
            if repaired != stream:
                document.update_stream(xref, repaired)
                changed = True
        if changed:
            document.saveIncr()


def render_pdf(
    request: RenderInput,
    replay: Replay,
) -> RenderedPdfs:
    with redirect_stdout(sys.stderr):
        from pdf2zh import converter, high_level
        from pdf2zh.doclayout import OnnxModel

        model = OnnxModel.load_available()
    original = converter.GoogleTranslator

    @final
    class ReplayTranslator:
        name: str = "google"

        def __init__(
            self,
            _lang_in: str,
            lang_out: str,
            _model: OnnxModel,
            **_options: str | int | bool,
        ) -> None:
            self.lang_out: str = lang_out

        def translate(self, text: str, ignore_cache: bool = False) -> str:
            _ = ignore_cache
            return replay.translate(text)

    converter.GoogleTranslator = ReplayTranslator
    try:
        backend_source = request.stage_dir / request.source.name
        _ = shutil.copyfile(request.source, backend_source)
        with redirect_stdout(sys.stderr):
            result = high_level.translate(
                files=[str(backend_source)],
                output=str(request.stage_dir),
                pages=(
                    [page - 1 for page in request.pages]
                    if request.pages is not None
                    else None
                ),
                lang_in=request.source_lang,
                lang_out=request.target_lang,
                service="google",
                thread=1,
                model=model,
                ignore_cache=True,
            )
    finally:
        converter.GoogleTranslator = original
    raw_mono, raw_bilingual = result[0]
    mono = Path(raw_mono)
    bilingual = Path(raw_bilingual) if raw_bilingual else None
    outputs = {mono}
    if request.include_bilingual:
        if bilingual is None:
            raise JobError(
                "render_failed",
                "Backend did not produce a bilingual PDF.",
            )
        outputs.add(bilingual)
    for output in outputs:
        _repair_dash_arrays(output)
    return RenderedPdfs(
        mono=mono,
        bilingual=bilingual if request.include_bilingual else None,
    )
