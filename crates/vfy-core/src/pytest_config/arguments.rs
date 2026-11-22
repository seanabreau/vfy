//! Pytest command-line parsing for configuration discovery and coverage intent.

use std::borrow::Cow;
use std::ffi::OsStr;
use std::path::Path;

use super::PytestConfigPolicy;

#[derive(Default)]
enum CoverageSources {
    #[default]
    Unspecified,
    Filtered(Vec<String>),
    Unrestricted,
}

#[derive(Default)]
pub(super) struct Arguments {
    pub(super) config_file: Option<String>,
    pub(super) rootdir: Option<String>,
    pub(super) overrides: Vec<String>,
    pub(super) basetemp: Option<String>,
    coverage_sources: CoverageSources,
    coverage_policy: MutationBaselineCoveragePolicy,
    pytest_workers: Option<String>,
    pub(super) targets: Vec<String>,
    filtered_scope: bool,
    pub(super) collect_only: bool,
    /// Selectors that depend on cached run state or skip execution.
    invocation_selection: bool,
}

impl Arguments {
    pub(super) fn invocation_location(&self) -> Option<&str> {
        let [target] = self.targets.as_slice() else {
            return None;
        };
        let (path, line) = target.rsplit_once(':')?;
        (!path.is_empty()
            && !line.is_empty()
            && line.bytes().all(|byte| byte.is_ascii_digit())
            && line.bytes().any(|byte| byte != b'0'))
        .then_some(target.as_str())
    }

    fn filtered_scope(&self) -> bool {
        self.filtered_scope
            || self.targets.iter().any(|target| {
                target.contains("::") || Path::new(target).extension() == Some(OsStr::new("py"))
            })
    }
}

/// Purpose of coverage collected during a mutation baseline.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum MutationBaselineCoveragePolicy {
    /// Coverage is collected only to select tests for mutations.
    #[default]
    Implicit,
    /// The original pytest arguments request coverage or a coverage threshold.
    Requested,
    /// Public pytest coverage is disabled; only private selection data is collected.
    Disabled,
}

impl MutationBaselineCoveragePolicy {
    /// Stable token for the private baseline child environment.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Implicit => "implicit",
            Self::Requested => "requested",
            Self::Disabled => "disabled",
        }
    }
}

/// Classify the original pytest arguments before mutation collection adds coverage.
#[must_use]
pub fn mutation_baseline_coverage_policy(
    arguments: &[String],
    parser_behavior: PytestParserBehavior,
) -> MutationBaselineCoveragePolicy {
    let parsed = parse_arguments(
        arguments,
        ArgumentPhase::Effective,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
        parser_behavior,
    );
    if parsed.coverage_policy == MutationBaselineCoveragePolicy::Disabled {
        return MutationBaselineCoveragePolicy::Disabled;
    }
    let requested = parsed.coverage_policy == MutationBaselineCoveragePolicy::Requested
        || match parsed.coverage_sources {
            CoverageSources::Unspecified => false,
            CoverageSources::Filtered(sources) => !sources.is_empty(),
            CoverageSources::Unrestricted => true,
        };
    if requested {
        MutationBaselineCoveragePolicy::Requested
    } else {
        MutationBaselineCoveragePolicy::Implicit
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum ArgumentPhase {
    Discovery,
    Effective,
}

/// Python's version-dependent early and final pytest argument parsers.
#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PytestParserBehavior {
    /// `CPython` 3.12.0-3.12.6 and 3.13.0: argparse's early known-args discovery
    /// and intermixed final parsing both predate the argparse fixes.
    Legacy,
    /// `CPython` 3.12.7: early discovery predates the argparse fix; final parsing has it.
    FixedFinal,
    /// All other releases: both phases include the argparse fixes.
    Current,
}

impl PytestParserBehavior {
    /// Select the parser behavior of a supported `CPython` release.
    #[must_use]
    pub const fn for_cpython(major: u8, minor: u8, patch: u8) -> Self {
        match (major, minor, patch) {
            (3, 12, 0..=6) | (3, 13, 0) => Self::Legacy,
            (3, 12, 7) => Self::FixedFinal,
            _ => Self::Current,
        }
    }
}

pub(super) fn filtered_scope(
    configured: &[String],
    invocation: &Arguments,
    addopts_override: Option<&str>,
    policy: PytestConfigPolicy,
    parser_behavior: PytestParserBehavior,
) -> bool {
    let configured_options = parse_arguments(
        configured,
        ArgumentPhase::Effective,
        policy,
        parser_behavior,
    );
    // An -o addopts override comes from the environment or CLI even though
    // pytest places its replacement tokens before those invocation arguments.
    configured_options.invocation_selection
        || (addopts_override.is_some() && configured_options.filtered_scope())
        || invocation.filtered_scope()
}

pub(super) fn parse_arguments(
    arguments: &[String],
    phase: ArgumentPhase,
    policy: PytestConfigPolicy,
    parser_behavior: PytestParserBehavior,
) -> Arguments {
    parse_arguments_from(arguments, phase, policy, parser_behavior, 0)
}

/// Keep inherited pytest workers serial within each mutation's process budget.
///
/// # Errors
/// Reject an explicit parallel pytest worker request on the mutation command.
pub fn mutation_worker_arguments(
    effective: &[String],
    cli: &[String],
    parser_behavior: PytestParserBehavior,
) -> Result<Vec<String>, &'static str> {
    let Some(cli_start) = effective.len().checked_sub(cli.len()) else {
        return Err("mutation configuration CLI does not match effective arguments");
    };
    if !effective.ends_with(cli) {
        return Err("mutation configuration CLI does not match effective arguments");
    }
    let workers = |start| {
        parse_arguments_from(
            effective,
            ArgumentPhase::Effective,
            PytestConfigPolicy::TomlWithAddoptsOverrides,
            parser_behavior,
            start,
        )
        .pytest_workers
    };
    if let Some(value) = workers(cli_start) {
        if value.parse::<i64>() != Ok(0) {
            return Err("vfy mutate controls mutation workers; remove pytest -n/--numprocesses");
        }
        return Ok(vec!["--dist=no".to_owned()]);
    }
    Ok(if workers(0).is_some() {
        vec!["--numprocesses=0".to_owned(), "--dist=no".to_owned()]
    } else {
        Vec::new()
    })
}

fn pytest_worker_option(
    arguments: &[String],
    index: &mut usize,
    argument: &str,
    phase: ArgumentPhase,
    parsed: &mut Arguments,
) -> bool {
    if phase != ArgumentPhase::Effective
        || !(argument.starts_with("-n")
            || argument == "--numprocesses"
            || argument.starts_with("--numprocesses="))
    {
        return false;
    }
    parsed.pytest_workers = Some(
        option_value(arguments, index, argument, &["-n", "--numprocesses"]).unwrap_or_default(),
    );
    true
}

fn coverage_option(
    arguments: &[String],
    index: &mut usize,
    argument: &str,
    parsed: &mut Arguments,
) -> bool {
    if argument == "--no-cov" {
        parsed.coverage_policy = MutationBaselineCoveragePolicy::Disabled;
        *index += 1;
        return true;
    }
    if option_value(
        arguments,
        index,
        argument,
        &[
            "--cov-fail-under",
            "--cov-fail-under-decisions",
            "--cov-fail-under-conditions",
            "--cov-fail-under-mcdc",
        ],
    )
    .is_some()
    {
        if parsed.coverage_policy != MutationBaselineCoveragePolicy::Disabled {
            parsed.coverage_policy = MutationBaselineCoveragePolicy::Requested;
        }
        return true;
    }
    if argument == "--cov-reset" {
        parsed.coverage_sources = CoverageSources::Filtered(Vec::new());
        *index += 1;
        return true;
    }
    let source = if let Some(source) = argument.strip_prefix("--cov=") {
        *index += 1;
        Some(source.to_owned())
    } else if argument == "--cov" {
        optional_argument_value(arguments, index).map(str::to_owned)
    } else {
        return false;
    };
    if let Some(source) = source {
        match &mut parsed.coverage_sources {
            CoverageSources::Unspecified => {
                parsed.coverage_sources = CoverageSources::Filtered(vec![source]);
            }
            CoverageSources::Filtered(sources) => sources.push(source),
            CoverageSources::Unrestricted => {}
        }
    } else {
        parsed.coverage_sources = CoverageSources::Unrestricted;
    }
    true
}

/// Retain the effective coverage CLI source override when reconstructing reports.
/// A bare `--cov` leaves source filtering to the coverage configuration, while
/// `--cov=` retains the literal empty package name like pytest-cov.
#[must_use]
pub fn coverage_report_sources(
    arguments: &[String],
    fallback_roots: &[String],
    parser_behavior: PytestParserBehavior,
) -> Option<Vec<String>> {
    let parsed = parse_arguments(
        arguments,
        ArgumentPhase::Effective,
        PytestConfigPolicy::TomlWithAddoptsOverrides,
        parser_behavior,
    );
    match parsed.coverage_sources {
        CoverageSources::Filtered(sources) if !sources.is_empty() => Some(sources),
        CoverageSources::Filtered(_) | CoverageSources::Unrestricted => None,
        CoverageSources::Unspecified => {
            (!fallback_roots.is_empty()).then(|| fallback_roots.to_vec())
        }
    }
}

#[derive(Default, PartialEq, Eq)]
enum ArgumentMode {
    #[default]
    Options,
    Literal,
    LegacyUnknownFlags,
}

#[derive(Default, PartialEq, Eq)]
enum PositionalHistory {
    #[default]
    None,
    Seen,
    OptionAfter,
}

#[derive(Default)]
struct SeparatorState {
    mode: ArgumentMode,
    history: PositionalHistory,
    separator_seen: bool,
}

impl SeparatorState {
    fn consume(&mut self, phase: ArgumentPhase, behavior: PytestParserBehavior) {
        // Older intermixed parsing consumes a leading separator in its first pass.
        if !self.separator_seen
            && self.history == PositionalHistory::None
            && phase == ArgumentPhase::Effective
            && behavior == PytestParserBehavior::Legacy
        {
            self.separator_seen = true;
        } else if phase == ArgumentPhase::Discovery
            && behavior != PytestParserBehavior::Current
            && self.history == PositionalHistory::OptionAfter
        {
            self.mode = ArgumentMode::LegacyUnknownFlags;
        } else {
            self.mode = ArgumentMode::Literal;
        }
    }

    fn positional(&mut self) {
        if self.history == PositionalHistory::None {
            self.history = PositionalHistory::Seen;
        }
    }

    fn option(&mut self) {
        if self.history != PositionalHistory::None {
            self.history = PositionalHistory::OptionAfter;
        }
    }
}

// Parse the prefix too: its positionals and separator change how argparse
// interprets the invocation, even when only invocation selections are wanted.
pub(super) fn parse_arguments_from(
    arguments: &[String],
    phase: ArgumentPhase,
    policy: PytestConfigPolicy,
    parser_behavior: PytestParserBehavior,
    start: usize,
) -> Arguments {
    let mut parsed = Arguments::default();
    let mut index = 0;
    let mut separator = SeparatorState::default();
    let mut prefix_discarded = start == 0;
    while index < arguments.len() {
        if !prefix_discarded && index >= start {
            parsed = Arguments::default();
            prefix_discarded = true;
        }
        let argument = &arguments[index];
        if separator.mode != ArgumentMode::Options {
            if separator.mode == ArgumentMode::LegacyUnknownFlags && argument.starts_with('-') {
                index += 1;
                continue;
            }
            separator.positional();
            parsed.targets.push(argument.clone());
            index += 1;
            continue;
        }
        let argument = ungroup_short_option(argument);
        let argument = argument.as_ref();
        parsed.collect_only |= matches!(argument, "--collect-only" | "--co" | "--collectonly");
        if let Some(selector) = selector(argument) {
            parsed.filtered_scope = true;
            parsed.invocation_selection |= selector == Selector::Invocation;
        }
        if argument == "--" {
            separator.consume(phase, parser_behavior);
            index += 1;
            continue;
        }
        if argument.starts_with('-') {
            separator.option();
        }
        if policy == PytestConfigPolicy::TomlWithAddoptsOverrides {
            let setting = match argument {
                "--strict-config" => Some("strict_config=true"),
                "--strict-markers" => Some("strict_markers=true"),
                "--strict" => Some("strict=true"),
                _ => None,
            };
            if let Some(setting) = setting {
                parsed.overrides.push(setting.to_owned());
                index += 1;
                continue;
            }
        }
        if let Some(value) = option_value(
            arguments,
            &mut index,
            argument,
            &["-c", "--config-file", "--inifile"],
        ) {
            parsed.config_file = Some(value);
            continue;
        }
        if let Some(value) = option_value(arguments, &mut index, argument, &["--rootdir"]) {
            parsed.rootdir = Some(value);
            continue;
        }
        if let Some(value) =
            option_value(arguments, &mut index, argument, &["-o", "--override-ini"])
        {
            parsed.overrides.push(value);
            continue;
        }
        if let Some(value) = option_value(arguments, &mut index, argument, &["--basetemp"]) {
            parsed.basetemp = Some(value);
            continue;
        }
        if pytest_worker_option(arguments, &mut index, argument, phase, &mut parsed) {
            continue;
        }
        if phase == ArgumentPhase::Effective
            && coverage_option(arguments, &mut index, argument, &mut parsed)
        {
            continue;
        }
        if matches!(argument, "--cache-show" | "--debug") {
            optional_argument_value(arguments, &mut index);
            continue;
        }
        if argument.starts_with('-') {
            if is_value_option(argument, phase) && index + 1 < arguments.len() {
                index += 2;
            } else {
                index += 1;
            }
            continue;
        }
        separator.positional();
        parsed.targets.push(argument.to_owned());
        index += 1;
    }
    if prefix_discarded {
        parsed
    } else {
        Arguments::default()
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Selector {
    /// Keyword, marker, ignore, and deselect filters give the same tests on
    /// every run, so config `addopts` can use them to define the suite.
    Static,
    /// Last-failed, stepwise, ordering, and collect-only options depend on
    /// cached state or do not execute the suite.
    Invocation,
}

fn selector(argument: &str) -> Option<Selector> {
    if !argument.starts_with("--") && (argument.starts_with("-k") || argument.starts_with("-m")) {
        return Some(Selector::Static);
    }
    let name = argument.split('=').next();
    if matches!(name, Some("--ignore" | "--ignore-glob" | "--deselect")) {
        return Some(Selector::Static);
    }
    matches!(
        name,
        Some(
            "--lf"
                | "--last-failed"
                | "--ff"
                | "--failed-first"
                | "--nf"
                | "--new-first"
                | "--sw"
                | "--stepwise"
                | "--sw-skip"
                | "--stepwise-skip"
                | "--sw-reset"
                | "--stepwise-reset"
                | "--collect-only"
                | "--co"
                | "--collectonly"
        )
    )
    .then_some(Selector::Invocation)
}

fn ungroup_short_option(argument: &str) -> Cow<'_, str> {
    let Some(options) = argument.strip_prefix('-') else {
        return Cow::Borrowed(argument);
    };
    // argparse stops a short-option group at its first value-taking option.
    // The remainder is that option's attached value, not another flag group.
    for (position, option) in options.char_indices() {
        match option {
            'c' | 'o' | 'k' | 'm' | 'p' | 'n' | 'r' | 'W' if position > 0 => {
                return Cow::Owned(format!("-{}", &options[position..]));
            }
            'x' | 'V' | 'h' | 'v' | 'q' | 'l' | 's' | 'd' | 'f' => {}
            _ => break,
        }
    }
    Cow::Borrowed(argument)
}

fn optional_argument_value<'a>(arguments: &'a [String], index: &mut usize) -> Option<&'a str> {
    *index += 1;
    let value = arguments
        .get(*index)
        .filter(|value| !value.starts_with('-'));
    *index += usize::from(value.is_some());
    value.map(String::as_str)
}

fn option_value(
    arguments: &[String],
    index: &mut usize,
    argument: &str,
    names: &[&str],
) -> Option<String> {
    for name in names {
        if argument == *name {
            let value = arguments.get(*index + 1).cloned();
            *index += if value.is_some() { 2 } else { 1 };
            return value;
        }
        if let Some(value) = argument.strip_prefix(&format!("{name}=")) {
            *index += 1;
            return Some(value.to_owned());
        }
        if !argument.starts_with("--") && !name.starts_with("--") && argument.starts_with(name) {
            *index += 1;
            return Some(argument[name.len()..].to_owned());
        }
    }
    None
}

fn is_value_option(argument: &str, phase: ArgumentPhase) -> bool {
    // Pytest discovers configuration before loading positive -p requests or
    // entry points. Their separate operands are unknown positional paths then.
    const PLUGIN_VALUE_OPTIONS: &[&str] = &[
        "-n",
        "--numprocesses",
        "--dist",
        "--html",
        "--cov-report",
        "--cov-config",
        "--cov-context",
        "--cov-fail-under",
        "--native-result-json",
    ];
    const VALUE_OPTIONS: &[&str] = &[
        "-k",
        "-m",
        "-p",
        "-r",
        "-W",
        "--maxfail",
        "--pdbcls",
        "--lfnf",
        "--last-failed-no-failures",
        "--capture",
        "--color",
        "--code-highlight",
        "--junitxml",
        "--junit-xml",
        "--junitprefix",
        "--junit-prefix",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "--confcutdir",
        "--import-mode",
        "--tb",
        "--show-capture",
        "--log-level",
        "--log-format",
        "--log-date-format",
        "--log-cli-level",
        "--log-cli-format",
        "--log-cli-date-format",
        "--log-file",
        "--log-file-mode",
        "--log-file-level",
        "--log-file-format",
        "--log-file-date-format",
        "--log-auto-indent",
        "--log-disable",
        "--verbosity",
        "--report-chars",
        "--pastebin",
        "--max-warnings",
        "--doctest-report",
        "--doctest-glob",
        "--pythonwarnings",
        "--durations",
        "--durations-min",
        "--assert",
    ];
    if PLUGIN_VALUE_OPTIONS.contains(&argument) {
        return phase == ArgumentPhase::Effective;
    }
    VALUE_OPTIONS.contains(&argument)
}
