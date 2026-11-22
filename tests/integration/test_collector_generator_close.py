"""Delegated generator closure preserves Coverage.py exit arcs in each context."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict

import pytest
from coverage import CoverageData

from tests.support import VfyProject


class _Snapshot(TypedDict):
    lines: set[int]
    arcs: set[tuple[int, int]]
    contexts_by_lineno: dict[int, set[str]]
    context_arcs: dict[str, set[tuple[int, int]]]


def _snapshot(source: Path, destination: Path) -> _Snapshot:
    data = CoverageData(basename=str(destination))
    try:
        data.read()
        context_arcs = {}
        for context in sorted(data.measured_contexts()):
            data.set_query_context(context)
            context_arcs[context] = set(data.arcs(str(source)) or [])
        data.set_query_contexts(None)
        return {
            "lines": set(data.lines(str(source)) or []),
            "arcs": set(data.arcs(str(source)) or []),
            "contexts_by_lineno": {
                line: set(contexts)
                for line, contexts in data.contexts_by_lineno(str(source)).items()
            },
            "context_arcs": context_arcs,
        }
    finally:
        data.close(force=True)


@pytest.mark.parametrize("mode", ["branch", "semantic"])
def test_delegated_generator_close_preserves_exit_arcs(tmp_path: Path, mode: str) -> None:
    project = VfyProject(tmp_path)
    source = project.write(
        "subject.py",
        "def plain(values):\n"
        "    for value in values:\n"
        "        yield value\n"
        "\n"
        "def recursive(values):\n"
        "    for value in values:\n"
        "        if isinstance(value, int):\n"
        "            yield value\n"
        "        else:\n"
        "            yield from recursive(value)\n"
        "\n"
        "def cleanup(values, log):\n"
        "    try:\n"
        "        yield from recursive(values)\n"
        "    finally:\n"
        "        log.append('cleanup')\n"
        "\n"
        "def handled(values, log):\n"
        "    try:\n"
        "        yield from recursive(values)\n"
        "    except GeneratorExit:\n"
        "        log.append('handled')\n"
        "        return\n"
        "    finally:\n"
        "        log.append('cleanup')\n",
    )
    project.write(
        "driver.py",
        "from importlib import import_module\n"
        "import sys\n"
        "\n"
        "provider, destination, mode = sys.argv[1:]\n"
        "options = {'branch': True, 'source': ['subject'], 'config_file': False}\n"
        "if provider == 'vfy.api' and mode == 'semantic':\n"
        "    options['decisions'] = True\n"
        "coverage = import_module(provider).Coverage(data_file=destination, **options)\n"
        "coverage.set_option('run:core', 'ctrace')\n"
        "coverage.start()\n"
        "try:\n"
        "    import subject\n"
        "    for name in ('plain', 'recursive', 'cleanup', 'handled'):\n"
        "        function = getattr(subject, name)\n"
        "        values = [1, 2] if name == 'plain' else [[[1, 2], 3], 4]\n"
        "        for operation in ('close', 'throw', 'exhaust'):\n"
        "            coverage.switch_context(name + '-' + operation)\n"
        "            log = []\n"
        "            if name in ('cleanup', 'handled'):\n"
        "                iterator = function(values, log)\n"
        "            else:\n"
        "                iterator = function(values)\n"
        "            assert next(iterator) == 1\n"
        "            if operation == 'close':\n"
        "                iterator.close()\n"
        "            elif operation == 'throw':\n"
        "                try:\n"
        "                    iterator.throw(GeneratorExit)\n"
        "                except GeneratorExit:\n"
        "                    assert name != 'handled'\n"
        "                except StopIteration:\n"
        "                    assert name == 'handled'\n"
        "                else:\n"
        "                    raise AssertionError('GeneratorExit did not escape')\n"
        "            else:\n"
        "                assert list(iterator) == ([2] if name == 'plain' else [2, 3, 4])\n"
        "            if name in ('cleanup', 'handled'):\n"
        "                expected = ['cleanup']\n"
        "                if name == 'handled' and operation != 'exhaust':\n"
        "                    expected.insert(0, 'handled')\n"
        "                assert log == expected\n"
        "finally:\n"
        "    coverage.stop()\n"
        "coverage.save()\n",
    )
    snapshots = {}
    for provider, destination in (
        ("coverage", "reference.coverage"),
        ("vfy.api", "actual.coverage"),
    ):
        result = project.run_python("driver.py", provider, destination, mode)
        result.expect(0)
        snapshots[provider] = _snapshot(source, tmp_path / destination)
    expected = snapshots["coverage"]
    for operation in ("close", "throw"):
        assert (10, -5) in expected["context_arcs"]["recursive-" + operation]
        assert ((8, -5) in expected["context_arcs"]["recursive-" + operation]) == (
            sys.version_info >= (3, 14)
        )
        assert ((3, -1) in expected["context_arcs"]["plain-" + operation]) == (
            sys.version_info >= (3, 14)
        )
    assert snapshots["vfy.api"] == expected


@pytest.mark.parametrize("branch", [False, True], ids=["line", "branch"])
def test_unmatched_exception_delegate_close_preserves_events(tmp_path: Path, branch: bool) -> None:
    project = VfyProject(tmp_path)
    source = project.write(
        "subject.py",
        "def leaf():\n"
        "    yield 'first'\n"
        "    yield 'second'\n"
        "\n"
        "def unmatched(log):\n"
        "    try:\n"
        "        yield from leaf()\n"
        "    except Exception:\n"
        "        log.append('caught')\n",
    )
    project.write(
        "driver.py",
        "from importlib import import_module\n"
        "import json\n"
        "from pathlib import Path\n"
        "import sys\n"
        "\n"
        "provider, destination, branch = sys.argv[1:]\n"
        "coverage = import_module(provider).Coverage(\n"
        "    data_file=destination, branch=branch == '1',\n"
        "    source=['subject'], config_file=False)\n"
        "coverage.set_option('run:core', 'ctrace')\n"
        "coverage.start()\n"
        "try:\n"
        "    trace = sys.gettrace()\n"
        "    identity = {\n"
        "        'trace_module': None if trace is None else type(trace).__module__,\n"
        "        'trace_name': None if trace is None else type(trace).__name__,\n"
        "        'requested_core': coverage.get_option('run:core')\n"
        "            if provider == 'coverage' else None,\n"
        "    }\n"
        "    if provider == 'coverage':\n"
        "        assert identity['requested_core'] == 'ctrace'\n"
        "        assert identity['trace_name'] == 'CTracer'\n"
        "        assert identity['trace_module'].split('.')[0] == 'coverage'\n"
        "    import subject\n"
        "    log = []\n"
        "    coverage.switch_context('first-yield')\n"
        "    generator = subject.unmatched(log)\n"
        "    assert next(generator) == 'first'\n"
        "    assert generator.gi_frame is not None\n"
        "    coverage.switch_context('explicit-close')\n"
        "    generator.close()\n"
        "    assert generator.gi_frame is None\n"
        "    assert log == []\n"
        "finally:\n"
        "    coverage.stop()\n"
        "coverage.save()\n"
        "Path(destination + '.producer.json').write_text(json.dumps(identity))\n",
    )
    snapshots: dict[str, _Snapshot] = {}
    for provider, destination in (
        ("coverage", "reference.coverage"),
        ("vfy.api", "actual.coverage"),
    ):
        result = project.run_python("driver.py", provider, destination, str(int(branch)))
        result.expect(0)
        snapshots[provider] = _snapshot(source, tmp_path / destination)
    reference = json.loads((tmp_path / "reference.coverage.producer.json").read_text())
    assert reference["requested_core"] == "ctrace"
    assert reference["trace_name"] == "CTracer"
    assert snapshots["vfy.api"] == snapshots["coverage"]


@pytest.mark.parametrize("exception", ["ValueError", "GeneratorExit"])
@pytest.mark.skipif(sys.platform == "win32", reason="independent execution requires Unix")
def test_context_manager_throw_preserves_exit_arcs(tmp_path: Path, exception: str) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", "[run]\ncore = ctrace\n")
    source = project.write(
        "product.py",
        "from contextlib import contextmanager\n"
        "@contextmanager\n"
        "def plain():\n"
        "    yield\n"
        "@contextmanager\n"
        "def delegated():\n"
        "    yield from plain.__wrapped__()\n"
        "@contextmanager\n"
        "def cleanup():\n"
        "    try:\n"
        "        yield\n"
        "    finally:\n"
        "        pass\n",
    )
    project.write(
        "test_product.py",
        "import pytest\n"
        "from product import plain, delegated, cleanup\n"
        "@pytest.mark.parametrize('factory', [plain, delegated, cleanup], "
        "ids=['plain', 'delegated', 'cleanup'])\n"
        "def test_throw(factory):\n"
        f"    with pytest.raises({exception}), factory():\n"
        f"        raise {exception}\n",
    )
    snapshots: dict[str, _Snapshot] = {}
    for engine in ("stock", "native"):
        destination = tmp_path / f".coverage.{engine}"
        result = project.run_engine(
            engine,
            "-q",
            "--cov=product",
            "--cov-branch",
            "--cov-context=test",
            "--cov-report=",
            stock=("-p", "pytest_cov.plugin"),
            independent=("--workers=1",),
            no_cov=False,
            env={"COVERAGE_FILE": str(destination)},
        )
        result.expect(0)
        snapshots[engine] = _snapshot(source, destination)
    expected = snapshots["stock"]
    contexts = expected["context_arcs"]
    plain = next(key for key in contexts if key.endswith("test_product.py::test_throw[plain]|run"))
    delegated = next(
        key for key in contexts if key.endswith("test_product.py::test_throw[delegated]|run")
    )
    cleanup = next(
        key for key in contexts if key.endswith("test_product.py::test_throw[cleanup]|run")
    )
    plain_arcs = {(-2, 4), (4, -2)} if sys.version_info >= (3, 14) else {(-2, 4)}
    assert contexts[plain] == plain_arcs
    assert (7, -5) in contexts[delegated]
    assert (13, -8) in contexts[cleanup]
    assert snapshots["native"] == expected
