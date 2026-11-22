"""Consumer declarations must describe the installed native extension."""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.support.platforms import requires_native_pytest
from vfy import _native


def _parameters(
    node: ast.FunctionDef, *, constructor: bool = False
) -> list[tuple[str, bool, int, object]]:
    positional = [*node.args.posonlyargs, *node.args.args]
    required = len(positional) - len(node.args.defaults)
    values: list[tuple[str, bool, int, object]] = [
        (
            arg.arg,
            index < required,
            inspect.Parameter.POSITIONAL_ONLY
            if index < len(node.args.posonlyargs)
            else inspect.Parameter.POSITIONAL_OR_KEYWORD,
            None if index < required else ast.literal_eval(node.args.defaults[index - required]),
        )
        for index, arg in enumerate(positional)
    ]
    if constructor:
        values = values[1:]
    if node.args.vararg is not None:
        values.append((node.args.vararg.arg, True, inspect.Parameter.VAR_POSITIONAL, None))
    values.extend(
        (
            arg.arg,
            default is None,
            inspect.Parameter.KEYWORD_ONLY,
            None if default is None else ast.literal_eval(default),
        )
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
    )
    if node.args.kwarg is not None:
        values.append((node.args.kwarg.arg, True, inspect.Parameter.VAR_KEYWORD, None))
    return values


def _assert_signature(value: object, node: ast.FunctionDef, *, constructor: bool = False) -> None:
    assert callable(value)
    signature = inspect.signature(value)
    expected = _parameters(node, constructor=constructor)
    assert len(signature.parameters) == len(expected), node.name
    for parameter, (name, required, kind, default) in zip(
        signature.parameters.values(), expected, strict=True
    ):
        assert parameter.name == name, node.name
        assert (parameter.default is inspect.Parameter.empty) == required, node.name
        assert parameter.kind == kind, node.name
        if not required and default is not Ellipsis:
            assert parameter.default == default, node.name


def _assert_class_declarations(value: object, node: ast.ClassDef) -> None:
    members: list[ast.stmt] = []
    for member in node.body:
        if isinstance(member, ast.If):
            # Native pytest methods are compiled only on Unix; reject unknown
            # guards instead of silently dropping conditional declarations.
            assert ast.unparse(member.test) == "sys.platform != 'win32'", node.name
            members.extend(member.body if sys.platform != "win32" else member.orelse)
        else:
            members.append(member)
    methods = {member.name: member for member in members if isinstance(member, ast.FunctionDef)}
    properties = {
        member.name
        for member in members
        if isinstance(member, ast.FunctionDef)
        and any(
            isinstance(decorator, ast.Name) and decorator.id == "property"
            for decorator in member.decorator_list
        )
    }
    attributes = {
        member.target.id
        for member in members
        if isinstance(member, ast.AnnAssign)
        and isinstance(member.target, ast.Name)
        and not member.target.id.startswith("_")
    }
    public = {member for member in vars(value) if not member.startswith("_")}
    stored = attributes & vars(value).keys()
    # Dynamic instance fields are checked by their owning boundary tests;
    # storage descriptors can also be checked on the native class.
    assert public == {name for name in methods if not name.startswith("_")} | stored, node.name
    attribute_values = {
        member.target.id: member.value
        for member in members
        if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name)
    }
    for attribute in stored:
        default = attribute_values[attribute]
        if default is None:
            assert inspect.isgetsetdescriptor(getattr(value, attribute)), (node.name, attribute)
        else:
            assert getattr(value, attribute) == ast.literal_eval(default), (node.name, attribute)
    for method_name, method in methods.items():
        if method_name == "__init__":
            _assert_signature(value, method, constructor=True)
        elif any(
            isinstance(decorator, ast.Name) and decorator.id == "classmethod"
            for decorator in method.decorator_list
        ):
            _assert_signature(getattr(value, method_name), method, constructor=True)
        elif method_name in properties:
            assert inspect.isgetsetdescriptor(getattr(value, method_name))
        elif method_name == "__call__" and "__signature__" in methods:
            # PyO3 exposes a generic call slot; the callable's owned signature
            # is verified on real instances by the hook boundary tests.
            slot = inspect.signature(getattr(value, method_name))
            assert list(slot.parameters) == ["self", "args", "kwargs"]
        else:
            _assert_signature(getattr(value, method_name), method)


def _assert_module_declarations(module: ModuleType, declaration: Path) -> None:
    body = ast.parse(declaration.read_text(encoding="utf-8")).body
    declarations = {
        node.name: node for node in body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }
    exports = set(getattr(module, "__all__", ()))
    # A missing inventory entry must not silently remove its signature check.
    public = {name for name in vars(module) if not name.startswith("_")}
    if module.__name__ == "vfy._native._pytest" and sys.platform == "win32":
        # The pytest adapter is compiled only on Unix; its Windows module is empty.
        assert not public, module.__name__
        assert not exports, module.__name__
        return
    assert exports, module.__name__
    assert public == {name for name in exports if not name.startswith("_")}, (
        module.__name__,
        public.symmetric_difference(exports),
    )
    inventories = [
        node.value
        for node in body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    ]
    assert len(inventories) == 1, module.__name__
    assert exports == set(ast.literal_eval(inventories[0])), module.__name__
    for name in sorted(exports):
        value = getattr(module, name)
        if isinstance(value, ModuleType):
            _assert_module_declarations(value, declaration.with_name("_pytest_native.pyi"))
            continue
        node = declarations[name]
        if isinstance(node, ast.FunctionDef):
            _assert_signature(value, node)
        else:
            _assert_class_declarations(value, node)


def test_native_declarations_match_exports_and_inspectable_signatures() -> None:
    declaration = Path(_native.__file__).with_name("_native.pyi")
    _assert_module_declarations(_native, declaration)
    assert declaration.with_name("py.typed").is_file()


@pytest.mark.parametrize("submodule", [False, pytest.param(True, marks=requires_native_pytest)])
@pytest.mark.parametrize("empty", [False, True])
def test_native_declaration_check_rejects_incomplete_export_inventory(
    monkeypatch: pytest.MonkeyPatch, submodule: bool, empty: bool
) -> None:
    module = _native._pytest if submodule else _native
    exports = list(vars(module)["__all__"])
    monkeypatch.setattr(module, "__all__", [] if empty else exports[1:])
    with pytest.raises(AssertionError, match=r"vfy\._native"):
        test_native_declarations_match_exports_and_inspectable_signatures()


@requires_native_pytest
def test_native_declaration_check_rejects_removed_submodule_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _native._pytest
    declaration = Path(_native.__file__).with_name("_pytest_native.pyi")
    _assert_module_declarations(module, declaration)
    monkeypatch.setattr(
        module, "__all__", [name for name in module.__all__ if name != "NativeHookUndo"]
    )
    monkeypatch.delattr(module, "NativeHookUndo")
    with pytest.raises(AssertionError, match=r"vfy\._native\._pytest"):
        _assert_module_declarations(module, declaration)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_html_declarations_check_exports_for_each_platform(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    declaration = Path(_native.__file__).with_name("_native.pyi")
    node = next(
        member
        for member in ast.parse(declaration.read_text(encoding="utf-8")).body
        if isinstance(member, ast.ClassDef) and member.name == "PytestHtmlReport"
    )

    def constructor(self: object, destination: str) -> None:
        pass

    def write(self: object, /, payload: str) -> None:
        pass

    def write_native(self: object, /, payload: str, result: object) -> None:
        pass

    methods: dict[str, object] = {"__init__": constructor, "write": write}
    if platform != "win32":
        methods["write_native"] = write_native
    monkeypatch.setattr(sys, "platform", platform)
    _assert_class_declarations(type("PytestHtmlReport", (), methods), node)

    if platform == "win32":
        methods["write_native"] = write_native
    else:
        del methods["write_native"]
    with pytest.raises(AssertionError, match="PytestHtmlReport"):
        _assert_class_declarations(type("PytestHtmlReport", (), methods), node)
