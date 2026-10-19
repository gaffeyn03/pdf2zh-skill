#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "typing-extensions>=4.4; python_version < '3.12'",
# ]
# ///

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn, final

from job_files import JobError, JsonValue, load_json
from job_loop import next_batch, submit_batch
from job_loop import status as job_status
from runtime_setup import (
    RunnerError,
    doctor_payload,
    marker_ready,
    run_command,
    runtime_environment,
    runtime_paths,
    setup_runtime,
)
from typing_compat import override


@final
class CliArgs(argparse.Namespace):
    command: str = ""
    json: bool = False
    source: Path = Path()
    job_dir: Path = Path()
    source_lang: str = ""
    target_lang: str = ""
    pages: str | None = None
    output_mode: str = ""
    glossary: Path | None = None
    style: str = ""
    input: Path = Path()


class JsonParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        raise RunnerError("invalid_arguments", message)


def relay_command_output(
    completed: subprocess.CompletedProcess[str],
    error_code: str,
) -> None:
    if completed.stderr:
        print(completed.stderr.strip(), file=sys.stderr)
    lines = tuple(line for line in completed.stdout.splitlines() if line.strip())
    try:
        final = load_json(lines[-1]) if lines else None
    except json.JSONDecodeError:
        final = None
    if isinstance(final, dict):
        for diagnostic in lines[:-1]:
            print(diagnostic, file=sys.stderr)
        print(lines[-1])
        return
    for diagnostic in lines:
        print(diagnostic, file=sys.stderr)
    emit(
        {
            "ok": False,
            "error": {
                "code": error_code,
                "message": completed.stderr.strip() or "Command returned no JSON.",
            },
        }
    )


def dispatch_prepare(args: CliArgs) -> int:
    paths = runtime_paths()
    if not marker_ready(paths):
        raise RunnerError("runtime_not_ready", "Run setup before prepare.")
    command = [
        str(paths.python),
        str(Path(__file__).resolve().parent / "pdf_job.py"),
        "prepare",
        "--source",
        str(args.source),
        "--job-dir",
        str(args.job_dir),
        "--source-lang",
        args.source_lang,
        "--target-lang",
        args.target_lang,
        "--output-mode",
        args.output_mode,
        "--style",
        args.style,
    ]
    for name, value in (
        ("--pages", args.pages),
        ("--glossary", args.glossary),
    ):
        if value is not None:
            command.extend((name, str(value)))
    completed = run_command(command, runtime_environment(paths, offline=True))
    relay_command_output(completed, "prepare_failed")
    return completed.returncode


def dispatch_pipeline(args: CliArgs) -> int:
    paths = runtime_paths()
    if not marker_ready(paths):
        raise RunnerError("runtime_not_ready", f"Run setup before {args.command}.")
    command = [
        str(paths.python),
        str(Path(__file__).resolve().parent / "pdf_pipeline.py"),
        args.command,
        "--job-dir",
        str(args.job_dir),
        "--json",
    ]
    completed = run_command(command, runtime_environment(paths, offline=True))
    relay_command_output(completed, f"{args.command}_failed")
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = JsonParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("doctor", "setup"):
        item = subparsers.add_parser(command)
        _ = item.add_argument("--json", action="store_true")
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
    for command in ("next", "status"):
        item = subparsers.add_parser(command)
        _ = item.add_argument("--job-dir", required=True, type=Path)
        _ = item.add_argument("--json", action="store_true")
    submit = subparsers.add_parser("submit")
    _ = submit.add_argument("--job-dir", required=True, type=Path)
    _ = submit.add_argument("--input", required=True, type=Path)
    _ = submit.add_argument("--json", action="store_true")
    for command in ("render", "validate"):
        item = subparsers.add_parser(command)
        _ = item.add_argument("--job-dir", required=True, type=Path)
        _ = item.add_argument("--json", action="store_true")
    resume = subparsers.add_parser("resume")
    _ = resume.add_argument("--json", action="store_true")
    return parser


def emit(payload: Mapping[str, JsonValue]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(namespace=CliArgs())
        if args.command == "doctor":
            report = doctor_payload()
            emit(report)
            return 0 if report["ok"] else 2
        if args.command == "setup":
            emit(setup_runtime())
            return 0
        if args.command == "prepare":
            return dispatch_prepare(args)
        if args.command == "next":
            emit(next_batch(args.job_dir))
            return 0
        if args.command == "submit":
            emit(submit_batch(args.job_dir, args.input))
            return 0
        if args.command == "status":
            emit(job_status(args.job_dir))
            return 0
        if args.command in {"render", "validate"}:
            return dispatch_pipeline(args)
        raise RunnerError(
            "not_implemented",
            f"Command '{args.command}' is reserved for a later Todo.",
        )
    except (RunnerError, JobError) as exc:
        emit({"ok": False, "error": {"code": exc.code, "message": exc.message}})
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        emit({"ok": False, "error": {"code": "runner_failed", "message": str(exc)}})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
