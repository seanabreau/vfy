use std::collections::{BTreeMap, BTreeSet};

use crate::SourceLocator;
use ruff_python_ast::token::TokenKind;
use ruff_python_ast::visitor::{Visitor, walk_expr, walk_stmt};
use ruff_python_ast::{BoolOp, ExceptHandler, Expr, Stmt, StmtTry};
use ruff_text_size::{Ranged, TextRange};
use sha2::{Digest, Sha256};

use crate::Logic;
use crate::{
    DecisionDefinition, PathDecisionDefinition, PathDecisionKind, PathOutcomeDefinition,
    PathOutcomeRole,
};

type Locator<'a> = &'a SourceLocator<'a>;

trait RuffRangePosition {
    fn position(&self, range: TextRange) -> (i32, i32, i32, i32);
}

impl RuffRangePosition for SourceLocator<'_> {
    fn position(&self, range: TextRange) -> (i32, i32, i32, i32) {
        self.range_position(range.start().to_usize()..range.end().to_usize())
    }
}

fn logic_for_expression(expression: &Expr, next: &mut usize) -> Logic {
    match expression {
        Expr::BoolOp(node) => {
            let values = node
                .values
                .iter()
                .map(|value| logic_for_expression(value, next))
                .collect();
            match node.op {
                BoolOp::And => Logic::And(values),
                BoolOp::Or => Logic::Or(values),
            }
        }
        Expr::UnaryOp(node) if node.op.is_not() => {
            Logic::Not(Box::new(logic_for_expression(&node.operand, next)))
        }
        Expr::Compare(node) if node.ops.len() > 1 => {
            let values = node
                .ops
                .iter()
                .enumerate()
                .map(|_| {
                    let index = *next;
                    *next += 1;
                    Logic::Condition(index)
                })
                .collect();
            Logic::And(values)
        }
        _ => {
            let index = *next;
            *next += 1;
            Logic::Condition(index)
        }
    }
}

fn condition_ranges(
    expression: &Expr,
    locator: Locator<'_>,
    ranges: &mut Vec<(i32, i32, i32, i32)>,
) {
    match expression {
        Expr::BoolOp(node) => node
            .values
            .iter()
            .for_each(|value| condition_ranges(value, locator, ranges)),
        Expr::UnaryOp(node) if node.op.is_not() => {
            condition_ranges(&node.operand, locator, ranges);
        }
        Expr::Compare(node) if node.ops.len() > 1 => {
            // CPython uses the complete comparison range for each chained
            // comparison jump. Keep the ranges identical and select the
            // atomic condition by the Rust branch ordinal.
            let position = locator.position(node.range());
            ranges.extend(std::iter::repeat_n(position, node.ops.len()));
        }
        _ => ranges.push(locator.position(expression.range())),
    }
}

fn decision(
    path: &str,
    source: &str,
    locator: Locator<'_>,
    expression: &Expr,
    kind: &str,
    range: TextRange,
    ordinal: usize,
) -> DecisionDefinition {
    let line = locator.line(range.start().to_usize());
    let end_line = locator.line(range.end().to_usize().saturating_sub(1));
    let mut next = 0;
    let logic = logic_for_expression(expression, &mut next);
    let mut condition_range_values = Vec::new();
    condition_ranges(expression, locator, &mut condition_range_values);
    let condition_line_values = condition_range_values
        .iter()
        .map(|(line, _, _, _)| *line)
        .collect();
    let expression_text = source
        .get(range.start().to_usize()..range.end().to_usize())
        .unwrap_or("")
        .trim()
        .to_owned();
    let (key, digest) = semantic_key(path, source, kind, range, ordinal);
    DecisionDefinition {
        key,
        path: path.to_owned(),
        line,
        end_line,
        kind: kind.to_owned(),
        source_hash: digest,
        expression: expression_text,
        logic,
        condition_lines: condition_line_values,
        condition_ranges: condition_range_values,
    }
}

fn source_hash(source: &str) -> String {
    hex_digest(&Sha256::digest(source.as_bytes()))
}

fn hex_digest(bytes: &[u8]) -> String {
    use std::fmt::Write;

    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        write!(output, "{byte:02x}").expect("writing to a String cannot fail");
    }
    output
}

struct Collector<'a> {
    path: &'a str,
    source: &'a str,
    locator: Locator<'a>,
    excluded_lines: &'a BTreeSet<i32>,
    definitions: Vec<DecisionDefinition>,
    ordinal: usize,
}

impl Collector<'_> {
    fn add(&mut self, expression: &Expr, kind: &str, range: TextRange) {
        let ordinal = self.ordinal;
        self.ordinal = self.ordinal.saturating_add(1);
        if excluded(self.locator, self.excluded_lines, range) {
            return;
        }
        let definition = decision(
            self.path,
            self.source,
            self.locator,
            expression,
            kind,
            range,
            ordinal,
        );
        self.definitions.push(definition);
    }
}

impl<'a> Visitor<'a> for Collector<'a> {
    fn visit_stmt(&mut self, statement: &'a Stmt) {
        match statement {
            Stmt::If(node) => self.add(&node.test, "if", node.test.range()),
            Stmt::While(node) => self.add(&node.test, "while", node.test.range()),
            Stmt::Assert(node) => self.add(&node.test, "assert", node.test.range()),
            _ => {}
        }
        walk_stmt(self, statement);
    }

    fn visit_elif_else_clause(&mut self, clause: &'a ruff_python_ast::ElifElseClause) {
        if let Some(test) = &clause.test {
            self.add(test, "elif", test.range());
        }
        ruff_python_ast::visitor::walk_elif_else_clause(self, clause);
    }

    fn visit_match_case(&mut self, case: &'a ruff_python_ast::MatchCase) {
        if let Some(guard) = &case.guard {
            self.add(guard, "match_guard", guard.range());
        }
        ruff_python_ast::visitor::walk_match_case(self, case);
    }

    fn visit_expr(&mut self, expression: &'a Expr) {
        if let Expr::If(node) = expression {
            self.add(&node.test, "if_expr", node.range);
        }
        walk_expr(self, expression);
    }

    fn visit_comprehension(&mut self, comprehension: &'a ruff_python_ast::Comprehension) {
        for expression in &comprehension.ifs {
            self.add(expression, "comprehension", expression.range());
        }
        ruff_python_ast::visitor::walk_comprehension(self, comprehension);
    }
}

fn semantic_key(
    path: &str,
    source: &str,
    kind: &str,
    range: TextRange,
    ordinal: usize,
) -> (String, String) {
    let digest = source_hash(source);
    let mut hasher = Sha256::new();
    hasher.update(digest.as_bytes());
    hasher.update(b"\0");
    hasher.update(kind.as_bytes());
    hasher.update(b"\0");
    hasher.update(range.start().to_u32().to_le_bytes());
    hasher.update(range.end().to_u32().to_le_bytes());
    hasher.update(ordinal.to_le_bytes());
    (
        file_scoped_key(path, &hex_digest(&hasher.finalize())),
        digest,
    )
}

fn file_scoped_key(path: &str, digest: &str) -> String {
    // Keep the file namespace separate so path aliases can update it without
    // changing the source-and-location identity shared by matching shards.
    format!("vfy:{}:{digest}", hex_digest(&Sha256::digest(path)))
}

pub(super) fn remap_semantic_key(
    key: &str,
    path: &str,
    namespaces: &mut BTreeMap<String, String>,
) -> String {
    let Some(identity) = key.strip_prefix("vfy:") else {
        return key.to_owned();
    };
    let is_digest = |value: &str| {
        value.len() == 64
            && value
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    };
    let digest = match identity.split_once(':') {
        Some((namespace, digest)) if is_digest(namespace) && is_digest(digest) => digest,
        None if is_digest(identity) => identity,
        _ => return key.to_owned(),
    };
    if let Some(namespace) = namespaces.get(path) {
        return format!("vfy:{namespace}:{digest}");
    }
    let namespace = hex_digest(&Sha256::digest(path));
    let remapped = format!("vfy:{namespace}:{digest}");
    namespaces.insert(path.to_owned(), namespace);
    remapped
}

fn excluded(locator: Locator<'_>, excluded_lines: &BTreeSet<i32>, range: TextRange) -> bool {
    excluded_lines.contains(&locator.line(range.start().to_usize()))
}

fn suite_lines(suite: &[Stmt], locator: Locator<'_>, fallback: TextRange) -> (i32, i32) {
    let start = suite.first().map_or(fallback.start(), Ranged::start);
    let end = suite.last().map_or(fallback.end(), Ranged::end);
    (
        locator.line(start.to_usize()),
        locator.line(end.to_usize().saturating_sub(1)),
    )
}

#[derive(Default)]
struct AwaitFinder {
    found: bool,
}

impl<'a> Visitor<'a> for AwaitFinder {
    fn visit_expr(&mut self, expression: &'a Expr) {
        if matches!(expression, Expr::Await(_)) {
            self.found = true;
            return;
        }
        walk_expr(self, expression);
    }
}

fn contains_await(suite: &[Stmt]) -> bool {
    let mut finder = AwaitFinder::default();
    Visitor::visit_body(&mut finder, suite);
    finder.found
}

fn handler_text(handler: &ExceptHandler) -> Option<&Expr> {
    let ExceptHandler::ExceptHandler(handler) = handler;
    handler.type_.as_deref()
}

fn cancellation_handler(
    handler: &ExceptHandler,
    source: &str,
    cancellation_aliases: &BTreeSet<String>,
    base_aliases: &BTreeSet<String>,
) -> bool {
    let Some(expression) = handler_text(handler) else {
        return true;
    };
    let text = source
        .get(expression.range().start().to_usize()..expression.range().end().to_usize())
        .unwrap_or("");
    text.split(|character: char| !(character.is_alphanumeric() || character == '_'))
        .any(|name| cancellation_aliases.contains(name) || base_aliases.contains(name))
}

struct ControlRanges {
    ranges: Vec<TextRange>,
}

impl<'a> Visitor<'a> for ControlRanges {
    fn visit_stmt(&mut self, statement: &'a Stmt) {
        match statement {
            Stmt::If(node) => self.ranges.push(node.test.range()),
            Stmt::While(node) => self.ranges.push(node.test.range()),
            Stmt::Assert(node) => self.ranges.push(node.test.range()),
            _ => {}
        }
        walk_stmt(self, statement);
    }

    fn visit_elif_else_clause(&mut self, clause: &'a ruff_python_ast::ElifElseClause) {
        if let Some(test) = &clause.test {
            self.ranges.push(test.range());
        }
        ruff_python_ast::visitor::walk_elif_else_clause(self, clause);
    }

    fn visit_match_case(&mut self, case: &'a ruff_python_ast::MatchCase) {
        if let Some(guard) = &case.guard {
            self.ranges.push(guard.range());
        }
        ruff_python_ast::visitor::walk_match_case(self, case);
    }

    fn visit_expr(&mut self, expression: &'a Expr) {
        if let Expr::If(node) = expression {
            self.ranges.push(node.test.range());
        }
        walk_expr(self, expression);
    }

    fn visit_comprehension(&mut self, comprehension: &'a ruff_python_ast::Comprehension) {
        self.ranges
            .extend(comprehension.ifs.iter().map(Ranged::range));
        ruff_python_ast::visitor::walk_comprehension(self, comprehension);
    }
}

struct PathCollector<'a> {
    path: &'a str,
    source: &'a str,
    locator: Locator<'a>,
    excluded_lines: &'a BTreeSet<i32>,
    control_ranges: &'a [TextRange],
    cancellation_aliases: &'a BTreeSet<String>,
    base_aliases: &'a BTreeSet<String>,
    definitions: Vec<PathDecisionDefinition>,
    ordinal: usize,
}

impl PathCollector<'_> {
    fn add(
        &mut self,
        kind: PathDecisionKind,
        range: TextRange,
        body: (i32, i32),
        outcomes: Vec<PathOutcomeDefinition>,
    ) {
        let ordinal = self.ordinal;
        self.ordinal = self.ordinal.saturating_add(1);
        if excluded(self.locator, self.excluded_lines, range) || outcomes.len() < 2 {
            return;
        }
        let (key, source_hash) =
            semantic_key(self.path, self.source, kind.as_str(), range, ordinal);
        self.definitions.push(PathDecisionDefinition {
            key,
            path: self.path.to_owned(),
            line: self.locator.line(range.start().to_usize()),
            end_line: self.locator.line(range.end().to_usize().saturating_sub(1)),
            kind,
            source_hash,
            expression: self
                .source
                .get(range.start().to_usize()..range.end().to_usize())
                .unwrap_or("")
                .trim()
                .to_owned(),
            body_start: body.0,
            body_end: body.1,
            range: self.locator.position(range),
            outcomes,
        });
    }

    fn add_try(&mut self, node: &StmtTry) {
        let mut outcomes = vec![PathOutcomeDefinition {
            label: "normal completion".to_owned(),
            range: self.locator.position(node.range),
            role: PathOutcomeRole::Normal,
        }];
        let mut catches_all = false;
        for (index, handler) in node.handlers.iter().enumerate() {
            let ExceptHandler::ExceptHandler(handler) = handler;
            let type_text = handler.type_.as_deref().map_or("bare", |expression| {
                self.source
                    .get(expression.range().start().to_usize()..expression.range().end().to_usize())
                    .unwrap_or("exception")
            });
            let tokens: BTreeSet<&str> = type_text
                .split(|character: char| !(character.is_alphanumeric() || character == '_'))
                .collect();
            let resolved_cancel = tokens
                .iter()
                .any(|name| self.cancellation_aliases.contains(*name));
            let resolved_base = tokens.iter().any(|name| self.base_aliases.contains(*name));
            catches_all |= handler.type_.is_none() || resolved_base;
            let resolved = match (
                resolved_cancel && !tokens.contains("CancelledError"),
                resolved_base && !tokens.contains("BaseException"),
            ) {
                (true, true) => " (CancelledError, BaseException)",
                (true, false) => " (CancelledError)",
                (false, true) => " (BaseException)",
                (false, false) => "",
            };
            outcomes.push(PathOutcomeDefinition {
                label: format!("handler {} {type_text}{resolved}", index + 1),
                range: self.locator.position(handler.range),
                role: PathOutcomeRole::Handler {
                    body: suite_lines(&handler.body, self.locator, handler.range),
                },
            });
        }
        if !catches_all {
            outcomes.push(PathOutcomeDefinition {
                label: "unhandled exception".to_owned(),
                range: self.locator.position(node.range),
                role: PathOutcomeRole::Unhandled,
            });
        }
        let body = suite_lines(&node.body, self.locator, node.range);
        self.add(
            if node.is_star {
                PathDecisionKind::TryStar
            } else {
                PathDecisionKind::Try
            },
            node.range,
            body,
            outcomes,
        );

        if contains_await(&node.body)
            && (!node.finalbody.is_empty()
                || node.handlers.iter().any(|handler| {
                    cancellation_handler(
                        handler,
                        self.source,
                        self.cancellation_aliases,
                        self.base_aliases,
                    )
                }))
        {
            self.add_cancellation(node.range, body);
        }
    }

    fn add_cancellation(&mut self, range: TextRange, body: (i32, i32)) {
        self.add(
            PathDecisionKind::AsyncCancellation,
            range,
            body,
            vec![
                PathOutcomeDefinition {
                    label: "normal completion".to_owned(),
                    range: self.locator.position(range),
                    role: PathOutcomeRole::Normal,
                },
                PathOutcomeDefinition {
                    label: "cancelled".to_owned(),
                    range: self.locator.position(range),
                    role: PathOutcomeRole::Cancelled,
                },
            ],
        );
    }

    fn add_match(&mut self, node: &ruff_python_ast::StmtMatch) {
        let mut outcomes = Vec::with_capacity(node.cases.len() + 1);
        for (index, case) in node.cases.iter().enumerate() {
            let pattern = self
                .source
                .get(case.pattern.range().start().to_usize()..case.pattern.range().end().to_usize())
                .unwrap_or("")
                .trim();
            let body = suite_lines(&case.body, self.locator, case.range);
            outcomes.push(PathOutcomeDefinition {
                label: format!("case {} selected: {pattern}", index + 1),
                range: self.locator.position(case.pattern.range()),
                role: PathOutcomeRole::MatchCase { body },
            });
        }
        // A guarded wildcard/capture is not exhaustive: its guard may fail and
        // execution can still leave the match without selecting a case.
        let exhaustive = node
            .cases
            .iter()
            .any(|case| case.guard.is_none() && case.pattern.is_irrefutable());
        if !exhaustive {
            outcomes.push(PathOutcomeDefinition {
                label: "no case matched".to_owned(),
                range: self.locator.position(node.range),
                role: PathOutcomeRole::NoMatch,
            });
        }
        let body = (
            self.locator.line(node.range.start().to_usize()),
            self.locator
                .line(node.range.end().to_usize().saturating_sub(1)),
        );
        self.add(PathDecisionKind::Match, node.range, body, outcomes);
    }
}

impl<'a> Visitor<'a> for PathCollector<'a> {
    fn visit_stmt(&mut self, statement: &'a Stmt) {
        match statement {
            Stmt::Try(node) => self.add_try(node),
            Stmt::Match(node) => self.add_match(node),
            Stmt::With(node) if node.is_async => {
                let body = suite_lines(&node.body, self.locator, node.range);
                self.add_cancellation(node.range, body);
            }
            _ => {}
        }
        walk_stmt(self, statement);
    }

    fn visit_expr(&mut self, expression: &'a Expr) {
        if let Expr::BoolOp(node) = expression
            && !self
                .control_ranges
                .iter()
                .any(|range| range.start() <= node.range.start() && range.end() >= node.range.end())
        {
            let outcomes = node
                .values
                .iter()
                .enumerate()
                .map(|(index, value)| PathOutcomeDefinition {
                    label: format!("operand {} selected", index + 1),
                    range: self.locator.position(value.range()),
                    role: PathOutcomeRole::Operand,
                })
                .collect();
            self.add(
                match node.op {
                    BoolOp::And => PathDecisionKind::ValueAnd,
                    BoolOp::Or => PathDecisionKind::ValueOr,
                },
                node.range,
                (
                    self.locator.line(node.range.start().to_usize()),
                    self.locator
                        .line(node.range.end().to_usize().saturating_sub(1)),
                ),
                outcomes,
            );
        }
        walk_expr(self, expression);
    }
}

/// Discover semantic decisions in a Python source file.
///
/// # Errors
///
/// Returns the source path and parser diagnostic when the source cannot be parsed.
pub fn analyze_semantic_source_data(
    path: &str,
    source: &str,
) -> Result<(Vec<DecisionDefinition>, Vec<PathDecisionDefinition>), crate::CoreError> {
    let locator = SourceLocator::new(source);
    let parsed = match ruff_python_parser::parse_module(source) {
        Ok(parsed) if parsed.errors().is_empty() => parsed,
        Ok(parsed) => {
            let detail = parsed
                .errors()
                .first()
                .map_or_else(|| "invalid syntax".to_owned(), ToString::to_string);
            return Err(crate::CoreError::InvalidInput(format!(
                "failed to parse semantic source {path}: {detail}"
            )));
        }
        Err(error) => {
            return Err(crate::CoreError::InvalidInput(format!(
                "failed to parse semantic source {path}: {error}"
            )));
        }
    };
    let excluded_lines = parsed
        .tokens()
        .iter()
        .filter(|token| token.kind() == TokenKind::Comment)
        .filter_map(|token| {
            let comment = &source[token.start().to_usize()..token.end().to_usize()];
            (comment.contains("pragma: no cover")
                || comment.contains("pragma: no branch")
                || comment.contains("pragma: no decision"))
            .then(|| locator.line(token.start().to_usize()))
        })
        .collect();
    let mut decision_collector = Collector {
        path,
        source,
        locator: &locator,
        excluded_lines: &excluded_lines,
        definitions: Vec::new(),
        ordinal: 0,
    };
    Visitor::visit_body(&mut decision_collector, parsed.suite());
    decision_collector
        .definitions
        .sort_by(|left, right| left.key.cmp(&right.key));

    let mut controls = ControlRanges { ranges: Vec::new() };
    Visitor::visit_body(&mut controls, parsed.suite());
    let mut cancellation_aliases = BTreeSet::from(["CancelledError".to_owned()]);
    let mut base_aliases = BTreeSet::from(["BaseException".to_owned()]);
    for statement in parsed.suite() {
        if let Stmt::ImportFrom(import) = statement
            && let Some(module) = &import.module
        {
            let module = module.as_str();
            for alias in &import.names {
                let imported = alias.name.as_str();
                let local = alias.asname.as_ref().unwrap_or(&alias.name).as_str();
                if module == "asyncio" && imported == "CancelledError" {
                    cancellation_aliases.insert(local.to_owned());
                }
                if module == "builtins" && imported == "BaseException" {
                    base_aliases.insert(local.to_owned());
                }
            }
        }
    }
    let mut collector = PathCollector {
        path,
        source,
        locator: &locator,
        excluded_lines: &excluded_lines,
        control_ranges: &controls.ranges,
        cancellation_aliases: &cancellation_aliases,
        base_aliases: &base_aliases,
        definitions: Vec::new(),
        ordinal: 0,
    };
    Visitor::visit_body(&mut collector, parsed.suite());
    collector
        .definitions
        .sort_by(|left, right| left.key.cmp(&right.key));
    Ok((decision_collector.definitions, collector.definitions))
}
