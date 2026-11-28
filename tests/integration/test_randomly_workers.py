"""Fresh workers retain the controller's ordering plugin seeds."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = requires_native_pytest


def _project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write(
        "conftest.py",
        dedent("""\
            import json
            import os
            from pathlib import Path
            import pytest

            @pytest.hookimpl(trylast=True)
            def pytest_configure(config):
                worker = os.getenv('PYTEST_XDIST_WORKER') or os.getenv(
                    'VFY_NATIVE_PARALLEL_INDEX', 'controller')
                directory = Path('configs')
                directory.mkdir(exist_ok=True)
                (directory / (worker + '.json')).write_text(json.dumps({
                    'seed': config.getoption('randomly_seed'),
                    'worker_seed': getattr(config, 'workerinput', {}).get('randomly_seed'),
                    'order_seed': config.getoption('random_order_seed'),
                    'worker_order_seed': getattr(config, 'workerinput', {}).get(
                        'random_order_seed'),
                    'order_enabled': config.getoption('random_order_enabled'),
                }))
            """),
    )
    for name in ("first", "second"):
        project.write(
            f"test_{name}.py",
            "import random\nfrom pathlib import Path\n"
            f"def test_{name}():\n"
            f"    Path('{name}.txt').write_text(str(random.getrandbits(32)))\n",
        )
    return project


@pytest.mark.parametrize(
    "route",
    ["native", "pytest-native", "native-xdist", "native-xdist-canonical", "native-xdist-serial"],
)
@pytest.mark.parametrize("seed_option", ["default", "last"])
def test_ordering_seeds_match_controller_and_stock_worker_values(
    tmp_path: Path, route: str, seed_option: str
) -> None:
    project = _project(tmp_path / "vfy")
    environment = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": None,
        "PYTHONHASHSEED": "0",
        "VFY_WORKER_START": "exec",
        "VFY_WORKER_COLLECTION": "0",
    }
    arguments = (
        [
            "--engine=pytest-native",
            "--numprocesses=0" if route.endswith("serial") else "--numprocesses=2",
        ]
        if route.startswith("native-xdist")
        else [f"--engine={route}", "--workers=2"]
    )
    if route in {"native-xdist-canonical", "native-xdist-serial"}:
        arguments.extend(("-p", "xdist"))
    reorder = ["--random-order"] if route == "pytest-native" else []
    if seed_option == "last":
        engine = "native" if route == "native" else "pytest-native"
        project.run_vfy(
            "test",
            f"--engine={engine}",
            "--no-cov",
            "--collect-only",
            "--randomly-seed=137",
            "-p",
            "no:pytest-testmon",
            "-q",
            env=environment,
        ).expect(0)
        (project.root / "configs" / "controller.json").unlink()
    completed = project.run_vfy(
        "test",
        *arguments,
        *reorder,
        "--no-cov",
        f"--randomly-seed={seed_option}",
        "-p",
        "no:pytest-testmon",
        "-q",
        env=environment,
    )
    completed.expect(0)
    configurations = {
        path.stem: json.loads(path.read_text())
        for path in (project.root / "configs").glob("*.json")
    }
    assert len(configurations) == (1 if route.endswith("serial") else 3), configurations
    seed = configurations["controller"]["seed"]
    assert isinstance(seed, int)
    if seed_option == "last":
        assert seed == 137
    assert {configuration["seed"] for configuration in configurations.values()} == {seed}
    order_seed = configurations["controller"]["order_seed"]
    assert order_seed.startswith("default:")
    assert {configuration["order_seed"] for configuration in configurations.values()} == {
        order_seed
    }
    assert all(
        configuration["order_enabled"] is bool(reorder) for configuration in configurations.values()
    )
    if route != "pytest-native":
        assert all(
            configuration["worker_seed"] == seed
            for name, configuration in configurations.items()
            if name != "controller"
        ), configurations
        assert all(
            configuration["worker_order_seed"] == order_seed
            for name, configuration in configurations.items()
            if name != "controller"
        ), configurations

    stock = _project(tmp_path / "stock")
    reference = stock.run_python(
        "-m",
        "pytest",
        "-p",
        "xdist",
        "-p",
        "randomly",
        "-p",
        "random_order.plugin",
        "--numprocesses=0" if route.endswith("serial") else "--numprocesses=2",
        f"--randomly-seed={seed}",
        f"--random-order-seed={order_seed}",
        *reorder,
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONHASHSEED": "0"},
    )
    reference.expect(0)
    for name in ("first", "second"):
        actual = (project.root / f"{name}.txt").read_text()
        assert actual == (stock.root / f"{name}.txt").read_text()
