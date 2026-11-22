//! Discover source mutations with the coverage analyzer's parser, locations,
//! exclusions, and source digest. Each mutation replaces one source range.

use std::collections::{BTreeMap, BTreeSet};

use pyo3::prelude::*;
use ruff_python_ast::token::{TokenKind, Tokens};
use ruff_python_ast::visitor::{Visitor, walk_expr, walk_stmt};
use ruff_python_ast::{BoolOp, CmpOp, Expr, Operator, Stmt, UnaryOp};
use ruff_text_size::{Ranged, TextRange, TextSize};
use vfy_core::SourceLocator;
use vfy_core::mutation::{ExecutionOwner, MutationPhase, MutationTimeoutPolicy, OwnerKind};

mod planning;
pub(crate) use planning::{
    collected_mutation_worker_limit, mutation_portable_worker_limit, mutation_test_preferences,
    next_mutation_job, order_mutation_jobs, plan_mutation_test_groups, plan_mutation_tests,
    rank_mutation_tests, select_mutation_catalog, select_mutation_sources,
};

type MutationRecord = (
    String,
    String,
    String,
    String,
    String,
    String,
    String,
    u32,
    u32,
    i32,
    i32,
    // PyO3 supports tuples up to twelve elements. Encode execution-owner
    // metadata in the final JSON field.
    String,
);

#[derive(Clone)]
struct Owner {
    kind: OwnerKind,
    qualname: String,
    range: TextRange,
    body_start: Option<TextSize>,
    fast_eligible: bool,
}

type Locator<'a> = &'a SourceLocator<'a>;

fn range_position(locator: Locator<'_>, range: TextRange) -> (i32, i32, i32, i32) {
    locator.range_position(range.start().to_usize()..range.end().to_usize())
}

fn has_exclusion(locator: Locator<'_>, line: i32) -> bool {
    let header = locator.line_text(line);
    header.contains("pragma: no cover")
        || header.contains("pragma: no branch")
        || header.contains("pragma: no decision")
}

fn suite_start(body: &[Stmt]) -> Option<TextSize> {
    body.first().map(Ranged::start)
}

fn statement_body_start(statement: &Stmt) -> Option<TextSize> {
    match statement {
        Stmt::FunctionDef(node) => suite_start(&node.body),
        Stmt::ClassDef(node) => suite_start(&node.body),
        Stmt::For(node) => suite_start(&node.body),
        Stmt::While(node) => suite_start(&node.body),
        Stmt::If(node) => suite_start(&node.body),
        Stmt::With(node) => suite_start(&node.body),
        Stmt::Match(node) => node.cases.first().and_then(|case| suite_start(&case.body)),
        Stmt::Try(node) => suite_start(&node.body),
        _ => None,
    }
}

fn has_exclusion_in_header(
    locator: Locator<'_>,
    range: TextRange,
    body_start: Option<TextSize>,
) -> bool {
    let first_line = locator.line(range.start().to_usize());
    let last_line = body_start
        .map_or_else(
            || locator.line(range.end().to_usize().saturating_sub(1)),
            |start| locator.line(start.to_usize()).saturating_sub(1),
        )
        .max(first_line);
    (first_line..=last_line).any(|line| has_exclusion(locator, line))
}

fn token_range(
    source: &str,
    tokens: &Tokens,
    left: TextSize,
    right: TextSize,
    token: &str,
) -> Option<TextRange> {
    let parts: Vec<_> = token.split_whitespace().collect();
    if parts.is_empty() {
        return None;
    }
    let trivia = |kind: TokenKind| {
        matches!(
            kind,
            TokenKind::Comment
                | TokenKind::Newline
                | TokenKind::NonLogicalNewline
                | TokenKind::Indent
                | TokenKind::Dedent
        )
    };
    let range = TextRange::new(left, right);
    let meaningful: Vec<_> = tokens
        .in_range(range)
        .iter()
        .filter(|item| !trivia(item.kind()))
        .filter_map(|item| {
            let text = source.get(item.start().to_usize()..item.end().to_usize())?;
            Some((text, item.range()))
        })
        .collect();
    for (index, (text, first_range)) in meaningful.iter().enumerate() {
        if *text != parts[0] {
            continue;
        }
        let Some(last) = meaningful.get(index + parts.len() - 1) else {
            continue;
        };
        if meaningful[index..index + parts.len()]
            .iter()
            .zip(parts.iter())
            .all(|((value, _), part)| *value == *part)
        {
            return Some(TextRange::new(first_range.start(), last.1.end()));
        }
    }
    None
}

fn integer_shift(source: &str, range: TextRange) -> Option<String> {
    let text = source
        .get(range.start().to_usize()..range.end().to_usize())?
        .trim();
    if text.is_empty()
        || text.contains(['.', 'e', 'E', 'j', 'J'])
        || text.starts_with(['"', '\''])
        || ["0x", "0X", "0o", "0O", "0b", "0B"]
            .iter()
            .any(|prefix| text.starts_with(prefix))
    {
        return None;
    }
    let value = text.replace('_', "").parse::<i128>().ok()?;
    let shifted = value.checked_add(1)?;
    Some(shifted.to_string())
}

fn cmp_alternatives(operator: CmpOp) -> [&'static str; 2] {
    match operator {
        CmpOp::Eq => ["!=", "=="],
        CmpOp::NotEq => ["==", "!="],
        CmpOp::Lt => [">=", "<="],
        CmpOp::LtE => [">", "<"],
        CmpOp::Gt => ["<=", ">="],
        CmpOp::GtE => ["<", ">"],
        CmpOp::Is => ["is not", "is"],
        CmpOp::IsNot => ["is", "is not"],
        CmpOp::In => ["not in", "in"],
        CmpOp::NotIn => ["in", "not in"],
    }
}

fn bin_alternative(operator: Operator) -> Option<&'static str> {
    match operator {
        Operator::Add => Some("-"),
        Operator::Sub => Some("+"),
        Operator::Mult | Operator::FloorDiv => Some("/"),
        Operator::Div | Operator::Mod => Some("*"),
        Operator::MatMult
        | Operator::Pow
        | Operator::LShift
        | Operator::RShift
        | Operator::BitOr
        | Operator::BitXor
        | Operator::BitAnd => None,
    }
}

struct Collector<'a> {
    path: &'a str,
    source: &'a str,
    tokens: &'a Tokens,
    digest: String,
    locator: Locator<'a>,
    mutations: BTreeMap<String, MutationRecord>,
    excluded_ranges: Vec<TextRange>,
    /// Function owners are kept as a stack while walking the AST. The
    /// outermost owner is the live code object that a forked
    /// interpreter can replace; nested functions are constants inside that
    /// code object.
    function_owners: Vec<Owner>,
    class_prefix: Vec<String>,
    class_fast_eligible: Vec<bool>,
}

impl Collector<'_> {
    fn owner_for(&self, range: TextRange) -> ExecutionOwner {
        let Some(owner) = self.function_owners.first() else {
            return ExecutionOwner {
                kind: OwnerKind::Module,
                qualname: String::new(),
                start: 0,
                end: self.source.len().try_into().unwrap_or(u32::MAX),
                body_start: None,
                fast_eligible: false,
                phase: MutationPhase::Definition,
            };
        };
        let in_body = owner
            .body_start
            .is_some_and(|start| range.start() >= start && range.end() <= owner.range.end());
        let in_current_body = self
            .function_owners
            .last()
            .and_then(|current| current.body_start)
            .is_some_and(|start| range.start() >= start);
        let all_function_owners_safe = self
            .function_owners
            .iter()
            .all(|current| current.fast_eligible);
        // A class statement nested in a callable executes its body while the
        // callable runs. Its code object is not the class method/function
        // owner, so keep that mutation on the portable path. A direct method
        // body is safe: its qualified name begins with the active class path.
        let in_nested_class_body = self.class_prefix.last().is_some_and(|class| {
            let prefix = format!("{}.", self.class_prefix.join("."));
            self.function_owners
                .first()
                .is_some_and(|enclosing| !enclosing.qualname.starts_with(&prefix))
                || (self.function_owners.is_empty() && !class.is_empty())
        });
        let in_active_class_callable = self.class_prefix.is_empty()
            || self.function_owners.last().is_some_and(|current| {
                let prefix = format!("{}.", self.class_prefix.join("."));
                current.qualname.starts_with(&prefix)
            });
        let phase = self
            .function_owners
            .last()
            .filter(|current| {
                in_active_class_callable
                    && current.body_start.is_some_and(|start| {
                        range.start() >= start && range.end() <= current.range.end()
                    })
            })
            .map_or(MutationPhase::Definition, |_| MutationPhase::Body);
        ExecutionOwner {
            kind: owner.kind.clone(),
            qualname: owner.qualname.clone(),
            start: owner.range.start().to_u32(),
            end: owner.range.end().to_u32(),
            body_start: owner.body_start.as_ref().map(TextSize::to_u32),
            fast_eligible: owner.fast_eligible
                && in_body
                && in_current_body
                && all_function_owners_safe
                && !in_nested_class_body,
            phase,
        }
    }

    fn function_owner(&self, statement: &Stmt) -> Option<Owner> {
        let Stmt::FunctionDef(node) = statement else {
            return None;
        };
        let mut qualname = self.class_prefix.join(".");
        if !qualname.is_empty() {
            qualname.push('.');
        }
        qualname.push_str(node.name.as_str());
        Some(Owner {
            kind: OwnerKind::Function,
            qualname,
            range: node.range,
            body_start: suite_start(&node.body),
            // Decorators can replace the live callable with an arbitrary
            // wrapper. Leave those to the portable subprocess path.
            fast_eligible: node.decorator_list.is_empty()
                && self.class_fast_eligible.iter().all(|eligible| *eligible),
        })
    }

    fn add(&mut self, range: TextRange, kind: &str, operator: &str, replacement: &str) {
        let line_excluded =
            has_exclusion(self.locator, self.locator.line(range.start().to_usize()));
        let parent_excluded = self
            .excluded_ranges
            .iter()
            .any(|parent| range.start() >= parent.start() && range.end() <= parent.end());
        if range.start() >= range.end() || line_excluded || parent_excluded {
            return;
        }
        let Some(original) = self
            .source
            .get(range.start().to_usize()..range.end().to_usize())
        else {
            return;
        };
        if original == replacement {
            return;
        }
        let (line, column, _end_line, _end_column) = range_position(self.locator, range);
        let owner = serde_json::json!(self.owner_for(range)).to_string();
        let key = vfy_core::mutation::MutationIdentity {
            path: self.path,
            digest: &self.digest,
            kind,
            operator,
            start: range.start().to_u32(),
            end: range.end().to_u32(),
            replacement,
        }
        .key();
        self.mutations.insert(
            key.clone(),
            (
                key,
                self.path.to_owned(),
                self.digest.clone(),
                kind.to_owned(),
                operator.to_owned(),
                original.to_owned(),
                replacement.to_owned(),
                range.start().to_u32(),
                range.end().to_u32(),
                line,
                column,
                owner,
            ),
        );
    }

    fn add_token(
        &mut self,
        left: TextSize,
        right: TextSize,
        token: &str,
        kind: &str,
        operator: &str,
        replacement: &str,
    ) {
        if let Some(range) = token_range(self.source, self.tokens, left, right, token) {
            self.add(range, kind, operator, replacement);
        }
    }

    fn add_number(&mut self, range: TextRange) {
        if let Some(replacement) = integer_shift(self.source, range) {
            self.add(range, "value", "integer_plus_one", &replacement);
        }
    }
}

impl<'a> Visitor<'a> for Collector<'a> {
    fn visit_stmt(&mut self, statement: &'a Stmt) {
        let excluded = has_exclusion_in_header(
            self.locator,
            statement.range(),
            statement_body_start(statement),
        );
        if excluded {
            self.excluded_ranges.push(statement.range());
        }
        let function_owner = self.function_owner(statement);
        let has_function_owner = function_owner.is_some();
        if let Stmt::ClassDef(node) = statement {
            self.class_prefix.push(node.name.to_string());
            self.class_fast_eligible
                .push(node.decorator_list.is_empty());
        }
        if let Some(owner) = function_owner {
            self.function_owners.push(owner);
        }
        match statement {
            Stmt::Return(node) => {
                if let Some(value) = &node.value {
                    let range = value.range();
                    match value.as_ref() {
                        Expr::BooleanLiteral(literal) => self.add(
                            range,
                            "value",
                            "boolean_flip",
                            if literal.value { "False" } else { "True" },
                        ),
                        Expr::NumberLiteral(_) => self.add_number(range),
                        Expr::NoneLiteral(_) => {}
                        _ => self.add(range, "return", "return_none", "None"),
                    }
                }
            }
            Stmt::Raise(node) => self.add(node.range(), "failure", "raise_to_pass", "pass"),
            Stmt::Expr(node) if matches!(node.value.as_ref(), Expr::Call(_)) => {
                self.add(node.value.range(), "effect", "remove_call", "pass");
            }
            Stmt::Break(node) => self.add(node.range(), "control", "break_to_continue", "continue"),
            Stmt::Continue(node) => self.add(node.range(), "control", "continue_to_break", "break"),
            _ => {}
        }
        walk_stmt(self, statement);
        if has_function_owner {
            self.function_owners.pop();
        }
        if matches!(statement, Stmt::ClassDef(_)) {
            self.class_prefix.pop();
            self.class_fast_eligible.pop();
        }
        if excluded {
            self.excluded_ranges.pop();
        }
    }

    fn visit_elif_else_clause(&mut self, clause: &'a ruff_python_ast::ElifElseClause) {
        let excluded =
            has_exclusion_in_header(self.locator, clause.range, suite_start(&clause.body));
        if excluded {
            self.excluded_ranges.push(clause.range);
        }
        ruff_python_ast::visitor::walk_elif_else_clause(self, clause);
        if excluded {
            self.excluded_ranges.pop();
        }
    }

    fn visit_expr(&mut self, expression: &'a Expr) {
        match expression {
            Expr::BoolOp(node) => {
                let token = node.op.as_str();
                let replacement = match node.op {
                    BoolOp::And => "or",
                    BoolOp::Or => "and",
                };
                for pair in node.values.windows(2) {
                    self.add_token(
                        pair[0].range().end(),
                        pair[1].range().start(),
                        token,
                        "predicate",
                        "boolean_operator",
                        replacement,
                    );
                }
            }
            Expr::UnaryOp(node) if matches!(node.op, UnaryOp::Not) => {
                self.add_token(
                    node.range().start(),
                    node.operand.range().start(),
                    "not",
                    "predicate",
                    "remove_not",
                    "",
                );
            }
            Expr::BinOp(node) => {
                if let Some(replacement) = bin_alternative(node.op) {
                    self.add_token(
                        node.left.range().end(),
                        node.right.range().start(),
                        node.op.as_str(),
                        "computation",
                        "binary_operator",
                        replacement,
                    );
                }
            }
            Expr::Compare(node) => {
                let mut left_end = node.left.range().end();
                for (index, (operator, comparator)) in
                    node.ops.iter().zip(node.comparators.iter()).enumerate()
                {
                    let token = operator.as_str();
                    let alternatives = cmp_alternatives(*operator);
                    self.add_token(
                        left_end,
                        comparator.range().start(),
                        token,
                        "predicate",
                        if index == 0 {
                            "comparison_negation"
                        } else {
                            "comparison_chain"
                        },
                        alternatives[0],
                    );
                    if matches!(operator, CmpOp::Lt | CmpOp::LtE | CmpOp::Gt | CmpOp::GtE) {
                        self.add_token(
                            left_end,
                            comparator.range().start(),
                            token,
                            "predicate",
                            "comparison_boundary",
                            alternatives[1],
                        );
                    }
                    left_end = comparator.range().end();
                }
            }
            Expr::BooleanLiteral(node) => self.add(
                node.range(),
                "value",
                "boolean_flip",
                if node.value { "False" } else { "True" },
            ),
            Expr::NumberLiteral(node) => self.add_number(node.range()),
            _ => {}
        }
        walk_expr(self, expression);
    }
}

pub(crate) fn analyze_source(path: &str, source: &str) -> Vec<MutationRecord> {
    let locator = SourceLocator::new(source);
    let parsed = match ruff_python_parser::parse_module(source) {
        Ok(parsed) if parsed.errors().is_empty() => parsed,
        _ => return Vec::new(),
    };
    let mut collector = Collector {
        path,
        source,
        digest: vfy_core::mutation::source_hash(source),
        tokens: parsed.tokens(),
        locator: &locator,
        mutations: BTreeMap::new(),
        excluded_ranges: Vec::new(),
        function_owners: Vec::new(),
        class_prefix: Vec::new(),
        class_fast_eligible: Vec::new(),
    };
    Visitor::visit_body(&mut collector, parsed.suite());
    let mut records: Vec<_> = collector.mutations.into_values().collect();
    records.sort_by(|left, right| {
        left.1
            .cmp(&right.1)
            .then(left.7.cmp(&right.7))
            .then(left.8.cmp(&right.8))
            .then(left.4.cmp(&right.4))
            .then(left.6.cmp(&right.6))
    });
    records
}

#[pyfunction]
pub(crate) fn analyze_mutations(path: &str) -> PyResult<Vec<MutationRecord>> {
    let source = std::fs::read_to_string(path).map_err(|error| {
        pyo3::exceptions::PyOSError::new_err(format!("failed to read {path}: {error}"))
    })?;
    Ok(analyze_source(path, &source))
}

/// Analyze source files in one batch, deduplicating paths and sorting results.
#[pyfunction]
pub(crate) fn analyze_mutation_files(paths: Vec<String>) -> PyResult<Vec<MutationRecord>> {
    let mut records = Vec::new();
    // Normalize before deduplicating so a caller cannot accidentally analyze
    // one file twice by spelling it once relatively and once absolutely.
    let mut unique_paths = BTreeSet::new();
    for path in paths {
        let canonical = std::fs::canonicalize(&path).map_err(|error| {
            pyo3::exceptions::PyOSError::new_err(format!("failed to read {path}: {error}"))
        })?;
        unique_paths.insert(canonical.to_string_lossy().into_owned());
    }
    for path in unique_paths {
        let source = std::fs::read_to_string(&path).map_err(|error| {
            pyo3::exceptions::PyOSError::new_err(format!("failed to read {path}: {error}"))
        })?;
        records.extend(analyze_source(&path, &source));
    }
    records.sort_by(|left, right| {
        left.1
            .cmp(&right.1)
            .then(left.7.cmp(&right.7))
            .then(left.8.cmp(&right.8))
            .then(left.4.cmp(&right.4))
            .then(left.6.cmp(&right.6))
    });
    Ok(records)
}

#[cfg(test)]
mod tests;

#[pyfunction]
#[pyo3(signature = (path, fingerprint, mutation_fingerprints=None))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts one fingerprint map for the cache operation."
)]
pub(crate) fn read_mutation_cache(
    path: &str,
    fingerprint: &str,
    mutation_fingerprints: Option<BTreeMap<String, String>>,
) -> PyResult<String> {
    let results = vfy_core::mutation::read_mutation_cache(
        std::path::Path::new(path),
        fingerprint,
        mutation_fingerprints.as_ref(),
    )
    .map_err(pyo3::exceptions::PyOSError::new_err)?;
    serde_json::to_string(&results)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))
}

#[pyfunction]
#[pyo3(signature = (path, report, mutation_fingerprints=None))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts one fingerprint map for the cache operation."
)]
pub(crate) fn write_mutation_cache(
    path: &str,
    report: &str,
    mutation_fingerprints: Option<BTreeMap<String, String>>,
) -> PyResult<()> {
    vfy_core::mutation::write_mutation_cache(
        std::path::Path::new(path),
        report,
        mutation_fingerprints.as_ref(),
    )
    .map_err(pyo3::exceptions::PyOSError::new_err)
}

#[pyfunction]
pub(crate) fn build_mutation_report(
    fingerprint: &str,
    results: &str,
    metrics: &str,
    scope: Option<&str>,
) -> PyResult<String> {
    vfy_core::mutation::build_mutation_report(fingerprint, results, metrics, scope)
        .map_err(pyo3::exceptions::PyValueError::new_err)
}

#[pyfunction]
pub(crate) fn render_mutation_report(
    py: Python<'_>,
    report: &str,
    output_format: &str,
    pretty: bool,
    fail_on_survivor: bool,
) -> PyResult<(String, i32)> {
    // Use CPython repr so quoted source follows the active interpreter's Unicode rules.
    vfy_core::mutation::render_mutation_report(
        report,
        output_format,
        pretty,
        fail_on_survivor,
        |value| {
            pyo3::types::PyString::new(py, value)
                .repr()
                .and_then(|value| value.extract::<String>())
                .map_err(|error| error.to_string())
        },
    )
    .map_err(pyo3::exceptions::PyValueError::new_err)
}

#[pyfunction]
#[pyo3(signature = (roots, include_tests=false))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "Owned filesystem roots are extracted as one batch."
)]
pub(crate) fn mutation_sources(roots: Vec<String>, include_tests: bool) -> Vec<String> {
    vfy_core::mutation::mutation_sources(&roots, include_tests)
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts one complete test invocation."
)]
pub(crate) fn mutation_suite_state(
    py: Python<'_>,
    tests: Vec<String>,
    arguments: Vec<String>,
) -> PyResult<Vec<(String, String)>> {
    let root = std::env::current_dir()
        .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))?;
    vfy_core::mutation::mutation_suite_state(&root, &tests, &arguments)
        .into_iter()
        .map(|(path, digest)| format_file_digest(py, &path, digest).map(|digest| (path, digest)))
        .collect()
}

#[pyfunction]
pub(crate) fn mutation_file_digest(py: Python<'_>, path: &str) -> PyResult<String> {
    format_file_digest(
        py,
        path,
        vfy_core::mutation::mutation_file_digest(std::path::Path::new(path)),
    )
}

fn format_file_digest(
    py: Python<'_>,
    path: &str,
    digest: std::io::Result<String>,
) -> PyResult<String> {
    match digest {
        Ok(digest) => Ok(digest),
        Err(error) => {
            let mut code = error.raw_os_error().unwrap_or(5);
            let message = error.to_string();
            let suffix = format!(" (os error {code})");
            let mut message = message.strip_suffix(&suffix).unwrap_or(&message).to_owned();
            if cfg!(windows) {
                // Rust reports Win32 codes; Python file IO reports their CRT errno mapping.
                let translated =
                    pyo3::exceptions::PyOSError::new_err((0, "", path.to_owned(), code));
                code = translated.value(py).getattr("errno")?.extract()?;
                message = py
                    .import("os")?
                    .getattr("strerror")?
                    .call1((code,))?
                    .extract()?;
            }
            // Normalize the same OSError subclass and filename repr as Path.read_bytes.
            let exception = pyo3::exceptions::PyOSError::new_err((code, message, path.to_owned()));
            let value = exception.value(py);
            Ok(format!(
                "unreadable:{}:{}",
                value.get_type().name()?,
                value.str()?
            ))
        }
    }
}

fn baseline_request(py: Python<'_>, request: &str) -> PyResult<String> {
    let mut value: serde_json::Value = serde_json::from_str(request)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
    if let Some(path) = value.get("data_file").and_then(serde_json::Value::as_str) {
        let digest = mutation_file_digest(py, path)?;
        value["data_hash"] = serde_json::Value::String(digest);
    }
    serde_json::to_string(&value)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts the coverage-data handle by value."
)]
pub(crate) fn mutation_contexts(
    data: PyRef<'_, crate::data::CoverageData>,
) -> (BTreeMap<String, BTreeSet<String>>, BTreeSet<String>) {
    vfy_core::mutation::mutation_contexts(data.core_snapshot().as_ref())
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts the coverage-data handle by value."
)]
pub(crate) fn mutation_executed_paths(
    data: PyRef<'_, crate::data::CoverageData>,
) -> BTreeSet<String> {
    vfy_core::mutation::mutation_executed_paths(data.core_snapshot().as_ref())
}

#[pyfunction]
pub(crate) fn mutation_test_durations(path: &str) -> BTreeMap<String, f64> {
    crate::selection::read_history(std::path::Path::new(path))
        .durations
        .into_iter()
        .filter(|(_, duration)| *duration > 0.0)
        .collect()
}

#[pyfunction]
pub(crate) fn mutation_fingerprints(request: &str) -> PyResult<(String, BTreeMap<String, String>)> {
    vfy_core::mutation::mutation_fingerprints(request)
        .map_err(pyo3::exceptions::PyValueError::new_err)
}

#[pyfunction]
pub(crate) fn mutation_baseline_valid(py: Python<'_>, path: &str, request: &str) -> PyResult<bool> {
    vfy_core::mutation::mutation_baseline_valid(
        std::path::Path::new(path),
        &baseline_request(py, request)?,
    )
    .map_err(pyo3::exceptions::PyValueError::new_err)
}

#[pyfunction]
pub(crate) fn write_mutation_baseline(py: Python<'_>, path: &str, request: &str) -> PyResult<()> {
    vfy_core::mutation::write_mutation_baseline(
        std::path::Path::new(path),
        &baseline_request(py, request)
            .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))?,
    )
    .map_err(pyo3::exceptions::PyOSError::new_err)
}

#[pyfunction]
#[pyo3(signature = (tests, durations, ceiling, policy="adaptive"))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts one watchdog request."
)]
pub(crate) fn mutation_timeout(
    tests: Vec<String>,
    durations: BTreeMap<String, f64>,
    ceiling: f64,
    policy: &str,
) -> PyResult<f64> {
    let policy = match policy {
        "adaptive" => MutationTimeoutPolicy::Adaptive,
        "fixed" => MutationTimeoutPolicy::Fixed,
        other => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "invalid mutation timeout policy {other:?}; expected 'adaptive' or 'fixed'"
            )));
        }
    };
    Ok(vfy_core::mutation::mutation_timeout(
        &tests, &durations, ceiling, policy,
    ))
}

#[pyfunction]
pub(crate) fn mutation_baseline_timeout(timeout: f64) -> PyResult<f64> {
    vfy_core::mutation::mutation_baseline_timeout(timeout)
        .map_err(pyo3::exceptions::PyValueError::new_err)
}
