from __future__ import annotations

import csv
import hashlib
from base64 import urlsafe_b64encode
from importlib.metadata import Distribution, distribution
from io import StringIO
from pathlib import Path
from typing import Final

from job_files import JobError

_SOURCE_HASHES: Final = {
    "translator.py": "213d8f97f77261a1bfe25bca8d938b6783e5119cfa1d96a70a86384a7dbeee85",
    "converter.py": "32ebf01c1fd20016c198e1be9a1d4ee52f4f7a9e83a5899192aace88e9d39c60",
}
_SANITIZED_SOURCE_HASHES: Final = {
    "translator.py": "9a02d10d26ed7cf38e4861f4fd00d65c3a26e67ae3d19f439054d8d77523f96e",
    "converter.py": "99d124e7196ed987e5c518705957837b299584a0554b1aef14c04d8e174f605d",
}
_BACKEND_VERSION: Final = "1.9.11"
_PROVIDER_TOKENS: Final = ("tencentcloud", "qcloud")
_PROVIDER_IMPORT_START: Final = "\nimport deepl\n"
_PROVIDER_IMPORT_END: Final = "\nfrom pdf2zh.cache import TranslationCache"
_PROVIDER_CLASS_START: Final = "\nclass BingTranslator"
_CONVERTER_IMPORT_START: Final = "from pdf2zh.translator import (\n"
_CONVERTER_IMPORT_END: Final = ")\n\nlog = logging.getLogger(__name__)"
_TRANSLATOR_LOOP_START: Final = "        for translator in ["
_TRANSLATOR_LOOP_END: Final = "]:\n            if service_name == translator.name:"


def _checked_source(root: Path, name: str) -> tuple[Path, str, bool]:
    path = root / name
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest == _SOURCE_HASHES[name]:
        return path, source.decode("utf-8"), True
    if digest == _SANITIZED_SOURCE_HASHES[name]:
        return path, source.decode("utf-8"), False
    raise JobError(
        "backend_source_mismatch",
        f"Installed pdf2zh {name} does not match the locked wheel.",
    )


def _validate_sanitized_metadata(package: Distribution) -> None:
    metadata_path = _distribution_path(package, "METADATA")
    if metadata_path is not None and _has_provider_reference(
        metadata_path.read_text(encoding="utf-8")
    ):
        raise JobError(
            "backend_source_mismatch",
            "Installed pdf2zh metadata still declares a provider dependency.",
        )
    direct_url_path = _distribution_path(package, "direct_url.json")
    if (
        direct_url_path is not None
        and direct_url_path.is_file()
        and _has_provider_reference(direct_url_path.read_text(encoding="utf-8"))
    ):
        raise JobError(
            "backend_source_mismatch",
            "Installed pdf2zh direct URL still declares a provider dependency.",
        )


def _validate_record(package: Distribution) -> None:
    record_path = _distribution_path(package, "RECORD")
    if record_path is None:
        raise JobError("backend_source_mismatch", "Installed pdf2zh has no RECORD.")
    with record_path.open(encoding="utf-8", newline="") as record_file:
        for row in csv.reader(record_file, strict=True):
            if not row:
                continue
            if len(row) != 3:
                raise JobError(
                    "backend_source_mismatch", "Installed pdf2zh RECORD is invalid."
                )
            path, hash_value, size = row
            if not hash_value:
                if size:
                    raise JobError(
                        "backend_source_mismatch",
                        "Installed pdf2zh RECORD is incomplete.",
                    )
                continue
            algorithm, separator, expected_hash = hash_value.partition("=")
            if algorithm != "sha256" or not separator:
                raise JobError(
                    "backend_source_mismatch", "Installed pdf2zh RECORD is invalid."
                )
            data_path = Path(str(package.locate_file(path)))
            if not data_path.is_file():
                raise JobError(
                    "backend_source_mismatch", "Installed pdf2zh RECORD is stale."
                )
            data = data_path.read_bytes()
            if _wheel_hash(data) != expected_hash or size != str(len(data)):
                raise JobError(
                    "backend_source_mismatch", "Installed pdf2zh RECORD is stale."
                )


def _between(source: str, start: str, end: str, replacement: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index) + len(end)
    return source[:start_index] + replacement + source[end_index:]


def _has_provider_reference(source: str) -> bool:
    normalized = source.casefold()
    return any(token in normalized for token in _PROVIDER_TOKENS)


def _wheel_hash(data: bytes) -> str:
    return (
        urlsafe_b64encode(hashlib.sha256(data).digest())
        .decode("ascii")
        .rstrip(
            "=",
        )
    )


def _distribution_path(package: Distribution, name: str) -> Path | None:
    files = package.files
    if files is None:
        return None
    for file in files:
        if file.name == name and ".dist-info/" in str(file):
            return Path(str(package.locate_file(file)))
    return None


def _rewrite_record(
    record_path: Path,
    updates: tuple[tuple[str, bytes], ...],
    remove_direct_url: bool,
) -> None:
    with record_path.open(encoding="utf-8", newline="") as record_file:
        record = record_file.read()
    physical_lines = record.splitlines(keepends=True)
    reader = csv.reader(StringIO(record, newline=""), strict=True)
    rewritten: list[str] = []
    line_start = 0
    for row in reader:
        line_end = reader.line_num
        raw_row = "".join(physical_lines[line_start:line_end])
        line_start = line_end
        if (
            row
            and row[0] == "pdf2zh-1.9.11.dist-info/direct_url.json"
            and remove_direct_url
        ):
            continue
        updated_bytes = next(
            (data for path, data in updates if row and path == row[0]),
            None,
        )
        if updated_bytes is not None:
            line_ending = raw_row[len(raw_row.rstrip("\r\n")) :]
            raw_row = (
                f"{row[0]},sha256={_wheel_hash(updated_bytes)},{len(updated_bytes)}"
                f"{line_ending}"
            )
        rewritten.append(raw_row)
    if line_start < len(physical_lines):
        rewritten.extend(physical_lines[line_start:])
    sanitized = "".join(rewritten)
    if sanitized != record:
        with record_path.open("w", encoding="utf-8", newline="") as record_file:
            _ = record_file.write(sanitized)


def _remove_provider_metadata(
    package: Distribution,
    source_updates: tuple[tuple[str, bytes], ...] = (),
) -> None:
    metadata_path = _distribution_path(package, "METADATA")
    metadata_bytes: bytes | None = None
    if metadata_path is not None:
        metadata_bytes_before = metadata_path.read_bytes()
        metadata = metadata_bytes_before.decode("utf-8")
        sanitized = "".join(
            line
            for line in metadata.splitlines(keepends=True)
            if not (
                line.casefold().startswith("requires-dist:")
                and _has_provider_reference(line)
            )
        )
        if sanitized != metadata:
            metadata_bytes = sanitized.encode("utf-8")
            _ = metadata_path.write_bytes(metadata_bytes)
    direct_url_path = _distribution_path(package, "direct_url.json")
    direct_url_removed = False
    if (
        direct_url_path is not None
        and direct_url_path.is_file()
        and _has_provider_reference(direct_url_path.read_text(encoding="utf-8"))
    ):
        direct_url_path.unlink()
        direct_url_removed = True
    record_path = _distribution_path(package, "RECORD")
    updates = source_updates
    if metadata_bytes is not None:
        updates += (("pdf2zh-1.9.11.dist-info/METADATA", metadata_bytes),)
    if record_path is not None and (updates or direct_url_removed):
        _rewrite_record(record_path, updates, direct_url_removed)


def remove_provider_metadata(package: Distribution) -> None:
    _remove_provider_metadata(package)


def remove_provider_integrations() -> None:
    package = distribution("pdf2zh")
    if package.version != _BACKEND_VERSION:
        raise JobError(
            "backend_source_mismatch",
            "Installed pdf2zh does not match the locked version.",
        )
    root = Path(str(package.locate_file("pdf2zh")))
    translator_path, translator, translator_is_upstream = _checked_source(
        root, "translator.py"
    )
    converter_path, converter, converter_is_upstream = _checked_source(
        root, "converter.py"
    )
    if translator_is_upstream != converter_is_upstream:
        raise JobError(
            "backend_source_mismatch",
            "Installed pdf2zh source is only partially sanitized.",
        )
    if translator_is_upstream:
        translator = _between(
            translator,
            _PROVIDER_IMPORT_START,
            _PROVIDER_IMPORT_END,
            "\n\nfrom pdf2zh.cache import TranslationCache",
        )
        translator = translator.split(_PROVIDER_CLASS_START, maxsplit=1)[0] + "\n"
        converter = _between(
            converter,
            _CONVERTER_IMPORT_START,
            _CONVERTER_IMPORT_END,
            (
                "from pdf2zh.translator import BaseTranslator, GoogleTranslator\n\n"
                "log = logging.getLogger(__name__)"
            ),
        )
        converter = _between(
            converter,
            _TRANSLATOR_LOOP_START,
            _TRANSLATOR_LOOP_END,
            (
                "        for translator in [GoogleTranslator]:\n"
                "            if service_name == translator.name:"
            ),
        )
        _ = translator_path.write_text(translator, encoding="utf-8", newline="\n")
        _ = converter_path.write_text(converter, encoding="utf-8", newline="\n")
        _remove_provider_metadata(
            package,
            (
                ("pdf2zh/translator.py", translator.encode("utf-8")),
                ("pdf2zh/converter.py", converter.encode("utf-8")),
            ),
        )
    _validate_sanitized_metadata(package)
    _validate_record(package)


if __name__ == "__main__":
    remove_provider_integrations()
