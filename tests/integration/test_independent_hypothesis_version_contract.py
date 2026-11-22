"""Each admitted installed Hypothesis version retains its upstream plugin behavior."""

from __future__ import annotations

import ast
import json
from importlib.metadata import distributions, version
from pathlib import Path
from typing import Any

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PROFILE = """import json
from importlib.metadata import version
from pathlib import Path
from hypothesis import Phase, core, settings

settings.register_profile('version-contract', max_examples=3, database=None, deadline=None)

def pytest_runtest_logreport(report):
    with Path('reports.jsonl').open('a') as output:
        output.write(json.dumps(dict(when=report.when, outcome=report.outcome,
                                    hypothesis='hypothesis' in report.keywords,
                                    sections=report.sections)) + '\\n')

def pytest_sessionfinish(session):
    Path('profile.json').write_text(json.dumps(dict(
        version=version('hypothesis'), name=settings.get_current_profile_name(),
        verbosity=settings.default.verbosity.name,
        explain=Phase.explain in settings.default.phases,
        seed=core.global_force_seed, max_examples=settings.default.max_examples,
        deadline=settings.default.deadline, database=settings.default.database)))
"""

_PATCH_PROBE = """
def pytest_configure(config):
    from hypothesis.extra import _patching
    get_patch = _patching.get_patch_for
    save_patch = _patching.save_patch

    def record(value):
        with Path('patches.jsonl').open('a') as output:
            output.write(json.dumps(value) + '\\n')

    def observed_get(*args, **kwargs):
        result = get_patch(*args, **kwargs)
        record(dict(operation='get', triple=result))
        return result

    def observed_save(patch, **kwargs):
        result = save_patch(patch, **kwargs)
        record(dict(operation='save', path=str(result), text=patch))
        return result

    _patching.get_patch_for = observed_get
    _patching.save_patch = observed_save
    config.add_cleanup(lambda: setattr(_patching, 'get_patch_for', get_patch))
    config.add_cleanup(lambda: setattr(_patching, 'save_patch', save_patch))
"""


def _actual_version() -> str:
    current = version("hypothesis")
    major, minor, _patch = (int(part) for part in current.split("."))
    assert major == 6, current
    assert minor >= 152, current
    return current


def _options(*, autoload: bool = False) -> tuple[str, ...]:
    other_plugins = {
        entry.name
        for distribution in distributions()
        for entry in distribution.entry_points
        if entry.group == "pytest11" and entry.name not in {"hypothesispytest", "pytest_cov"}
    }
    disabled = tuple(
        argument for name in sorted(other_plugins) for argument in ("-p", f"no:{name}")
    )
    explicit = () if autoload else ("-p", "hypothesispytest")
    return (*disabled, "-p", "pytest_cov", "--no-cov", *explicit)


def _run(
    project: VfyProject, engine: str, options: tuple[str, ...], *, autoload: bool = False
) -> CommandResult:
    environment = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": None if autoload else "1",
        "HYPOTHESIS_STORAGE_DIRECTORY": str(project.root / ".hypothesis"),
    }
    result = project.run_engine(
        engine,
        *options,
        "test_property.py",
        independent=("--workers=1",),
        no_cov=False,
        env=environment,
    )
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    return result


def _take_records(project: VfyProject, filename: str, engine: str) -> list[dict[str, Any]]:
    path = project.root / filename
    records = [json.loads(line) for line in path.read_text().splitlines()]
    project.write(f"{engine}-{filename}", path.read_text())
    path.unlink()
    return records


def test_hypothesis_version_contract_profiles_and_reports(tmp_path: Path) -> None:
    current = _actual_version()
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _PROFILE)
    project.write(
        "test_property.py",
        """from pathlib import Path
from hypothesis import given, strategies as st

@given(value=st.integers(min_value=0, max_value=9))
def test_property(value):
    with Path('values').open('a') as output:
        output.write(str(value) + '\\n')
    assert value >= 0
""",
    )
    options = (
        *_options(),
        "-v",
        "--hypothesis-profile=version-contract",
        "--hypothesis-verbosity=verbose",
        "--hypothesis-explain",
        "--hypothesis-seed=17",
    )
    observed = []
    for engine in ("stock", "native"):
        result = _run(project, engine, options)
        result.expect(0)
        headers = [
            line for line in result.stdout.splitlines() if line.startswith("hypothesis profile ")
        ]
        assert len(headers) == 1
        assert "version-contract" in headers[0]
        profile = json.loads((tmp_path / "profile.json").read_text())
        assert profile["version"] == current
        assert profile["verbosity"] == "verbose"
        assert profile["explain"] is True
        assert profile["seed"] == 17
        assert profile["max_examples"] == 3
        assert profile["deadline"] is profile["database"] is None
        reports = _take_records(project, "reports.jsonl", engine)
        assert [report["when"] for report in reports] == ["setup", "call", "teardown"]
        assert all(report["outcome"] == "passed" and report["hypothesis"] for report in reports)
        values = (tmp_path / "values").read_text().splitlines()
        assert values
        project.write(f"{engine}-values", "\n".join(values))
        project.write(f"{engine}-profile.json", json.dumps(profile))
        (tmp_path / "values").unlink()
        observed.append((headers, profile, reports, values))
    assert observed[0] == observed[1]


def _patch_message(project: VfyProject, engine: str, stdout: str) -> str:
    lines = [line for line in stdout.splitlines() if line.startswith("`git apply ")]
    assert len(lines) == 1
    command, message = lines[0].split("` to add ", 1)
    assert message.endswith(" to your code.")
    saved = Path(command.removeprefix("`git apply "))
    if not saved.is_absolute():
        saved = project.root / saved
    records = _take_records(project, "patches.jsonl", engine)
    assert [record["operation"] for record in records] == ["get", "save"]
    assert saved.is_file()
    assert saved.resolve().is_relative_to(project.root.resolve())
    assert saved.read_text() == records[-1]["text"]
    recorded_path = Path(records[-1]["path"])
    if not recorded_path.is_absolute():
        recorded_path = project.root / recorded_path
    assert recorded_path.resolve() == saved.resolve()
    triple = records[0]["triple"]
    assert len(triple) == 3
    assert all(isinstance(part, str) for part in triple)
    function = ast.parse(triple[2]).body[0]
    assert isinstance(function, ast.FunctionDef)
    decorators = [
        decorator.func.value
        for decorator in function.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "via"
    ]
    assert any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "example"
        and [(item.arg, ast.literal_eval(item.value)) for item in decorator.keywords]
        == [("value", 3)]
        for decorator in decorators
    )
    return message


def test_hypothesis_version_contract_real_patch_message(tmp_path: Path) -> None:
    current = _actual_version()
    version("libcst")  # A real codemods environment is required; absence fails this proof.
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _PROFILE + _PATCH_PROBE)
    project.write(
        "test_property.py",
        """from hypothesis import example, given, strategies as st

@given(value=st.just(3))
def test_property(value):
    assert value < 3
""",
    )
    messages = []
    for engine in ("stock", "native"):
        result = _run(
            project,
            engine,
            (*_options(), "-q", "--hypothesis-profile=version-contract", "--hypothesis-seed=17"),
        )
        result.expect(1)
        assert json.loads((tmp_path / "profile.json").read_text())["version"] == current
        messages.append(_patch_message(project, engine, result.stdout))
    assert messages[0] == messages[1]


def test_patch_callback_observability_set_remains_live_during_summary(tmp_path: Path) -> None:
    _actual_version()
    version("libcst")
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            _PROFILE
            + _PATCH_PROBE
            + """
import pytest

_configure_patch_probe = pytest_configure

def pytest_configure(config):
    _configure_patch_probe(config)
    from hypothesis.extra import _patching
    from hypothesis.internal.observability import _WROTE_TO
    assert not _WROTE_TO
    original = _patching.save_patch
    def save_patch(patch, **kwargs):
        result = original(patch, **kwargs)
        observation = Path('callback-observations.jsonl').resolve()
        observation.write_text('{}\\n')
        _WROTE_TO.add(observation)
        Path('mutation.json').write_text(json.dumps({
            'patch_saved': Path(result).is_file(), 'observations': len(_WROTE_TO),
        }))
        return result
    _patching.save_patch = save_patch
    config.add_cleanup(lambda: setattr(_patching, 'save_patch', original))

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_terminal_summary(terminalreporter):
    sections = []
    original = terminalreporter.section
    def section(title, *args, **kwargs):
        sections.append(title)
        return original(title, *args, **kwargs)
    terminalreporter.section = section
    try:
        return (yield)
    finally:
        terminalreporter.section = original
        Path('sections.json').write_text(json.dumps(sections))
""",
        )
        project.write(
            "test_property.py",
            "from hypothesis import example, given, strategies as st\n\n"
            "@given(value=st.just(3))\ndef test_property(value):\n    assert value < 3\n",
        )
        result = _run(
            project,
            engine,
            (*_options(), "-q", "--hypothesis-profile=version-contract", "--hypothesis-seed=17"),
        )
        result.expect(1)
        assert "git apply" in result.stdout, result.stdout + result.stderr
        assert json.loads((project.root / "mutation.json").read_text()) == {
            "patch_saved": True,
            "observations": 1,
        }
        sections = json.loads((project.root / "sections.json").read_text())
        observations.append(sections.count("Hypothesis"))
    assert observations == [0, 0]
