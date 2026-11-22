"""Coverage.py differentials for configured source discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject


def _relative_files(project: VfyProject, data_file: str) -> set[str]:
    files = set()
    for filename in project.data(data_file).measured_files():
        path = Path(filename)
        files.add((path.relative_to(project.root) if path.is_absolute() else path).as_posix())
    return files


@pytest.mark.parametrize("branch", [False, True])
def test_dynamic_tracer_return_without_source_preserves_last_file(
    tmp_path: Path, *, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "def run():\n    value = 1\n    return value\n")
    mapped = project.write("mapped.py", "def run():\n    value = 1\n    return value\n")
    project.write(
        "collect.py",
        "import dis\nimport importlib\nimport sys\nfrom pathlib import Path\n"
        "from subject import run\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "protocols = importlib.import_module(sys.argv[1] + '.plugin')\n"
        "class Tracer(protocols.FileTracer):\n"
        "    def has_dynamic_source_filename(self):\n        return True\n"
        "    def dynamic_source_filename(self, filename, frame):\n"
        "        if frame.f_code.co_code[frame.f_lasti] == dis.opmap['RETURN_VALUE']:\n"
        "            return None\n"
        "        return str(Path('mapped.py').resolve())\n"
        "    def line_number_range(self, frame):\n        return frame.f_lineno, frame.f_lineno\n"
        "class Plugin(protocols.CoveragePlugin):\n"
        "    def file_tracer(self, filename):\n"
        "        if Path(filename).name == 'subject.py':\n            return Tracer()\n"
        "def initialize(registry):\n    registry.add_file_tracer(Plugin())\n"
        "coverage = tool.Coverage(config_file=False, data_file='.' + sys.argv[1], "
        f"branch={branch!r}, plugins=[initialize])\n"
        "coverage.start()\nassert run() == 1\ncoverage.stop()\ncoverage.save()\n",
    )
    for tool in ("coverage", "vfy"):
        result = project.run_python("collect.py", tool, env={"COVERAGE_CORE": "ctrace"})
        result.expect(0)
    actual = project.data(".vfy")
    expected = project.data(".coverage")
    try:
        path = str(mapped)
        assert actual.lines(path) == expected.lines(path) == [2, 3]
        if branch:
            assert (
                sorted(actual.arcs(path) or [])
                == sorted(expected.arcs(path) or [])
                == [
                    (-1, 2),
                    (2, 3),
                    (3, -1),
                ]
            )
    finally:
        actual.close(force=True)
        expected.close(force=True)


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("package", ["pkg.target", "pkg.namespace.target", "single"])
def test_source_packages_resolve_without_preimporting_or_measuring_siblings(
    tmp_path: Path, *, configured: bool, package: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "from . import target\n")
    project.write("pkg/target/__init__.py", "first = 1\nsecond = 2\nthird = 3\n")
    project.write("pkg/namespace/target/__init__.py", "value = 1\n")
    project.write("single.py", "value = 1\n")
    project.write("sibling.py", "value = 2\n")
    project.write(".coveragerc", f"[run]\nsource_pkgs = {package}\n")
    project.write(
        "collect.py",
        "import importlib\n"
        "import sys\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "package = sys.argv[3]\n"
        "options = {} if sys.argv[4] == 'True' else "
        "{'config_file': False, 'source_pkgs': [package]}\n"
        "coverage = tool.Coverage(data_file=sys.argv[2], **options)\n"
        "assert package not in sys.modules\n"
        "assert package.split('.')[0] not in sys.modules\n"
        "coverage.start()\n"
        "importlib.import_module(package)\n"
        "import sibling\n"
        "coverage.stop()\n"
        "coverage.save()\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        package,
        str(configured),
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", package, str(configured))

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    expected_files = _relative_files(project, ".coverage.reference")
    assert _relative_files(project, ".coverage.actual") == expected_files
    for filename in expected_files:
        assert project.lines(filename, ".coverage.actual") == project.lines(
            filename, ".coverage.reference"
        )


@pytest.mark.parametrize("configured", [False, True])
def test_source_entries_that_are_not_directories_name_packages(
    tmp_path: Path, *, configured: bool
) -> None:
    # Coverage.py measures `source = ["pkg"]` as a package when no `pkg`
    # directory exists, as in a src layout.
    project = VfyProject(tmp_path)
    project.write("src/pkg/__init__.py", "")
    project.write("src/pkg/used.py", "def run():\n    return 1\n")
    project.write("src/pkg/unused.py", "def run():\n    return 2\n")
    project.write("src/other.py", "value = 1\n")
    project.write(".coveragerc", "[run]\nsource = pkg\n")
    project.write(
        "collect.py",
        "import importlib\n"
        "import sys\n"
        "sys.path.insert(0, 'src')\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "options = {} if sys.argv[3] == 'True' else {'config_file': False, 'source': ['pkg']}\n"
        "coverage = tool.Coverage(data_file=sys.argv[2], **options)\n"
        "coverage.start()\n"
        "from pkg import used\n"
        "import other\n"
        "assert used.run() == 1\n"
        "coverage.stop()\n"
        "coverage.save()\n",
    )

    expected = project.run_python("collect.py", "coverage", ".coverage.reference", str(configured))
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", str(configured))

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    expected_files = _relative_files(project, ".coverage.reference")
    assert "src/pkg/unused.py" in expected_files
    assert _relative_files(project, ".coverage.actual") == expected_files
    for filename in expected_files:
        assert project.lines(filename, ".coverage.actual") == project.lines(
            filename, ".coverage.reference"
        )


@pytest.mark.parametrize("package", ["pkg.sub", "pkg.nested.sub"])
def test_source_packages_use_loaded_parents_extended_path(tmp_path: Path, package: str) -> None:
    project = VfyProject(tmp_path)
    initializer = "from pkgutil import extend_path\n__path__ = extend_path(__path__, __name__)\n"
    project.write("first/pkg/__init__.py", initializer)
    project.write("first/pkg/nested/__init__.py", initializer)
    project.write("second/pkg/__init__.py", "")
    project.write("second/pkg/nested/__init__.py", "")
    module = "second/" + package.replace(".", "/") + ".py"
    project.write(module, "value = 1\n")
    project.write("second/pkg/sibling.py", "value = 2\n")
    project.write(
        "collect.py",
        "import sys\n"
        "sys.path[:0] = ['first', 'second']\n"
        "import importlib\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "package = sys.argv[3]\n"
        "importlib.import_module(package.rsplit('.', 1)[0])\n"
        "assert package not in sys.modules\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=[package])\n"
        "assert package not in sys.modules\n"
        "coverage.start()\n"
        "importlib.import_module(package)\n"
        "import pkg.sibling\n"
        "coverage.stop()\n"
        "coverage.save()\n",
    )

    expected = project.run_python("collect.py", "coverage", ".coverage.reference", package)
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", package)

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {module}
    )
    assert project.lines(module, ".coverage.actual") == project.lines(module, ".coverage.reference")


@pytest.mark.parametrize("branch", [False, True])
def test_source_packages_preserve_existing_thread_frame_dispositions(
    tmp_path: Path, *, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "")
    project.write(
        "pkg/target.py",
        "def work(started=None, release=None):\n"
        "    if started is not None:\n"
        "        started.set()\n"
        "        release.wait()\n"
        "    value = 1\n"
        "    return value\n",
    )
    project.write(
        "collect.py",
        f"""import importlib
import sys
import threading

import pkg.target

tool = importlib.import_module(sys.argv[1])
started = threading.Event()
release = threading.Event()
thread = threading.Thread(target=pkg.target.work, args=(started, release))
thread.start()
try:
    assert started.wait(timeout=10), 'worker never entered target frame'
    coverage = tool.Coverage(
        config_file=False, data_file=sys.argv[2], source_pkgs=['pkg.target'], branch={branch!r},
    )
    coverage.start()
    assert pkg.target.work() == 1
    coverage.stop()
    coverage.save()
finally:
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive(), 'worker never left target frame'
""",
    )
    expected = project.run_python("collect.py", "coverage", ".coverage.reference")
    actual = project.run_python("collect.py", "vfy", ".coverage.actual")

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {"pkg/target.py"}
    )
    assert (
        project.lines("pkg/target.py", ".coverage.actual")
        == project.lines("pkg/target.py", ".coverage.reference")
        == [2, 5, 6]
    )
    assert project.arcs("pkg/target.py", ".coverage.actual") == project.arcs(
        "pkg/target.py", ".coverage.reference"
    )


@pytest.mark.parametrize(
    ("package", "load_nested"),
    [("pkg.target", False), ("pkg.nested.target", False), ("pkg.nested.target", True)],
)
def test_source_packages_use_loaded_parent_without_module_spec(
    tmp_path: Path, package: str, *, load_nested: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pkg/nested/__init__.py",
        "from pathlib import Path\nPath('parent-imported').touch()\n",
    )
    module = package.replace(".", "/") + ".py"
    project.write(module, "first = 1\nsecond = 2\n")
    project.write("pkg/sibling.py", "sibling = True\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\nfrom types import ModuleType\n"
        "parent = ModuleType('pkg')\nparent.__path__ = [str(Path('pkg').resolve())]\n"
        "sys.modules['pkg'] = parent\n"
        "if sys.argv[4] == 'True':\n"
        "    nested = ModuleType('pkg.nested')\n"
        "    nested.__path__ = [str(Path('pkg/nested').resolve())]\n"
        "    sys.modules['pkg.nested'] = nested\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=[sys.argv[3]])\n"
        "assert not Path('parent-imported').exists()\n"
        "assert sys.argv[3] not in sys.modules\n"
        "coverage.start()\nimportlib.import_module(sys.argv[3])\nimport pkg.sibling\n"
        "coverage.stop()\ncoverage.save()\n"
        "Path('parent-imported').unlink(missing_ok=True)\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        package,
        str(load_nested),
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", package, str(load_nested))

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {module}
    )
    assert (
        project.lines(module, ".coverage.actual")
        == project.lines(module, ".coverage.reference")
        == [1, 2]
    )


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("kind", ["module", "package", "namespace"])
@pytest.mark.parametrize("extension", ["pkgutil", "custom", "replacement"])
def test_source_packages_defer_unimported_parents_extended_path(
    tmp_path: Path, *, branch: bool, kind: str, extension: str
) -> None:
    project = VfyProject(tmp_path)
    if extension == "pkgutil":
        initializer = (
            "from pkgutil import extend_path\n__path__ = extend_path(__path__, __name__)\n"
        )
    else:
        extend = (
            "__path__.insert(0, str(Path('second/pkg').resolve()))"
            if extension == "replacement"
            else "__path__.append(str(Path('second/pkg').resolve()))"
        )
        initializer = f"from pathlib import Path\n{extend}\n"
    project.write("first/pkg/__init__.py", initializer)
    project.write("second/pkg/__init__.py", "")
    project.write("second/pkg/target_sibling.py", "sibling = True\n")
    if kind == "module":
        if extension == "replacement":
            project.write("first/pkg/target.py", "original = True\n")
        project.write("second/pkg/target.py", "first = 1\nsecond = 2\n")
        imported = "pkg.target"
    else:
        if extension == "replacement":
            project.write("first/pkg/target/original.py", "original = True\n")
            if kind == "package":
                project.write("first/pkg/target/__init__.py", "original = True\n")
        if kind == "package":
            project.write("second/pkg/target/__init__.py", "value = 1\n")
        project.write("second/pkg/target/used.py", "first = 1\nsecond = 2\n")
        project.write("second/pkg/target/unused.py", "unused = True\n")
        project.write("second/pkg/target/omitted.py", "omitted = True\n")
        imported = "pkg.target.used"
    project.write("sibling.py", "sibling = True\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\n"
        "sys.path[:0] = ['first', 'second']\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['pkg.target'], branch=sys.argv[3] == 'True', "
        "omit=['*/omitted.py'])\n"
        "assert 'pkg' not in sys.modules\n"
        "assert 'pkg.target' not in sys.modules\n"
        "coverage.start()\n"
        f"importlib.import_module({imported!r})\n"
        "import pkg.target_sibling\nimport sibling\n"
        "coverage.stop()\ncoverage.save()\n"
        "coverage.start()\n"
        f"importlib.import_module({imported!r})\n"
        "coverage.stop()\ncoverage.save()\n",
    )
    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        str(branch),
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", str(branch))

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    expected_files = _relative_files(project, ".coverage.reference")
    target = "second/pkg/target.py" if kind == "module" else "second/pkg/target/used.py"
    assert target in expected_files
    assert _relative_files(project, ".coverage.actual") == expected_files
    assert "sibling.py" not in expected_files
    assert "second/pkg/target_sibling.py" not in expected_files
    assert "second/pkg/target/omitted.py" not in expected_files
    assert all(not filename.startswith("first/") for filename in expected_files)
    for filename in expected_files:
        assert project.lines(filename, ".coverage.actual") == project.lines(
            filename, ".coverage.reference"
        )
        assert project.arcs(filename, ".coverage.actual") == project.arcs(
            filename, ".coverage.reference"
        )


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("compiled_path", ["single.py", "old/single.py", "old/renamed.py"])
def test_source_packages_collect_sourceless_bytecode_modules(
    tmp_path: Path, *, branch: bool, compiled_path: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("single.py", "first = 1\nsecond = 2\n")
    project.write("sibling.py", "value = 3\n")
    project.write(
        "prepare.py",
        "import py_compile\nimport sys\nfrom pathlib import Path\n"
        "py_compile.compile('single.py', cfile='single.pyc', dfile=sys.argv[1], doraise=True)\n"
        "Path('single.py').unlink()\n",
    )
    prepared = project.run_python("prepare.py", str(project.root / compiled_path))
    prepared.expect(0)
    project.write(
        "collect.py",
        "import importlib\nimport sys\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['single'], branch=sys.argv[3] == 'True')\n"
        "assert 'single' not in sys.modules\n"
        "coverage.start()\n"
        "import single\nimport sibling\n"
        "coverage.stop()\ncoverage.save()\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        str(branch),
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", str(branch))

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    filename = "old/renamed.py" if compiled_path == "old/renamed.py" else "single.py"
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {filename}
    )
    assert (
        project.lines(filename, ".coverage.actual")
        == project.lines(filename, ".coverage.reference")
        == [1, 2]
    )
    assert project.arcs(filename, ".coverage.actual") == project.arcs(
        filename, ".coverage.reference"
    )


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("raw_filename", ["old/target.py", ""])
def test_source_packages_normalize_path_valued_frame_files(
    tmp_path: Path, *, branch: bool, raw_filename: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("target.py", "value = 1\n")
    project.write("loaded/target.py", "value = 1\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "CoveragePlugin = importlib.import_module(sys.argv[1] + '.plugin').CoveragePlugin\n"
        "class CheckFilename(CoveragePlugin):\n"
        "    seen = False\n"
        "    def file_tracer(self, filename):\n"
        "        if Path(filename).name == 'target.py':\n"
        "            assert filename == str(Path('loaded/target.py').resolve())\n"
        "            self.seen = True\n"
        "        return None\n"
        "plugin = CheckFilename()\n"
        "def initialize(registry):\n    registry.add_file_tracer(plugin)\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['target'], branch=sys.argv[3] == 'True', plugins=[initialize])\n"
        "coverage.start()\n"
        "filename = str(Path(sys.argv[4]).resolve()) if sys.argv[4] else ''\n"
        "exec(compile('value = 1\\n', filename, 'exec'), "
        "{'__name__': '__main__', '__file__': Path('loaded/target.py')})\n"
        "coverage.stop()\ncoverage.save()\nassert plugin.seen\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        str(branch),
        raw_filename,
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", str(branch), raw_filename)

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {"loaded/target.py"}
    )
    assert (
        project.lines("loaded/target.py", ".coverage.actual")
        == project.lines("loaded/target.py", ".coverage.reference")
        == [1]
    )
    assert project.arcs("loaded/target.py", ".coverage.actual") == project.arcs(
        "loaded/target.py", ".coverage.reference"
    )


@pytest.mark.parametrize("branch", [False, True])
def test_source_packages_cache_first_normalized_frame_filename(
    tmp_path: Path, *, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("target.py", "value = 1\n")
    project.write("first/target.py", "first = 1\nsecond = 2\n")
    project.write("second/target.py", "first = 1\nsecond = 2\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['target'], branch=sys.argv[3] == 'True')\n"
        "coverage.start()\n"
        "filename = str(Path('old/target.py').resolve())\n"
        "exec(compile('first = 1\\n', filename, 'exec'), "
        "{'__name__': 'target', '__file__': str(Path('first/target.py').resolve())})\n"
        "exec(compile('\\nsecond = 2\\n', filename, 'exec'), "
        "{'__name__': 'target', '__file__': str(Path('second/target.py').resolve())})\n"
        "coverage.stop()\ncoverage.save()\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        str(branch),
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", str(branch))

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {"first/target.py"}
    )
    assert (
        project.lines("first/target.py", ".coverage.actual")
        == project.lines("first/target.py", ".coverage.reference")
        == [1, 2]
    )
    assert project.arcs("first/target.py", ".coverage.actual") == project.arcs(
        "first/target.py", ".coverage.reference"
    )


@pytest.mark.parametrize("branch", [False, True])
def test_source_packages_frame_filename_errors_preserve_destination(
    tmp_path: Path, *, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("target.py", "value = 1\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "class BadFile:\n"
        "    def __str__(self):\n        raise RuntimeError('module filename failed')\n"
        "destination = Path(sys.argv[2])\ndestination.write_bytes(b'unchanged')\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['target'], branch=sys.argv[3] == 'True')\n"
        "coverage.start()\n"
        "try:\n"
        "    filename = str(Path('target.py').resolve())\n"
        "    exec(compile('value = 1\\n', filename, 'exec'), "
        "{'__name__': 'target', '__file__': BadFile()})\n"
        "except RuntimeError as error:\n"
        "    assert str(error) == 'module filename failed'\n"
        "else:\n    raise AssertionError('frame filename errors must propagate')\n"
        "finally:\n    coverage.stop()\n"
        "assert destination.read_bytes() == b'unchanged'\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        str(branch),
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", str(branch))

    assert (
        (actual.returncode, actual.stdout, actual.stderr)
        == (expected.returncode, expected.stdout, expected.stderr)
        == (0, "", "")
    )
    assert (project.root / ".coverage.actual").read_bytes() == b"unchanged"


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize(
    ("original", "mapped"), [("executed.py", "target.py"), ("target.py", "other.py")]
)
def test_source_packages_derive_main_module_names_from_plugin_sources(
    tmp_path: Path, *, branch: bool, original: str, mapped: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("target/__init__.py", "value = 0\n")
    project.write("target.py", "value = 1\n")
    project.write("other.py", "value = 2\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "protocols = importlib.import_module(sys.argv[1] + '.plugin')\n"
        "class Tracer(protocols.FileTracer):\n"
        "    def source_filename(self):\n        return str(Path(sys.argv[5]).resolve())\n"
        "    def line_number_range(self, frame):\n        return frame.f_lineno, frame.f_lineno\n"
        "class Plugin(protocols.CoveragePlugin):\n"
        "    def file_tracer(self, filename):\n"
        "        if filename == str(Path(sys.argv[4]).resolve()):\n            return Tracer()\n"
        "        return None\n"
        "def initialize(registry):\n    registry.add_file_tracer(Plugin())\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['target'], branch=sys.argv[3] == 'True', plugins=[initialize])\n"
        "coverage.start()\nimport target\n"
        "exec(compile('value = 1\\n', str(Path(sys.argv[4]).resolve()), 'exec'), {})\n"
        "coverage.stop()\ncoverage.save()\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        str(branch),
        original,
        mapped,
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_python(
        "collect.py", "vfy", ".coverage.actual", str(branch), original, mapped
    )

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    expected_files = {"target/__init__.py"}
    if mapped == "target.py":
        expected_files.add(mapped)
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == expected_files
    )
    for filename in expected_files:
        assert (
            project.lines(filename, ".coverage.actual")
            == project.lines(filename, ".coverage.reference")
            == [1]
        )
        assert project.arcs(filename, ".coverage.actual") == project.arcs(
            filename, ".coverage.reference"
        )


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("matching_first", [False, True])
@pytest.mark.parametrize("plugin_kind", ["noop", "static"])
@pytest.mark.parametrize("metadata", ["path", "error"])
def test_source_packages_cache_plugin_raw_dispositions(
    tmp_path: Path, *, branch: bool, matching_first: bool, plugin_kind: str, metadata: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("target/__init__.py", "value = 0\n")
    project.write("first/target.py", "first = 1\nsecond = 2\n")
    project.write("second/target.py", "first = 1\nsecond = 2\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "protocols = importlib.import_module(sys.argv[1] + '.plugin')\n"
        "class BadFile:\n"
        "    def __str__(self):\n        raise RuntimeError('cached filename was inspected')\n"
        "class Tracer(protocols.FileTracer):\n"
        "    def __init__(self, filename):\n        self.filename = filename\n"
        "    def source_filename(self):\n        return self.filename\n"
        "    def line_number_range(self, frame):\n        return frame.f_lineno, frame.f_lineno\n"
        "class Plugin(protocols.CoveragePlugin):\n"
        "    calls = 0\n"
        "    def file_tracer(self, filename):\n"
        "        if Path(filename).name == 'target.py':\n"
        "            self.calls += 1\n"
        "            if sys.argv[5] == 'static':\n                return Tracer(filename)\n"
        "        return None\n"
        "plugin = Plugin()\n"
        "def initialize(registry):\n    registry.add_file_tracer(plugin)\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['target'], branch=sys.argv[3] == 'True', plugins=[initialize])\n"
        "names = ('target', 'sibling') if sys.argv[4] == 'True' else ('sibling', 'target')\n"
        "coverage.start()\nimport target\n"
        "filename = str(Path('old/target.py').resolve())\n"
        "exec(compile('first = 1\\n', filename, 'exec'), "
        "{'__name__': names[0], '__file__': str(Path('first/target.py').resolve())})\n"
        "second_file = BadFile() if sys.argv[6] == 'error' "
        "else str(Path('second/target.py').resolve())\n"
        "exec(compile('\\nsecond = 2\\n', filename, 'exec'), "
        "{'__name__': names[1], '__file__': second_file})\n"
        "coverage.stop()\ncoverage.save()\nassert plugin.calls == 1\n",
    )

    arguments = (str(branch), str(matching_first), plugin_kind, metadata)
    expected = project.run_python(
        "collect.py", "coverage", ".coverage.reference", *arguments, env={"COVERAGE_CORE": "ctrace"}
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", *arguments)

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    expected_files = {"target/__init__.py"}
    if matching_first:
        expected_files.add("first/target.py")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == expected_files
    )
    for filename in expected_files:
        assert (
            project.lines(filename, ".coverage.actual")
            == project.lines(filename, ".coverage.reference")
            == ([1] if filename.endswith("__init__.py") else [1, 2])
        )
        assert project.arcs(filename, ".coverage.actual") == project.arcs(
            filename, ".coverage.reference"
        )


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("matching_first", [False, True])
def test_source_packages_cache_dynamic_mapped_decisions(
    tmp_path: Path, *, branch: bool, matching_first: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("target/__init__.py", "value = 0\n")
    project.write("mapped/target.py", "first = 1\nsecond = 2\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "protocols = importlib.import_module(sys.argv[1] + '.plugin')\n"
        "class BadSpec:\n"
        "    def __bool__(self):\n        raise RuntimeError('cached module was inspected')\n"
        "class Tracer(protocols.FileTracer):\n"
        "    def has_dynamic_source_filename(self):\n        return True\n"
        "    def dynamic_source_filename(self, filename, frame):\n"
        "        return str(Path('mapped/target.py').resolve())\n"
        "    def line_number_range(self, frame):\n        return frame.f_lineno, frame.f_lineno\n"
        "class Plugin(protocols.CoveragePlugin):\n"
        "    def file_tracer(self, filename):\n"
        "        if Path(filename).name.startswith('executed'):\n            return Tracer()\n"
        "        return None\n"
        "def initialize(registry):\n    registry.add_file_tracer(Plugin())\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['target'], branch=sys.argv[3] == 'True', plugins=[initialize])\n"
        "name = 'target' if sys.argv[4] == 'True' else 'sibling'\n"
        "coverage.start()\nimport target\n"
        "exec(compile('first = 1\\n', str(Path('executed.py').resolve()), 'exec'), "
        "{'__name__': name})\n"
        "exec(compile('\\nsecond = 2\\n', str(Path('executed.py').resolve()), 'exec'), "
        "{'__name__': '__main__', '__spec__': BadSpec()})\n"
        "exec(compile('\\nsecond = 2\\n', str(Path('executed_other.py').resolve()), 'exec'), "
        "{'__name__': '__main__', '__spec__': BadSpec()})\n"
        "coverage.stop()\ncoverage.save()\n",
    )

    arguments = (str(branch), str(matching_first))
    expected = project.run_python(
        "collect.py", "coverage", ".coverage.reference", *arguments, env={"COVERAGE_CORE": "ctrace"}
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", *arguments)

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    expected_files = {"target/__init__.py"}
    if matching_first:
        expected_files.add("mapped/target.py")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == expected_files
    )
    for filename in expected_files:
        assert (
            project.lines(filename, ".coverage.actual")
            == project.lines(filename, ".coverage.reference")
            == ([1] if filename.endswith("__init__.py") else [1, 2])
        )
        assert project.arcs(filename, ".coverage.actual") == project.arcs(
            filename, ".coverage.reference"
        )


@pytest.mark.parametrize("branch", [False, True])
def test_source_packages_refresh_dynamic_paths_without_frame_file_lookup(
    tmp_path: Path, *, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("target/__init__.py", "value = 0\n")
    project.write("first/target.py", "value = 1\n")
    project.write("second/target.py", "value = 1\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "protocols = importlib.import_module(sys.argv[1] + '.plugin')\n"
        "class BadFile:\n"
        "    def __str__(self):\n        raise RuntimeError('dynamic frame file was inspected')\n"
        "class Tracer(protocols.FileTracer):\n"
        "    current = None\n"
        "    def has_dynamic_source_filename(self):\n        return True\n"
        "    def dynamic_source_filename(self, filename, frame):\n"
        "        self.current = frame.f_globals.get('virtual', self.current)\n"
        "        return self.current\n"
        "    def line_number_range(self, frame):\n        return frame.f_lineno, frame.f_lineno\n"
        "class Plugin(protocols.CoveragePlugin):\n"
        "    def file_tracer(self, filename):\n"
        "        if Path(filename).name == 'executed.py':\n            return Tracer()\n"
        "        return None\n"
        "def initialize(registry):\n    registry.add_file_tracer(Plugin())\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['target'], branch=sys.argv[3] == 'True', plugins=[initialize])\n"
        "code = compile('value = 1\\n', str(Path('executed.py').resolve()), 'exec')\n"
        "coverage.start()\nimport target\n"
        "exec(code, {'__name__': 'target', 'virtual': str(Path('first/target.py').resolve())})\n"
        "exec(code, {'__name__': 'target', '__file__': BadFile(), "
        "'virtual': str(Path('second/target.py').resolve())})\n"
        "coverage.stop()\ncoverage.save()\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        str(branch),
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", str(branch))

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    expected_files = {"target/__init__.py", "first/target.py", "second/target.py"}
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == expected_files
    )
    for filename in expected_files:
        assert (
            project.lines(filename, ".coverage.actual")
            == project.lines(filename, ".coverage.reference")
            == [1]
        )
        assert project.arcs(filename, ".coverage.actual") == project.arcs(
            filename, ".coverage.reference"
        )


@pytest.mark.parametrize("branch", [False, True])
def test_source_packages_reject_synthetic_originals_before_plugins(
    tmp_path: Path, *, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("target/__init__.py", "value = 0\n")
    project.write("mapped/target.py", "value = 1\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "protocols = importlib.import_module(sys.argv[1] + '.plugin')\n"
        "class Tracer(protocols.FileTracer):\n"
        "    def source_filename(self):\n        return str(Path('mapped/target.py').resolve())\n"
        "class Plugin(protocols.CoveragePlugin):\n"
        "    calls = 0\n"
        "    def file_tracer(self, filename):\n"
        "        if filename == '<string>':\n"
        "            self.calls += 1\n            return Tracer()\n"
        "        return None\n"
        "plugin = Plugin()\n"
        "def initialize(registry):\n    registry.add_file_tracer(plugin)\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['target'], branch=sys.argv[3] == 'True', plugins=[initialize])\n"
        "coverage.start()\nimport target\n"
        "exec(compile('value = 1\\n', '<string>', 'exec'), {'__name__': 'target'})\n"
        "coverage.stop()\ncoverage.save()\nassert plugin.calls == 0\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        str(branch),
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", str(branch))

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {"target/__init__.py"}
    )


def test_subprocess_source_packages_defer_unimported_parents_extended_path(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "first/pkg/__init__.py",
        "from pkgutil import extend_path\n__path__ = extend_path(__path__, __name__)\n",
    )
    project.write("second/pkg/__init__.py", "")
    project.write("second/pkg/target.py", "first = 1\nsecond = 2\n")
    project.write("second/pkg/target_sibling.py", "sibling = True\n")
    project.write(
        "child.py",
        "import sys\n"
        "if sys.argv[1] == 'coverage':\n"
        "    from coverage import Coverage\n"
        "    coverage = Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['pkg.target'], branch=True)\n"
        "    coverage.start()\n"
        "else:\n"
        "    from vfy.process_startup import active_coverage\n"
        "    assert active_coverage() is not None\n"
        "assert 'pkg' not in sys.modules\n"
        "import pkg.target\nimport pkg.target_sibling\n"
        "if sys.argv[1] == 'coverage':\n"
        "    coverage.stop()\n    coverage.save()\n",
    )
    project.write(
        "collect.py",
        "import os\nimport subprocess\nimport sys\nfrom pathlib import Path\n"
        "sys.path[:0] = ['first', 'second']\n"
        "os.environ['PYTHONPATH'] = os.pathsep.join(str(Path(root).resolve()) "
        "for root in ['first', 'second'])\n"
        "if sys.argv[1] == 'vfy':\n"
        "    from vfy import Coverage\n"
        "    from vfy.process_startup import subprocess_environment\n"
        "    coverage = Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['pkg.target'], branch=True)\n"
        "    with subprocess_environment(coverage):\n"
        "        subprocess.run([sys.executable, 'child.py', *sys.argv[1:]], check=True)\n"
        "    coverage.combine()\n    coverage.save()\n"
        "else:\n"
        "    subprocess.run([sys.executable, 'child.py', *sys.argv[1:]], check=True)\n",
    )

    expected = project.run_python("collect.py", "coverage", ".coverage.reference")
    actual = project.run_python("collect.py", "vfy", ".coverage.actual")

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {"second/pkg/target.py"}
    )
    for filename in _relative_files(project, ".coverage.reference"):
        assert project.lines(filename, ".coverage.actual") == project.lines(
            filename, ".coverage.reference"
        )
        assert project.arcs(filename, ".coverage.actual") == project.arcs(
            filename, ".coverage.reference"
        )


@pytest.mark.parametrize("branch", [False, True])
def test_deferred_source_module_imported_before_start_matches_reference(
    tmp_path: Path, *, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pkg/__init__.py",
        "from pathlib import Path\n__path__.append(str(Path('extra').resolve()))\n",
    )
    project.write("extra/target.py", "first = 1\nsecond = 2\n")
    project.write("extra/target_sibling.py", "sibling = True\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        f"source_pkgs=['pkg.target'], branch={branch!r})\n"
        "assert 'pkg' not in sys.modules\n"
        "import pkg.target\nimport pkg.target_sibling\n"
        "coverage.start()\ncoverage.stop()\ncoverage.save()\n",
    )
    expected = project.run_python(
        "collect.py", "coverage", ".coverage.reference", env={"COVERAGE_CORE": "ctrace"}
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual")

    assert actual.returncode == expected.returncode == 0
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == set()
    )
    assert project.lines("extra/target.py", ".coverage.actual") == project.lines(
        "extra/target.py", ".coverage.reference"
    )
    assert project.arcs("extra/target.py", ".coverage.actual") == project.arcs(
        "extra/target.py", ".coverage.reference"
    )


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("imported", [False, True])
@pytest.mark.parametrize("kind", ["module", "package", "namespace"])
def test_source_package_discovery_uses_loaded_package_directories(
    tmp_path: Path, *, branch: bool, imported: bool, kind: str
) -> None:
    project = VfyProject(tmp_path)
    if kind == "module":
        project.write("target.py", "value = 1\n")
    else:
        if kind == "package":
            project.write("target/__init__.py", "value = 1\n")
        project.write("target/unused.py", "unused = True\n")
    project.write("sibling.py", "sibling = True\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        f"if {imported!r}:\n    import target\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        f"source_pkgs=['target'], branch={branch!r})\n"
        "coverage.start()\ncoverage.stop()\ncoverage.save()\n",
    )
    expected = project.run_python(
        "collect.py", "coverage", ".coverage.reference", env={"COVERAGE_CORE": "ctrace"}
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual")

    assert actual.returncode == expected.returncode == 0
    expected_files = _relative_files(project, ".coverage.reference")
    assert _relative_files(project, ".coverage.actual") == expected_files
    assert expected_files == (
        {"target/__init__.py", "target/unused.py"} if imported and kind == "package" else set()
    )
    for filename in expected_files:
        assert project.lines(filename, ".coverage.actual") == project.lines(
            filename, ".coverage.reference"
        )
        assert project.arcs(filename, ".coverage.actual") == project.arcs(
            filename, ".coverage.reference"
        )


def test_source_packages_resolve_dotted_modules_through_meta_path(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "parent_executed = True\n")
    project.write("alternate.py", "first = 1\nsecond = 2\n")
    project.write("sibling.py", "value = 3\n")
    project.write(
        "collect.py",
        "import importlib\nimport importlib.abc\nimport importlib.util\nimport sys\n"
        "from pathlib import Path\n"
        "class Finder(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'pkg.target':\n"
        "            return importlib.util.spec_from_file_location(\n"
        "                fullname, Path('alternate.py').resolve())\n"
        "finder = Finder()\n"
        "sys.meta_path.insert(0, finder)\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['pkg.target'])\n"
        "assert 'pkg' not in sys.modules\n"
        "assert 'pkg.target' not in sys.modules\n"
        "coverage.start()\n"
        "importlib.import_module('pkg.target')\nimport sibling\n"
        "coverage.stop()\ncoverage.save()\n"
        "sys.meta_path.remove(finder)\n",
    )

    expected = project.run_python(
        "collect.py", "coverage", ".coverage.reference", env={"COVERAGE_CORE": "ctrace"}
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual")

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {"alternate.py"}
    )
    assert (
        project.lines("alternate.py", ".coverage.actual")
        == project.lines("alternate.py", ".coverage.reference")
        == [1, 2]
    )


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize(
    ("module", "package"), [(False, "target"), (True, "target"), (True, "pkg.target")]
)
def test_source_packages_match_main_module_names(
    tmp_path: Path, *, branch: bool, module: bool, package: str
) -> None:
    project = VfyProject(tmp_path)
    filename = package.replace(".", "/") + ".py"
    project.write(".coveragerc", f"[run]\nsource_pkgs = {package}\n")
    project.write("pkg/__init__.py", "value = 3\n")
    project.write(filename, "import sibling\nvalue = 1\n")
    project.write("sibling.py", "value = 2\n")
    arguments = (
        *(["--branch"] if branch else []),
        *(["-m", package] if module else [filename]),
    )

    expected = project.run_coverage_py("run", "--data-file=.coverage.reference", *arguments)
    actual = project.run_vfy("coverage", "run", "--data-file=.coverage.actual", *arguments)

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {filename}
    )
    assert (
        project.lines(filename, ".coverage.actual")
        == project.lines(filename, ".coverage.reference")
        == [1, 2]
    )
    assert project.arcs(filename, ".coverage.actual") == project.arcs(
        filename, ".coverage.reference"
    )


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("spec", ["falsy", "main", "non_string"])
def test_source_packages_main_module_spec_falls_back_to_filename(
    tmp_path: Path, *, branch: bool, spec: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("target.py", "first = 1\nsecond = 2\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "class FalsySpec:\n"
        "    def __bool__(self):\n        return False\n"
        "    @property\n    def name(self):\n"
        "        raise AssertionError('a falsy spec must not be inspected')\n"
        "specs = {'falsy': FalsySpec(), 'main': SimpleNamespace(name='__main__'), "
        "'non_string': SimpleNamespace(name=42)}\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['target'], branch=sys.argv[3] == 'True')\n"
        "coverage.start()\n"
        "filename = str(Path('target.py').resolve())\n"
        "exec(compile(Path(filename).read_text(), filename, 'exec'), "
        "{'__name__': '__main__', '__spec__': specs[sys.argv[4]]})\n"
        "coverage.stop()\ncoverage.save()\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        str(branch),
        spec,
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", str(branch), spec)

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {"target.py"}
    )
    assert (
        project.lines("target.py", ".coverage.actual")
        == project.lines("target.py", ".coverage.reference")
        == [1, 2]
    )
    assert project.arcs("target.py", ".coverage.actual") == project.arcs(
        "target.py", ".coverage.reference"
    )


@pytest.mark.parametrize("branch", [False, True])
def test_source_package_discovery_preserves_module_selection_after_save(
    tmp_path: Path, *, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "value = 1\n")
    project.write("pkg/unused.py", "value = 2\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['pkg'], branch=sys.argv[3] == 'True')\n"
        "coverage.start()\nimport pkg\ncoverage.save()\n"
        "exec(compile('value = 2\\n', str(Path('pkg/unused.py').resolve()), 'exec'), "
        "{'__name__': 'sibling'})\n"
        "coverage.stop()\ncoverage.save()\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        str(branch),
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", str(branch))

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {"pkg/__init__.py", "pkg/unused.py"}
    )
    for filename in ("pkg/__init__.py", "pkg/unused.py"):
        assert project.lines(filename, ".coverage.actual") == project.lines(
            filename, ".coverage.reference"
        )
        assert project.arcs(filename, ".coverage.actual") == project.arcs(
            filename, ".coverage.reference"
        )
    assert project.lines("pkg/unused.py", ".coverage.actual") == []


@pytest.mark.parametrize("branch", [False, True])
def test_source_package_discovery_requires_existing_loaded_module_file(
    tmp_path: Path, *, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "value = 1\n")
    project.write("pkg/unused.py", "value = 2\n")
    project.write("pkg/missing/unused.py", "value = 3\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['pkg'], branch=sys.argv[3] == 'True')\n"
        "coverage.start()\nimport pkg\n"
        "pkg.__file__ = str(Path('pkg/missing/__init__.py').resolve())\n"
        "coverage.stop()\ncoverage.save()\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        str(branch),
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", str(branch))

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {"pkg/__init__.py"}
    )
    assert (
        project.lines("pkg/__init__.py", ".coverage.actual")
        == project.lines("pkg/__init__.py", ".coverage.reference")
        == [1]
    )
    assert project.arcs("pkg/__init__.py", ".coverage.actual") == project.arcs(
        "pkg/__init__.py", ".coverage.reference"
    )


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("matching_first", [False, True])
def test_source_packages_cache_first_module_decision_for_each_filename(
    tmp_path: Path, *, branch: bool, matching_first: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "value = 0\n")
    project.write("pkg/used.py", "first = 1\nsecond = 2\n")
    project.write(
        "collect.py",
        "import importlib\nimport sys\nfrom pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "source_pkgs=['pkg'], branch=sys.argv[3] == 'True')\n"
        "names = ('pkg.used', 'sibling') if sys.argv[4] == 'True' "
        "else ('sibling', 'pkg.used')\n"
        "coverage.start()\nimport pkg\n"
        "filename = str(Path('pkg/used.py').resolve())\n"
        "exec(compile('first = 1\\n', filename, 'exec'), {'__name__': names[0]})\n"
        "exec(compile('\\nsecond = 2\\n', filename, 'exec'), {'__name__': names[1]})\n"
        "coverage.stop()\ncoverage.save()\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        str(branch),
        str(matching_first),
    )
    actual = project.run_python(
        "collect.py", "vfy", ".coverage.actual", str(branch), str(matching_first)
    )

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert (
        _relative_files(project, ".coverage.actual")
        == _relative_files(project, ".coverage.reference")
        == {"pkg/__init__.py", "pkg/used.py"}
    )
    assert (
        project.lines("pkg/used.py", ".coverage.actual")
        == project.lines("pkg/used.py", ".coverage.reference")
        == ([1, 2] if matching_first else [])
    )
    assert project.arcs("pkg/used.py", ".coverage.actual") == project.arcs(
        "pkg/used.py", ".coverage.reference"
    )


@pytest.mark.parametrize(("branch", "relative_files"), [(False, False), (True, True)])
def test_configured_source_discovery_matches_coverage_python_before_persistence(
    tmp_path: Path, *, branch: bool, relative_files: bool
) -> None:
    """Unexecuted importable files must survive the public collection boundary."""

    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "")
    project.write("pkg/used.py", "value = 1\n")
    project.write("pkg/unused.py", "first = 1\nsecond = 2\n")
    project.write("pkg/empty.py", "")
    project.write("pkg/window.pyw", "window = True\n")
    project.write("pkg/omitted.py", "omitted = True\n")
    project.write("pkg/module.test.py", "editor_copy = True\n")
    project.write("pkg/nested/__init__.py", "")
    project.write("pkg/nested/module.py", "nested = True\n")
    project.write("pkg/not_package/module.py", "not_importable = True\n")
    project.write("driver.py", "import pkg.used\n")
    if relative_files:
        project.write(".coveragerc", "[run]\nrelative_files = true\n")
    arguments = (
        *(["--branch"] if branch else []),
        "--source=pkg",
        "--omit=*/omitted.py",
        "driver.py",
    )

    expected = project.run_coverage_py("run", "--data-file=.coverage.reference", *arguments)
    actual = project.run_vfy("coverage", "run", "--data-file=.coverage.actual", *arguments)

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert _relative_files(project, ".coverage.actual") == _relative_files(
        project, ".coverage.reference"
    )
    for filename in _relative_files(project, ".coverage.reference"):
        assert project.lines(filename, ".coverage.actual") == project.lines(
            filename, ".coverage.reference"
        )
        assert project.arcs(filename, ".coverage.actual") == project.arcs(
            filename, ".coverage.reference"
        )


def test_plugin_executable_discovery_is_persisted_with_its_tracer(tmp_path: Path) -> None:
    """The Python plug-in callback must feed the Rust collection snapshot."""

    project = VfyProject(tmp_path)
    project.write("template.tpl", "hello\n")
    project.write("ignored.tpl", "ignored\n")
    project.write(
        "collect.py",
        "import importlib\n"
        "import sys\n"
        "from pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "CoveragePlugin = importlib.import_module(sys.argv[1] + '.plugin').CoveragePlugin\n\n"
        "class Plugin(CoveragePlugin):\n"
        "    def find_executable_files(self, src_dir):\n"
        "        return [str(Path(src_dir) / name) for name in "
        "('template.tpl', 'ignored.tpl')]\n\n"
        "def initialize(registry):\n"
        "    registry.add_file_tracer(Plugin())\n\n"
        "coverage = tool.Coverage(data_file=sys.argv[2], source=['.'], "
        "omit=['*/ignored.tpl'], "
        "plugins=[initialize], config_file=False)\n"
        "coverage.start()\n"
        "exec(compile('value = 1\\n', 'measured.py', 'exec'), {})\n"
        "coverage.stop()\n"
        "coverage.save()\n",
    )

    expected = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference-plugin",
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual-plugin")

    assert (actual.returncode, actual.stderr) == (expected.returncode, expected.stderr) == (0, "")
    assert _relative_files(project, ".coverage.actual-plugin") == _relative_files(
        project, ".coverage.reference-plugin"
    )
    data = project.data(".coverage.actual-plugin")
    reference = project.data(".coverage.reference-plugin")
    virtual = str(tmp_path / "template.tpl")
    assert virtual in data.measured_files()
    actual_tracer = data.file_tracer(virtual)
    expected_tracer = reference.file_tracer(virtual)
    assert actual_tracer is not None
    assert actual_tracer.endswith(".Plugin")
    assert expected_tracer is not None
    assert expected_tracer.endswith(".Plugin")
    assert str(tmp_path / "ignored.tpl") not in data.measured_files()


def test_conflicting_plugin_discovery_is_rejected_before_persistence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("template.tpl", "hello\n")
    project.write(
        "conflict.py",
        "import importlib\n"
        "import sys\n"
        "from pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "CoveragePlugin = importlib.import_module(sys.argv[1] + '.plugin').CoveragePlugin\n\n"
        "class First(CoveragePlugin):\n"
        "    def find_executable_files(self, src_dir):\n"
        "        return [str(Path(src_dir) / 'template.tpl')]\n\n"
        "class Second(First):\n"
        "    pass\n\n"
        "def initialize(registry):\n"
        "    registry.add_file_tracer(First())\n"
        "    registry.add_file_tracer(Second())\n\n"
        "coverage = tool.Coverage(data_file=sys.argv[2], source=['.'], "
        "plugins=[initialize], config_file=False)\n"
        "coverage.start()\n"
        "exec(compile('value = 1\\n', 'measured.py', 'exec'), {})\n"
        "coverage.stop()\n"
        "coverage.save()\n",
    )

    expected = project.run_python(
        "conflict.py",
        "coverage",
        ".coverage.reference-conflict",
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_python("conflict.py", "vfy", ".coverage.actual-conflict")

    assert expected.returncode != 0
    assert actual.returncode != 0
    assert "Conflicting file tracer name" in expected.stderr
    assert "cannot combine conflicting file tracers" in actual.stderr
    assert not (tmp_path / ".coverage.actual-conflict").exists()


def test_discovered_tracer_conflict_with_observed_tracer_preserves_destination(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("template.tpl", "hello\n")
    project.write("baseline.py", "baseline = True\n")
    project.write(
        "observed_conflict.py",
        "import importlib\n"
        "import sys\n"
        "from pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "protocols = importlib.import_module(sys.argv[1] + '.plugin')\n"
        "CoveragePlugin = protocols.CoveragePlugin\n"
        "FileTracer = protocols.FileTracer\n\n"
        "class Tracer(FileTracer):\n"
        "    def __init__(self, filename):\n"
        "        self.filename = filename\n"
        "    def source_filename(self):\n"
        "        return self.filename\n\n"
        "class Observed(CoveragePlugin):\n"
        "    def file_tracer(self, filename):\n"
        "        if filename.endswith('executed.py'):\n"
        "            return Tracer(str(Path(filename).with_name('template.tpl')))\n"
        "        return None\n\n"
        "class Discovered(CoveragePlugin):\n"
        "    def find_executable_files(self, src_dir):\n"
        "        return [str(Path(src_dir) / 'template.tpl')]\n\n"
        "def initialize(registry):\n"
        "    registry.add_file_tracer(Observed())\n"
        "    registry.add_file_tracer(Discovered())\n\n"
        "coverage = tool.Coverage(data_file=sys.argv[2], source=['.'], "
        "plugins=[initialize], config_file=False)\n"
        "coverage.start()\n"
        "filename = str(Path.cwd() / 'executed.py')\n"
        "exec(compile('value = 1\\n', filename, 'exec'), {})\n"
        "coverage.stop()\n"
        "coverage.save()\n",
    )
    actual_file = tmp_path / ".coverage.actual-observed"
    baseline = project.run_vfy("coverage", "run", f"--data-file={actual_file.name}", "baseline.py")
    assert (baseline.returncode, baseline.stderr) == (0, "")
    before = actual_file.read_bytes()

    expected = project.run_python(
        "observed_conflict.py",
        "coverage",
        ".coverage.reference-observed",
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_python("observed_conflict.py", "vfy", actual_file.name)

    assert expected.returncode != 0
    assert actual.returncode != 0
    assert "Conflicting file tracer name" in expected.stderr
    assert "cannot combine conflicting file tracers" in actual.stderr
    assert actual_file.read_bytes() == before
