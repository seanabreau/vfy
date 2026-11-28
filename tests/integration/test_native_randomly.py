"""Native execution retains the reviewed upstream plugin's random state per phase."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from zlib import crc32

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = requires_native_pytest

_ENVIRONMENT = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONHASHSEED": "0"}


@pytest.mark.parametrize("seed", [0, 137])
@pytest.mark.parametrize("reset", [True, False])
def test_seeded_order_and_fixture_phase_values_match_stock(
    tmp_path: Path, seed: int, *, reset: bool
) -> None:
    observations = []
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            dedent("""\
                import json
                import random
                from pathlib import Path
                import pytest

                def record(nodeid, phase):
                    with Path('events.jsonl').open('a') as output:
                        output.write(json.dumps([nodeid, phase, random.getrandbits(32)]) + '\\n')

                @pytest.fixture
                def resource(request):
                    record(request.node.nodeid, 'fixture setup')
                    yield
                    record(request.node.nodeid, 'fixture teardown')

                @pytest.hookimpl(trylast=True)
                def pytest_runtest_setup(item):
                    record(item.nodeid, 'setup hook')

                @pytest.hookimpl(trylast=True)
                def pytest_runtest_teardown(item):
                    record(item.nodeid, 'teardown hook')
                """),
        )
        for module in ("first", "second"):
            project.write(
                f"test_{module}.py",
                dedent("""\
                    import pytest
                    from conftest import record

                    def test_value(resource, request):
                        record(request.node.nodeid, 'call')

                    @pytest.mark.xfail(reason='expected', strict=True)
                    def test_xfail(resource, request):
                        record(request.node.nodeid, 'call')
                        assert False

                    @pytest.mark.skip(reason='unused')
                    def test_skip():
                        raise AssertionError('skipped body ran')
                    """),
            )
        arguments = (
            "-p",
            "randomly",
            f"--randomly-seed={seed}",
            *([] if reset else ["--randomly-dont-reset-seed"]),
            "-q",
            "--junitxml=results.xml",
        )
        result = (
            project.run_python("-m", "pytest", *arguments, env=_ENVIRONMENT)
            if route == "stock"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--no-cov", *arguments, env=_ENVIRONMENT
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed, 2 skipped, 2 xfailed" in result.stdout
        observations.append(
            [json.loads(row) for row in (project.root / "events.jsonl").read_text().splitlines()]
        )
    assert observations[0] == observations[1]
    assert sum(row[1] == "call" for row in observations[1]) == 4


def test_shadow_randomly_module_cannot_claim_reviewed_phase_hooks(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest_randomly.py",
        "from pathlib import Path\ndef pytest_runtest_call(item):\n    Path('called').touch()\n",
    )
    project.write("test_body.py", "from pathlib import Path\nPath('collected').touch()\n")
    result = project.run_vfy(
        "test", "--engine=pytest-native", "--no-cov", "-p", "pytest_randomly", env=_ENVIRONMENT
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "requires the installed upstream pytest-randomly plugin" in result.stdout + result.stderr
    assert not (project.root / "called").exists()
    assert not (project.root / "collected").exists()


def test_reviewed_randomly_phase_wrappers_are_rejected_before_execution(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        dedent("""\
            import pytest
            import pytest_randomly

            def pytest_configure(config):
                @pytest.hookimpl(wrapper=True)
                def pytest_runtest_setup(item):
                    yield

                config.pluginmanager.unregister(pytest_randomly)
                pytest_randomly.pytest_runtest_setup = pytest_runtest_setup
                config.pluginmanager.register(pytest_randomly, 'randomly')
            """),
    )
    project.write("test_body.py", "from pathlib import Path\nPath('collected').touch()\n")
    result = project.run_vfy(
        "test", "--engine=pytest-native", "--no-cov", "-p", "randomly", env=_ENVIRONMENT
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "requires plain pytest-randomly phase hooks" in result.stdout + result.stderr
    assert not (project.root / "collected").exists()


@pytest.mark.parametrize("exception", ["RuntimeError", "KeyboardInterrupt"])
def test_call_seed_failure_preserves_fixture_cleanup_and_skips_the_body(
    tmp_path: Path, exception: str
) -> None:
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "seed_probe-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: seed-probe\nVersion: 1.0\n",
        )
        project.write(
            "seed_probe-1.0.dist-info/entry_points.txt",
            "[pytest_randomly.random_seeder]\nprobe = seed_probe:seed\n",
        )
        # The callback fails on the real plugin's call-phase seed for this node.
        call_seed = crc32(b"test_body.py::test_value")
        project.write(
            "seed_probe.py",
            f"def seed(value):\n    if value == {call_seed}:\n"
            f"        raise {exception}('seed callback failed')\n",
        )
        project.write(
            "test_body.py",
            dedent("""\
                from pathlib import Path
                import pytest

                @pytest.fixture
                def resource():
                    Path('setup').touch()
                    yield
                    Path('teardown').touch()

                def test_value(resource):
                    Path('body').touch()
                """),
        )
        arguments = ("-p", "randomly", "--randomly-seed=0", "-q")
        result = (
            project.run_python("-m", "pytest", *arguments, env=_ENVIRONMENT)
            if route == "stock"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--no-cov", *arguments, env=_ENVIRONMENT
            )
        )
        assert result.returncode == (2 if exception == "KeyboardInterrupt" else 1), result
        assert "seed callback failed" in result.stdout + result.stderr
        assert (project.root / "setup").exists()
        assert (project.root / "teardown").exists()
        assert not (project.root / "body").exists()
