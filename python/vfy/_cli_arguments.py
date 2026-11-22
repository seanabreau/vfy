"""Carry surrogate-escaped command arguments across the UTF-8 native boundary."""

from __future__ import annotations

from collections.abc import Sequence


def escape_arguments(arguments: Sequence[str]) -> tuple[list[str], str]:
    """Encode lone surrogates using a marker absent from the original arguments."""
    suffix = 0
    marker = f"\U0010ffffvfy{suffix}:"
    while any(marker in argument for argument in arguments):
        suffix += 1
        marker = f"\U0010ffffvfy{suffix}:"
    escaped = [
        "".join(
            f"{marker}{ord(character):04x};" if 0xD800 <= ord(character) <= 0xDFFF else character
            for character in argument
        )
        for argument in arguments
    ]
    return escaped, marker


def _restore_string(value: str, marker: str) -> str:
    parts = value.split(marker)
    return parts[0] + "".join(chr(int(part[:4], 16)) + part[5:] for part in parts[1:])


def restore_value(value: object, marker: str) -> object:
    """Restore native argument records produced from escaped input."""
    if isinstance(value, str):
        return _restore_string(value, marker)
    if isinstance(value, list):
        return [restore_value(item, marker) for item in value]
    if isinstance(value, dict):
        return {key: restore_value(item, marker) for key, item in value.items()}
    return value


def restore_record(value: dict[str, object], marker: str) -> dict[str, object]:
    """Restore the argument mapping returned by the native parser."""
    return {name: restore_value(item, marker) for name, item in value.items()}


def restore_arguments(arguments: list[str], marker: str) -> list[str]:
    """Restore the argument list returned by the serial-xdist rewrite."""
    return [_restore_string(argument, marker) for argument in arguments]
