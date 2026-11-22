"""Pinned project workloads shared by the ecosystem and edit-loop benchmarks.

The manifest is the only place where a real-project benchmark names an
upstream revision, its test plug-ins, worker policy, and reviewed source
patches.  Preparation records the complete resolved environment beside a
result; benchmark timers never install packages or clone repositories.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PatchKind = Literal["local", "shared", "breaking", "repair"]


@dataclass(frozen=True)
class EditPatch:
    """A reviewed, byte-for-byte source edit used by an edit sequence."""

    name: str
    path: str
    before: str
    after: str
    kind: PatchKind
    tests: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class ProjectManifest:
    """Reproducible commands and inputs for one external project."""

    key: str
    name: str
    repository: str
    revision: str
    package_path: str
    tests: tuple[str, ...]
    coverage_args: tuple[str, ...]
    reports: tuple[str, ...]
    parallel_args: tuple[str, ...]
    serial_args: tuple[str, ...]
    plugins: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    requirements: tuple[str, ...]
    dependency_lock: str
    workers: int | str
    serial_workers: int
    phases: tuple[str, ...] = ("parallel",)
    scope: str = "full project suite"
    url: str | None = None
    patches: tuple[EditPatch, ...] = ()
    generated_files: tuple[str, ...] = ()

    @property
    def parallel_phase(self) -> tuple[str, ...]:
        """Arguments for the recorded parallel pytest phase."""

        return self.parallel_args

    @property
    def serial_phase(self) -> tuple[str, ...]:
        """Arguments for the independent serial pytest phase."""

        return self.serial_args


COMMON_REQUIREMENTS = (
    "pytest==9.1.1",
    "pytest-cov==7.1.0",
    "pytest-xdist==3.8.0",
    "coverage==7.16.2",
)


PROJECTS: dict[str, ProjectManifest] = {
    "click": ProjectManifest(
        key="click",
        name="Click",
        repository="pallets/click",
        revision="8b19813f2bfca99f1018a587a8cf54fc959f2e5d",
        package_path="src/click/",
        tests=("tests",),
        coverage_args=("--cov=click",),
        reports=("term", "json:coverage.json"),
        parallel_args=(),
        serial_args=(),
        plugins=(),
        environment=(),
        requirements=COMMON_REQUIREMENTS,
        dependency_lock="benchmarks/locks/click.txt",
        workers=1,
        serial_workers=0,
        scope="full configured Click suite; upstream addopts exclude stress-marker tests",
        url="https://github.com/pallets/click",
    ),
    "packaging": ProjectManifest(
        key="packaging",
        name="Packaging",
        repository="pypa/packaging",
        revision="7b898d9f0b343ca06993157fc328d7caad51d5c2",
        package_path="src/packaging/",
        tests=("tests",),
        coverage_args=("--cov=packaging",),
        reports=("term", "json:coverage.json"),
        parallel_args=("--hypothesis-seed=0",),
        serial_args=("--hypothesis-seed=0",),
        plugins=("hypothesis.extra.pytestplugin",),
        environment=(),
        requirements=COMMON_REQUIREMENTS,
        dependency_lock="benchmarks/locks/packaging.txt",
        workers=1,
        serial_workers=0,
        scope="full configured Packaging suite; upstream default excludes opt-in property tests",
        url="https://github.com/pypa/packaging",
    ),
    "pluggy": ProjectManifest(
        key="pluggy",
        name="Pluggy",
        repository="pytest-dev/pluggy",
        revision="6abdcf6ca167f5d61297b5d86e22f3dd058031b3",
        package_path="src/pluggy/",
        tests=("testing",),
        coverage_args=("--cov=pluggy",),
        reports=("term", "json:coverage.json"),
        parallel_args=(),
        serial_args=(),
        plugins=(),
        environment=(),
        requirements=(*COMMON_REQUIREMENTS, "pytest-benchmark"),
        dependency_lock="benchmarks/locks/pluggy.txt",
        workers=1,
        serial_workers=0,
        scope="full Pluggy test suite",
        url="https://github.com/pytest-dev/pluggy",
    ),
    "attrs": ProjectManifest(
        key="attrs",
        name="attrs",
        repository="python-attrs/attrs",
        revision="8f767776326faaed11e6c2974798787f6e19b343",
        package_path="src/",
        tests=("tests",),
        coverage_args=("--cov=attr", "--cov=attrs"),
        reports=("term", "json:coverage.json"),
        parallel_args=(),
        serial_args=(),
        plugins=(),
        environment=(),
        requirements=(*COMMON_REQUIREMENTS, "editables"),
        dependency_lock="benchmarks/locks/attrs.txt",
        workers=1,
        serial_workers=0,
        scope="full attrs test suite",
        url="https://github.com/python-attrs/attrs",
    ),
    "platformdirs": ProjectManifest(
        key="platformdirs",
        name="Platformdirs",
        repository="tox-dev/platformdirs",
        revision="ea7be87d58a05c5e0ac27b6e56aee6a5583a951e",
        package_path="src/platformdirs/",
        tests=("tests",),
        coverage_args=("--cov=platformdirs",),
        reports=("term", "json:coverage.json"),
        parallel_args=(),
        serial_args=(),
        plugins=("pytest_mock", "platformdirs.pytest_plugin"),
        environment=(),
        requirements=(*COMMON_REQUIREMENTS, "editables"),
        dependency_lock="benchmarks/locks/platformdirs.txt",
        workers=1,
        serial_workers=0,
        scope="full Platformdirs test suite",
        url="https://github.com/tox-dev/platformdirs",
        generated_files=("src/platformdirs/version.py",),
    ),
    "black": ProjectManifest(
        key="black",
        name="Black",
        repository="psf/black",
        revision="8947c48ef2077c3a301b03c1e814dc2e3f78436e",
        package_path="src/black/",
        tests=("tests",),
        coverage_args=("--cov=src/black",),
        reports=("term",),
        parallel_args=("--run-optional", "no_jupyter"),
        serial_args=("--run-optional", "no_jupyter"),
        plugins=("xdist.plugin",),
        environment=(("PYTHONPATH", "./src"),),
        requirements=(
            *COMMON_REQUIREMENTS,
            "hatch-fancy-pypi-readme",
            "hatch-vcs>=0.3.0",
            "hatchling>=1.27.0",
        ),
        dependency_lock="benchmarks/locks/black.txt",
        workers=4,
        serial_workers=0,
        scope="official no_jupyter tox test phase",
        url="https://github.com/psf/black",
    ),
    "rich": ProjectManifest(
        key="rich",
        name="Rich",
        repository="Textualize/rich",
        revision="9d8f9a372cc5916fd4781fec207ced7ddac2f08f",
        package_path="rich/",
        tests=("tests",),
        coverage_args=("--cov=./rich",),
        reports=("xml:coverage.xml", "term-missing"),
        parallel_args=(),
        serial_args=(),
        plugins=(),
        environment=(("TERM", "xterm-256color"),),
        requirements=(
            *COMMON_REQUIREMENTS,
            "attrs==26.1.0",
            "pygments==2.19.2",
            "markdown-it-py==3.0.0",
            "poetry-core>=1.0.0",
            "pytest-testmon==2.2.0",
        ),
        dependency_lock="benchmarks/locks/rich.txt",
        workers=1,
        serial_workers=0,
        scope="full Rich test suite",
        url="https://github.com/Textualize/rich",
        patches=(
            EditPatch(
                "rich-local-loop-edit",
                "rich/_loop.py",
                "yield True, value",
                "yield True, (value)",
                "local",
                ("tests/test_tools.py",),
                "Preserves loop_first output while changing one local expression.",
            ),
            EditPatch(
                "rich-shared-loop-edit",
                "rich/_loop.py",
                "yield False, previous_value",
                "yield False, (previous_value)",
                "shared",
                ("tests/test_tools.py",),
                "Preserves loop_last output while invalidating a shared utility.",
            ),
            EditPatch(
                "rich-breaking-loop-edit",
                "rich/_loop.py",
                "yield first, False, previous_value",
                "yield False, False, previous_value",
                "breaking",
                ("tests/test_tools.py",),
                "Deliberately changes the first-item flag and must produce a test failure.",
            ),
            EditPatch(
                "rich-repair-loop-edit",
                "rich/_loop.py",
                "yield False, False, previous_value",
                "yield first, False, previous_value",
                "repair",
                ("tests/test_tools.py",),
                "Restores the reviewed loop contract after the breaking edit.",
            ),
        ),
    ),
    "fastapi": ProjectManifest(
        key="fastapi",
        name="FastAPI",
        repository="fastapi/fastapi",
        revision="c3f316b7e814667e8ee81e03a7330d00ee61e45c",
        package_path="fastapi/",
        tests=("tests", "scripts/tests"),
        coverage_args=("--cov",),
        reports=(),
        parallel_args=("--dist", "loadgroup"),
        serial_args=("-n", "0"),
        plugins=("inline_snapshot", "anyio.pytest_plugin", "pytest_timeout", "xdist.plugin"),
        environment=(("CONTEXT", "benchmark"), ("PYTHONPATH", "./docs_src")),
        requirements=(
            *COMMON_REQUIREMENTS,
            "inline-snapshot==0.31.1",
            "pytest-timeout==2.4.0",
            "pdm-backend",
        ),
        dependency_lock="benchmarks/locks/fastapi.txt",
        workers=4,
        serial_workers=0,
        scope="full FastAPI test suite",
        url="https://github.com/fastapi/fastapi",
        generated_files=("log.txt",),
    ),
    "dotenv": ProjectManifest(
        key="dotenv",
        name="python-dotenv",
        repository="theskumar/python-dotenv",
        revision="a00cb2eed0704cd6d2071b2004c37e95ccc86ee5",
        package_path="src/dotenv/",
        tests=("tests",),
        coverage_args=("--cov=dotenv",),
        reports=("term", "json:coverage.json"),
        parallel_args=(),
        serial_args=(),
        plugins=(),
        environment=(),
        requirements=(*COMMON_REQUIREMENTS, "pytest-testmon==2.2.0"),
        dependency_lock="benchmarks/locks/dotenv-selection.txt",
        workers=1,
        serial_workers=0,
        scope="full python-dotenv test suite",
        url="https://github.com/theskumar/python-dotenv",
    ),
    "sortedcontainers": ProjectManifest(
        key="sortedcontainers",
        name="Sorted Containers",
        repository="grantjenks/python-sortedcontainers",
        revision="3ac358631f58c1347f1d6d2d92784117db0f38ed",
        package_path="src/sortedcontainers/",
        tests=(),
        coverage_args=("--cov=sortedcontainers",),
        reports=("term", "json:coverage.json"),
        parallel_args=(),
        serial_args=(),
        plugins=(),
        environment=(),
        requirements=(*COMMON_REQUIREMENTS, "pytest-testmon==2.2.0"),
        dependency_lock="benchmarks/locks/sortedcontainers-selection.txt",
        workers=1,
        serial_workers=0,
        scope="configured Sorted Containers suite, including module and documentation doctests",
        url="https://github.com/grantjenks/python-sortedcontainers",
    ),
    "colorama": ProjectManifest(
        key="colorama",
        name="Colorama",
        repository="tartley/colorama",
        revision="841634ed2a0da5d5ac2d867db533da8131266cb2",
        package_path="colorama/",
        tests=("colorama/tests",),
        coverage_args=("--cov=colorama",),
        reports=("term", "json:coverage.json"),
        parallel_args=(),
        serial_args=(),
        plugins=(),
        environment=(),
        requirements=(*COMMON_REQUIREMENTS, "pytest-testmon==2.2.0", "hatchling", "editables"),
        dependency_lock="benchmarks/locks/colorama-selection.txt",
        workers=1,
        serial_workers=0,
        scope="pytest-discoverable unittest cases under colorama/tests",
        url="https://github.com/tartley/colorama",
    ),
    "requests": ProjectManifest(
        key="requests",
        name="Requests",
        repository="psf/requests",
        revision="8f8b212de8c2129d7954c6cd373762880375620a",
        package_path="src/requests/",
        tests=("tests",),
        coverage_args=("--cov=src/requests", "--cov-config=.coveragerc"),
        reports=("term", "xml"),
        parallel_args=(),
        serial_args=(),
        plugins=("pytest_httpbin.plugin", "pytest_mock"),
        environment=(),
        requirements=(
            *COMMON_REQUIREMENTS,
            "pytest-httpbin==2.1.0",
            "pytest-mock==3.15.1",
            "setuptools>=61.0",
        ),
        dependency_lock="benchmarks/locks/requests.txt",
        workers=1,
        serial_workers=0,
        scope="full Requests test suite",
        url="https://github.com/psf/requests",
    ),
    "datasette": ProjectManifest(
        key="datasette",
        name="Datasette",
        repository="simonw/datasette",
        revision="7403ae68bb0e1c39f2ff1927953d2775b932b9d3",
        package_path="datasette/",
        tests=("tests",),
        coverage_args=("--cov=datasette", "--cov-context=test"),
        reports=("term-missing",),
        parallel_args=("-m", "not serial", "--dist", "loadfile"),
        serial_args=("-m", "serial"),
        plugins=(
            "datasette._pytest_plugin",
            "pytest_asyncio.plugin",
            "pytest_timeout",
            "xdist.plugin",
        ),
        # Datasette's nested pytest subprocesses need its auto-close plug-in
        # while top-level benchmark commands load the remaining plug-ins
        # explicitly.  Keeping this inherited contract separate prevents
        # xdist/observer plug-ins from leaking into nested sessions.
        environment=(("PYTEST_PLUGINS", "datasette._pytest_plugin"),),
        requirements=(
            *COMMON_REQUIREMENTS,
            "pytest-asyncio==1.4.0",
            "pytest-timeout==2.4.0",
            "beautifulsoup4==4.14.3",
            "trustme==1.2.1",
            "setuptools",
            "poetry-core>=1.0.0",
        ),
        dependency_lock="benchmarks/locks/datasette.txt",
        workers=4,
        serial_workers=0,
        phases=("parallel", "serial"),
        scope="full Datasette test suite",
        url="https://github.com/simonw/datasette",
        patches=(
            EditPatch(
                "datasette-local-escape-edit",
                "datasette/utils/__init__.py",
                "return '\"{}\"'.format(s.replace('\"', '\"\"'))",
                "return '\"{}\"'.format(s.replace(chr(34), chr(34) * 2))",
                "local",
                ("tests/test_utils.py::test_escape_sqlite",),
                "Equivalent quote escaping in the utility under test.",
            ),
            EditPatch(
                "datasette-shared-escape-edit",
                "datasette/utils/__init__.py",
                "if _boring_keyword_re.match(s) and (s.lower() not in reserved_words):",
                "if _boring_keyword_re.match(s) and (s.casefold() not in reserved_words):",
                "shared",
                ("tests/test_utils.py",),
                "Equivalent case normalization for the shared SQLite identifier utility.",
            ),
            EditPatch(
                "datasette-breaking-escape-edit",
                "datasette/utils/__init__.py",
                "return s\n    return '\"{}\"'.format(s.replace('\"', '\"\"'))",
                "return s + '_broken'\n    return '\"{}\"'.format(s.replace('\"', '\"\"'))",
                "breaking",
                ("tests/test_utils.py::test_escape_sqlite",),
                "Deliberately corrupts boring identifiers and must fail a test call.",
            ),
            EditPatch(
                "datasette-repair-escape-edit",
                "datasette/utils/__init__.py",
                "return s + '_broken'\n    return '\"{}\"'.format(s.replace('\"', '\"\"'))",
                "return s\n    return '\"{}\"'.format(s.replace('\"', '\"\"'))",
                "repair",
                ("tests/test_utils.py::test_escape_sqlite",),
                "Restores the SQLite identifier utility after the breaking edit.",
            ),
        ),
    ),
}


def manifest(key: str) -> ProjectManifest:
    """Return a named manifest; raise ValueError for an unknown project."""

    try:
        return PROJECTS[key]
    except KeyError as error:
        choices = ", ".join(sorted(PROJECTS))
        raise ValueError(f"unknown benchmark project {key!r}; choose from {choices}") from error


def project_revisions() -> dict[str, str]:
    """Return the pinned upstream revisions for workflow matrix generation."""

    return {key: project.revision for key, project in PROJECTS.items()}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", choices=sorted(PROJECTS))
    parser.add_argument("--field", choices=("repository", "revision"), required=True)
    args = parser.parse_args()
    print(json.dumps(getattr(manifest(args.project), args.field)))
