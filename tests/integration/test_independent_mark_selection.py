"""Independent mark and keyword selection match pytest's live item policies."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_EVENT_SOURCE = """import json
from pathlib import Path
import pytest

def event(request):
    with Path('events').open('a') as output:
        output.write(json.dumps(['run', request.node.nodeid]) + '\\n')
"""


def _assert_selection_matches_pytest(
    project: VfyProject,
    arguments: list[str],
    expected: list[str],
    *,
    expected_events: list[object] | None = None,
) -> None:
    events = project.root / "events"
    expected_status = 0 if expected else 5
    oracle = project.run_pytest("-q", *arguments, load_xdist=False)
    assert oracle.returncode == expected_status, oracle.stdout + oracle.stderr
    observed = (
        [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
    )
    assert observed == (
        expected_events if expected_events is not None else [["run", nodeid] for nodeid in expected]
    )
    events.unlink(missing_ok=True)

    independent = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--native-result-json=results.json",
        *arguments,
    )
    assert independent.returncode == expected_status, independent.stdout + independent.stderr
    actual = (
        [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
    )
    assert actual == observed
    record = json.loads((project.root / "results.json").read_text())
    assert [case["nodeid"] for case in record["cases"]] == expected


@requires_native_pytest
@pytest.mark.parametrize(
    ("expression", "names"),
    [
        ("Fast and not Slow", ["test_fast"]),
        ("(Fast and Slow) or fast", ["test_lower", "TestInherited::test_both"]),
        ("Module and not (Fast or fast or Faster or Slow)", ["test_neutral"]),
    ],
)
def test_mark_boolean_selection_uses_exact_case_sensitive_inherited_names(
    tmp_path: Path, expression: str, names: list[str]
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\nmarkers =\n    Module: module mark\n    Fast: exact mark\n"
        "    fast: lowercase mark\n    Faster: longer mark\n    Slow: class mark\n",
    )
    project.write(
        "test_marks.py",
        _EVENT_SOURCE
        + """
pytestmark = pytest.mark.Module

@pytest.mark.Fast
def test_fast(request): event(request)

@pytest.mark.fast
def test_lower(request): event(request)

@pytest.mark.Faster
def test_longer(request): event(request)

def test_neutral(request): event(request)

@pytest.mark.Slow
class TestInherited:
    @pytest.mark.Fast
    def test_both(self, request): event(request)
    def test_slow(self, request): event(request)
""",
    )

    _assert_selection_matches_pytest(
        project, ["-m", expression], [f"test_marks.py::{name}" for name in names]
    )


@requires_native_pytest
@pytest.mark.parametrize(
    ("expression", "names"),
    [
        ("named.PY and classtoken", ["TestClassToken::test_alpha"]),
        ("mArKtOk", ["test_mark"]),
        ("FunctionToken and not ValueToken", ["test_attribute"]),
        ("EXTRATOKEN and not parentextra", ["test_extra"]),
        ("ParentExtraToken", ["TestClassToken::test_alpha"]),
        ("ParentToken/test_named.py", []),
    ],
)
def test_keywords_match_separate_parent_marker_attribute_and_extra_names(
    tmp_path: Path, expression: str, names: list[str]
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = MarkToken: keyword source\n")
    project.write(
        "conftest.py",
        """def pytest_itemcollected(item):
    if item.name == 'test_extra':
        item.extra_keyword_matches.add('ExtraToken')
    if item.name == 'test_alpha':
        item.parent.extra_keyword_matches.add('ParentExtraToken')
""",
    )
    project.write(
        "ParentToken/test_named.py",
        _EVENT_SOURCE
        + """
class TestClassToken:
    def test_alpha(self, request): event(request)

@pytest.mark.MarkToken
def test_mark(request): event(request)

def test_attribute(request): event(request)
test_attribute.FunctionToken = False
test_attribute.note = 'ValueToken'

def test_extra(request): event(request)
def test_control(request): event(request)
""",
    )

    _assert_selection_matches_pytest(
        project,
        ["-k", expression],
        [f"ParentToken/test_named.py::{name}" for name in names],
    )


@requires_native_pytest
@pytest.mark.parametrize("priority", ["tryfirst", "default", "trylast"])
def test_builtin_selection_respects_user_modifyitems_priority(
    tmp_path: Path, priority: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = keep: selected case\n")
    decorator = (
        "@pytest.hookimpl()" if priority == "default" else f"@pytest.hookimpl({priority}=True)"
    )
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

def event(kind, items):
    with Path('events').open('a') as output:
        output.write(json.dumps([kind, [item.nodeid for item in items]]) + '\\n')

"""
        + decorator
        + """
def pytest_collection_modifyitems(items):
    event('modify', items)
    for item in items:
        if item.name == 'test_dynamic':
            item.add_marker(pytest.mark.keep)

def pytest_collection_finish(session):
    event('finish', session.items)
""",
    )
    project.write(
        "test_cases.py",
        _EVENT_SOURCE
        + """
@pytest.mark.keep
def test_initial(request): event(request)
def test_dynamic(request): event(request)
def test_drop(request): event(request)
""",
    )
    all_ids = [f"test_cases.py::{name}" for name in ("test_initial", "test_dynamic", "test_drop")]
    selected = all_ids[:1] if priority == "trylast" else all_ids[:2]

    _assert_selection_matches_pytest(
        project,
        ["-m", "keep"],
        selected,
        expected_events=[
            ["modify", selected if priority == "trylast" else all_ids],
            ["finish", selected],
            *[["run", nodeid] for nodeid in selected],
        ],
    )


@requires_native_pytest
@pytest.mark.parametrize(
    ("option", "expression", "oracle_status"),
    [
        ("-k", "tag(flag=True)", 4),
        ("-m", "tag(flag=True)", 0),
        ("-k", "\u0345", 4),
        ("-m", "\u05b0", 4),
    ],
)
def test_invalid_or_unsupported_expressions_report_usage_errors(
    tmp_path: Path, option: str, expression: str, oracle_status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = tag: parameterized marker\n")
    project.write(
        "test_cases.py",
        _EVENT_SOURCE
        + """
@pytest.mark.tag(flag=True)
def test_tag(request): event(request)
""",
    )
    oracle = project.run_pytest("-q", option, expression, load_xdist=False)
    assert oracle.returncode == oracle_status, oracle.stdout + oracle.stderr
    events = tmp_path / "events"
    assert events.exists() is (oracle_status == 0)
    events.unlink(missing_ok=True)

    independent = project.run_vfy("test", "--engine=native", "--no-cov", option, expression)
    independent.expect(4)
    if option == "-m" and "=" in expression:
        assert "marker keyword arguments" in independent.stderr
        assert "unsupported" in independent.stderr
    else:
        assert "expression" in independent.stderr.lower()
        assert option in independent.stderr or "Keyword expressions" in independent.stderr
    assert not events.exists()


@requires_native_pytest
def test_keyword_mapping_retains_construction_values_and_explicit_updates(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\nmarkers =\n    tag: initial marker\n    direct: list-only marker\n"
        "    added: added marker\n    module_tag: inherited marker\n",
    )
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

def pytest_collection_modifyitems(items):
    item, other = items
    keywords = item.keywords
    original_name = item.name
    original_parent = item.parent
    original_markers = list(item.own_markers)
    observed = [['initial', str(keywords['tag']), 'module_tag' in keywords]]
    try:
        item.name = 'renamed'
        item.function.late = True
        item.own_markers.append(pytest.mark.direct.mark)
        observed.append(['snapshot', original_name in keywords, 'renamed' in keywords,
                         'late' in keywords, 'direct' in keywords])
        added = pytest.mark.added
        item.add_marker(added)
        observed.append(['added', keywords['added'] is added])
        keywords.update({'tag': 'explicit'})
        observed.append(['updated', keywords['tag']])
        reads = []
        class Source(dict):
            def __getitem__(self, key):
                reads.append(key)
                return 'overridden'
        keywords.update(Source({'copied': 'stored'}))
        observed.append(['mapping', keywords['copied'], reads])
        original_parent.keywords['inherited'] = 'original'
        other.parent.keywords['inherited'] = 'replacement'
        item.parent = other.parent
        observed.append(['parent', keywords['inherited']])
        keys = list(keywords)
        observed.append(['iteration', original_name in keys, len(keys) == len(set(keys))])
        Path('keywords.json').write_text(json.dumps(observed))
    finally:
        item.name = original_name
        item.parent = original_parent
        item.own_markers[:] = original_markers
        del item.function.late
""",
    )
    project.write(
        "test_a.py",
        """import pytest
pytestmark = pytest.mark.module_tag

@pytest.mark.tag
def test_one():
    pass
test_one.tag = 'function-attribute'
""",
    )
    project.write("test_b.py", "def test_two(): pass\n")
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "keywords.json").read_text())
    assert expected == [
        ["initial", "function-attribute", True],
        ["snapshot", True, False, False, False],
        ["added", True],
        ["updated", "explicit"],
        ["mapping", "stored", []],
        ["parent", "original"],
        ["iteration", True, True],
    ]
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((tmp_path / "keywords.json").read_text()) == expected
