from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn, final

from audit_runtime import enforce_offline

enforce_offline()

from job_files import JobError, JsonValue
from pdf_backend import RenderedPdfs
from pdf_publish import validate_job
from pdf_replay import (
    OccurrenceReplay,
    RenderRequest,
    render_job,
)
from typing_compat import override

__all__ = (
    "OccurrenceReplay",
    "RenderRequest",
    "RenderedPdfs",
    "render_job",
    "validate_job",
)


@final
class CliArgs(argparse.Namespace):
    command: str = ""
    job_dir: Path = Path()
    json: bool = False


class JsonParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        raise JobError("invalid_arguments", message)


def emit(payload: Mapping[str, JsonValue]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = JsonParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("render", "validate"):
        item = commands.add_parser(command)
        _ = item.add_argument("--job-dir", required=True, type=Path)
        _ = item.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(namespace=CliArgs())
        if args.command == "render":
            emit(render_job(args.job_dir))
            return 0
        result = validate_job(args.job_dir)
        emit(result)
        return 0 if result["structural_ok"] is True else 2
    except JobError as exc:
        emit({"ok": False, "error": {"code": exc.code, "message": exc.message}})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
