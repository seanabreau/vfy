"""Named annotation evaluators preserve Coverage.py's report obligations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.parametrize(
    ("source", "branch"),
    [
        pytest.param(
            "def __annotate__(value):\n    if value:\n        return 1\n    return 0\nresult = 1\n",
            False,
            id="uncalled",
        ),
        *[
            pytest.param(
                "def __annotate__(value):\n"
                "    if value:\n"
                "        return 1\n"
                "    return 0\n"
                "result = __annotate__(True)\n",
                branch,
                id=f"called-{'branches' if branch else 'lines'}",
            )
            for branch in (False, True)
        ],
    ],
)
def test_annotation_evaluator_reports_match_reference(
    tmp_path: Path, source: str, branch: bool
) -> None:
    _compare_annotation_coverage(tmp_path, source, branch=branch)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "class Model:\n"
            "    def __annotate__(self, value):\n"
            "        if value:\n"
            "            return 1\n"
            "        return 0\n"
            "result = Model().__annotate__(True)\n",
            id="method",
        ),
        pytest.param(
            "def build():\n"
            "    def __annotate__(value):\n"
            "        if value:\n"
            "            return 1\n"
            "        return 0\n"
            "    return __annotate__\n"
            "result = build()(True)\n",
            id="nested-evaluator",
        ),
        pytest.param(
            "def __annotate__(value):\n"
            "    def helper(value):\n"
            "        if value:\n"
            "            return 1\n"
            "        return 0\n"
            "    return helper(value)\n"
            "result = __annotate__(True)\n",
            id="independent-child",
        ),
        pytest.param(
            "def identity(value):\n"
            "    return value\n"
            "def __annotate__(value):\n"
            "    @identity\n"
            "    class Holder:\n"
            "        flag = value\n"
            "        def helper(self):\n"
            "            if self.flag:\n"
            "                return 1\n"
            "            return 0\n"
            "    return Holder().helper()\n"
            "result = __annotate__(True)\n",
            id="independent-decorated-class",
        ),
        pytest.param(
            "class __annotate__:\n"
            "    value = 1\n"
            "    def helper(self):\n"
            "        return self.value\n"
            "result = __annotate__().helper()\n",
            id="named-class",
        ),
    ],
)
def test_annotation_evaluator_nested_code_matches_reference(tmp_path: Path, source: str) -> None:
    _compare_annotation_coverage(tmp_path, source, branch=True)


@pytest.mark.parametrize("expression", ["lambda: value", "(item for item in [value])"])
def test_annotation_evaluator_retains_expression_child_statements(
    tmp_path: Path, expression: str
) -> None:
    source = (
        "def __annotate__(value):\n"
        f"    child = {expression}\n"
        "    return child\n"
        "result = __annotate__(1)\n"
    )
    _compare_annotation_coverage(tmp_path, source, branch=False)


@pytest.mark.parametrize(
    "alias",
    [
        "type Alias = int",
        "type Alias[T] = list[T]",
        "type Alias[T: int] = list[T]",
        "type Alias = (\n        int\n    )",
    ],
    ids=["plain", "generic", "bounded", "multiline"],
)
@pytest.mark.parametrize("called", [False, True], ids=["uncalled", "called"])
def test_annotation_evaluator_retains_type_alias_children(
    tmp_path: Path, alias: str, *, called: bool
) -> None:
    source = (
        "def __annotate__():\n"
        f"    {alias}\n"
        "    return Alias.__value__\n"
        f"result = {'__annotate__()' if called else '1'}\n"
    )
    _compare_annotation_coverage(tmp_path, source, branch=True)


@pytest.mark.parametrize(
    "decorator",
    ["(lambda f: f)", "(identity for identity in [lambda f: f]).__next__()"],
    ids=["lambda", "generator"],
)
def test_annotation_evaluator_decorator_child_uses_decorator_line(
    tmp_path: Path, decorator: str
) -> None:
    source = (
        "def __annotate__():\n"
        f"    @{decorator}\n"
        "    def helper():\n"
        "        return 1\n"
        "    return helper\n"
        "result = __annotate__()()\n"
    )
    _compare_annotation_coverage(tmp_path, source, branch=True)


def test_annotation_evaluator_retains_decorated_default_child(tmp_path: Path) -> None:
    source = (
        "def __annotate__():\n"
        "    @(lambda f: f)\n"
        "    def helper(value=lambda: 1):\n"
        "        return value()\n"
        "    return helper\n"
        "result = __annotate__()()\n"
    )
    _compare_annotation_coverage(tmp_path, source, branch=True)


@pytest.mark.parametrize(
    "definition", ["def __annotate__[T: int]():", "class __annotate__[T: int]:"]
)
@pytest.mark.parametrize("decorated", [False, True], ids=["plain", "decorated"])
def test_annotation_evaluator_retains_generic_parameter_children(
    tmp_path: Path, definition: str, *, decorated: bool
) -> None:
    source = (
        "def identity(value):\n"
        "    return value\n"
        "def __annotate__():\n"
        f"{'    @identity\n' if decorated else ''}"
        f"    {definition}\n"
        "        pass\n"
        "    return __annotate__\n"
        "result = 1\n"
    )
    _compare_annotation_coverage(tmp_path, source, branch=True)


def _compare_annotation_coverage(tmp_path: Path, source: str, *, branch: bool) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", source)
    reports = []
    observations = []
    options = ["--source=.", *(["--branch"] if branch else []), "app.py"]
    for engine in ("coverage", "vfy"):
        report_name = f"{engine}.json"
        run = (
            project.run_python("-m", "coverage", "run", *options)
            if engine == "coverage"
            else project.run_vfy("coverage", "run", *options)
        )
        run.expect(0)
        report = (
            project.run_python("-m", "coverage", "json", "-o", report_name)
            if engine == "coverage"
            else project.run_vfy("coverage", "json", "-o", report_name)
        )
        report.expect(0)
        payload = json.loads((tmp_path / report_name).read_text(encoding="utf-8"))
        files = {
            (
                Path(name).relative_to(tmp_path).as_posix() if Path(name).is_absolute() else name
            ): value
            for name, value in payload["files"].items()
        }
        assert set(files) == {"app.py"}
        reports.append((files, payload["totals"]))
        observation = project.run_python(
            "-c",
            f"from {engine} import CoverageData\n"
            "from pathlib import Path\n"
            "import json\n"
            "data = CoverageData()\n"
            "data.read()\n"
            "filename = str(Path('app.py').resolve())\n"
            "lines, arcs = data.lines(filename), data.arcs(filename)\n"
            "print(json.dumps([sorted(lines), None if arcs is None else sorted(arcs)]))\n",
        )
        observation.expect(0)
        observations.append(json.loads(observation.stdout))
    assert observations[0] == observations[1]
    assert reports[0] == reports[1]
