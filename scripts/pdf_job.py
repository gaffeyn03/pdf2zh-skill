#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping
from contextlib import redirect_stdout
from pathlib import Path
from typing import Final, NoReturn, final

from audit_runtime import deny_network
from job_files import (
    CapturedOccurrence,
    JobError,
    JsonValue,
    PrepareRequest,
    build_units,
    create_job,
    split_batches,
)
from runtime_assets import backend_version, prepare_assets
from typing_compat import override

__all__ = ("build_units", "split_batches")

DEFAULT_MAX_ITEMS: Final = 24
DEFAULT_MAX_CHARACTERS: Final = 14_000
LANGUAGE_CODE: Final = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$", re.IGNORECASE)


@final
class CliArgs(argparse.Namespace):
    command: str = ""
    source: Path = Path()
    job_dir: Path = Path()
    source_lang: str = ""
    target_lang: str = ""
    pages: str | None = None
    output_mode: str = ""
    glossary: Path | None = None
    style: str = ""
    max_items: int = 0
    max_characters: int = 0
    runtime_root: Path = Path()


class JsonParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        raise JobError("invalid_arguments", message)


def parse_pages(value: str | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    pages: list[int] = []
    try:
        for part in value.split(","):
            bounds = part.split("-", maxsplit=1)
            pages.extend(range(int(bounds[0]), int(bounds[-1]) + 1))
    except ValueError as exc:
        raise JobError("bad_page_range", f"Invalid page range: {value}") from exc
    if not pages or min(pages) < 1 or pages != sorted(set(pages)):
        raise JobError("bad_page_range", f"Invalid page range: {value}")
    return tuple(pages)


def capture_pdf(request: PrepareRequest) -> tuple[CapturedOccurrence, ...]:
    _ = backend_version()
    deny_network()
    with redirect_stdout(sys.stderr):
        from pdf2zh import converter, high_level
        from pdf2zh.doclayout import OnnxModel
        from pymupdf import Document

    try:
        with Document(str(request.source)) as document:
            page_count = document.page_count
    except RuntimeError as exc:
        raise JobError("invalid_pdf", "Source is not a parseable PDF.") from exc
    pages = request.pages or tuple(range(1, page_count + 1))
    if not pages or pages[-1] > page_count:
        raise JobError(
            "bad_page_range", f"Selected pages exceed the {page_count}-page source."
        )
    model = OnnxModel.load_available()
    original = converter.GoogleTranslator
    captured: list[CapturedOccurrence] = []
    runtime_root = Path(os.environ["PDF_TRANSLATE_RUNTIME_ROOT"])
    work_root = runtime_root / "work"
    work_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="capture-",
        dir=work_root,
    ) as raw:
        backend_source = Path(raw) / "source.pdf"
        _ = shutil.copyfile(request.source, backend_source)
        output = Path(raw) / "output"
        output.mkdir()
        try:
            for page in pages:

                class CaptureTranslator:
                    name: str = "google"
                    captured_page: int = page

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
                        captured.append(
                            CapturedOccurrence(page=self.captured_page, source=text)
                        )
                        return text

                converter.GoogleTranslator = CaptureTranslator
                _ = high_level.translate(
                    files=[str(backend_source)],
                    output=str(output),
                    pages=[page - 1],
                    lang_in=request.source_lang,
                    lang_out=request.target_lang,
                    service="google",
                    thread=1,
                    model=model,
                    ignore_cache=True,
                )
        finally:
            converter.GoogleTranslator = original
    return tuple(captured)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    _ = prepare.add_argument("--source", required=True, type=Path)
    _ = prepare.add_argument("--job-dir", required=True, type=Path)
    _ = prepare.add_argument("--source-lang", required=True)
    _ = prepare.add_argument("--target-lang", required=True)
    _ = prepare.add_argument("--pages")
    _ = prepare.add_argument(
        "--output-mode",
        choices=("translated", "bilingual"),
        default="translated",
    )
    _ = prepare.add_argument("--glossary", type=Path)
    _ = prepare.add_argument("--style", default="academic")
    _ = prepare.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    _ = prepare.add_argument(
        "--max-characters",
        type=int,
        default=DEFAULT_MAX_CHARACTERS,
    )
    assets = subparsers.add_parser("setup-assets")
    _ = assets.add_argument("--runtime-root", required=True, type=Path)
    for command in ("next", "submit", "status", "render", "validate", "resume"):
        _ = subparsers.add_parser(command)
    return parser


def request_from_args(args: CliArgs) -> PrepareRequest:
    source = args.source.resolve()
    if not source.is_file():
        raise JobError("source_missing", f"Source PDF does not exist: {source}")
    job_dir = args.job_dir.resolve()
    if job_dir.exists():
        raise JobError("job_exists", f"Job directory already exists: {job_dir}")
    for language in (args.source_lang, args.target_lang):
        if LANGUAGE_CODE.fullmatch(language) is None:
            raise JobError("invalid_language", f"Invalid language code: {language}")
    glossary = ()
    if args.glossary:
        try:
            glossary = tuple(
                line.strip()
                for line in args.glossary.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except OSError as exc:
            raise JobError("glossary_unreadable", str(exc)) from exc
    return PrepareRequest(
        source=source,
        job_dir=job_dir,
        source_lang=args.source_lang,
        target_lang=args.target_lang,
        pages=parse_pages(args.pages),
        output_mode=args.output_mode,
        glossary=glossary,
        style=args.style,
        max_items=args.max_items,
        max_characters=args.max_characters,
    )


def emit(payload: Mapping[str, JsonValue]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(namespace=CliArgs())
        if args.command == "prepare":
            request = request_from_args(args)
            job_dir = create_job(request, capture_pdf(request))
            emit({"ok": True, "state": "PREPARED", "job_dir": str(job_dir)})
            return 0
        if args.command == "setup-assets":
            marker = prepare_assets(args.runtime_root.resolve())
            emit({"ok": True, "offline_ready": True, "marker": str(marker)})
            return 0
        raise JobError(
            "not_implemented", f"Command '{args.command}' is reserved for a later Todo."
        )
    except JobError as exc:
        emit({"ok": False, "error": {"code": exc.code, "message": exc.message}})
        return 2
    except (OSError, RuntimeError, ImportError, TypeError, ValueError) as exc:
        emit({"ok": False, "error": {"code": "prepare_failed", "message": str(exc)}})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
