from __future__ import annotations

import re
from typing import Final

_WHITESPACE: Final = b"\x00\t\n\x0c\r "
_DELIMITERS: Final = b"()<>[]{}/%"
_NUMBER: Final = re.compile(rb"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


def _skip_space_and_comments(stream: bytes, start: int) -> int:
    index = start
    while index < len(stream):
        if stream[index] in _WHITESPACE:
            index += 1
            continue
        if stream[index] != ord("%"):
            return index
        end = stream.find(b"\n", index + 1)
        index = len(stream) if end < 0 else end + 1
    return index


def _literal_end(stream: bytes, start: int) -> int:
    index = start + 1
    depth = 1
    while index < len(stream) and depth:
        byte = stream[index]
        if byte == ord("\\"):
            index += 2
            continue
        if byte == ord("("):
            depth += 1
        elif byte == ord(")"):
            depth -= 1
        index += 1
    return index


def _hex_end(stream: bytes, start: int) -> int:
    end = stream.find(b">", start + 1)
    return len(stream) if end < 0 else end + 1


def _token_end(stream: bytes, start: int) -> int:
    index = start
    while (
        index < len(stream)
        and stream[index] not in _WHITESPACE
        and stream[index] not in _DELIMITERS
    ):
        index += 1
    return max(index, start + 1)


def _compound_end(stream: bytes, start: int, *, dictionary: bool) -> int:
    index = start + (2 if dictionary else 1)
    while index < len(stream):
        index = _skip_space_and_comments(stream, index)
        if dictionary and stream[index : index + 2] == b">>":
            return index + 2
        if not dictionary and stream[index : index + 1] == b"]":
            return index + 1
        index = _object_end(stream, index)
    return index


def _object_end(stream: bytes, start: int) -> int:
    if start >= len(stream):
        return start
    if stream[start : start + 1] == b"(":
        return _literal_end(stream, start)
    if stream[start : start + 2] == b"<<":
        return _compound_end(stream, start, dictionary=True)
    if stream[start : start + 1] == b"<":
        return _hex_end(stream, start)
    if stream[start : start + 1] == b"[":
        return _compound_end(stream, start, dictionary=False)
    if stream[start : start + 1] == b"/":
        return _token_end(stream, start + 1)
    return _token_end(stream, start)


def _inline_image_end(stream: bytes, start: int) -> int:
    index = start
    while index < len(stream):
        index = _skip_space_and_comments(stream, index)
        end = _object_end(stream, index)
        if stream[index:end] != b"ID":
            index = end
            continue
        data_start = end
        if stream[data_start : data_start + 2] == b"\r\n":
            data_start += 2
        elif data_start < len(stream) and stream[data_start] in _WHITESPACE:
            data_start += 1
        index = data_start
        while index < len(stream):
            end = stream.find(b"EI", index)
            if end < 0:
                return len(stream)
            after = end + 2
            if (
                end > data_start
                and stream[end - 1] in _WHITESPACE
                and (
                    after == len(stream)
                    or stream[after] in _WHITESPACE
                    or stream[after] in _DELIMITERS
                )
            ):
                return after
            index = end + 2
    return index


def _dash_array(stream: bytes, start: int, end: int) -> bytes | None:
    index = start + 1
    values: list[bytes] = []
    saw_comma = False
    while index < end - 1:
        while index < end - 1 and stream[index] in _WHITESPACE:
            index += 1
        match = _NUMBER.match(stream, index, end - 1)
        if match is None:
            return None
        values.append(match.group())
        index = match.end()
        while index < end - 1 and stream[index] in _WHITESPACE:
            index += 1
        if index == end - 1:
            break
        if stream[index : index + 1] != b",":
            return None
        saw_comma = True
        index += 1
    if not saw_comma or not values:
        return None
    return b"[" + b" ".join(values) + b"]"


def repair_dash_arrays(stream: bytes) -> bytes:
    replacements: list[tuple[int, int, bytes]] = []
    operands: list[tuple[int, int, bytes | None]] = []
    index = 0
    while index < len(stream):
        index = _skip_space_and_comments(stream, index)
        if index >= len(stream):
            break
        end = _object_end(stream, index)
        token = stream[index:end]
        if token == b"BI":
            operands.clear()
            index = _inline_image_end(stream, end)
            continue
        if token == b"d":
            if (
                len(operands) >= 2
                and operands[-1][2] is None
                and _NUMBER.fullmatch(stream[operands[-1][0] : operands[-1][1]])
                and operands[-2][2] is not None
            ):
                start, array_end, replacement = operands[-2]
                if replacement is not None:
                    replacements.append((start, array_end, replacement))
            operands.clear()
        elif token[:1] in b"[/<(+" or token[:1].isdigit() or token[:1] in b"-.":
            replacement = (
                _dash_array(stream, index, end) if token.startswith(b"[") else None
            )
            operands.append((index, end, replacement))
        else:
            operands.clear()
        index = end
    repaired = stream
    for start, end, replacement in reversed(replacements):
        repaired = repaired[:start] + replacement + repaired[end:]
    return repaired
