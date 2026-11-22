"""Wire types for configuration validated by the Rust resolver."""

from __future__ import annotations

from typing import TypedDict

from .native_types import MutationTimeoutPolicy, SelectionMode


class RunConfig(TypedDict, total=False):
    branch: bool
    concurrency: list[str]
    context: str
    core: str
    data_file: str
    disable_warnings: list[str]
    include: list[str]
    omit: list[str]
    parallel: bool
    patch: list[str]
    plugins: list[str]
    relative_files: bool
    sigterm: bool
    source: list[str]
    source_pkgs: list[str]


class ReportConfig(TypedDict, total=False):
    contexts: list[str]
    exclude_also: list[str]
    exclude_lines: list[str]
    fail_under: float
    include: list[str]
    omit: list[str]
    partial_branches: list[str]
    partial_also: list[str]
    precision: int
    format: str
    ignore_errors: bool
    show_missing: bool
    skip_covered: bool
    skip_empty: bool
    sort: str


class HtmlConfig(TypedDict, total=False):
    directory: str
    show_contexts: bool
    skip_covered: bool
    skip_empty: bool
    title: str


class XmlConfig(TypedDict, total=False):
    output: str
    package_depth: int


class LcovConfig(TypedDict, total=False):
    output: str


class JsonConfig(TypedDict, total=False):
    output: str
    pretty_print: bool
    show_contexts: bool


class AnnotateConfig(TypedDict, total=False):
    directory: str


class VfyRunConfig(TypedDict, total=False):
    decisions: bool


class VfyTestConfig(TypedDict, total=False):
    history: str
    selection: SelectionMode
    source: list[str]
    workers: str | int


class VfyMutateConfig(TypedDict, total=False):
    history: str
    source: list[str]
    workers: str | int
    timeout: float
    timeout_policy: MutationTimeoutPolicy


class VfyCheckConfig(TypedDict, total=False):
    fail_under_decisions: float
    fail_under_conditions: float
    fail_under_mcdc: float
    format: str
    output: str
    include: list[str]
    omit: list[str]
    contexts: list[str]
    exclude_decisions: list[str]


class VfyConfig(TypedDict, total=False):
    run: VfyRunConfig
    test: VfyTestConfig
    mutate: VfyMutateConfig
    check: VfyCheckConfig


class Config(TypedDict, total=False):
    run: RunConfig
    report: ReportConfig
    html: HtmlConfig
    xml: XmlConfig
    lcov: LcovConfig
    json: JsonConfig
    annotate: AnnotateConfig
    vfy: VfyConfig
    paths: dict[str, list[str]]
    plugin: dict[str, dict[str, str]]


class ResolvedCommand(TypedDict):
    arguments: dict[str, object]
    config: Config


class ResolvedConfigRecord(TypedDict):
    settings: Config
    source: str | None
    discovery: str
