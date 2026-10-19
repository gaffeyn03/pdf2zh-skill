from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from job_files import JsonValue, load_json, sha3_256_file

BACKEND_VERSION: Final = "1.9.11"
COMMAND_TIMEOUT_SECONDS: Final = 300
SCRIPT_DIR: Final = Path(__file__).resolve().parent
LOCK_FILE: Final = SCRIPT_DIR.parent / "requirements.lock"


@dataclass(frozen=True, slots=True)
class RunnerError(Exception):
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    python: Path
    marker: Path
    lock_hash: str


def runtime_data_root() -> Path:
    if os.name == "nt":
        raw_base = os.environ.get("LOCALAPPDATA")
        base = Path(raw_base) if raw_base else Path.home() / "AppData" / "Local"
        return base / "PaperTranslateSkill"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "论文翻译Skill"
    raw_base = os.environ.get("XDG_DATA_HOME")
    base = Path(raw_base).expanduser() if raw_base else Path.home() / ".local" / "share"
    return base / "pdf-translate-skill"


def runtime_paths() -> RuntimePaths:
    digest = hashlib.sha256(LOCK_FILE.read_bytes()).hexdigest()
    base = runtime_data_root()
    root = base / "runtimes" / digest[:16]
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return RuntimePaths(root, python, root / "offline-ready.json", digest)


def marker_ready(paths: RuntimePaths) -> bool:
    if not paths.python.is_file() or not paths.marker.is_file():
        return False
    try:
        payload = load_json(paths.marker.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        if payload["backend_version"] != BACKEND_VERSION:
            return False
        assets = payload["assets"]
        if not isinstance(assets, list):
            return False
        for asset in assets:
            if not isinstance(asset, dict):
                return False
            raw_path = asset.get("path")
            expected_hash = asset.get("sha3_256")
            if not isinstance(raw_path, str) or not isinstance(expected_hash, str):
                return False
            asset_path = Path(raw_path)
            if not asset_path.is_file() or sha3_256_file(asset_path) != expected_hash:
                return False
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return False
    return True


def doctor_payload() -> dict[str, JsonValue]:
    paths = runtime_paths()
    supported = (3, 11) <= sys.version_info[:2] < (3, 13)
    lock_valid = f"pdf2zh=={BACKEND_VERSION}" in LOCK_FILE.read_text(encoding="utf-8")
    ready = supported and lock_valid and marker_ready(paths)
    return {
        "ok": supported and lock_valid,
        "python": {
            "executable": sys.executable,
            "version": ".".join(map(str, sys.version_info[:3])),
            "supported": supported,
        },
        "runtime": {
            "path": str(paths.root),
            "lock_hash": paths.lock_hash,
            "exists": paths.python.is_file(),
        },
        "backend": {
            "name": "pdf2zh",
            "required_version": BACKEND_VERSION,
            "lock_valid": lock_valid,
        },
        "offline_ready": ready,
        "next_action": "prepare" if ready else "setup",
    }


def runtime_environment(paths: RuntimePaths, offline: bool) -> dict[str, str]:
    allowed = (
        "HOME",
        "USERPROFILE",
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    )
    environment = {
        name: value for name in allowed if (value := os.environ.get(name)) is not None
    }
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PDF_TRANSLATE_RUNTIME_ROOT": str(paths.root),
        }
    )
    if offline:
        environment["PDF_TRANSLATE_OFFLINE"] = "1"
    return environment


def run_command(
    command: list[str],
    environment: Mapping[str, str] | None = None,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(
            "command_timeout",
            f"Command exceeded {timeout_seconds} seconds.",
        ) from exc


def setup_runtime() -> dict[str, JsonValue]:
    report = doctor_payload()
    if not report["ok"]:
        raise RunnerError(
            "unsupported_python",
            "Setup requires CPython 3.11 or 3.12 and an exact pdf2zh lock pin.",
        )
    paths = runtime_paths()
    if marker_ready(paths):
        return {**report, "setup": "already_ready"}
    paths.root.parent.mkdir(parents=True, exist_ok=True)
    created = run_command([sys.executable, "-m", "venv", str(paths.root)])
    if created.returncode != 0:
        raise RunnerError(
            "venv_failed", created.stderr.strip() or created.stdout.strip()
        )
    installed = run_command(
        [
            str(paths.python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--require-hashes",
            "--no-deps",
            "--requirement",
            str(LOCK_FILE),
        ],
        runtime_environment(paths, offline=False),
        timeout_seconds=1800,
    )
    if installed.returncode != 0:
        raise RunnerError(
            "dependency_install_failed",
            installed.stderr.strip() or installed.stdout.strip(),
        )
    sanitized = run_command(
        [str(paths.python), str(SCRIPT_DIR / "sanitize_pdf2zh.py")],
        runtime_environment(paths, offline=False),
    )
    if sanitized.returncode != 0:
        raise RunnerError(
            "backend_sanitization_failed",
            sanitized.stderr.strip() or sanitized.stdout.strip(),
        )
    assets = run_command(
        [
            str(paths.python),
            str(SCRIPT_DIR / "pdf_job.py"),
            "setup-assets",
            "--runtime-root",
            str(paths.root),
        ],
        runtime_environment(paths, offline=False),
        timeout_seconds=1800,
    )
    if assets.returncode != 0:
        raise RunnerError(
            "asset_setup_failed",
            assets.stdout.strip() or assets.stderr.strip(),
        )
    ready = doctor_payload()
    if not ready["offline_ready"]:
        raise RunnerError(
            "offline_assets_invalid", "Prepared assets failed checksum verification."
        )
    return {**ready, "setup": "completed"}
