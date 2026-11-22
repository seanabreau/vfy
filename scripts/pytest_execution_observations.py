"""Canonicalize known runner roots without interpreting user diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def normalize_text(
    value: object,
    root: Path,
    temporary: str | None = None,
    runner: str | None = None,
    *,
    pytest_temporary: str | None = None,
) -> str:
    if not isinstance(value, str):
        raise ValueError("execution observation text must be a string")
    if not value:
        return value
    return TextNormalizer.from_paths(root, temporary, runner, pytest_temporary=pytest_temporary)(
        value
    )


@dataclass(frozen=True)
class TextNormalizer:
    """Resolved literal replacements owned by one observation operation."""

    replacements: tuple[tuple[str, str], ...]

    @classmethod
    def from_paths(
        cls,
        root: Path,
        temporary: str | None = None,
        runner: str | None = None,
        *,
        pytest_temporary: str | None = None,
    ) -> TextNormalizer:
        return cls(_replacements(root, temporary, runner, pytest_temporary))

    def __call__(self, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("execution observation text must be a string")
        if not value:
            return value
        for source, destination in self.replacements:
            value = value.replace(source, destination)
        return value


def _replacements(
    root: Path,
    temporary: str | None,
    runner: str | None,
    pytest_temporary: str | None,
) -> tuple[tuple[str, str], ...]:
    replacements = [(str(root.resolve()), "<UPSTREAM>"), (str(root), "<UPSTREAM>")]
    if runner:
        replacements.extend(((str(Path(runner).resolve()), "<RUNNER>"), (runner, "<RUNNER>")))
    if temporary:
        replacements.extend(((str(Path(temporary).resolve()), "<TMP>"), (temporary, "<TMP>")))
    if pytest_temporary:
        exact_base = str(Path(pytest_temporary).resolve())
        normalized_base = exact_base
        for source, destination in sorted(
            replacements, key=lambda pair: len(pair[0]), reverse=True
        ):
            normalized_base = normalized_base.replace(source, destination)
        replacements.extend(((exact_base, "<PYTEST_TMP>"), (normalized_base, "<PYTEST_TMP>")))
    return tuple(sorted(replacements, key=lambda pair: len(pair[0]), reverse=True))
