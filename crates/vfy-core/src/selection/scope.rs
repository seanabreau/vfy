//! Function-body ownership of changed source lines.
//!
//! A change confined to a function body can affect only tests that execute that
//! body. A change to a definition header, decorator, docstring, class body, or
//! module statement can affect every importer, so it keeps file-level selection.

use std::collections::BTreeSet;

use ruff_python_ast::statement_visitor::{StatementVisitor, walk_stmt};
use ruff_python_ast::visitor::{Visitor, walk_expr, walk_stmt as walk_expr_stmt};
use ruff_python_ast::{Expr, Stmt, StmtFunctionDef};
use ruff_text_size::Ranged;

use crate::SourceLocator;

/// One unified-diff hunk with the old-side and new-side text it covers.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct DiffHunk {
    pub old_start: i32,
    pub old_count: i32,
    pub new_start: i32,
    pub new_count: i32,
    /// Context and removed lines, in order.
    pub old_text: Vec<String>,
    /// Context and added lines, in order.
    pub new_text: Vec<String>,
    /// Old-side line numbers of removed lines.
    pub removed: BTreeSet<i32>,
    /// New-side line numbers of added lines.
    pub added: BTreeSet<i32>,
}

/// Executable body of one function, after its header and docstring.
#[derive(Clone, Debug, Eq, PartialEq)]
struct FunctionBody {
    name: String,
    start: i32,
    end: i32,
    generator: bool,
}

impl FunctionBody {
    const fn contains(&self, line: i32) -> bool {
        self.start <= line && line <= self.end
    }
}

/// Decorators that return the function itself or a descriptor that calls it on
/// every access. Any other decorator can cache or replace the body's result, so
/// a call that reuses that result never executes the changed lines.
fn transparent_decorator(expression: &Expr) -> bool {
    match expression {
        Expr::Name(name) => matches!(
            name.id.as_str(),
            "property" | "staticmethod" | "classmethod" | "abstractmethod" | "override" | "final"
        ),
        Expr::Attribute(attribute) => {
            matches!(attribute.attr.as_str(), "setter" | "getter" | "deleter")
                || matches!(
                    (attribute.value.as_ref(), attribute.attr.as_str()),
                    (Expr::Name(module), "abstractmethod") if module.id.as_str() == "abc"
                )
                || matches!(
                    (attribute.value.as_ref(), attribute.attr.as_str()),
                    (Expr::Name(module), "override" | "final")
                        if matches!(module.id.as_str(), "typing" | "typing_extensions")
                )
        }
        _ => false,
    }
}

#[derive(Default)]
struct GeneratorProbe {
    found: bool,
}

impl<'a> Visitor<'a> for GeneratorProbe {
    fn visit_stmt(&mut self, statement: &'a Stmt) {
        if !matches!(statement, Stmt::FunctionDef(_) | Stmt::ClassDef(_)) {
            walk_expr_stmt(self, statement);
        }
    }

    fn visit_expr(&mut self, expression: &'a Expr) {
        match expression {
            Expr::Yield(_) | Expr::YieldFrom(_) => self.found = true,
            Expr::Lambda(_) => {}
            _ => walk_expr(self, expression),
        }
    }
}

struct BodyCollector<'a> {
    locator: SourceLocator<'a>,
    scope: Vec<String>,
    bodies: Vec<FunctionBody>,
}

impl BodyCollector<'_> {
    fn line(&self, offset: usize) -> i32 {
        self.locator.line(offset)
    }

    fn record(&mut self, function: &StmtFunctionDef) {
        if !function
            .decorator_list
            .iter()
            .all(|decorator| transparent_decorator(&decorator.expression))
        {
            return;
        }
        let statements = match function.body.first() {
            Some(Stmt::Expr(docstring)) if docstring.value.is_string_literal_expr() => {
                &function.body[1..]
            }
            _ => &function.body[..],
        };
        let (Some(first), Some(last)) = (statements.first(), statements.last()) else {
            return;
        };
        let header_end = function
            .returns
            .as_ref()
            .map_or(function.parameters.range().end(), |returns| {
                returns.range().end()
            })
            .to_usize();
        let start = self.line(first.range().start().to_usize());
        if start <= self.line(header_end) {
            return;
        }
        let mut probe = GeneratorProbe::default();
        for statement in &function.body {
            probe.visit_stmt(statement);
        }
        self.bodies.push(FunctionBody {
            name: self
                .scope
                .iter()
                .map(String::as_str)
                .chain(std::iter::once(function.name.as_str()))
                .collect::<Vec<_>>()
                .join("."),
            start,
            end: self.line(last.range().end().to_usize().saturating_sub(1)),
            generator: probe.found,
        });
    }
}

impl<'a> StatementVisitor<'a> for BodyCollector<'_> {
    fn visit_stmt(&mut self, statement: &'a Stmt) {
        let name = match statement {
            Stmt::FunctionDef(function) => {
                self.record(function);
                Some(function.name.as_str())
            }
            Stmt::ClassDef(class) => Some(class.name.as_str()),
            _ => None,
        };
        if let Some(name) = name {
            self.scope.push(name.to_owned());
        }
        walk_stmt(self, statement);
        if name.is_some() {
            self.scope.pop();
        }
    }
}

fn function_bodies(source: &str) -> Option<Vec<FunctionBody>> {
    let parsed = ruff_python_parser::parse_module(source).ok()?;
    if !parsed.errors().is_empty() {
        return None;
    }
    let mut collector = BodyCollector {
        locator: SourceLocator::new(source),
        scope: Vec::new(),
        bodies: Vec::new(),
    };
    collector.visit_body(&parsed.syntax().body);
    Some(collector.bodies)
}

/// Innermost function body that owns `line`.
fn owner(bodies: &[FunctionBody], line: i32) -> Option<&FunctionBody> {
    bodies
        .iter()
        .filter(|body| body.contains(line))
        .min_by_key(|body| body.end - body.start)
}

/// Rebuild the pre-change source by reversing each hunk on the current source.
///
/// Returns `None` when the hunk text does not match the current source.
fn previous_source(current: &str, hunks: &[DiffHunk]) -> Option<String> {
    let lines: Vec<&str> = current.lines().collect();
    let mut previous: Vec<&str> = Vec::with_capacity(lines.len());
    let mut cursor = 0_usize;
    for hunk in hunks {
        let begin = usize::try_from(if hunk.new_count == 0 {
            hunk.new_start
        } else {
            hunk.new_start.checked_sub(1)?
        })
        .ok()?;
        let end = begin.checked_add(usize::try_from(hunk.new_count).ok()?)?;
        if begin < cursor || end > lines.len() || lines[begin..end] != hunk.new_text[..] {
            return None;
        }
        previous.extend_from_slice(&lines[cursor..begin]);
        previous.extend(hunk.old_text.iter().map(String::as_str));
        cursor = end;
    }
    previous.extend_from_slice(&lines[cursor..]);
    let mut text = previous.join("\n");
    text.push('\n');
    Some(text)
}

/// Old-side line spans of the function bodies that own every change in a file.
///
/// Returns `None` when any changed line lies outside a function body, the source
/// cannot be parsed before or after the change, or a change alters whether a
/// function is a generator. Callers then keep file-level selection.
pub(super) fn changed_function_spans(current: &str, hunks: &[DiffHunk]) -> Option<Vec<(i32, i32)>> {
    if hunks.is_empty() {
        return None;
    }
    let previous = previous_source(current, hunks)?;
    let old = function_bodies(&previous)?;
    let new = function_bodies(current)?;
    let mut owners: BTreeSet<(i32, i32)> = BTreeSet::new();
    let mut names: BTreeSet<&str> = BTreeSet::new();
    for hunk in hunks {
        for line in &hunk.removed {
            names.insert(owner(&old, *line)?.name.as_str());
        }
        for line in &hunk.added {
            // A nested function added inside a body runs only when its enclosing
            // function runs, so attribute it to the closest existing function.
            let mut name = owner(&new, *line)?.name.as_str();
            while !old.iter().any(|body| body.name == name) {
                name = name.rsplit_once('.')?.0;
            }
            names.insert(name);
        }
    }
    for name in names {
        let before: Vec<&FunctionBody> = old.iter().filter(|body| body.name == name).collect();
        let after: BTreeSet<bool> = new
            .iter()
            .filter(|body| body.name == name)
            .map(|body| body.generator)
            .collect();
        // A nested function removed with its whole definition has no new body.
        if before.is_empty()
            || (!after.is_empty()
                && before
                    .iter()
                    .map(|body| body.generator)
                    .collect::<BTreeSet<_>>()
                    != after)
        {
            return None;
        }
        owners.extend(before.iter().map(|body| (body.start, body.end)));
    }
    Some(owners.into_iter().collect())
}
