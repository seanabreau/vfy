"""CPython assertion compilation and helper-module import registration."""

from __future__ import annotations

import ast
import contextlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import marshal
import os
import sys
import warnings
import weakref
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import BuiltinFunctionType, CodeType, FunctionType, MethodType, ModuleType
from typing import TYPE_CHECKING, cast, override

from vfy._version import __version__
from vfy.mutation import read_mutation_source

if TYPE_CHECKING:
    from _typeshed.importlib import LoaderProtocol

    from .protocol import Config, Item

# AST identifiers cannot collide with identifiers accepted by Python's parser.
_ADAPTER = "@vfy_assertion_adapter"
_BUILTINS = "@vfy_assertion_builtins"
# Execution temporaries are numbered as pytest numbers `@py_assert<N>`, so a
# failed assertion leaves the same values alive until a later assertion
# overwrites the same slot. Diagnostic temporaries have their own namespace and
# are always released.
_TEMPORARY = "@vfy_assertion_"
_EXPLANATION = "@vfy_explain_"

# Cached rewritten code depends on the source bytes, the assert mode, and this
# module's output. Hashing this file keeps an edited rewriter from reusing stale code.
_REWRITE_REVISION = f"{zlib.crc32(Path(__file__).read_bytes()):08x}"
_CACHE_TAIL = f".{sys.implementation.cache_tag}-vfy-{__version__}-{_REWRITE_REVISION}.pyc"


class _AssertionConfiguration:
    def __init__(self, config: Config, previous: _AssertionConfiguration | None) -> None:
        self.config = weakref.ref(config)
        self.previous = previous

    def close(self) -> None:
        global _assertion_configuration  # noqa: PLW0603 - scoped CPython configuration binding
        _assertion_configuration = self.previous


_assertion_configuration: _AssertionConfiguration | None = None


def _current_config() -> Config | None:
    return None if _assertion_configuration is None else _assertion_configuration.config()


def install_assertion_config(config: Config) -> None:
    """Bind the configuration until cleanup runs."""
    global _assertion_configuration  # noqa: PLW0603 - scoped CPython configuration binding
    handle = _AssertionConfiguration(config, _assertion_configuration)
    _assertion_configuration = handle
    try:
        config.add_cleanup(handle.close)
    except BaseException:
        handle.close()
        raise


def _assertion_verbosity() -> int:
    config = _current_config()
    return 0 if config is None else config.get_verbosity("assertions")


def _safe_repr(value: object) -> str:
    if isinstance(value, MethodType):
        return value.__name__
    return _object_repr(value)


def _object_repr(value: object) -> str:
    try:
        return repr(value).replace("\n", "\\n")
    # Mirrors pytest's assertion-rewrite `_safe_repr`: a failing repr becomes text.
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt | SystemExit):
            raise
        return (
            f"<[{type(error).__name__}({str(error)!r}) raised in repr()] "
            f"{type(value).__name__} object at {hex(id(value))}>"
        )


def _name_repr(local: bool, value: object, source: str) -> str:
    if local or not isinstance(value, ModuleType | FunctionType | BuiltinFunctionType | type):
        return _safe_repr(value)
    return source


def _message_text(*message: object) -> str | None:
    if not message:
        return None
    return message[0] if isinstance(message[0], str) else _object_repr(message[0])


def _render_explanation(message: str | None, description: object) -> str:
    from vfy import native  # noqa: PLC0415 - failing assertions acquire the native renderer lazily

    return native._pytest.render_independent_assertion(
        message, json.dumps(description), _assertion_verbosity(), *_assertion_truncation()
    )


def _assertion_truncation() -> tuple[int, int, bool]:
    config = _current_config()
    max_lines = None if config is None else config.getini("truncation_limit_lines")
    max_chars = None if config is None else config.getini("truncation_limit_chars")
    return (
        8 if max_lines is None else int(cast("str | int", max_lines)),
        640 if max_chars is None else int(cast("str | int", max_chars)),
        bool(os.environ.get("CI") or os.environ.get("BUILD_NUMBER")),
    )


def _string_values(selected: int, operands: tuple[object, ...]) -> tuple[str, str, str, str] | None:
    """Return both strings and their representations, which pytest's summary line shows."""
    left, right = operands[selected], operands[selected + 1]
    if type(left) is str and type(right) is str:
        return left, right, _object_repr(left), _object_repr(right)
    return None


def _builtin_comparison(
    selected: int, operators: tuple[str, ...], operands: tuple[object, ...]
) -> str | None:
    from vfy import native  # noqa: PLC0415 - failure-only native object protocols

    scope = cast("tuple[Item, Sequence[object]] | None", native._pytest.current_assertion_item())
    if scope is not None:
        item, exclusions = scope
        config = item.config
        # Plug-ins run before the built-in rules, and the first non-empty answer wins.
        answers = cast(
            "list[Sequence[str] | None]",
            config.pluginmanager.subset_hook_caller("pytest_assertrepr_compare", exclusions)(
                config=config,
                op=operators[selected],
                left=operands[selected],
                right=operands[selected + 1],
            ),
        )
        for lines in answers:
            if lines:
                return native._pytest.render_independent_hook_explanation(
                    lines,
                    _assertion_verbosity(),
                    *_assertion_truncation(),
                )
    return native._pytest.describe_independent_comparison(
        operators[selected],
        operands[selected],
        operands[selected + 1],
        _assertion_verbosity(),
        *_assertion_truncation(),
    )


def _first_failed(results: tuple[object, ...]) -> int:
    for index, result in enumerate(results):
        try:
            failed = not result
        except Exception:  # noqa: BLE001 - diagnostic truth errors retain the failed comparison
            failed = True
        if failed:
            return index
    return len(results) - 1


_OPERATORS: dict[type[ast.AST], str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
    ast.Pow: "**",
    ast.LShift: "<<",
    ast.RShift: ">>",
    ast.BitOr: "|",
    ast.BitAnd: "&",
    ast.BitXor: "^",
    ast.MatMult: "@",
    ast.Not: "not ",
    ast.Invert: "~",
    ast.UAdd: "+",
    ast.USub: "-",
}


def _adapter_call(name: str, arguments: list[ast.expr]) -> ast.Call:
    return ast.Call(ast.Attribute(ast.Name(_ADAPTER, ast.Load()), name, ast.Load()), arguments, [])


def _operator_names(operators: list[ast.cmpop]) -> ast.Tuple:
    return ast.Tuple(
        [ast.Constant(_OPERATORS[type(operator)]) for operator in operators], ast.Load()
    )


def _record(kind: str, **fields: ast.expr) -> ast.Dict:
    return ast.Dict(
        [ast.Constant("kind"), *[ast.Constant(name) for name in fields]],
        [ast.Constant(kind), *fields.values()],
    )


@dataclass
class _Expression:
    setup: list[ast.stmt]
    value: ast.expr
    diagnostic: list[ast.stmt]
    description: ast.expr


_SCOPE_NODES = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _nested_scope(scope: tuple[int, ...], node: ast.AST) -> tuple[int, ...]:
    return (*scope, id(node)) if isinstance(node, _SCOPE_NODES) else scope


class _AssertionRewriter(ast.NodeTransformer):
    def __init__(self) -> None:
        self.slot_index = 0
        self.explanation_index = 0
        self.temporaries: list[str] = []
        self.explanations: list[str] = []
        self.rewritten = False
        # Pytest keeps comparison assignment-expression substitutions across
        # assertion nodes, including call arguments visited afterward, within
        # one enclosing function or class scope.
        self.scopes: dict[tuple[int, ...], dict[str, ast.NamedExpr]] = {}
        self.bindings: dict[str, ast.NamedExpr] = {}

    def rewrite(self, tree: ast.Module) -> None:
        # Match pytest's statement-container traversal so those substitutions
        # preserve their evaluation order across nested suites.
        # Python expressions cannot contain assert statements, including in
        # lambdas and comprehensions, so their subtrees need no traversal.
        pending: list[tuple[ast.AST, tuple[int, ...]]] = [(tree, ())]
        while pending:
            parent, scope = pending.pop()
            self.bindings = self.scopes.setdefault(scope, {})
            for field, value in ast.iter_fields(parent):
                if isinstance(value, list):
                    replaced = []
                    for child in value:
                        if isinstance(child, ast.Assert):
                            replaced.extend(self.visit_Assert(child))
                        else:
                            replaced.append(child)
                            if isinstance(child, ast.AST) and not isinstance(child, ast.expr):
                                pending.append((child, _nested_scope(scope, child)))
                    setattr(parent, field, replaced)
                elif isinstance(value, ast.AST) and not isinstance(value, ast.expr):
                    pending.append((value, _nested_scope(scope, value)))

    def rebound(self, node: ast.expr) -> ast.expr:
        if isinstance(node, ast.Name):
            return self.bindings.get(node.id, node)
        return node

    def slot(self) -> ast.Name:
        """Allocate the next execution temporary, as pytest's `variable()` does."""
        name = f"{_TEMPORARY}{self.slot_index}"
        self.slot_index += 1
        self.temporaries.append(name)
        return ast.Name(name, ast.Load())

    def reserve(self, count: int = 1) -> None:
        """Consume numbers that pytest spends on explanation names vfy stores elsewhere.

        Pytest draws temporaries, `%(py<N>)s` placeholders, and `@py_format<N>`
        results from one counter, so every one of them shifts later slots.
        """
        self.slot_index += count

    def save(self, node: ast.expr, *, explanation: bool = False) -> tuple[ast.stmt, ast.Name]:
        if explanation:
            name = f"{_EXPLANATION}{self.explanation_index}"
            self.explanation_index += 1
            self.explanations.append(name)
            reference = ast.Name(name, ast.Load())
        else:
            reference = self.slot()
        target = ast.Name(reference.id, ast.Store())
        return ast.Assign([target], node), reference

    def spill(self, children: list[_Expression]) -> tuple[list[ast.stmt], list[ast.expr]]:
        """Record each child description in source order."""
        diagnostic: list[ast.stmt] = []
        references: list[ast.expr] = []
        for child in children:
            diagnostic.extend(child.diagnostic)
            text, reference = self.save(child.description, explanation=True)
            diagnostic.append(text)
            references.append(reference)
        return diagnostic, references

    def operation(
        self,
        node: ast.expr,
        children: list[_Expression],
        build: Callable[[list[ast.expr]], ast.expr],
    ) -> tuple[list[ast.stmt], ast.expr, list[ast.stmt], list[ast.expr]]:
        """Evaluate children once, then describe them."""
        statement, value = self.save(ast.copy_location(build([c.value for c in children]), node))
        setup = [step for child in children for step in child.setup]
        diagnostic, references = self.spill(children)
        return [*setup, statement], value, diagnostic, references

    def expression(self, node: ast.expr) -> _Expression:
        if isinstance(node, ast.Name | ast.NamedExpr):
            name = node.id if isinstance(node, ast.Name) else node.target.id
            local = ast.Compare(
                ast.Constant(name),
                [ast.In()],
                [
                    ast.Call(
                        ast.Attribute(ast.Name(_BUILTINS, ast.Load()), "locals", ast.Load()), [], []
                    )
                ],
            )
            self.reserve()
            # Pytest checks locals before evaluating the name for its repr.
            # A named expression can replace an object retained by locals().
            return _Expression(
                [],
                node,
                [],
                _record(
                    "value", value=_adapter_call("_name_repr", [local, node, ast.Constant(name)])
                ),
            )
        if isinstance(node, ast.Compare):
            return self.comparison(node)
        if isinstance(node, ast.BoolOp):
            return self.boolean(node)
        if isinstance(node, ast.BinOp):
            setup, value, diagnostic, (left, right) = self.operation(
                node,
                [self.expression(node.left), self.expression(node.right)],
                lambda values: ast.BinOp(values[0], node.op, values[1]),
            )
            return _Expression(
                setup,
                value,
                diagnostic,
                _record(
                    "binary",
                    operator=ast.Constant(_OPERATORS[type(node.op)]),
                    left=left,
                    right=right,
                ),
            )
        if isinstance(node, ast.UnaryOp):
            operand = self.expression(node.operand)
            statement, value = self.save(
                ast.copy_location(ast.UnaryOp(node.op, operand.value), node)
            )
            return _Expression(
                [*operand.setup, statement],
                value,
                operand.diagnostic,
                _record(
                    "unary",
                    operator=ast.Constant(_OPERATORS[type(node.op)]),
                    operand=operand.description,
                ),
            )
        if isinstance(node, ast.Attribute):
            setup, value, diagnostic, (owner,) = self.operation(
                node,
                [self.expression(node.value)],
                lambda values: ast.Attribute(values[0], node.attr, node.ctx),
            )
            self.reserve()
            return _Expression(
                setup,
                value,
                diagnostic,
                _record(
                    "attribute",
                    value=_adapter_call("_safe_repr", [value]),
                    owner=owner,
                    name=ast.Constant(node.attr),
                ),
            )
        if isinstance(node, ast.Call):
            return self.call(node)
        statement, value = self.save(node)
        self.reserve()
        return _Expression(
            [statement], value, [], _record("value", value=_adapter_call("_safe_repr", [value]))
        )

    def call(self, node: ast.Call) -> _Expression:
        positional, named = len(node.args), len(node.keywords)
        children = [
            self.expression(node.func),
            *(
                self.expression(
                    argument.value if isinstance(argument, ast.Starred) else self.rebound(argument)
                )
                for argument in node.args
            ),
            *(self.expression(self.rebound(keyword.value)) for keyword in node.keywords),
        ]

        def build(values: list[ast.expr]) -> ast.Call:
            arguments = values[1 : 1 + positional]
            return ast.Call(
                values[0],
                [
                    ast.Starred(value, argument.ctx) if isinstance(argument, ast.Starred) else value
                    for argument, value in zip(node.args, arguments, strict=True)
                ],
                [
                    ast.keyword(keyword.arg, value)
                    for keyword, value in zip(node.keywords, values[1 + positional :], strict=True)
                ],
            )

        setup, value, diagnostic, references = self.operation(node, children, build)
        self.reserve()
        function, arguments, keywords = (
            references[0],
            references[1 : 1 + positional],
            references[1 + positional : 1 + positional + named],
        )
        return _Expression(
            setup,
            value,
            diagnostic,
            _record(
                "call",
                value=_adapter_call("_safe_repr", [value]),
                function=function,
                arguments=ast.Tuple(
                    [
                        ast.Dict(
                            [ast.Constant("unpack"), ast.Constant("expression")],
                            [ast.Constant(isinstance(argument, ast.Starred)), reference],
                        )
                        for argument, reference in zip(node.args, arguments, strict=True)
                    ],
                    ast.Load(),
                ),
                keywords=ast.Tuple(
                    [
                        ast.Dict(
                            [ast.Constant("name"), ast.Constant("expression")],
                            [ast.Constant(keyword.arg), reference],
                        )
                        for keyword, reference in zip(node.keywords, keywords, strict=True)
                    ],
                    ast.Load(),
                ),
            ),
        )

    def comparison(self, node: ast.Compare) -> _Expression:
        node.left = self.rebound(node.left)
        if isinstance(node.left, ast.NamedExpr):
            self.bindings[node.left.target.id] = node.left
        operands = [self.expression(node.left)]
        setup = operands[0].setup[:]
        # Pytest allocates every result slot before it visits the comparators.
        slots = [self.slot() for _ in node.ops]
        results: list[ast.expr] = []
        for slot, operator, comparator in zip(slots, node.ops, node.comparators, strict=True):
            if (
                isinstance(node.left, ast.Name)
                and isinstance(comparator, ast.NamedExpr)
                and node.left.id == comparator.target.id
            ):
                comparator.target = ast.Name(self.slot().id, ast.Store())
            operand = self.expression(comparator)
            setup.extend(operand.setup)
            setup.append(
                ast.Assign(
                    [ast.Name(slot.id, ast.Store())],
                    ast.Compare(operands[-1].value, [operator], [operand.value]),
                )
            )
            results.append(slot)
            operands.append(operand)
        value = results[0] if len(results) == 1 else ast.BoolOp(ast.And(), results)
        selected_statement, selected = self.save(
            _adapter_call("_first_failed", [ast.Tuple(results, ast.Load())]), explanation=True
        )
        operand_statement, operand_values = self.save(
            ast.Tuple([operand.value for operand in operands], ast.Load()), explanation=True
        )
        strings_statement, strings = self.save(
            _adapter_call("_string_values", [selected, operand_values]), explanation=True
        )
        builtin_statement, builtin = self.save(
            _adapter_call(
                "_builtin_comparison", [selected, _operator_names(node.ops), operand_values]
            ),
            explanation=True,
        )
        spilled, rendered = self.spill(operands)
        self.reserve(2)
        return _Expression(
            setup,
            value,
            [
                operand_statement,
                selected_statement,
                strings_statement,
                builtin_statement,
                # Pytest releases its temporary comparison tuple before
                # evaluating operand descriptions for interpolation.
                ast.Assign([ast.Name(operand_values.id, ast.Store())], ast.Constant(None)),
                *spilled,
            ],
            _record(
                "comparison",
                selected=selected,
                operators=_operator_names(node.ops),
                operands=ast.Tuple(rendered, ast.Load()),
                strings=strings,
                builtin=builtin,
            ),
        )

    def rename_repeated_walrus(self, operand: ast.expr, earlier: list[ast.expr]) -> None:
        """Rebind a comparison walrus to a temporary when an earlier operand names its target.

        Pytest does this only for a comparison whose left side is an assignment
        expression, so the user's variable keeps its earlier value. Later reads of
        that name evaluate the renamed expression again.
        """
        if not (isinstance(operand, ast.Compare) and isinstance(operand.left, ast.NamedExpr)):
            return
        target = operand.left.target.id
        if any(isinstance(value, ast.Name) and value.id == target for value in earlier):
            operand.left.target = ast.Name(self.slot().id, ast.Store())
            self.bindings[target] = operand.left

    def boolean(self, node: ast.BoolOp) -> _Expression:
        result = self.slot()
        # Pytest also keeps the explanation list in a numbered temporary.
        self.reserve()
        operands = []
        for position, operand in enumerate(node.values):
            self.rename_repeated_walrus(operand, node.values[:position])
            operands.append(self.expression(operand))
            self.reserve()
        self.reserve(2)
        result_target = ast.Name(result.id, ast.Store())
        setup: list[ast.stmt] = []
        diagnostic: list[ast.stmt] = []
        output_setup, output = self.save(ast.List([], ast.Load()), explanation=True)
        diagnostic.append(output_setup)
        setup_branch, diagnostic_branch = setup, diagnostic
        for index, operand in enumerate(operands):
            setup_branch.extend([*operand.setup, ast.Assign([result_target], operand.value)])
            diagnostic_branch.extend(operand.diagnostic)
            diagnostic_branch.append(
                ast.Expr(
                    ast.Call(ast.Attribute(output, "append", ast.Load()), [operand.description], [])
                )
            )
            if index < len(operands) - 1:
                condition = (
                    operand.value
                    if isinstance(node.op, ast.And)
                    else ast.UnaryOp(ast.Not(), operand.value)
                )
                setup_if = ast.If(condition, [], [])
                setup_branch.append(setup_if)
                setup_branch = setup_if.body
                diagnostic_if = ast.If(condition, [], [])
                diagnostic_branch.append(diagnostic_if)
                diagnostic_branch = diagnostic_if.body
        return _Expression(
            setup,
            result,
            diagnostic,
            _record(
                "boolean",
                operator=ast.Constant("and" if isinstance(node.op, ast.And) else "or"),
                operands=output,
            ),
        )

    @override
    def visit_Assert(self, node: ast.Assert) -> list[ast.stmt]:
        self.rewritten = True
        self.slot_index = 0
        self.explanation_index = 0
        self.temporaries = []
        self.explanations = []
        expression = self.expression(node.test)
        message = [] if node.msg is None else [node.msg]
        message_statement, prefix = self.save(
            _adapter_call("_message_text", message), explanation=True
        )
        failure = ast.Raise(
            ast.Call(
                ast.Name("AssertionError", ast.Load()),
                [_adapter_call("_render_explanation", [prefix, expression.description])],
                [],
            ),
            None,
        )
        # Diagnostic slots hold operand tuples and comparison results that pytest
        # never stores in frame locals, so they must not outlive the failure.
        diagnostic = [message_statement, *expression.diagnostic]
        release: list[ast.expr] = [ast.Name(name, ast.Store()) for name in self.explanations]
        check = ast.If(
            ast.UnaryOp(ast.Not(), expression.value),
            [ast.Try([*diagnostic, failure], [], [], [ast.Assign(release, ast.Constant(None))])],
            [],
        )
        # As in pytest, clear execution temporaries only after success.
        targets: list[ast.expr] = [ast.Name(name, ast.Store()) for name in self.temporaries]
        statements = [*expression.setup, check]
        if targets:
            statements.append(ast.Assign(targets, ast.Constant(None)))
        for statement in statements:
            ast.copy_location(statement, node)
            ast.fix_missing_locations(statement)
        return statements


def _assertion_rewrite_disabled() -> bool:
    config = _current_config()
    return config is not None and config.getoption("assertmode", "rewrite") == "plain"


def compile_assertions(source: bytes, filename: str) -> CodeType:
    """Compile assertions using pytest-compatible operand and diagnostic evaluation."""
    tree = ast.parse(source, filename)
    if (
        "PYTEST_DONT_REWRITE" in (ast.get_docstring(tree, clean=False) or "")
        or _assertion_rewrite_disabled()
    ):
        return compile(tree, filename, "exec", dont_inherit=True)
    rewriter = _AssertionRewriter()
    rewriter.rewrite(tree)
    if rewriter.rewritten:
        position = 0
        for index, statement in enumerate(tree.body):
            if (
                index == 0
                and isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ) or (isinstance(statement, ast.ImportFrom) and statement.module == "__future__"):
                position = index + 1
            else:
                break
        tree.body.insert(
            position,
            ast.fix_missing_locations(ast.Import([ast.alias("vfy.independent.imports", _ADAPTER)])),
        )
        tree.body.insert(
            position, ast.fix_missing_locations(ast.Import([ast.alias("builtins", _BUILTINS)]))
        )
    return compile(tree, filename, "exec", dont_inherit=True)


def _cache_path(source: str) -> Path:
    # CPython's __pycache__ location, with the vfy tag in place of CPython's tag.
    return Path(importlib.util.cache_from_source(source)).with_name(Path(source).stem + _CACHE_TAIL)


def _write_cached_code(cache: Path, key: tuple[bytes, str, int, int], code: CodeType) -> None:
    # Publish atomically so a concurrent reader never sees a partial entry.
    import tempfile  # noqa: PLC0415 - only a cache miss writes bytecode

    temporary = None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=cache.parent, prefix=f"{cache.name}.", delete=False
        ) as file:
            temporary = Path(file.name)
            marshal.dump((*key, code), file)
        os.replace(temporary, cache)
    except OSError:
        # An unwritable cache only costs the next import a rewrite.
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _read_cached_code(cache: Path, key: tuple[bytes, str, int, int]) -> CodeType | None:
    with contextlib.suppress(OSError, EOFError, ValueError, TypeError):
        with cache.open("rb") as file:
            entry = marshal.load(file)
        if (
            isinstance(entry, tuple)
            and len(entry) == 5
            and entry[:4] == key
            and isinstance(entry[4], CodeType)
        ):
            return entry[4]
    return None


def _assertion_code(source: str) -> CodeType:
    """Return rewritten code, reusing the cached entry while the source stat matches."""
    if (mutated := read_mutation_source(source)) is not None:
        return compile_assertions(mutated, source)
    if _assertion_rewrite_disabled():
        # Plain and rewritten code share no cache entries, so plain mode bypasses the cache.
        return compile_assertions(Path(source).read_bytes(), source)
    stat = os.stat(source)
    cache = _cache_path(source)
    key = (importlib.util.MAGIC_NUMBER, source, stat.st_mtime_ns, stat.st_size)
    if (code := _read_cached_code(cache, key)) is not None:
        return code
    from vfy import native  # noqa: PLC0415 - only a cache miss acquires population ownership

    lease = (
        contextlib.nullcontext()
        if sys.dont_write_bytecode
        else native._pytest.NativeAssertionCacheLease(source)
    )
    with lease:
        if (code := _read_cached_code(cache, key)) is not None:
            return code
        code = compile_assertions(Path(source).read_bytes(), source)
        if not sys.dont_write_bytecode:
            _write_cached_code(cache, key, code)
        return code


def execute_module(module: ModuleType, path: str) -> None:
    """Execute a collected source module with assertion rewriting enabled."""
    exec(_assertion_code(path), module.__dict__)


class _AssertionLoader(importlib.machinery.SourceFileLoader):
    @override
    def get_code(self, fullname: str) -> CodeType:
        return _assertion_code(self.get_filename(fullname))


class AssertionImportHook(importlib.abc.MetaPathFinder):
    """Rewrite configured test files and registered helpers during one lifecycle."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.pending_warnings: list[warnings.WarningMessage] = []

    @override
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        registered = any(fullname == name or fullname.startswith(name + ".") for name in self.names)
        config = _current_config()
        if not registered and config is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return None
        if spec.origin is None:
            return None
        if not registered and (
            config is None
            or not config._configuration.assertion_rewrite_matches(config, spec.origin)
        ):
            return None
        spec.loader = _AssertionLoader(fullname, spec.origin)
        return spec

    def close(self) -> None:
        """Remove this hook and its rewrite registrations from the interpreter."""
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        self.names.clear()
        pending, self.pending_warnings = self.pending_warnings, []
        for record in pending:
            warnings.showwarning(
                record.message, record.category, record.filename, record.lineno, line=record.line
            )


def install_import_hook() -> AssertionImportHook:
    """Install an isolated helper assertion hook before importing user modules."""
    hook = AssertionImportHook()
    sys.meta_path.insert(0, hook)
    return hook


def register_assert_rewrite(*names: str) -> None:
    """Rewrite named helper modules and their descendants on their next import."""
    if any(not isinstance(name, str) for name in names):
        raise TypeError(f"expected module names as *args, got {names!r} instead")
    hook = next((item for item in sys.meta_path if isinstance(item, AssertionImportHook)), None)
    if hook is None:
        return
    for name in names:
        module = sys.modules.get(name)
        if (
            module is not None
            and not isinstance(getattr(module, "__loader__", None), _AssertionLoader)
            and "PYTEST_DONT_REWRITE" not in (module.__doc__ or "")
        ):
            # Configuration warnings bypass a user's warning-recording context,
            # while retaining its filters and immediate warning-as-error behavior.
            with warnings.catch_warnings(record=True) as captured:
                warnings.warn(
                    "Module already imported so cannot be rewritten; " + name,
                    category=PytestAssertRewriteWarning,
                    stacklevel=2,
                )
            hook.pending_warnings.extend(captured)
        hook.names.add(name)


class PytestAssertRewriteWarning(UserWarning):
    """A registered helper was imported before assertion rewriting was enabled."""


def load_module(
    name: str,
    path: str,
    search_directory: str | None,
    mode: str,
    purpose: str = "generic",
) -> ModuleType:
    """Load a source module using a module identity selected by the native collector.

    Only ``purpose="test_module"`` wraps an ``ImportError`` or ``SyntaxError`` for the native
    collect report. Every other purpose raises the import exception unchanged.
    """
    from .errors import TestModuleImportFailure  # noqa: PLC0415 - error path only
    from .facade import Skipped  # noqa: PLC0415 - the facade imports this module
    from .protocol import CollectError  # noqa: PLC0415 - protocols import the facade

    source = Path(path)
    if search_directory is not None:
        if mode == "prepend" and (not sys.path or sys.path[0] != search_directory):
            sys.path.insert(0, search_directory)
        elif mode == "append" and search_directory not in sys.path:
            sys.path.append(search_directory)
    existing = _existing_module(name, source)
    if existing is not None:
        return existing
    hook = next((item for item in sys.meta_path if isinstance(item, AssertionImportHook)), None)
    owns_hook = hook is None
    if hook is None:
        hook = install_import_hook()
    hook.names.add(name)
    module: ModuleType | None = None
    try:
        if mode == "importlib":
            module = _load_importlib_module(name, source, purpose)
        else:
            importlib.import_module(name)
    except Skipped as error:
        # A module-level skip is a collection error in test modules; conftest keeps its own path.
        if error.allow_module_level or source.name == "conftest.py":
            raise
        raise CollectError(
            "Using pytest.skip outside of a test will skip the entire module. "
            "If that's your intention, pass `allow_module_level=True`. "
            "If you want to skip a specific test or an entire class, "
            "use the @pytest.mark.skip or @pytest.mark.skipif decorators."
        ) from error
    except (ImportError, SyntaxError) as error:
        if purpose != "test_module":
            raise
        raise TestModuleImportFailure(source, cause=error) from error
    finally:
        if owns_hook:
            hook.close()
    if module is None:
        # A file mismatch after import keeps its ImportError, outside the collect-report wrapper.
        module = _existing_module(name, source)
        assert module is not None
    return module


def _load_importlib_module(name: str, source: Path, purpose: str) -> ModuleType:
    parent_name, _, child_name = name.rpartition(".")
    parent = None
    if parent_name:
        parent_directory = source.parent.parent if source.name == "__init__.py" else source.parent
        parent = _importlib_parent(parent_name, parent_directory, purpose)
    path = str(source)
    # The factory accepts modern loaders; its bundled 3.15 typing protocol still
    # requires SourceFileLoader.load_module, which CPython removed in 3.15.
    loader = cast("LoaderProtocol", _AssertionLoader(name, path))
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    if spec is None:
        raise ImportError(f"cannot create module specification for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        execute_module(module, path)
    except BaseException:
        del sys.modules[name]
        raise
    if parent is not None:
        setattr(parent, child_name, module)
    return module


def _importlib_parent(name: str, directory: Path, purpose: str) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    if (directory / "__init__.py").is_file():
        return load_module(name, str(directory / "__init__.py"), None, "importlib", purpose)
    parent_name, _, child_name = name.rpartition(".")
    parent = _importlib_parent(parent_name, directory.parent, purpose) if parent_name else None
    module = ModuleType(name)
    module.__package__ = name
    module.__path__ = [str(directory)]
    module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    sys.modules[name] = module
    if parent is not None:
        setattr(parent, child_name, module)
    return module


def _existing_module(name: str, source: Path) -> ModuleType | None:
    module = sys.modules.get(name)
    if module is None:
        return None
    existing_path = getattr(module, "__file__", None)
    if existing_path is not None and Path(existing_path).resolve() == source.resolve():
        return module
    raise ImportError(
        f"import file mismatch:\nimported module {name!r} has this __file__ attribute:\n"
        f"  {existing_path}\nwhich is not the same as the test file we want to collect:\n"
        f"  {source}\nHINT: remove __pycache__ / .pyc files and/or use a unique basename "
        "for your test file modules"
    )
