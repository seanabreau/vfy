"""Compare ``Coverage.get_option`` and deferred source checks with Coverage.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject

_PLUGIN = (
    "import coverage\n"
    "class Plugin(coverage.CoveragePlugin):\n"
    "    pass\n"
    "def coverage_init(reg, options):\n"
    "    reg.add_noop(Plugin())\n"
)

_READ_OPTIONS = (
    "import json, sys\n"
    "import importlib\n"
    "module = importlib.import_module(sys.argv[1])\n"
    "cov = module.Coverage(config_file='.coveragerc')\n"
    "seen = {}\n"
    "for name in sys.argv[2:]:\n"
    "    try:\n"
    "        seen[name] = cov.get_option(name)\n"
    "    except Exception as error:\n"
    "        seen[name] = [type(error).__name__, str(error)]\n"
    "print(json.dumps(seen, sort_keys=True))\n"
)

_OPTIONS = (
    "paths",
    "paths:source",
    "myplug:answer",
    "myplug:missing",
    "run:plugins",
    "run:data_file",
    "run:source",
    "run:source_pkgs",
    "run:parallel",
    "run:relative_files",
    "run:sigterm",
    "run:concurrency",
    "run:disable_warnings",
    "report:fail_under",
    "report:format",
    "run:context",
    "unknown:option",
)


@pytest.mark.parametrize("data_file_source", ["config", "environment"])
@pytest.mark.parametrize("configured", [True, False])
def test_get_option_matches_coverage_py(
    tmp_path: Path, configured: bool, data_file_source: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("myplug.py", _PLUGIN)
    project.write("read.py", _READ_OPTIONS)
    run = (
        "[run]\nplugins = myplug\nparallel = true\nsigterm = true\n"
        "disable_warnings = no-data-collected\n"
    )
    if configured:
        run += "source_pkgs = alpha\nsource = src\nconcurrency = thread\ncontext = ctx\n"
        if data_file_source == "config":
            run += "data_file = sub/.cov\n"
    project.write(
        ".coveragerc",
        run
        + "[report]\nfail_under = 97.5\n"
        + "[paths]\nsource =\n    src\n    /elsewhere/src\nother =\n    one\n    two\n"
        + "[myplug]\nanswer = 42\n",
    )
    env = {"COVERAGE_FILE": "env/.cov"} if data_file_source == "environment" else None

    expected = project.run_python("read.py", "coverage", *_OPTIONS, env=env)
    actual = project.run_python("read.py", "vfy", *_OPTIONS, env=env)

    assert expected.returncode == actual.returncode == 0, expected.stderr + actual.stderr
    reference = json.loads(expected.stdout)
    observed = json.loads(actual.stdout)
    # Coverage.py raises its own ConfigError; vfy raises vfy.errors.ConfigError.
    assert (
        observed["unknown:option"]
        == reference["unknown:option"]
        == [
            "ConfigError",
            "No such option: 'unknown:option'",
        ]
    )
    assert observed == reference


@pytest.mark.parametrize("explicit", [None, "named.cov"])
def test_run_data_file_option_is_the_configured_name(tmp_path: Path, explicit: str | None) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", "[run]\ndata_file = configured/.cov\n")
    project.write(
        "read.py",
        "import importlib, sys\n"
        "module = importlib.import_module(sys.argv[1])\n"
        f"cov = module.Coverage(data_file={explicit!r}, config_file='.coveragerc')\n"
        "print(cov.get_option('run:data_file'))\n",
    )

    expected = project.run_python("read.py", "coverage")
    actual = project.run_python("read.py", "vfy")

    assert (actual.returncode, actual.stdout, actual.stderr) == (
        expected.returncode,
        expected.stdout,
        expected.stderr,
    )
    assert actual.stdout.strip() == (explicit or "configured/.cov")


_DEFERRED = (
    "import importlib, sys, warnings\n"
    "module = importlib.import_module(sys.argv[1])\n"
    "with warnings.catch_warnings(record=True) as caught:\n"
    "    warnings.simplefilter('always')\n"
    "    cov = module.Coverage(\n"
    "        source_pkgs=['absent_package'], config_file=False, data_file=None)\n"
    "    print('constructed')\n"
    "    cov.start()\n"
    "    import target\n"
    "    cov.stop()\n"
    "    try:\n"
    "        cov.report(file=sys.stdout)\n"
    "    except Exception as error:\n"
    "        print(type(error).__name__, error)\n"
    "for item in caught:\n"
    "    message = str(item.message)\n"
    "    print(item.category.__name__, message[: message.index(' (')])\n"
    "    print(message[message.index('(') : message.index(')') + 1])\n"
)


def test_unimportable_source_package_is_deferred_like_coverage_py(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("target.py", "value = 1\n")
    project.write("run.py", _DEFERRED)

    expected = project.run_python("run.py", "coverage")
    actual = project.run_python("run.py", "vfy")

    assert expected.returncode == actual.returncode == 0, expected.stderr + actual.stderr
    assert not (tmp_path / ".coverage").exists()
    assert actual.stdout == expected.stdout
    assert "constructed" in actual.stdout
    assert "NoDataError No data to report." in actual.stdout
    assert "module-not-imported" in actual.stdout


@pytest.mark.parametrize(
    "configuration",
    [
        "[run]\nomit = b.py\n",
        "[report]\nomit = b.py\n",
        "[run]\nomit = a.py\n[report]\nomit = b.py\n",
    ],
)
def test_report_omit_comes_from_the_report_section_like_coverage_py(
    tmp_path: Path, configuration: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", configuration)
    first = project.write("a.py", "x = 1\ny = 2\n")
    second = project.write("b.py", "x = 1\n")
    project.write(
        "seed.py",
        "import sys\n"
        "from coverage import CoverageData\n"
        "data = CoverageData(basename='.coverage')\n"
        "data.add_lines({sys.argv[1]: [1], sys.argv[2]: [1]})\n"
        "data.write()\n",
    )
    project.run_python("seed.py", str(first), str(second)).expect(0)

    expected = project.run_coverage_py("report")
    actual = project.run_vfy("coverage", "report")

    assert (actual.returncode, actual.stdout) == (expected.returncode, expected.stdout)


@pytest.mark.parametrize("entry_point", ["cli", "pytest"])
@pytest.mark.parametrize("warning", ["default", "suppressed", "error"])
def test_source_takes_precedence_over_run_include(
    tmp_path: Path, entry_point: str, warning: str
) -> None:
    project = VfyProject(tmp_path)
    for name in ("left", "right"):
        project.write(f"lib/{name}.py", "value = 1\n")
    project.write(
        "test_run.py",
        "from lib import left, right\ndef test_values():\n    assert left.value == right.value\n",
    )
    settings = "[run]\nsource = lib\ninclude = lib/left.py\n"
    if warning == "suppressed":
        settings += "disable_warnings = include-ignored\n"
    project.write(".coveragerc", settings)

    for module, plugin in (("coverage", "pytest_cov.plugin"), ("vfy", "vfy.pytest_plugin")):
        database = f".{module}.db"
        env = {
            "COVERAGE_FILE": database,
            "PYTHONWARNINGS": "default" if warning == "default" else "error",
        }
        cli = ("-m", "coverage") if module == "coverage" else ("-m", "vfy", "coverage")
        if entry_point == "cli":
            result = project.run_python(*cli, "run", "test_run.py", env=env)
        else:
            result = project.run_python("-m", "pytest", "-p", plugin, "--cov", "-q", env=env)

        result.expect(1 if warning == "error" else 0)
        diagnostic = (
            "CoverageWarning: --include is ignored because --source is set (include-ignored)"
        )
        stream = result.stderr if entry_point == "cli" or warning == "error" else result.stdout
        assert stream.count(diagnostic) == (0 if warning == "suppressed" else 1)
        if warning == "error":
            assert not (tmp_path / database).exists()
        else:
            assert project.lines("lib/left.py", database) == [1]
            assert project.lines("lib/right.py", database) == [1]
            report = project.run_python(*cli, "report", "--format=total", env=env).expect(0)
            assert report.stdout.strip() == "100"


def test_include_ignored_warning_preserves_the_active_collector(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", "[run]\nsource_pkgs = lib\ninclude = lib/left.py\n")
    project.write("lib/left.py", "value = 1\n")
    project.write("lib/right.py", "value = 2\n")
    project.write(
        "run.py",
        "import importlib, sys, warnings\n"
        "module = importlib.import_module(sys.argv[1])\n"
        "cov = module.Coverage(data_file=sys.argv[2])\n"
        "with warnings.catch_warnings():\n"
        "    warnings.simplefilter('ignore')\n"
        "    cov.start()\n"
        "try:\n"
        "    import lib.left\n"
        "    cov.set_option('run:include', [])\n"
        "    try:\n"
        "        cov.start()\n"
        "    except Warning as error:\n"
        "        print(type(error).__name__, str(error).partition(';')[0])\n"
        "    assert module.Coverage.current() is cov\n"
        "    import lib.right\n"
        "finally:\n"
        "    cov.stop()\n"
        "assert module.Coverage.current() is None\n"
        "cov.save()\n",
    )

    for module in ("coverage", "vfy"):
        database = f".{module}.db"
        result = project.run_python("run.py", module, database, env={"PYTHONWARNINGS": "error"})
        assert result.expect(0).stdout.strip() == (
            "CoverageWarning --include is ignored because --source is set (include-ignored)"
        )
        assert project.lines("lib/left.py", database) == [1]
        assert project.lines("lib/right.py", database) == [1]
