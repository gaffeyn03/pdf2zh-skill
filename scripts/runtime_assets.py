from __future__ import annotations

import importlib.metadata
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Final

from job_files import JobError, JsonValue, atomic_write_json

BACKEND_NAME: Final = "pdf2zh"
BACKEND_VERSION: Final = "1.9.11"
FONT_NAMES: Final = (
    "GoNotoKurrent-Regular.ttf",
    "SourceHanSerifCN-Regular.ttf",
)


def backend_version() -> str:
    try:
        installed = importlib.metadata.version(BACKEND_NAME)
    except importlib.metadata.PackageNotFoundError as exc:
        raise JobError(
            "backend_missing", "pdf2zh is not installed in the runtime."
        ) from exc
    if installed != BACKEND_VERSION:
        raise JobError(
            "backend_version_mismatch",
            f"Expected pdf2zh=={BACKEND_VERSION}; found {installed}.",
        )
    return installed


def prepare_assets(runtime_root: Path) -> Path:
    version = backend_version()
    with redirect_stdout(sys.stderr):
        from babeldoc.assets.assets import get_font_and_metadata
        from babeldoc.assets.embedding_assets_metadata import (
            DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256,
        )
        from pdf2zh.doclayout import OnnxModel

    model = OnnxModel.load_available()
    assets: list[JsonValue] = [
        {
            "name": Path(model.model_path).name,
            "path": str(Path(model.model_path).resolve()),
            "sha3_256": DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256,
        }
    ]
    for font_name in FONT_NAMES:
        font_path, metadata = get_font_and_metadata(font_name)
        assets.append(
            {
                "name": font_name,
                "path": str(font_path.resolve()),
                "sha3_256": str(metadata["sha3_256"]),
            }
        )
    dependencies = {
        distribution.metadata["Name"]: distribution.version
        for distribution in importlib.metadata.distributions()
        if distribution.metadata["Name"]
    }
    marker = runtime_root / "offline-ready.json"
    atomic_write_json(
        marker,
        {
            "schema_version": 1,
            "backend": BACKEND_NAME,
            "backend_version": version,
            "dependencies": dict(sorted(dependencies.items())),
            "assets": assets,
        },
    )
    return marker
