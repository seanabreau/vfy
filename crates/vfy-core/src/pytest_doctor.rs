//! Static project scan for native pytest compatibility diagnostics.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use ruff_python_ast::visitor::{self, Visitor};
use ruff_python_ast::{
    Alias, ExceptHandler, Expr, ExprCall, ExprContext, Pattern, Stmt, StmtFunctionDef,
    StmtImportFrom,
};
use ruff_text_size::Ranged;

use crate::SourceLocator;

const MAX_DIRECTORIES: usize = 10_000;
const MAX_CONFTESTS: usize = 1_000;
const IGNORED_DIRECTORIES: &[&str] = &[
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "third_party",
    "tmp",
    "venv",
    "vendor",
];

/// Hooks accepted by the native pytest adapter from project conftests.
pub const SUPPORTED_CONFTEST_HOOKS: &[&str] = &[
    "pytest_addoption",
    "pytest_configure",
    "pytest_unconfigure",
    "pytest_sessionfinish",
    "pytest_report_header",
    "pytest_collection_modifyitems",
    "pytest_collection",
    "pytest_collection_finish",
    "pytest_runtest_protocol",
    "pytest_plugin_registered",
    "pytest_runtest_setup",
    "pytest_runtest_teardown",
    "pytest_sessionstart",
    "pytest_collect_file",
    "pytest_ignore_collect",
    "pytest_pycollect_makemodule",
    "pytest_pycollect_makeitem",
    "pytest_generate_tests",
    "pytest_make_parametrize_id",
    "pytest_itemcollected",
    "pytest_deselected",
    "pytest_html_report_title",
    "pytest_html_results_summary",
    "pytest_html_results_table_header",
    "pytest_asyncio_loop_factories",
];

/// Whether a hook from an installed plug-in module runs unchanged under native
/// execution. pytest calls each before, around, or after the native test loop.
/// Phase hooks are excluded: the native lifecycle runs them only from conftests.
#[must_use]
pub fn native_plugin_hook_supported(hook: &str) -> bool {
    hook == "pytest_addhooks"
        || (SUPPORTED_CONFTEST_HOOKS.contains(&hook)
            && !matches!(hook, "pytest_runtest_setup" | "pytest_runtest_teardown"))
}

/// Whether a plug-in module's hooks are all statically visible and supported.
///
/// The scan is conservative: hook reexports must resolve to Python source in
/// the same package. Assignments to `pytest_*` names, `pytest_plugins`, missing
/// modules, and import cycles are not admitted. `pytest_*` methods of
/// classes count as hooks, because a plug-in can register an instance at
/// configuration time, as pytest-run-parallel does.
#[must_use]
pub fn native_plugin_module_supported(path: &Path) -> bool {
    let Ok(path) = path.canonicalize() else {
        return false;
    };
    let package = plugin_package(&path);
    scan_plugin_module(&path, package.as_deref(), &mut BTreeSet::new(), &mut 64)
}

fn plugin_package(path: &Path) -> Option<PathBuf> {
    let mut package = path.parent()?;
    if !package.join("__init__.py").is_file() {
        return None;
    }
    while let Some(parent) = package.parent() {
        if !parent.join("__init__.py").is_file() {
            break;
        }
        package = parent;
    }
    Some(package.to_path_buf())
}

fn scan_plugin_module(
    path: &Path,
    package: Option<&Path>,
    visiting: &mut BTreeSet<PathBuf>,
    remaining: &mut usize,
) -> bool {
    // Bound recursive source inspection and reject cycles rather than assuming
    // Python's partially initialized modules expose the requested hook.
    if *remaining == 0 || visiting.len() >= 32 || !visiting.insert(path.to_path_buf()) {
        return false;
    }
    *remaining -= 1;
    let Ok(source) = fs::read_to_string(path) else {
        return false;
    };
    let supported = ruff_python_parser::parse_module(&source).is_ok_and(|parsed| {
        let mut scan = PluginHookScan {
            supported: true,
            path,
            package,
            visiting,
            remaining,
        };
        scan.visit_body(parsed.suite());
        scan.supported
    });
    visiting.remove(path);
    supported
}

struct PluginHookScan<'a> {
    supported: bool,
    path: &'a Path,
    package: Option<&'a Path>,
    visiting: &'a mut BTreeSet<PathBuf>,
    remaining: &'a mut usize,
}

impl PluginHookScan<'_> {
    fn hook_reexport_supported(&mut self, import: &StmtImportFrom) -> bool {
        let Some(package) = self.package else {
            return false;
        };
        // Renaming a non-hook or another hook conceals its effective hook name.
        if import.names.iter().any(|alias| {
            let bound = alias.asname.as_ref().unwrap_or(&alias.name).as_str();
            bound.starts_with("pytest_")
                && (bound != alias.name.as_str() || !native_plugin_hook_supported(bound))
        }) {
            return false;
        }
        let mut module = if import.level == 0 {
            let Some(parent) = package.parent() else {
                return false;
            };
            parent.to_path_buf()
        } else {
            let Some(parent) = self.path.parent() else {
                return false;
            };
            let mut parent = parent.to_path_buf();
            for _ in 1..import.level {
                if !parent.pop() {
                    return false;
                }
            }
            parent
        };
        if let Some(name) = &import.module {
            for part in name.as_str().split('.') {
                module.push(part);
            }
        }
        if !module.starts_with(package) {
            return false;
        }
        let package_module = module.join("__init__.py");
        let file_module = module.with_extension("py");
        // Ambiguous layouts and extension/namespace packages require Python's
        // import machinery; source-only admission leaves them unadmitted.
        let target = match (package_module.is_file(), file_module.is_file()) {
            (true, false) => package_module,
            (false, true) => file_module,
            _ => return false,
        };
        let Ok(target) = target.canonicalize() else {
            return false;
        };
        target.starts_with(package)
            && scan_plugin_module(&target, Some(package), self.visiting, self.remaining)
    }
}

fn plugin_hook_supported(function: &StmtFunctionDef) -> bool {
    plugin_hook_decorators_supported(function)
        && hook_name(function).is_some_and(native_plugin_hook_supported)
}

fn class_hooks_supported(statements: &[Stmt]) -> bool {
    statements.iter().all(|statement| match statement {
        Stmt::FunctionDef(function) if function.name.as_str().starts_with("pytest_") => {
            plugin_hook_supported(function)
        }
        Stmt::ClassDef(class) => class_hooks_supported(&class.body),
        _ => true,
    })
}

impl<'a> Visitor<'a> for PluginHookScan<'_> {
    fn visit_stmt(&mut self, statement: &'a Stmt) {
        if !self.supported {
            return;
        }
        match statement {
            Stmt::ImportFrom(import)
                if import.names.iter().any(|alias| {
                    alias
                        .asname
                        .as_ref()
                        .unwrap_or(&alias.name)
                        .as_str()
                        .starts_with("pytest_")
                }) =>
            {
                self.supported = !import.names.iter().any(|alias| alias.name.as_str() == "*")
                    && self.hook_reexport_supported(import);
            }
            // Function and class bodies do not bind names in the plug-in module.
            Stmt::FunctionDef(function) => {
                if function.name.as_str().starts_with("pytest_") {
                    self.supported = plugin_hook_supported(function);
                }
            }
            Stmt::ClassDef(class) => {
                self.supported = !class.name.as_str().starts_with("pytest_")
                    && class_hooks_supported(&class.body);
            }
            _ => visitor::walk_stmt(self, statement),
        }
    }

    fn visit_expr(&mut self, expression: &'a Expr) {
        if matches!(expression, Expr::Name(name)
            if name.ctx == ExprContext::Store && name.id.as_str().starts_with("pytest_"))
        {
            self.supported = false;
        } else {
            visitor::walk_expr(self, expression);
        }
    }

    fn visit_alias(&mut self, alias: &'a Alias) {
        let bound = alias.asname.as_ref().unwrap_or(&alias.name).as_str();
        self.supported &= bound != "*" && !bound.starts_with("pytest_");
    }

    fn visit_except_handler(&mut self, handler: &'a ExceptHandler) {
        let ExceptHandler::ExceptHandler(details) = handler;
        if details
            .name
            .as_ref()
            .is_some_and(|name| name.as_str().starts_with("pytest_"))
        {
            self.supported = false;
        }
        visitor::walk_except_handler(self, handler);
    }

    fn visit_pattern(&mut self, pattern: &'a Pattern) {
        let bound = match pattern {
            Pattern::MatchAs(pattern) => pattern.name.as_ref(),
            Pattern::MatchStar(pattern) => pattern.name.as_ref(),
            Pattern::MatchMapping(pattern) => pattern.rest.as_ref(),
            _ => None,
        };
        if bound.is_some_and(|name| name.as_str().starts_with("pytest_")) {
            self.supported = false;
        } else {
            visitor::walk_pattern(self, pattern);
        }
    }
}

fn plugin_hook_decorators_supported(function: &StmtFunctionDef) -> bool {
    // Aliased decorators and dynamic options can change a supported function's
    // specname to an unsupported execution hook.
    function.decorator_list.iter().all(|decorator| {
        let Expr::Call(call) = &decorator.expression else {
            return is_hookimpl(&decorator.expression);
        };
        is_hookimpl(&call.func)
            && call.arguments.keywords.iter().all(|keyword| {
                keyword.arg.as_ref().is_some_and(|name| {
                    name.as_str() != "specname"
                        || matches!(
                            &keyword.value,
                            Expr::StringLiteral(_) | Expr::NoneLiteral(_)
                        )
                })
            })
    })
}

/// A source declaration category returned by the static scan.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConftestFindingKind {
    Plugin,
    DynamicPlugin,
    UnsupportedHook,
    UnsupportedHookWrapper,
    SyntaxError,
}

impl ConftestFindingKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Plugin => "plugin",
            Self::DynamicPlugin => "dynamic_plugin",
            Self::UnsupportedHook => "unsupported_hook",
            Self::UnsupportedHookWrapper => "unsupported_hook_wrapper",
            Self::SyntaxError => "syntax_error",
        }
    }
}

/// One source declaration relevant to native pytest.
#[derive(Debug, Eq, PartialEq)]
pub struct ConftestFinding {
    pub kind: ConftestFindingKind,
    pub name: String,
    pub path: PathBuf,
    pub line: Option<usize>,
}

/// Bounded scan result in deterministic path and source order.
#[derive(Debug, Default, Eq, PartialEq)]
pub struct ConftestScan {
    pub findings: Vec<ConftestFinding>,
    pub conftest_count: usize,
    pub truncated: bool,
}

/// Inspect project conftests without importing project code.
#[must_use]
pub fn scan_conftests(root: &Path) -> ConftestScan {
    let mut stack = vec![root.to_path_buf()];
    let mut paths = Vec::new();
    let mut directories_seen = 0;
    let mut truncated = false;
    while let Some(directory) = stack.pop() {
        directories_seen += 1;
        if directories_seen > MAX_DIRECTORIES {
            truncated = true;
            break;
        }
        let Ok(entries) = fs::read_dir(&directory) else {
            continue;
        };
        let mut children = Vec::new();
        let mut has_conftest = false;
        for entry in entries.flatten() {
            let name = entry.file_name();
            if name == "conftest.py"
                && entry.file_type().is_ok_and(|kind| {
                    kind.is_file() || (kind.is_symlink() && !entry.path().is_dir())
                })
            {
                has_conftest = true;
            }
            let Some(name) = name.to_str() else {
                continue;
            };
            if name.starts_with('.') || IGNORED_DIRECTORIES.contains(&name) {
                continue;
            }
            if entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                children.push(entry.path());
            }
        }
        if has_conftest {
            paths.push(directory.join("conftest.py"));
            if paths.len() >= MAX_CONFTESTS {
                truncated = true;
                break;
            }
        }
        children.sort();
        stack.extend(children.into_iter().rev());
    }
    paths.sort();
    let findings = paths
        .iter()
        .flat_map(|path| inspect_conftest(path))
        .collect();
    ConftestScan {
        findings,
        conftest_count: paths.len(),
        truncated,
    }
}

fn inspect_conftest(path: &Path) -> Vec<ConftestFinding> {
    let source = match fs::read_to_string(path) {
        Ok(source) => source,
        Err(error) => return vec![syntax_finding(path, &error.to_string(), None)],
    };
    let locator = SourceLocator::new(&source);
    let parsed = match ruff_python_parser::parse_module(&source) {
        Ok(parsed) => parsed,
        Err(error) => {
            let line = source_line(&locator, error.range().start().to_usize());
            return vec![syntax_finding(path, &error.to_string(), Some(line))];
        }
    };
    let mut findings = Vec::new();
    for statement in parsed.suite() {
        let line = source_line(&locator, statement.range().start().to_usize());
        let plugin_value = match statement {
            Stmt::Assign(assign) if assign.targets.iter().any(is_pytest_plugins) => {
                Some(assign.value.as_ref())
            }
            Stmt::AnnAssign(assign) if is_pytest_plugins(&assign.target) => assign.value.as_deref(),
            Stmt::AugAssign(assign) if is_pytest_plugins(&assign.target) => {
                Some(assign.value.as_ref())
            }
            _ => None,
        };
        if let Some(value) = plugin_value {
            add_plugin_findings(&mut findings, path, line, value);
        }
        let Stmt::FunctionDef(function) = statement else {
            continue;
        };
        let Some(hook) = hook_name(function) else {
            continue;
        };
        let kind = if !SUPPORTED_CONFTEST_HOOKS.contains(&hook) {
            Some(ConftestFindingKind::UnsupportedHook)
        } else if matches!(hook, "pytest_runtest_setup" | "pytest_runtest_teardown")
            && has_literal_hook_wrapper(function)
        {
            Some(ConftestFindingKind::UnsupportedHookWrapper)
        } else {
            None
        };
        if let Some(kind) = kind {
            let function_line = source_line(&locator, function.name.range.start().to_usize());
            findings.push(ConftestFinding {
                kind,
                name: hook.to_owned(),
                path: path.to_path_buf(),
                line: Some(function_line),
            });
        }
    }
    findings
}

fn source_line(locator: &SourceLocator<'_>, offset: usize) -> usize {
    usize::try_from(locator.line(offset)).unwrap_or(usize::MAX)
}

fn syntax_finding(path: &Path, detail: &str, line: Option<usize>) -> ConftestFinding {
    ConftestFinding {
        kind: ConftestFindingKind::SyntaxError,
        name: format!("cannot inspect conftest: {detail}"),
        path: path.to_path_buf(),
        line,
    }
}

fn is_pytest_plugins(value: &Expr) -> bool {
    matches!(value, Expr::Name(name) if name.id.as_str() == "pytest_plugins")
}

fn is_hookimpl(expression: &Expr) -> bool {
    matches!(expression, Expr::Name(name) if name.id.as_str() == "hookimpl")
        || matches!(expression, Expr::Attribute(attribute)
            if attribute.attr.as_str() == "hookimpl"
                && matches!(attribute.value.as_ref(), Expr::Name(name) if name.id.as_str() == "pytest"))
}

fn hookimpl_call(function: &StmtFunctionDef) -> Option<&ExprCall> {
    // Decorators apply bottom-up; the outermost hookimpl replaces all options,
    // including defaults supplied by a bare @pytest.hookimpl.
    let expression = function
        .decorator_list
        .iter()
        .map(|decorator| &decorator.expression)
        .find(|expression| match expression {
            Expr::Call(call) => is_hookimpl(&call.func),
            expression => is_hookimpl(expression),
        })?;
    match expression {
        Expr::Call(call) => Some(call),
        _ => None,
    }
}

fn hook_name(function: &StmtFunctionDef) -> Option<&str> {
    if let Some(call) = hookimpl_call(function) {
        for keyword in &call.arguments.keywords {
            if keyword
                .arg
                .as_ref()
                .is_some_and(|name| name.as_str() == "specname")
                && let Expr::StringLiteral(value) = &keyword.value
                && !value.value.to_str().is_empty()
            {
                return Some(value.value.to_str());
            }
        }
    }
    let name = function.name.as_str();
    name.starts_with("pytest_").then_some(name)
}

fn has_literal_hook_wrapper(function: &StmtFunctionDef) -> bool {
    let Some(call) = hookimpl_call(function) else {
        return false;
    };
    if call.arguments.keywords.iter().any(|keyword| {
        keyword.arg.is_none()
            || (keyword
                .arg
                .as_ref()
                .is_some_and(|name| name.as_str() == "specname")
                && !matches!(&keyword.value, Expr::StringLiteral(_)))
    }) {
        return false;
    }
    call.arguments.keywords.iter().any(|keyword| {
        keyword
            .arg
            .as_ref()
            .is_some_and(|name| matches!(name.as_str(), "wrapper" | "hookwrapper"))
            && matches!(&keyword.value, Expr::BooleanLiteral(value) if value.value)
    })
}

fn add_plugin_findings(
    findings: &mut Vec<ConftestFinding>,
    path: &Path,
    line: usize,
    value: &Expr,
) {
    let names = match value {
        Expr::StringLiteral(literal) => {
            let name = literal.value.to_str();
            if name.is_empty() {
                Some(Vec::new())
            } else {
                Some(name.split(',').map(str::to_owned).collect())
            }
        }
        Expr::Tuple(tuple) => literal_sequence(&tuple.elts),
        Expr::List(list) => literal_sequence(&list.elts),
        Expr::Dict(dict) if dict.items.is_empty() => Some(Vec::new()),
        Expr::Set(set) if set.elts.is_empty() => Some(Vec::new()),
        Expr::Call(call)
            if matches!(call.func.as_ref(), Expr::Name(name) if name.id.as_str() == "set")
                && call.arguments.args.is_empty()
                && call.arguments.keywords.is_empty() =>
        {
            Some(Vec::new())
        }
        Expr::NoneLiteral(_)
        | Expr::BooleanLiteral(ruff_python_ast::ExprBooleanLiteral { value: false, .. }) => {
            Some(Vec::new())
        }
        Expr::BooleanLiteral(_) => Some(vec!["pytest_plugins".to_owned()]),
        Expr::NumberLiteral(number) => {
            let nonzero = match &number.value {
                ruff_python_ast::Number::Int(value) => value.to_string() != "0",
                ruff_python_ast::Number::Float(value) => *value != 0.0,
                ruff_python_ast::Number::Complex { real, imag } => *real != 0.0 || *imag != 0.0,
            };
            Some(if nonzero {
                vec!["pytest_plugins".to_owned()]
            } else {
                Vec::new()
            })
        }
        _ => None,
    };
    if let Some(names) = names {
        findings.extend(names.into_iter().map(|name| ConftestFinding {
            kind: ConftestFindingKind::Plugin,
            name,
            path: path.to_path_buf(),
            line: Some(line),
        }));
    } else {
        findings.push(ConftestFinding {
            kind: ConftestFindingKind::DynamicPlugin,
            name: "pytest_plugins".to_owned(),
            path: path.to_path_buf(),
            line: Some(line),
        });
    }
}

fn literal_sequence(values: &[Expr]) -> Option<Vec<String>> {
    values
        .iter()
        .map(|value| match value {
            Expr::StringLiteral(value) => Some(value.value.to_str().to_owned()),
            _ => None,
        })
        .collect()
}

#[cfg(test)]
mod tests;
