//! Coverage.py-compatible branch arcs derived from the Python syntax tree.

use std::collections::{BTreeMap, BTreeSet};

use ruff_python_ast::{ExceptHandler, Expr, Pattern, Stmt};
use ruff_text_size::Ranged;

use super::{Arc, ExitDescription, Locator, WithJump, constant_test};

#[derive(Clone, Copy)]
enum Continuation {
    Fallthrough(i32),
    // Retain the cleanup destination for measured with-statement rewrites.
    Abrupt(i32),
}

impl Continuation {
    fn destination(self) -> i32 {
        match self {
            Self::Fallthrough(line) | Self::Abrupt(line) => line,
        }
    }
}

#[derive(Clone, Copy)]
struct SuiteFlow {
    falls_through: bool,
    loop_break: bool,
}

impl SuiteFlow {
    const NORMAL: Self = Self {
        falls_through: true,
        loop_break: false,
    };
    const ABRUPT: Self = Self {
        falls_through: false,
        loop_break: false,
    };

    fn union(self, other: Self) -> Self {
        Self {
            falls_through: self.falls_through || other.falls_through,
            loop_break: self.loop_break || other.loop_break,
        }
    }
}

pub(super) struct BranchRules<'a> {
    pub(super) structurally_partial: &'a BTreeSet<i32>,
    pub(super) constant_while_else: &'a BTreeSet<i32>,
    pub(super) compiler_removed: &'a BTreeSet<i32>,
    pub(super) debug_enabled: bool,
}

struct BranchCollector<'a> {
    locator: &'a Locator<'a>,
    executable: &'a BTreeSet<i32>,
    excluded: &'a BTreeSet<i32>,
    rules: BranchRules<'a>,
    arcs: BTreeSet<Arc>,
    arc_exit_descriptions: BTreeMap<Arc, ExitDescription>,
    scope: ExitDescription,
    with_jumps: Vec<WithJump>,
}

impl BranchCollector<'_> {
    fn definition_line(&self, statement: &Stmt) -> i32 {
        match statement {
            Stmt::FunctionDef(function) => function.decorator_list.first().map_or_else(
                || self.locator.statement_line(statement),
                |decorator| self.locator.line(decorator.range.start().to_usize()),
            ),
            Stmt::ClassDef(class) => class.decorator_list.first().map_or_else(
                || self.locator.statement_line(statement),
                |decorator| self.locator.line(decorator.range.start().to_usize()),
            ),
            _ => self.locator.statement_line(statement),
        }
    }

    fn entry(&self, body: &[Stmt], fallback: Continuation) -> Continuation {
        body.iter()
            .map(|statement| self.definition_line(statement))
            .find(|line| {
                self.executable.contains(line) && !self.rules.compiler_removed.contains(line)
            })
            .map_or(fallback, Continuation::Fallthrough)
    }

    fn add(&mut self, start: i32, end: Continuation) {
        if let Continuation::Fallthrough(end) = end
            && start != end
        {
            self.arcs.insert((start, end));
            if end < 0 {
                self.arc_exit_descriptions
                    .insert((start, end), self.scope.clone());
            }
        }
    }

    fn with_block(
        &mut self,
        with_statement: &ruff_python_ast::StmtWith,
        line: i32,
        after: Continuation,
    ) -> SuiteFlow {
        // Cleanup rewrites affect measured arcs, so keep them out of excluded suites.
        if !self.excluded.contains(&line)
            && let (Some(first), Some(last)) =
                (with_statement.body.first(), with_statement.body.last())
        {
            self.with_jumps.push(WithJump {
                header: line,
                body_start: self.locator.statement_line(first),
                body_end: self.locator.line(last.range().end().to_usize()),
                after: after.destination(),
            });
        }
        self.block(&with_statement.body, after)
    }

    fn block(&mut self, body: &[Stmt], after: Continuation) -> SuiteFlow {
        let mut flow = SuiteFlow::NORMAL;
        for (index, statement) in body.iter().enumerate() {
            let line = self.definition_line(statement);
            if !self.executable.contains(&line) || self.rules.compiler_removed.contains(&line) {
                continue;
            }
            let following = self.entry(&body[index + 1..], after);
            let statement_flow = self.statement(statement, following);
            flow.falls_through = statement_flow.falls_through;
            flow.loop_break |= statement_flow.loop_break;
        }
        flow
    }

    fn definition_block(
        &mut self,
        statement: &Stmt,
        body: &[Stmt],
        after: Continuation,
        child_scope: ExitDescription,
    ) {
        let line = self.locator.statement_line(statement);
        let exit = -self.definition_line(statement);
        if body
            .first()
            .is_some_and(|child| self.locator.statement_line(child) == line)
        {
            self.add(line, after);
            let parent_scope = std::mem::replace(&mut self.scope, child_scope.clone());
            self.add(line, Continuation::Fallthrough(exit));
            self.scope = parent_scope;
        }
        let parent_scope = std::mem::replace(&mut self.scope, child_scope);
        self.block(body, Continuation::Fallthrough(exit));
        self.scope = parent_scope;
    }

    fn conditional_flow(&self, test: &Expr, body: SuiteFlow, alternate: SuiteFlow) -> SuiteFlow {
        match constant_test(test, self.rules.debug_enabled) {
            Some(true) => body,
            Some(false) => alternate,
            None => body.union(alternate),
        }
    }

    fn if_block(
        &mut self,
        if_statement: &ruff_python_ast::StmtIf,
        line: i32,
        after: Continuation,
    ) -> SuiteFlow {
        let false_destination = if_statement
            .elif_else_clauses
            .first()
            .map_or(after, |clause| {
                clause.test.as_ref().map_or_else(
                    || self.entry(&clause.body, after),
                    |_| {
                        Continuation::Fallthrough(
                            self.locator.line(clause.range.start().to_usize()),
                        )
                    },
                )
            });
        if !self.rules.structurally_partial.contains(&line) {
            self.add(line, self.entry(&if_statement.body, after));
            self.add(line, false_destination);
        }
        let body_flow = self.block(&if_statement.body, after);
        let mut alternatives = Vec::new();
        for (index, clause) in if_statement.elif_else_clauses.iter().enumerate() {
            let next = if_statement.elif_else_clauses.get(index + 1);
            if clause.test.is_some() {
                let origin = self.locator.line(clause.range.start().to_usize());
                let false_destination = next.map_or(after, |next_clause| {
                    next_clause.test.as_ref().map_or_else(
                        || self.entry(&next_clause.body, after),
                        |_| {
                            Continuation::Fallthrough(
                                self.locator.line(next_clause.range.start().to_usize()),
                            )
                        },
                    )
                });
                self.add(origin, self.entry(&clause.body, after));
                self.add(origin, false_destination);
            }
            alternatives.push((clause.test.as_ref(), self.block(&clause.body, after)));
        }
        let mut alternate_flow = SuiteFlow::NORMAL;
        for (test, flow) in alternatives.into_iter().rev() {
            alternate_flow = test.map_or(flow, |test| {
                self.conditional_flow(test, flow, alternate_flow)
            });
        }
        self.conditional_flow(&if_statement.test, body_flow, alternate_flow)
    }

    fn try_block(
        &mut self,
        try_statement: &ruff_python_ast::StmtTry,
        after: Continuation,
    ) -> SuiteFlow {
        let final_entry = self.entry(&try_statement.finalbody, after);
        let else_entry = self.entry(&try_statement.orelse, final_entry);
        let body_flow = self.block(&try_statement.body, else_entry);
        let else_flow = self.block(&try_statement.orelse, final_entry);
        let else_has_statements = try_statement.orelse.iter().any(|statement| {
            let line = self.definition_line(statement);
            self.executable.contains(&line) && !self.rules.compiler_removed.contains(&line)
        });
        let mut flow = SuiteFlow {
            // Coverage.py processes an executable else suite even when the try body
            // has no normal exits, retaining the else suite's syntactic exits.
            falls_through: if else_has_statements {
                else_flow.falls_through
            } else {
                body_flow.falls_through
            },
            loop_break: body_flow.loop_break || else_flow.loop_break,
        };
        for handler in &try_statement.handlers {
            let ExceptHandler::ExceptHandler(handler) = handler;
            flow = flow.union(self.block(&handler.body, final_entry));
        }
        // Abrupt entries retain internal cleanup decisions without creating a normal exit.
        let final_after = if flow.falls_through {
            after
        } else {
            Continuation::Abrupt(after.destination())
        };
        let final_flow = self.block(&try_statement.finalbody, final_after);
        SuiteFlow {
            falls_through: flow.falls_through && final_flow.falls_through,
            // The upstream loop block records a break before later cleanup can
            // replace that exit with a return.
            loop_break: final_flow.loop_break || flow.loop_break,
        }
    }

    fn statement(&mut self, statement: &Stmt, after: Continuation) -> SuiteFlow {
        let line = self.locator.statement_line(statement);
        match statement {
            Stmt::If(if_statement) => self.if_block(if_statement, line, after),
            Stmt::For(loop_statement) => {
                let completion = self.entry(&loop_statement.orelse, after);
                self.add(
                    line,
                    self.entry(&loop_statement.body, Continuation::Fallthrough(line)),
                );
                self.add(line, completion);
                let body_flow = self.block(&loop_statement.body, Continuation::Fallthrough(line));
                let mut flow = self.block(&loop_statement.orelse, after);
                flow.falls_through |= body_flow.loop_break;
                flow
            }
            Stmt::While(loop_statement) => {
                let completion = if self.rules.constant_while_else.contains(&line) {
                    after
                } else {
                    self.entry(&loop_statement.orelse, after)
                };
                if !self.rules.structurally_partial.contains(&line) {
                    self.add(
                        line,
                        self.entry(&loop_statement.body, Continuation::Fallthrough(line)),
                    );
                    self.add(line, completion);
                }
                let body_flow = self.block(&loop_statement.body, Continuation::Fallthrough(line));
                let mut flow = self.block(&loop_statement.orelse, after);
                if self.rules.structurally_partial.contains(&line)
                    && loop_statement.orelse.is_empty()
                {
                    flow.falls_through = false;
                }
                flow.falls_through |= body_flow.loop_break;
                flow
            }
            Stmt::Match(match_statement) => {
                for cases in match_statement.cases.windows(2) {
                    let origin = self.locator.line(cases[0].range.start().to_usize());
                    let next = self.locator.line(cases[1].range.start().to_usize());
                    self.add(origin, self.entry(&cases[0].body, after));
                    self.add(origin, Continuation::Fallthrough(next));
                }
                if let Some(last) = match_statement.cases.last()
                    && (last.guard.is_some() || !is_wildcard(&last.pattern))
                {
                    let origin = self.locator.line(last.range.start().to_usize());
                    self.add(origin, self.entry(&last.body, after));
                    self.add(origin, after);
                }
                let mut flow = SuiteFlow::ABRUPT;
                for case in &match_statement.cases {
                    flow = flow.union(self.block(&case.body, after));
                }
                if match_statement
                    .cases
                    .last()
                    .is_none_or(|case| case.guard.is_some() || !is_wildcard(&case.pattern))
                {
                    flow.falls_through = true;
                }
                flow
            }
            Stmt::FunctionDef(function) => {
                self.definition_block(
                    statement,
                    &function.body,
                    after,
                    ExitDescription::ReturnFunction(function.name.to_string()),
                );
                SuiteFlow::NORMAL
            }
            Stmt::ClassDef(class) => {
                self.definition_block(
                    statement,
                    &class.body,
                    after,
                    ExitDescription::ExitClass(class.name.to_string()),
                );
                SuiteFlow::NORMAL
            }
            Stmt::With(with_statement) => self.with_block(with_statement, line, after),
            Stmt::Try(try_statement) => self.try_block(try_statement, after),
            Stmt::Return(_) | Stmt::Raise(_) | Stmt::Continue(_) => SuiteFlow::ABRUPT,
            Stmt::Break(_) => SuiteFlow {
                falls_through: false,
                loop_break: true,
            },
            _ => SuiteFlow::NORMAL,
        }
    }
}

pub(super) fn is_wildcard(pattern: &Pattern) -> bool {
    match pattern {
        Pattern::MatchAs(node) => node.pattern.as_deref().is_none_or(is_wildcard),
        Pattern::MatchOr(node) => node.patterns.last().is_some_and(is_wildcard),
        _ => false,
    }
}

pub(super) fn collect_branches(
    locator: &Locator<'_>,
    suite: &[Stmt],
    executable: &BTreeSet<i32>,
    excluded: &BTreeSet<i32>,
    rules: BranchRules<'_>,
) -> (Vec<Arc>, Vec<WithJump>, BTreeMap<Arc, ExitDescription>) {
    let mut branches = BranchCollector {
        locator,
        executable,
        excluded,
        rules,
        arcs: BTreeSet::new(),
        arc_exit_descriptions: BTreeMap::new(),
        scope: ExitDescription::Module,
        with_jumps: Vec::new(),
    };
    branches.block(suite, Continuation::Fallthrough(-1));
    let with_jumps = std::mem::take(&mut branches.with_jumps);
    let mut by_origin: BTreeMap<i32, Vec<Arc>> = BTreeMap::new();
    for arc in branches.arcs {
        by_origin.entry(arc.0).or_default().push(arc);
    }
    let possible_arcs: Vec<Arc> = by_origin
        .into_iter()
        .filter(|(origin, _)| executable.contains(origin) && !excluded.contains(origin))
        .flat_map(|(_, arcs)| {
            let arcs: Vec<Arc> = arcs
                .into_iter()
                .filter(|(_, destination)| *destination < 0 || !excluded.contains(destination))
                .collect();
            (arcs.len() > 1).then_some(arcs).into_iter().flatten()
        })
        .collect();
    branches
        .arc_exit_descriptions
        .retain(|arc, _| possible_arcs.contains(arc));
    (possible_arcs, with_jumps, branches.arc_exit_descriptions)
}
