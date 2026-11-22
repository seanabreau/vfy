"""Discover and load vfy and Coverage.py configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from .config_types import Config, ResolvedConfigRecord
from .errors import ConfigError
from .native import (
    load_config_json,
    resolve_config_json,
)


def load(path: str | None = None) -> Config:
    try:
        return cast(Config, json.loads(load_config_json(path, None)))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(str(exc)) from exc


@dataclass(frozen=True)
class ResolvedConfig:
    """Resolved settings, their source file, and how that file was discovered."""

    settings: Config
    source: str | None
    discovery: str


def resolve(scope: str, path: str | None = None) -> ResolvedConfig:
    try:
        result = cast(ResolvedConfigRecord, json.loads(resolve_config_json(scope, path, None)))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(str(exc)) from exc
    return ResolvedConfig(result["settings"], result["source"], result["discovery"])
