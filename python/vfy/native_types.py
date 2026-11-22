"""Structured records returned by the Rust extension."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, NotRequired, TypedDict


class TestInvocationFacts(TypedDict):
    engine: str
    mode: str
    force_full: bool
    fresh: bool
    no_replay: bool
    workers: str | int | None
    independent_workers_configured: bool
    native_xdist_requested: bool
    scheduling: str | None
    env_scheduling: str | None
    explicit_distribution: str | None
    invocation_location: bool
    filtered_scope: bool
    coverage_enabled: bool
    cov_append: bool


class TestInvocationPlan(TypedDict):
    mode: str
    fresh: bool
    controller_workers: str | int | None
    scheduling: str | None
    distribution: str
    replay_enabled: bool


class GitReplayRecord(TypedDict):
    head: str
    clean: bool
    changed: Sequence[str]
    untracked: bool
    deleted: bool
    patch: str


type SelectionMode = Literal["safe", "balanced", "aggressive"]
type TestMode = SelectionMode | Literal["full"]


class SelectionRecord(TypedDict):
    mode: SelectionMode
    selected: Sequence[str]
    all_tests: Sequence[str]
    durations: dict[str, float]
    affected_files: Sequence[str]
    affected_functions: int
    affected_branches: int
    selected_seconds: float | None
    full_seconds: float | None
    fallback_full: bool
    reasons: Sequence[str]
    location: str | None
    scoped_files: dict[str, Sequence[tuple[int, int, int]]]


class TestRunRecord(TypedDict):
    failed: Sequence[str]
    outcomes: dict[str, str]
    infrastructure_failed: bool


type PytestConfigPolicy = Literal[
    "legacy_without_argument_files",
    "legacy",
    "toml",
    "toml_with_addopts_overrides",
    "toml_with_non_file_config",
]


class PytestConfigRecord(TypedDict):
    config_file: str | None
    rootdir: str
    config_contents_fingerprint: str
    configured_args: Sequence[str]
    effective_args: Sequence[str]
    cli_args: Sequence[str]
    filtered_scope: bool
    collect_only: bool
    invocation_location: NotRequired[str]
    cache_dir: str
    basetemp: str | None


type MutationStatus = Literal[
    "protected", "survived", "unreached", "invalid", "error", "timeout", "unstable", "killed"
]
type MutationOwner = Literal["module", "function"]
type MutationExecutionPhase = Literal["definition", "body"]
type MutationTimeoutPolicy = Literal["adaptive", "fixed"]
type MutationBaselineCoveragePolicy = Literal["implicit", "requested", "disabled"]


type MutationRecord = tuple[str, str, str, str, str, str, str, int, int, int, int, str]


class AuditRecord(TypedDict):
    complete: bool
    omitted_failures: list[str]
