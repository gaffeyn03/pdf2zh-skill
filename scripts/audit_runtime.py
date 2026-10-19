from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol, TypedDict, TypeGuard

from job_files import JobError


class AuditArgument(Protocol):
    pass


class FontMetadata(TypedDict):
    ascent: int
    descent: int
    encoding_length: int
    sha3_256: str


class BabelAssets(Protocol):
    def verify_file(self, path: Path, sha3_256: str) -> bool: ...

    def get_doclayout_onnx_model_path(self) -> Path: ...

    def get_font_and_metadata(self, name: str) -> tuple[Path, FontMetadata]: ...


class BabelAssetMetadata(Protocol):
    DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256: str
    EMBEDDING_FONT_METADATA: dict[str, FontMetadata]


AuditArguments = tuple[AuditArgument, ...]
_NETWORK_EVENTS: Final = frozenset(
    {
        "socket.__new__",
        "socket.bind",
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.gethostbyaddr",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.getnameinfo",
        "socket.listen",
        "socket.send",
        "socket.sendto",
        "socket.sendmsg",
    },
)
_BABELDOC_CACHE: Final = Path(".cache") / "babeldoc"
_MODEL_NAME: Final = "doclayout_yolo_docstructbench_imgsz1024.onnx"


def _is_babel_assets(module: ModuleType) -> TypeGuard[BabelAssets]:
    return hasattr(module, "verify_file")


def _is_babel_asset_metadata(module: ModuleType) -> TypeGuard[BabelAssetMetadata]:
    return hasattr(
        module,
        "DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256",
    ) and hasattr(module, "EMBEDDING_FONT_METADATA")


def add_audit_hook(hook: Callable[[str, AuditArguments], None]) -> None:
    sys.addaudithook(hook)


def configure_offline_assets() -> None:
    runtime_root = os.environ.get("PDF_TRANSLATE_RUNTIME_ROOT")
    if runtime_root is None:
        return
    cache_root = Path.home() / _BABELDOC_CACHE
    model_path = cache_root / "models" / _MODEL_NAME
    if not model_path.is_file():
        return
    from babeldoc.assets import assets, embedding_assets_metadata

    if not _is_babel_assets(assets) or not _is_babel_asset_metadata(
        embedding_assets_metadata,
    ):
        raise JobError("offline_asset_invalid", "BabelDoc assets are incomplete.")
    asset_api = assets
    metadata_api = embedding_assets_metadata
    model_hash = metadata_api.DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256
    if not asset_api.verify_file(model_path, model_hash):
        raise JobError(
            "offline_asset_invalid", "Cached document-layout model is invalid."
        )

    def local_model_path() -> Path:
        return model_path

    def local_font_path(name: str) -> tuple[Path, FontMetadata]:
        metadata = metadata_api.EMBEDDING_FONT_METADATA.get(name)
        if metadata is None:
            raise JobError(
                "offline_asset_missing", f"Cached font is unavailable: {name}"
            )
        digest = metadata.get("sha3_256")
        font_path = cache_root / "fonts" / name
        if not asset_api.verify_file(font_path, digest):
            raise JobError("offline_asset_invalid", f"Cached font is invalid: {name}")
        return font_path, metadata

    asset_api.get_doclayout_onnx_model_path = local_model_path
    asset_api.get_font_and_metadata = local_font_path


def deny_network() -> None:
    configure_offline_assets()

    def audit(event: str, _arguments: AuditArguments) -> None:
        if event in _NETWORK_EVENTS:
            raise JobError(
                "network_blocked",
                "Network access is disabled during local PDF processing.",
            )

    add_audit_hook(audit)


def enforce_offline() -> None:
    if os.environ.get("PDF_TRANSLATE_OFFLINE") == "1":
        deny_network()
