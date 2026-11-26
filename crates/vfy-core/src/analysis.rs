//! Source analysis: executable lines, exclusions, possible branch arcs, code regions, and a bounded process-local result cache.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::mem::size_of;
use std::ops::Deref;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc as Shared, Mutex};

use crate::{CoreError, SourceLocator};
use fancy_regex::{Regex, RegexBuilder};
use ruff_python_ast::statement_visitor::{StatementVisitor, walk_stmt};
use ruff_python_ast::token::{Token, TokenKind};
use ruff_python_ast::{BoolOp, ExceptHandler, Expr, Number, Stmt, UnaryOp};
use ruff_text_size::{Ranged, TextRange};
use rustc_hash::FxHashMap as HashMap;
use sha2::{Digest, Sha256};

type Arc = (i32, i32);

/// Map physical lines to logical statement starts for sys.monitoring branches.
/// Unlike report exclusions, this follows logical NEWLINE tokens, including
/// compound headers and decorators. It is computed outside monitoring callbacks.
#[must_use]
pub fn multiline_map(source: &str) -> BTreeMap<i32, i32> {
    let Ok(parsed) = ruff_python_parser::parse_module(source) else {
        return BTreeMap::new();
    };
    multiline_map_from_tokens(&Locator::new(source), parsed.tokens())
}

fn multiline_map_from_tokens(locator: &Locator<'_>, tokens: &[Token]) -> BTreeMap<i32, i32> {
    let mut mapping = BTreeMap::new();
    let mut first = None;
    for token in tokens {
        match token.kind() {
            TokenKind::Newline => {
                if let Some(start) = first.take() {
                    let end = locator.line(token.start().to_usize());
                    if end > start {
                        mapping.extend((start..=end).map(|line| (line, start)));
                    }
                }
            }
            TokenKind::Comment
            | TokenKind::NonLogicalNewline
            | TokenKind::Indent
            | TokenKind::Dedent
            | TokenKind::EndOfFile => {}
            _ => {
                first.get_or_insert_with(|| locator.line(token.start().to_usize()));
            }
        }
    }
    mapping
}

/// Interpreter rules that change compiled source obligations.
#[derive(Clone, Copy)]
pub struct PythonSemantics {
    pub class_bare_annotations_execute: bool,
    pub debug_enabled: bool,
}

/// Line numbers of a measured `with` statement and its cleanup destination.
#[derive(Clone, Copy)]
pub struct WithJump {
    pub header: i32,
    pub body_start: i32,
    pub body_end: i32,
    pub after: i32,
    pub header_exit: WithHeaderExit,
}

/// Whether the rewrite drops a measured arc from the header to `after`.
///
/// Coverage.py keeps that arc as a branch for a `with` statement that ends a
/// suite chained through two enclosing `with` statements.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WithHeaderExit {
    Removed,
    Retained,
}

/// Source scope reached by a branch ending at a negative code-object exit.
#[derive(Clone)]
pub enum ExitDescription {
    Module,
    ReturnFunction(String),
    ExitClass(String),
}

/// A function or class body with its first line and executable lines.
#[derive(Clone)]
pub struct CodeRegion {
    pub kind: &'static str,
    pub name: String,
    pub start: i32,
    pub lines: BTreeSet<i32>,
}

struct Locator<'a> {
    source: SourceLocator<'a>,
}

impl<'a> Locator<'a> {
    fn new(source: &'a str) -> Self {
        Self {
            source: SourceLocator::new(source),
        }
    }

    fn statement_line(&self, statement: &Stmt) -> i32 {
        let offset = match statement {
            Stmt::FunctionDef(function) => function.name.range.start().to_usize(),
            Stmt::ClassDef(class) => class.name.range.start().to_usize(),
            _ => statement.range().start().to_usize(),
        };
        self.line(offset)
    }

    fn lines(&self, range: TextRange) -> std::ops::RangeInclusive<i32> {
        let start = self.line(range.start().to_usize());
        let end_offset = range.end().to_usize().saturating_sub(1);
        start..=self.line(end_offset)
    }

    fn statement_lines(&self, statement: &Stmt) -> std::ops::RangeInclusive<i32> {
        let start = self.line(statement.range().start().to_usize());
        if let Stmt::Match(node) = statement {
            let end = node.cases.first().map_or(start, |case| {
                self.line(case.range.start().to_usize())
                    .saturating_sub(1)
                    .max(start)
            });
            return start..=end;
        }
        let body_start = match statement {
            Stmt::FunctionDef(node) => node.body.first(),
            Stmt::ClassDef(node) => node.body.first(),
            Stmt::If(node) => node.body.first(),
            Stmt::For(node) => node.body.first(),
            Stmt::While(node) => node.body.first(),
            Stmt::With(node) => node.body.first(),
            Stmt::Try(node) => node.body.first(),
            _ => None,
        };
        let end = body_start
            .map_or_else(
                || self.line(statement.range().end().to_usize().saturating_sub(1)),
                |body| {
                    let body_offset = body.range().start().to_usize();
                    let body_line = self.line(body_offset);
                    let body_column =
                        usize::try_from(self.byte_column(body_offset)).unwrap_or(usize::MAX);
                    let header = self.line_text(body_line);
                    let shares_header_line = header
                        .get(..body_column.min(header.len()))
                        .is_some_and(|prefix| prefix.contains(':'));
                    if shares_header_line {
                        body_line
                    } else {
                        body_line.saturating_sub(1).max(start)
                    }
                },
            )
            .max(self.statement_line(statement));
        start..=end
    }
}

impl<'a> Deref for Locator<'a> {
    type Target = SourceLocator<'a>;

    fn deref(&self) -> &Self::Target {
        &self.source
    }
}

struct ExecutableCollector<'a> {
    locator: &'a Locator<'a>,
    lines: BTreeSet<i32>,
    line_map: BTreeMap<i32, i32>,
    annotations_execute: bool,
    class_annotations_execute: bool,
    docstring_scope: bool,
    code_scope: CodeStatementScope,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum CodeStatementScope {
    Executable,
    DeferredAnnotations,
}

#[derive(Default)]
struct ChildExpressionCodeFinder {
    found: bool,
    decorators: Vec<TextRange>,
}

impl<'a> ruff_python_ast::visitor::Visitor<'a> for ChildExpressionCodeFinder {
    fn visit_stmt(&mut self, _statement: &'a Stmt) {}

    fn visit_decorator(&mut self, decorator: &'a ruff_python_ast::Decorator) {
        let mut expression = Self::default();
        ruff_python_ast::visitor::walk_decorator(&mut expression, decorator);
        if expression.found {
            self.decorators.push(decorator.range);
        }
    }

    fn visit_expr(&mut self, expression: &'a Expr) {
        if matches!(expression, Expr::Lambda(_) | Expr::Generator(_)) {
            self.found = true;
        } else {
            ruff_python_ast::visitor::walk_expr(self, expression);
        }
    }
}

struct RegionContext {
    name: String,
    kind: &'static str,
    region: usize,
}

struct RegionCollector<'a> {
    locator: &'a Locator<'a>,
    contexts: Vec<RegionContext>,
    regions: Vec<CodeRegion>,
}

impl RegionCollector<'_> {
    fn qualified_name(&self, name: &str) -> String {
        self.contexts
            .iter()
            .map(|context| context.name.as_str())
            .chain(std::iter::once(name))
            .collect::<Vec<_>>()
            .join(".")
    }

    fn visit_body(&mut self, body: &[Stmt]) {
        for statement in body {
            self.visit_statement(statement);
        }
    }

    fn visit_statement(&mut self, statement: &Stmt) {
        match statement {
            Stmt::FunctionDef(function) => {
                let lines = function.body.first().zip(function.body.last()).map_or_else(
                    BTreeSet::new,
                    |(first, last)| {
                        let start = self.locator.statement_line(first);
                        let end = self
                            .locator
                            .line(last.range().end().to_usize().saturating_sub(1));
                        (start..=end).collect()
                    },
                );
                if let Some(class) = self
                    .contexts
                    .last()
                    .filter(|context| context.kind == "class")
                {
                    self.regions[class.region]
                        .lines
                        .extend(lines.iter().copied());
                }
                if let Some(function_context) = self
                    .contexts
                    .iter()
                    .rev()
                    .find(|context| context.kind == "function")
                {
                    self.regions[function_context.region]
                        .lines
                        .retain(|line| !lines.contains(line));
                }
                let name = function.name.as_str();
                let region = self.regions.len();
                self.regions.push(CodeRegion {
                    kind: "function",
                    name: self.qualified_name(name),
                    start: self.locator.statement_line(statement),
                    lines,
                });
                self.contexts.push(RegionContext {
                    name: name.to_owned(),
                    kind: "function",
                    region,
                });
                self.visit_body(&function.body);
                self.contexts.pop();
            }
            Stmt::ClassDef(class) => {
                let name = class.name.as_str();
                let region = self.regions.len();
                self.regions.push(CodeRegion {
                    kind: "class",
                    name: self.qualified_name(name),
                    start: self.locator.statement_line(statement),
                    lines: BTreeSet::new(),
                });
                self.contexts.push(RegionContext {
                    name: name.to_owned(),
                    kind: "class",
                    region,
                });
                self.visit_body(&class.body);
                self.contexts.pop();
                let lines = self.regions[region].lines.clone();
                if let Some(class_context) = self
                    .contexts
                    .iter()
                    .rev()
                    .find(|context| context.kind == "class")
                {
                    self.regions[class_context.region]
                        .lines
                        .retain(|line| !lines.contains(line));
                }
            }
            Stmt::For(node) => self.visit_body(&node.body),
            Stmt::While(node) => self.visit_body(&node.body),
            Stmt::If(node) => self.visit_body(&node.body),
            Stmt::With(node) => self.visit_body(&node.body),
            Stmt::Try(node) => self.visit_body(&node.body),
            _ => {}
        }
    }
}

impl<'a> ExecutableCollector<'a> {
    fn collect_child_code(&mut self, statement: &'a Stmt) {
        let child_entry = match statement {
            Stmt::FunctionDef(function)
                if function.name.as_str() != "__annotate__" || function.type_params.is_some() =>
            {
                Some(function.decorator_list.first().map_or_else(
                    || self.locator.statement_line(statement),
                    |decorator| self.locator.line(decorator.range.start().to_usize()),
                ))
            }
            Stmt::ClassDef(class)
                if class.name.as_str() != "__annotate__" || class.type_params.is_some() =>
            {
                Some(class.decorator_list.first().map_or_else(
                    || self.locator.statement_line(statement),
                    |decorator| self.locator.line(decorator.range.start().to_usize()),
                ))
            }
            _ => None,
        };
        if let Some(line) = child_entry {
            self.lines.insert(line);
        }
        let mut child_expression = ChildExpressionCodeFinder {
            found: match statement {
                Stmt::FunctionDef(function) => function.type_params.is_some(),
                Stmt::ClassDef(class) => class.type_params.is_some(),
                _ => false,
            },
            ..ChildExpressionCodeFinder::default()
        };
        ruff_python_ast::visitor::walk_stmt(&mut child_expression, statement);
        if child_expression.found || matches!(statement, Stmt::TypeAlias(_)) {
            let line = self.locator.statement_line(statement);
            self.lines.insert(line);
            for physical in self.locator.statement_lines(statement) {
                if physical >= line {
                    self.line_map.insert(physical, line);
                }
            }
        }
        for decorator in child_expression.decorators {
            let line = self.locator.line(decorator.start().to_usize());
            self.lines.insert(line);
            for physical in self.locator.lines(decorator) {
                self.line_map.insert(physical, line);
            }
        }
    }
}

impl<'a> StatementVisitor<'a> for ExecutableCollector<'a> {
    fn visit_body(&mut self, body: &'a [Stmt]) {
        let docstring_scope = std::mem::replace(&mut self.docstring_scope, false);
        for (index, statement) in body.iter().enumerate() {
            let docstring = docstring_scope
                && index == 0
                && matches!(
                    statement,
                    Stmt::Expr(expression)
                        if matches!(expression.value.as_ref(), Expr::StringLiteral(_))
                );
            if !docstring {
                self.visit_stmt(statement);
            }
        }
    }

    fn visit_stmt(&mut self, statement: &'a Stmt) {
        let function_local_annotation = !self.annotations_execute
            && matches!(
                statement,
                Stmt::AnnAssign(node)
                    if node.value.is_none() && matches!(node.target.as_ref(), Expr::Name(_))
            );
        if self.code_scope == CodeStatementScope::Executable
            && !matches!(statement, Stmt::Global(_) | Stmt::Nonlocal(_))
            && !function_local_annotation
        {
            let statement_line = self.locator.statement_line(statement);
            for line in self.locator.statement_lines(statement) {
                self.line_map.insert(line, statement_line);
            }
            match statement {
                Stmt::FunctionDef(function) => {
                    for decorator in &function.decorator_list {
                        let decorator_line = self.locator.line(decorator.range.start().to_usize());
                        self.lines.insert(decorator_line);
                        for line in self.locator.lines(decorator.range) {
                            self.line_map.insert(line, decorator_line);
                        }
                    }
                }
                Stmt::ClassDef(class) => {
                    for decorator in &class.decorator_list {
                        let decorator_line = self.locator.line(decorator.range.start().to_usize());
                        self.lines.insert(decorator_line);
                        for line in self.locator.lines(decorator.range) {
                            self.line_map.insert(line, decorator_line);
                        }
                    }
                }
                _ => {}
            }
            self.lines.insert(statement_line);
        } else if self.code_scope == CodeStatementScope::DeferredAnnotations
            && !function_local_annotation
        {
            self.collect_child_code(statement);
        }
        let previous = self.annotations_execute;
        let previous_docstring_scope = self.docstring_scope;
        let previous_code_scope = self.code_scope;
        self.docstring_scope = matches!(statement, Stmt::FunctionDef(_) | Stmt::ClassDef(_));
        match statement {
            Stmt::FunctionDef(function) => {
                self.annotations_execute = false;
                // Coverage.py omits this code object's statements, while
                // still inspecting independently compiled children.
                self.code_scope = if function.name.as_str() == "__annotate__" {
                    CodeStatementScope::DeferredAnnotations
                } else {
                    CodeStatementScope::Executable
                };
            }
            // With postponed annotations, CPython emits a class-body update
            // for a bare annotation. Without it, Coverage.py excludes that
            // source line just as it excludes function-local bare annotations.
            Stmt::ClassDef(class) => {
                self.annotations_execute = self.class_annotations_execute;
                self.code_scope = if class.name.as_str() == "__annotate__" {
                    CodeStatementScope::DeferredAnnotations
                } else {
                    CodeStatementScope::Executable
                };
            }
            _ => {}
        }
        walk_stmt(self, statement);
        self.annotations_execute = previous;
        self.docstring_scope = previous_docstring_scope;
        self.code_scope = previous_code_scope;
    }

    fn visit_except_handler(&mut self, handler: &'a ExceptHandler) {
        if self.code_scope == CodeStatementScope::Executable {
            self.lines
                .insert(self.locator.line(handler.range().start().to_usize()));
        }
        ruff_python_ast::statement_visitor::walk_except_handler(self, handler);
    }

    fn visit_match_case(&mut self, case: &'a ruff_python_ast::MatchCase) {
        let line = self.locator.line(case.range.start().to_usize());
        if self.code_scope == CodeStatementScope::Executable {
            self.lines.insert(line);
        }
        let end = case.body.first().map_or(line, |body| {
            self.locator
                .line(body.range().start().to_usize())
                .saturating_sub(1)
                .max(line)
        });
        for physical in line..=end {
            self.line_map.insert(physical, line);
        }
        ruff_python_ast::statement_visitor::walk_match_case(self, case);
    }

    fn visit_elif_else_clause(&mut self, clause: &'a ruff_python_ast::ElifElseClause) {
        if self.code_scope == CodeStatementScope::Executable && clause.test.is_some() {
            let range_line = self.locator.line(clause.range.start().to_usize());
            let line = range_line.checked_sub(1).map_or(range_line, |previous| {
                let text = self.locator.line_text(previous).trim_start();
                if text.starts_with("elif ") || text.starts_with("else:") {
                    previous
                } else {
                    range_line
                }
            });
            self.lines.insert(line);
            let body_start = clause.body.first().map_or(line, |body| {
                self.locator
                    .line(body.range().start().to_usize())
                    .saturating_sub(1)
                    .max(line)
            });
            for physical in line..=body_start {
                self.line_map.insert(physical, line);
            }
        }
        ruff_python_ast::statement_visitor::walk_elif_else_clause(self, clause);
    }
}

fn future_annotations_enabled(body: &[Stmt]) -> bool {
    let mut enabled = false;
    for (index, statement) in body.iter().enumerate() {
        let docstring = index == 0
            && matches!(
                statement,
                Stmt::Expr(expression)
                    if matches!(expression.value.as_ref(), Expr::StringLiteral(_))
            );
        if docstring {
            continue;
        }
        let Stmt::ImportFrom(import) = statement else {
            break;
        };
        if import
            .module
            .as_ref()
            .is_none_or(|module| module.as_str() != "__future__")
        {
            break;
        }
        enabled |= import
            .names
            .iter()
            .any(|alias| alias.name.as_str() == "annotations");
    }
    enabled
}

struct MarkExcluded<'a> {
    locator: &'a Locator<'a>,
    lines: &'a mut BTreeSet<i32>,
}

impl<'a> StatementVisitor<'a> for MarkExcluded<'a> {
    fn visit_stmt(&mut self, statement: &'a Stmt) {
        self.lines.insert(self.locator.statement_line(statement));
        match statement {
            Stmt::FunctionDef(function) => {
                self.lines.extend(self.locator.lines(function.range));
                self.lines.extend(
                    function
                        .decorator_list
                        .iter()
                        .map(|decorator| self.locator.line(decorator.range.start().to_usize())),
                );
            }
            Stmt::ClassDef(class) => {
                self.lines.extend(self.locator.lines(class.range));
                self.lines.extend(
                    class
                        .decorator_list
                        .iter()
                        .map(|decorator| self.locator.line(decorator.range.start().to_usize())),
                );
            }
            Stmt::For(node) => self.mark_suite_header(&node.orelse, "else:"),
            Stmt::While(node) => self.mark_suite_header(&node.orelse, "else:"),
            Stmt::Try(node) => {
                self.mark_suite_header(&node.orelse, "else:");
                self.mark_suite_header(&node.finalbody, "finally:");
            }
            _ => {}
        }
        walk_stmt(self, statement);
    }

    fn visit_except_handler(&mut self, handler: &'a ExceptHandler) {
        self.lines
            .insert(self.locator.line(handler.range().start().to_usize()));
        ruff_python_ast::statement_visitor::walk_except_handler(self, handler);
    }

    fn visit_elif_else_clause(&mut self, clause: &'a ruff_python_ast::ElifElseClause) {
        let range_line = self.locator.line(clause.range.start().to_usize());
        let line = range_line.checked_sub(1).map_or(range_line, |previous| {
            let text = self.locator.line_text(previous).trim_start();
            if text.starts_with("elif ") || text.starts_with("else:") {
                previous
            } else {
                range_line
            }
        });
        self.lines.insert(line);
        ruff_python_ast::statement_visitor::walk_elif_else_clause(self, clause);
    }
}

impl MarkExcluded<'_> {
    fn mark_suite_header(&mut self, body: &[Stmt], keyword: &str) {
        let Some(statement) = body.first() else {
            return;
        };
        let body_line = self.locator.line(statement.range().start().to_usize());
        let header_line = body_line.saturating_sub(1);
        if self
            .locator
            .line_text(header_line)
            .trim_start()
            .starts_with(keyword)
        {
            self.lines.insert(header_line);
        }
    }
}

struct ExclusionCollector<'a> {
    locator: &'a Locator<'a>,
    matched_lines: &'a BTreeSet<i32>,
    lines: BTreeSet<i32>,
}

struct UnreachableCollector<'a> {
    debug_enabled: bool,
    locator: &'a Locator<'a>,
    lines: BTreeSet<i32>,
    partials: BTreeSet<i32>,
    constant_while_else: BTreeSet<i32>,
}

fn constant_test(expression: &Expr, debug_enabled: bool) -> Option<bool> {
    match expression {
        Expr::Name(node) if node.id == "__debug__" => Some(debug_enabled),
        Expr::BooleanLiteral(node) => Some(node.value),
        Expr::NoneLiteral(_) => Some(false),
        Expr::NumberLiteral(node) => Some(match &node.value {
            Number::Int(value) => value.as_u64() != Some(0),
            Number::Float(value) => *value != 0.0,
            Number::Complex { real, imag } => *real != 0.0 || *imag != 0.0,
        }),
        Expr::StringLiteral(node) => Some(!node.value.is_empty()),
        Expr::BytesLiteral(node) => Some(!node.value.is_empty()),
        Expr::UnaryOp(node) if node.op == UnaryOp::Not => {
            constant_test(&node.operand, debug_enabled).map(|value| !value)
        }
        Expr::BoolOp(node) => {
            let values = node
                .values
                .iter()
                .map(|value| constant_test(value, debug_enabled))
                .collect::<Option<Vec<_>>>()?;
            Some(match node.op {
                BoolOp::And => values.into_iter().all(std::convert::identity),
                BoolOp::Or => values.into_iter().any(std::convert::identity),
            })
        }
        _ => None,
    }
}

fn compiler_truth(expression: &Expr, debug_enabled: bool) -> Option<bool> {
    match expression {
        Expr::UnaryOp(node) if node.op == UnaryOp::Not => {
            compiler_truth(&node.operand, debug_enabled).map(|value| !value)
        }
        Expr::BoolOp(node) => {
            let short_circuit = node.op == BoolOp::Or;
            let mut all_known = true;
            for operand in &node.values {
                match compiler_truth(operand, debug_enabled) {
                    Some(value) if value == short_circuit => return Some(value),
                    Some(_) => {}
                    None => all_known = false,
                }
            }
            all_known.then_some(!short_circuit)
        }
        _ => constant_test(expression, debug_enabled),
    }
}

fn compiler_suite_exits(body: &[Stmt], debug_enabled: bool) -> bool {
    body.iter()
        .any(|statement| compiler_statement_exits(statement, debug_enabled))
}

fn may_break_current_loop(body: &[Stmt], debug_enabled: bool) -> bool {
    body.iter().any(|statement| match statement {
        Stmt::Break(_) => true,
        Stmt::If(node) => {
            may_break_current_loop(&node.body, debug_enabled)
                || node
                    .elif_else_clauses
                    .iter()
                    .any(|clause| may_break_current_loop(&clause.body, debug_enabled))
        }
        Stmt::Try(node) => {
            let final_overrides_break = !node.finalbody.is_empty()
                && compiler_suite_exits(&node.finalbody, debug_enabled)
                && !may_break_current_loop(&node.finalbody, debug_enabled);
            !final_overrides_break
                && (may_break_current_loop(&node.body, debug_enabled)
                    || may_break_current_loop(&node.orelse, debug_enabled)
                    || may_break_current_loop(&node.finalbody, debug_enabled)
                    || node.handlers.iter().any(|handler| {
                        let ExceptHandler::ExceptHandler(handler) = handler;
                        may_break_current_loop(&handler.body, debug_enabled)
                    }))
        }
        Stmt::With(node) => may_break_current_loop(&node.body, debug_enabled),
        Stmt::Match(node) => node
            .cases
            .iter()
            .any(|case| may_break_current_loop(&case.body, debug_enabled)),
        // Nested loops and definitions cannot break the current loop.
        _ => false,
    })
}

fn compiler_statement_exits(statement: &Stmt, debug_enabled: bool) -> bool {
    match statement {
        Stmt::Return(_) | Stmt::Raise(_) | Stmt::Break(_) | Stmt::Continue(_) => true,
        Stmt::If(node) => {
            let body_exits = compiler_suite_exits(&node.body, debug_enabled);
            if let Some(value) = compiler_truth(&node.test, debug_enabled) {
                if value {
                    return body_exits;
                }
                return node.elif_else_clauses.first().is_some_and(|clause| {
                    clause.test.is_none() && compiler_suite_exits(&clause.body, debug_enabled)
                });
            }
            body_exits
                && node
                    .elif_else_clauses
                    .last()
                    .is_some_and(|clause| clause.test.is_none())
                && node
                    .elif_else_clauses
                    .iter()
                    .all(|clause| compiler_suite_exits(&clause.body, debug_enabled))
        }
        Stmt::For(node) => {
            !may_break_current_loop(&node.body, debug_enabled)
                && !node.orelse.is_empty()
                && compiler_suite_exits(&node.orelse, debug_enabled)
        }
        Stmt::While(node) => {
            // With no reachable break, a constant true loop or an exiting else
            // leaves no executable statement after the loop.
            !may_break_current_loop(&node.body, debug_enabled)
                && (compiler_truth(&node.test, debug_enabled) == Some(true)
                    || (!node.orelse.is_empty()
                        && compiler_suite_exits(&node.orelse, debug_enabled)))
        }
        Stmt::Match(node) => {
            node.cases
                .last()
                .is_some_and(|case| case.guard.is_none() && branches::is_wildcard(&case.pattern))
                && node
                    .cases
                    .iter()
                    .all(|case| compiler_suite_exits(&case.body, debug_enabled))
        }
        Stmt::Try(node) => {
            compiler_suite_exits(&node.finalbody, debug_enabled)
                || (compiler_suite_exits(&node.body, debug_enabled)
                    && node.handlers.iter().all(|handler| {
                        let ExceptHandler::ExceptHandler(handler) = handler;
                        compiler_suite_exits(&handler.body, debug_enabled)
                    }))
        }
        _ => false,
    }
}

impl<'a> StatementVisitor<'a> for UnreachableCollector<'a> {
    fn visit_body(&mut self, body: &'a [Stmt]) {
        for (index, statement) in body.iter().enumerate() {
            self.visit_stmt(statement);
            if compiler_statement_exits(statement, self.debug_enabled) {
                let mut skipped_tail = BTreeSet::new();
                MarkExcluded {
                    locator: self.locator,
                    lines: &mut skipped_tail,
                }
                .visit_body(&body[index + 1..]);
                self.lines.extend(skipped_tail);
                break;
            }
        }
    }

    fn visit_stmt(&mut self, statement: &'a Stmt) {
        if let Stmt::Try(node) = statement
            && compiler_suite_exits(&node.body, self.debug_enabled)
        {
            MarkExcluded {
                locator: self.locator,
                lines: &mut self.lines,
            }
            .visit_body(&node.orelse);
        }
        let conditional = match statement {
            Stmt::If(node) => Some((&*node.test, node.body.as_slice())),
            Stmt::While(node) => Some((&*node.test, node.body.as_slice())),
            _ => None,
        };
        if let Some((test, body)) = conditional
            && let Some(value) = compiler_truth(test, self.debug_enabled)
        {
            let line = self.locator.statement_line(statement);
            let header_already_removed = self.lines.contains(&line);
            if constant_test(test, self.debug_enabled).is_some() {
                self.partials.insert(line);
            }
            if value && matches!(statement, Stmt::While(node) if !node.orelse.is_empty()) {
                self.constant_while_else.insert(line);
            }
            if value {
                match statement {
                    Stmt::If(node) => {
                        for clause in &node.elif_else_clauses {
                            MarkExcluded {
                                locator: self.locator,
                                lines: &mut self.lines,
                            }
                            .visit_elif_else_clause(clause);
                        }
                    }
                    Stmt::While(node) => {
                        for child in &node.orelse {
                            MarkExcluded {
                                locator: self.locator,
                                lines: &mut self.lines,
                            }
                            .visit_stmt(child);
                        }
                    }
                    _ => {}
                }
            } else {
                for child in body {
                    MarkExcluded {
                        locator: self.locator,
                        lines: &mut self.lines,
                    }
                    .visit_stmt(child);
                }
            }
            if !header_already_removed {
                self.lines.remove(&line);
            }
        }
        walk_stmt(self, statement);
    }
}

fn classify_constant_branches(
    source: &str,
    logical_lines: &BTreeMap<i32, i32>,
    mut partials: BTreeSet<i32>,
    constant_while_else: &BTreeSet<i32>,
) -> Result<(BTreeSet<i32>, BTreeSet<i32>), CoreError> {
    let structurally_partial = partials.difference(constant_while_else).copied().collect();
    if !constant_while_else.is_empty() {
        // Coverage.py retains while-else exits, but credits plain literal headers.
        let patterns = compile_patterns(&[r"while (True|1|False|0):".to_owned()])?;
        let implicit_partials = matching_lines(source, &patterns, logical_lines, "partial branch")?;
        partials
            .retain(|line| !constant_while_else.contains(line) || implicit_partials.contains(line));
    }
    Ok((structurally_partial, partials))
}

impl ExclusionCollector<'_> {
    fn matches(&self, statement: &Stmt) -> bool {
        self.locator
            .statement_lines(statement)
            .any(|line| self.matched_lines.contains(&line))
    }

    fn alternate_header(&self, body: &[Stmt], keyword: &str) -> Option<i32> {
        let statement = body.first()?;
        let mut line = self
            .locator
            .line(statement.range().start().to_usize())
            .saturating_sub(1);
        while line > 0 {
            let text = self.locator.line_text(line).trim_start();
            if text.starts_with(keyword) {
                return Some(line);
            }
            if !text.is_empty() && !text.starts_with('#') {
                return None;
            }
            line -= 1;
        }
        None
    }

    fn mark_alternate_if_matched(&mut self, body: &[Stmt], keyword: &str) {
        let Some(line) = self.alternate_header(body, keyword) else {
            return;
        };
        if self.matched_lines.contains(&line) {
            self.lines.insert(line);
            self.mark_body(body);
        }
    }
}

impl<'a> StatementVisitor<'a> for ExclusionCollector<'a> {
    fn visit_stmt(&mut self, statement: &'a Stmt) {
        if self.matches(statement) {
            self.lines.insert(self.locator.statement_line(statement));
            match statement {
                Stmt::If(node) => {
                    self.mark_body(&node.body);
                    for clause in &node.elif_else_clauses {
                        self.visit_elif_else_clause(clause);
                    }
                }
                Stmt::For(node) => {
                    self.mark_body(&node.body);
                    self.visit_body(&node.orelse);
                }
                Stmt::While(node) => {
                    self.mark_body(&node.body);
                    self.visit_body(&node.orelse);
                }
                Stmt::Try(node) => {
                    self.mark_body(&node.body);
                    for handler in &node.handlers {
                        self.visit_except_handler(handler);
                    }
                    self.visit_body(&node.orelse);
                    self.visit_body(&node.finalbody);
                }
                _ => {
                    MarkExcluded {
                        locator: self.locator,
                        lines: &mut self.lines,
                    }
                    .visit_stmt(statement);
                }
            }
        } else {
            walk_stmt(self, statement);
            match statement {
                Stmt::For(node) => self.mark_alternate_if_matched(&node.orelse, "else:"),
                Stmt::While(node) => self.mark_alternate_if_matched(&node.orelse, "else:"),
                Stmt::Try(node) => {
                    self.mark_alternate_if_matched(&node.orelse, "else:");
                    self.mark_alternate_if_matched(&node.finalbody, "finally:");
                }
                _ => {}
            }
        }
    }

    fn visit_except_handler(&mut self, handler: &'a ExceptHandler) {
        let line = self.locator.line(handler.range().start().to_usize());
        if self.matched_lines.contains(&line) {
            MarkExcluded {
                locator: self.locator,
                lines: &mut self.lines,
            }
            .visit_except_handler(handler);
        } else {
            ruff_python_ast::statement_visitor::walk_except_handler(self, handler);
        }
    }

    fn visit_elif_else_clause(&mut self, clause: &'a ruff_python_ast::ElifElseClause) {
        let range_line = self.locator.line(clause.range.start().to_usize());
        let header_line = range_line.checked_sub(1).map_or(range_line, |previous| {
            let text = self.locator.line_text(previous).trim_start();
            if text.starts_with("elif ") || text.starts_with("else:") {
                previous
            } else {
                range_line
            }
        });
        if self.matched_lines.contains(&header_line) {
            self.lines.insert(header_line);
            for statement in &clause.body {
                MarkExcluded {
                    locator: self.locator,
                    lines: &mut self.lines,
                }
                .visit_stmt(statement);
            }
        } else {
            ruff_python_ast::statement_visitor::walk_elif_else_clause(self, clause);
        }
    }
}

impl ExclusionCollector<'_> {
    fn mark_body(&mut self, body: &[Stmt]) {
        for statement in body {
            MarkExcluded {
                locator: self.locator,
                lines: &mut self.lines,
            }
            .visit_stmt(statement);
        }
    }
}

mod branches;
use branches::{BranchRules, collect_branches};

/// Executable lines, exclusions, and possible arcs for one source file.
#[derive(Clone)]
pub struct SourceAnalysis {
    pub executable: Vec<i32>,
    pub excluded: Vec<i32>,
    pub possible_arcs: Vec<Arc>,
    pub arc_exit_descriptions: BTreeMap<Arc, ExitDescription>,
    pub no_branch: Vec<i32>,
    pub source_failure: Option<SourceFailure>,
    pub regions: Vec<CodeRegion>,
    pub source_line_count: i32,
    pub line_map: BTreeMap<i32, i32>,
    pub report_excluded: Vec<i32>,
    pub with_jumps: Vec<WithJump>,
}

/// Why a source file could not be analyzed.
#[derive(Clone, Debug)]
pub enum SourceFailure {
    Missing,
    Encoding { detail: String },
    Unreadable { detail: String },
    NotPython { detail: String },
}

const ANALYSIS_CACHE_VERSION: u64 = 1;
const ANALYSIS_CACHE_LIMITS: CacheLimits = CacheLimits {
    entries: 4_096,
    bytes: 128 * 1024 * 1024,
};

#[derive(Clone, Copy)]
struct CacheLimits {
    entries: usize,
    bytes: usize,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct AnalysisKey([u8; 32]);

struct CachedAnalysis {
    analysis: Shared<SourceAnalysis>,
    retained_bytes: usize,
}

impl CachedAnalysis {
    fn new(analysis: SourceAnalysis) -> Self {
        let retained_bytes = analysis.estimated_retained_bytes();
        Self {
            analysis: Shared::new(analysis),
            retained_bytes,
        }
    }
}

/// Bounded process-local analyses keyed by a digest of the source, exclusion
/// patterns, and Python semantics. A changed input produces a different key, so
/// entries cannot go stale in a long-lived `vfy test --watch` process.
#[derive(Default)]
struct AnalysisCache {
    entries: HashMap<AnalysisKey, CachedAnalysis>,
    insertion_order: VecDeque<AnalysisKey>,
    retained_bytes: usize,
    hits: usize,
    misses: usize,
}

impl AnalysisCache {
    fn get(&mut self, key: AnalysisKey) -> Option<Shared<SourceAnalysis>> {
        if let Some(entry) = self.entries.get(&key) {
            self.hits = self.hits.saturating_add(1);
            Some(Shared::clone(&entry.analysis))
        } else {
            self.misses = self.misses.saturating_add(1);
            None
        }
    }

    fn insert(&mut self, key: AnalysisKey, analysis: CachedAnalysis) -> Vec<CachedAnalysis> {
        self.insert_with_limits(key, analysis, ANALYSIS_CACHE_LIMITS)
    }

    fn insert_with_limits(
        &mut self,
        key: AnalysisKey,
        analysis: CachedAnalysis,
        limits: CacheLimits,
    ) -> Vec<CachedAnalysis> {
        let mut released = Vec::new();
        if self.entries.contains_key(&key) {
            released.push(analysis);
            return released;
        }
        let retained_bytes = analysis.retained_bytes;
        if limits.entries == 0 || retained_bytes > limits.bytes {
            released.push(analysis);
            return released;
        }
        while self.entries.len() >= limits.entries
            || self.retained_bytes.saturating_add(retained_bytes) > limits.bytes
        {
            let Some(expired) = self.insertion_order.pop_front() else {
                released.extend(self.entries.drain().map(|(_, entry)| entry));
                self.retained_bytes = 0;
                break;
            };
            if let Some(expired) = self.entries.remove(&expired) {
                self.retained_bytes = self.retained_bytes.saturating_sub(expired.retained_bytes);
                released.push(expired);
            }
        }
        self.insertion_order.push_back(key);
        self.retained_bytes = self.retained_bytes.saturating_add(retained_bytes);
        self.entries.insert(key, analysis);
        released
    }
}

/// A forked child bypasses the inherited cache because its mutex may be held by
/// a vanished thread.
struct ProcessAnalysisCache {
    owner_pid: AtomicU32,
    cache: Mutex<Option<AnalysisCache>>,
}

impl ProcessAnalysisCache {
    const fn new() -> Self {
        Self {
            owner_pid: AtomicU32::new(0),
            cache: Mutex::new(None),
        }
    }

    fn for_process(&self, pid: u32) -> Option<&Mutex<Option<AnalysisCache>>> {
        let owner_pid = match self.owner_pid.load(Ordering::Acquire) {
            0 => self
                .owner_pid
                .compare_exchange(0, pid, Ordering::AcqRel, Ordering::Acquire)
                .map_or_else(|owner| owner, |_| pid),
            owner => owner,
        };
        (owner_pid == pid).then_some(&self.cache)
    }
}

fn analysis_cache() -> Option<&'static Mutex<Option<AnalysisCache>>> {
    static CACHE: ProcessAnalysisCache = ProcessAnalysisCache::new();
    CACHE.for_process(std::process::id())
}

/// Clear process-local source analysis entries and counters.
pub fn clear_analysis_cache() {
    let Some(cache) = analysis_cache() else {
        return;
    };
    let previous = {
        let mut cache = cache
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        cache.take()
    };
    drop(previous);
}

/// Return process-local cache hits, misses, retained entries, and estimated bytes.
#[must_use]
pub fn analysis_cache_stats() -> (usize, usize, usize, usize) {
    let Some(cache) = analysis_cache() else {
        return (0, 0, 0, 0);
    };
    let cache = cache
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let Some(cache) = cache.as_ref() else {
        return (0, 0, 0, 0);
    };
    (
        cache.hits,
        cache.misses,
        cache.entries.len(),
        cache.retained_bytes,
    )
}

fn analysis_key(
    source: &str,
    excludes: &[String],
    partials: &[String],
    collect_arcs: bool,
    python_semantics: PythonSemantics,
) -> AnalysisKey {
    let mut digest = Sha256::new();
    digest.update(ANALYSIS_CACHE_VERSION.to_le_bytes());
    digest.update([
        u8::from(collect_arcs),
        u8::from(python_semantics.class_bare_annotations_execute),
        u8::from(python_semantics.debug_enabled),
    ]);
    digest.update(source.len().to_le_bytes());
    digest.update(source.as_bytes());
    for group in [excludes, partials] {
        digest.update(group.len().to_le_bytes());
        for pattern in group {
            let pattern = pattern.as_bytes();
            digest.update(pattern.len().to_le_bytes());
            digest.update(pattern);
        }
    }
    AnalysisKey(digest.finalize().into())
}

impl SourceFailure {
    /// Classify a read failure: invalid data is an encoding failure, a missing file is
    /// `Missing`, and any other error is `Unreadable`.
    #[must_use]
    pub fn from_io_error(error: &std::io::Error) -> Self {
        if error.kind() == std::io::ErrorKind::InvalidData {
            Self::Encoding {
                detail: error.to_string(),
            }
        } else if error.kind() == std::io::ErrorKind::NotFound {
            Self::Missing
        } else {
            Self::Unreadable {
                detail: error.to_string(),
            }
        }
    }

    /// Return the failure category reported for the file: `no-source`, `not_python`, or `syntax-error`.
    #[must_use]
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Missing | Self::Unreadable { .. } => "no-source",
            Self::NotPython { .. } => "not_python",
            Self::Encoding { .. } => "syntax-error",
        }
    }

    #[must_use]
    pub fn detail(&self) -> Option<&str> {
        match self {
            Self::Missing => None,
            Self::Unreadable { detail }
            | Self::NotPython { detail }
            | Self::Encoding { detail } => Some(detail),
        }
    }
}

impl SourceAnalysis {
    fn estimated_retained_bytes(&self) -> usize {
        let vector_bytes = self.executable.capacity() * size_of::<i32>()
            + self.excluded.capacity() * size_of::<i32>()
            + self.possible_arcs.capacity() * size_of::<Arc>()
            + self.arc_exit_descriptions.len()
                * (size_of::<Arc>() + size_of::<ExitDescription>() + 3 * size_of::<usize>())
            + self
                .arc_exit_descriptions
                .values()
                .map(|description| match description {
                    ExitDescription::Module => 0,
                    ExitDescription::ReturnFunction(name) | ExitDescription::ExitClass(name) => {
                        name.capacity()
                    }
                })
                .sum::<usize>()
            + self.no_branch.capacity() * size_of::<i32>()
            + self.report_excluded.capacity() * size_of::<i32>()
            + self.with_jumps.capacity() * size_of::<WithJump>();
        let region_bytes = self.regions.capacity() * size_of::<CodeRegion>()
            + self
                .regions
                .iter()
                .map(|region| {
                    region.name.capacity()
                        + region.lines.len() * (size_of::<i32>() + 3 * size_of::<usize>())
                })
                .sum::<usize>();
        let line_map_bytes = self.line_map.len() * (2 * size_of::<i32>() + 3 * size_of::<usize>());
        let failure_bytes = self
            .source_failure
            .as_ref()
            .and_then(SourceFailure::detail)
            .map_or(0, str::len);
        size_of::<Self>()
            .saturating_add(vector_bytes)
            .saturating_add(region_bytes)
            .saturating_add(line_map_bytes)
            .saturating_add(failure_bytes)
    }

    /// Report whether any source failure was recorded, not only a missing file.
    #[must_use]
    pub fn source_missing(&self) -> bool {
        self.source_failure.is_some()
    }

    pub fn source_error_kind(&self) -> Option<&str> {
        self.source_failure.as_ref().map(SourceFailure::kind)
    }

    pub fn source_error_detail(&self) -> Option<&str> {
        self.source_failure.as_ref().and_then(SourceFailure::detail)
    }
}

/// Return an analysis with no executable lines that records `failure`.
#[must_use]
pub fn failed_analysis(failure: SourceFailure) -> SourceAnalysis {
    SourceAnalysis {
        executable: Vec::new(),
        excluded: Vec::new(),
        possible_arcs: Vec::new(),
        arc_exit_descriptions: BTreeMap::new(),
        no_branch: Vec::new(),
        source_failure: Some(failure),
        regions: Vec::new(),
        source_line_count: 0,
        line_map: BTreeMap::new(),
        report_excluded: Vec::new(),
        with_jumps: Vec::new(),
    }
}

/// Compile configured exclusion expressions.
///
/// # Errors
/// Returns an invalid-input error when an expression cannot be compiled.
pub fn compile_patterns(excludes: &[String]) -> Result<Vec<Regex>, CoreError> {
    if excludes.is_empty() {
        return Ok(Vec::new());
    }
    for pattern in excludes {
        RegexBuilder::new(pattern)
            .multi_line(true)
            .build()
            .map_err(|error| {
                CoreError::InvalidInput(format!("invalid exclusion pattern {pattern:?}: {error}"))
            })?;
    }
    let alternation = excludes
        .iter()
        .map(|pattern| format!("(?:{pattern})"))
        .collect::<Vec<_>>()
        .join("|");
    let literal_only = excludes.iter().all(|pattern| {
        !pattern.is_empty()
            && !pattern.bytes().any(|byte| {
                matches!(
                    byte,
                    b'.' | b'^'
                        | b'$'
                        | b'*'
                        | b'+'
                        | b'?'
                        | b'{'
                        | b'}'
                        | b'['
                        | b']'
                        | b'('
                        | b')'
                        | b'|'
                        | b'\\'
                )
            })
    });
    // Literal alternatives can use the delegated engine's full-source search.
    // Keep other expressions on the VM: delegation changes greedy multiline
    // `$` matches. Seek candidate positions in merged unions; keep a single
    // configured expression on its existing bounded VM search path.
    let combined = if literal_only {
        format!("(?:{alternation})")
    } else {
        format!("(?=)(?:{alternation})")
    };
    let combined = RegexBuilder::new(&combined)
        .multi_line(true)
        .seek(excludes.len() > 1)
        .build()
        .map_err(|error| {
            CoreError::InvalidInput(format!("failed to combine exclusion patterns: {error}"))
        })?;
    Ok(vec![combined])
}

fn matching_lines(
    source: &str,
    patterns: &[Regex],
    logical_lines: &BTreeMap<i32, i32>,
    pattern_kind: &str,
) -> Result<BTreeSet<i32>, CoreError> {
    if patterns.is_empty() {
        return Ok(BTreeSet::new());
    }
    let locator = Locator::new(source);
    let mut matched = BTreeSet::new();
    for pattern in patterns {
        for result in pattern.find_iter(source) {
            let found = result.map_err(|error| {
                let compiled = pattern.as_str();
                let configured = compiled
                    .strip_prefix("(?=)(?:")
                    .and_then(|value| value.strip_suffix(')'))
                    .unwrap_or(compiled);
                CoreError::InvalidInput(format!(
                    "{pattern_kind} pattern {configured:?} failed while matching source: {error}"
                ))
            })?;
            let start = locator.line(found.start());
            let end = locator.line(found.end());
            for line in start..=end {
                matched.insert(logical_lines.get(&line).copied().unwrap_or(line));
            }
        }
    }
    Ok(matched)
}

/// Return source lines matching a configured partial-branch expression.
///
/// # Errors
/// Returns an invalid-input error when matching exceeds the regex runtime limit.
pub fn partial_lines(source: &str, patterns: &[Regex]) -> Result<Vec<i32>, CoreError> {
    if patterns.is_empty() {
        return Ok(Vec::new());
    }
    let logical_lines = multiline_map(source);
    Ok(
        matching_lines(source, patterns, &logical_lines, "partial branch")?
            .into_iter()
            .collect(),
    )
}

fn partial_lines_with_map(
    source: &str,
    patterns: &[Regex],
    logical_lines: &BTreeMap<i32, i32>,
) -> Result<Vec<i32>, CoreError> {
    Ok(
        matching_lines(source, patterns, logical_lines, "partial branch")?
            .into_iter()
            .collect(),
    )
}

/// Analyze source using precompiled exclusion expressions.
///
/// # Errors
/// Returns an invalid-input error when matching exceeds the regex runtime limit.
pub fn analyze_with_patterns(
    source: &str,
    patterns: &[Regex],
    collect_arcs: bool,
    python_semantics: PythonSemantics,
) -> Result<SourceAnalysis, CoreError> {
    analyze_uncached_with_patterns(source, patterns, &[], collect_arcs, python_semantics)
}

pub(crate) fn analyze_cached_with_patterns(
    source: &str,
    patterns: &[Regex],
    partial_patterns: &[Regex],
    excludes: &[String],
    partials: &[String],
    collect_arcs: bool,
    python_semantics: PythonSemantics,
) -> Result<SourceAnalysis, CoreError> {
    analyze_cached(
        source,
        excludes,
        partials,
        collect_arcs,
        python_semantics,
        || {
            analyze_uncached_with_patterns(
                source,
                patterns,
                partial_patterns,
                collect_arcs,
                python_semantics,
            )
        },
    )
}

pub(crate) fn analyze_cached(
    source: &str,
    excludes: &[String],
    partials: &[String],
    collect_arcs: bool,
    python_semantics: PythonSemantics,
    compute: impl FnOnce() -> Result<SourceAnalysis, CoreError>,
) -> Result<SourceAnalysis, CoreError> {
    let cache = analysis_cache();
    let key =
        cache.map(|_| analysis_key(source, excludes, partials, collect_arcs, python_semantics));
    let cached = cache.zip(key).and_then(|(cache, key)| {
        let mut cache = cache
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        cache.get_or_insert_default().get(key)
    });
    if let Some(analysis) = cached {
        return Ok(analysis.as_ref().clone());
    }
    let analysis = compute()?;
    let released = cache.zip(key).map_or_else(Vec::new, |(cache, key)| {
        let cached = CachedAnalysis::new(analysis.clone());
        let mut cache = cache
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        cache.get_or_insert_default().insert(key, cached)
    });
    drop(released);
    Ok(analysis)
}

pub(crate) fn analyze_uncached_with_patterns(
    source: &str,
    patterns: &[Regex],
    partial_patterns: &[Regex],
    collect_arcs: bool,
    python_semantics: PythonSemantics,
) -> Result<SourceAnalysis, CoreError> {
    let parsed = match ruff_python_parser::parse_module(source) {
        Ok(parsed) if parsed.errors().is_empty() => parsed,
        Ok(parsed) => {
            let detail = parsed
                .errors()
                .first()
                .map_or_else(|| "invalid syntax".to_owned(), ToString::to_string);
            return Ok(failed_analysis(SourceFailure::NotPython { detail }));
        }
        Err(error) => {
            return Ok(failed_analysis(SourceFailure::NotPython {
                detail: error.to_string(),
            }));
        }
    };
    let locator = Locator::new(source);
    let class_annotations_execute = python_semantics.class_bare_annotations_execute
        || future_annotations_enabled(parsed.suite());
    let mut region_collector = RegionCollector {
        locator: &locator,
        contexts: Vec::new(),
        regions: Vec::new(),
    };
    region_collector.visit_body(parsed.suite());
    let mut executable = ExecutableCollector {
        locator: &locator,
        lines: BTreeSet::new(),
        line_map: BTreeMap::new(),
        annotations_execute: true,
        class_annotations_execute,
        docstring_scope: true,
        code_scope: CodeStatementScope::Executable,
    };
    executable.visit_body(parsed.suite());

    let logical_lines = multiline_map_from_tokens(&locator, parsed.tokens());
    let matched_exclusions = matching_lines(source, patterns, &logical_lines, "exclusion")?;
    let mut exclusions = ExclusionCollector {
        locator: &locator,
        matched_lines: &matched_exclusions,
        lines: matched_exclusions.clone(),
    };
    if !matched_exclusions.is_empty() {
        exclusions.visit_body(parsed.suite());
    }
    let report_excluded = exclusions.lines.iter().copied().collect();
    let mut unreachable = UnreachableCollector {
        debug_enabled: python_semantics.debug_enabled,
        locator: &locator,
        lines: BTreeSet::new(),
        partials: BTreeSet::new(),
        constant_while_else: BTreeSet::new(),
    };
    unreachable.visit_body(parsed.suite());
    let branch_entries = executable.lines.clone();
    executable
        .lines
        .retain(|line| !exclusions.lines.contains(line) && !unreachable.lines.contains(line));

    let (structurally_partial, mut no_branch) = classify_constant_branches(
        source,
        &logical_lines,
        unreachable.partials,
        &unreachable.constant_while_else,
    )?;
    let branch_excluded = &exclusions.lines | &unreachable.lines;

    let (possible_arcs, with_jumps, arc_exit_descriptions) = if collect_arcs {
        collect_branches(
            &locator,
            parsed.suite(),
            &branch_entries,
            &branch_excluded,
            BranchRules {
                structurally_partial: &structurally_partial,
                constant_while_else: &unreachable.constant_while_else,
                compiler_removed: &unreachable.lines,
                debug_enabled: python_semantics.debug_enabled,
            },
        )
    } else {
        (Vec::new(), Vec::new(), BTreeMap::new())
    };
    no_branch.extend(partial_lines_with_map(
        source,
        partial_patterns,
        &logical_lines,
    )?);
    Ok(SourceAnalysis {
        executable: executable.lines.into_iter().collect(),
        excluded: exclusions.lines.into_iter().collect(),
        possible_arcs,
        arc_exit_descriptions,
        no_branch: no_branch.into_iter().collect(),
        source_failure: None,
        regions: region_collector.regions,
        source_line_count: i32::try_from(source.lines().count()).unwrap_or(i32::MAX),
        line_map: executable.line_map,
        report_excluded,
        with_jumps,
    })
}

/// Analyze source with configured exclusions and interpreter annotation semantics.
///
/// # Errors
/// Returns an invalid-input error when an exclusion expression cannot be compiled or matched.
pub fn analyze(
    source: &str,
    excludes: &[String],
    collect_arcs: bool,
    python_semantics: PythonSemantics,
) -> Result<SourceAnalysis, CoreError> {
    let patterns = compile_patterns(excludes)?;
    analyze_cached_with_patterns(
        source,
        &patterns,
        &[],
        excludes,
        &[],
        collect_arcs,
        python_semantics,
    )
}

/// Load and analyze a source file, including configured partial branches.
///
/// Source failures are represented in the returned analysis for report policy.
///
/// # Errors
/// Returns an invalid-input error when a configured expression cannot be compiled or matched.
pub fn analyze_file(
    path: &str,
    excludes: &[String],
    collect_arcs: bool,
    partials: &[String],
    python_semantics: PythonSemantics,
) -> Result<SourceAnalysis, CoreError> {
    let partial_patterns = compile_patterns(partials)?;
    match crate::read_python_source(path) {
        Ok(source) => {
            let exclusion_patterns = compile_patterns(excludes)?;
            let with_path = |error: CoreError| match error {
                CoreError::InvalidInput(message) => {
                    CoreError::InvalidInput(format!("failed to analyze {path}: {message}"))
                }
                other => other,
            };
            let analysis = analyze_cached_with_patterns(
                &source,
                &exclusion_patterns,
                &partial_patterns,
                excludes,
                partials,
                collect_arcs,
                python_semantics,
            )
            .map_err(&with_path)?;
            Ok(analysis)
        }
        Err(error) => Ok(failed_analysis(SourceFailure::from_io_error(&error))),
    }
}

#[cfg(test)]
mod tests;
