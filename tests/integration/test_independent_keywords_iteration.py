"""Exercise live keyword iteration through the real collection protocol."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_keyword_iterator_observes_mutations_and_foreign_parent(tmp_path: Path) -> None:
    results = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_keywords.py",
            """\
import json
from pathlib import Path


def test_keywords(request):
    NodeKeywords = type(request.node.keywords)
    events = []
    class Node:
        def __init__(self, name, parent=None):
            self.name = name
            self.parent = parent
            self.keywords = NodeKeywords(self)
    class Foreign:
        def __iter__(self):
            events.append('foreign start')
            yield 'shared'
            yield 'foreign'
    root = Node('root')
    middle = Node('middle', root)
    leaf = Node('leaf', middle)
    leaf.keywords._markers['shared'] = True
    root.keywords._markers['shared'] = True
    it = iter(leaf.keywords)
    values = [next(it)]
    middle.keywords._markers['late'] = True
    values.extend(it)
    events.append(values)
    middle.keywords.parent = type('Parent', (), {'keywords': Foreign()})()
    events.append(list(leaf.keywords))
    class Retained:
        def __iter__(self):
            self.it = iter(root.keywords)
            return self.it
    retained = Retained()
    middle.keywords.parent = type('Parent', (), {'keywords': retained})()
    it = iter(leaf.keywords)
    events.append([next(it), next(it), next(it), next(it), next(it)])
    events.append(next(retained.it, 'done'))
    events.append(list(it))
    Path('keywords.json').write_text(json.dumps(events))
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        results[engine] = json.loads((project.root / "keywords.json").read_text())
    assert results["native"] == results["pytest"]


@requires_native_pytest
def test_keyword_iterator_exception_reentrancy_and_collection(tmp_path: Path) -> None:
    results = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_keyword_protocol.py",
            """\
import gc
import json
import weakref
from pathlib import Path


def test_keyword_protocol(request):
    NodeKeywords = type(request.node.keywords)
    events = []
    class Node:
        def __init__(self):
            self.name = 'node'
            self.parent = None
            self.keywords = NodeKeywords(self)
    class Stop:
        def __iter__(self):
            raise StopIteration('marker')
    node = Node()
    node.keywords._markers = Stop()
    it = iter(node.keywords)
    try:
        next(it)
    except RuntimeError as error:
        events.append([
            str(error), type(error.__cause__).__name__, str(error.__cause__),
            type(error.__context__).__name__, str(error.__context__),
            error.__suppress_context__,
        ])
    events.append(next(it, 'closed'))

    class Reenter:
        def __iter__(self):
            try:
                next(it)
            except ValueError as error:
                events.append(str(error))
            return iter(['resumed'])
    node.keywords._markers = Reenter()
    it = iter(node.keywords)
    events.append(list(it))

    node = Node()
    it = iter(node.keywords)
    node.iterator = it
    next(it)
    reference = weakref.ref(node)
    del it, node
    gc.collect()
    events.append(reference() is None)
    Path('protocol.json').write_text(json.dumps(events))
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        results[engine] = json.loads((project.root / "protocol.json").read_text())
    assert results["native"] == results["pytest"]
    assert results["pytest"] == [
        [
            "generator raised StopIteration",
            "StopIteration",
            "marker",
            "StopIteration",
            "marker",
            True,
        ],
        "closed",
        "generator already executing",
        ["resumed"],
        True,
    ]


@requires_native_pytest
@pytest.mark.parametrize("hash_seed", [0, 1, 9, 17, 42])
def test_keyword_iterator_observes_parent_key_hash_and_reentry(
    tmp_path: Path, hash_seed: int
) -> None:
    results = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_keyword_hash.py",
            """\
import json
from pathlib import Path


def test_keyword_hash(request):
    NodeKeywords = type(request.node.keywords)
    events = []
    class Node:
        def __init__(self):
            self.name = 'local'
            self.parent = None
            self.keywords = NodeKeywords(self)
    class Key:
        def __hash__(self):
            events.append('hash')
            try:
                next(iterator)
            except ValueError as error:
                events.append(str(error))
            return hash('remote')
        def __eq__(self, other):
            events.append('eq')
            return False
    class Foreign:
        def __iter__(self):
            events.append('iter parent')
            yield Key()
    node = Node()
    node.keywords._markers['remote'] = True
    node.keywords.parent = type('Parent', (), {'keywords': Foreign()})()
    iterator = iter(node.keywords)
    events.append(next(iterator))
    events.append(next(iterator))
    events.append(type(next(iterator)).__name__)
    events.append(next(iterator, 'closed'))
    Path('hash.json').write_text(json.dumps(events))
""",
        )
        env = {"PYTHONHASHSEED": str(hash_seed)}
        result = (
            project.run_pytest("-q", load_xdist=False, env=env)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        results[engine] = json.loads((project.root / "hash.json").read_text())
    assert results["native"] == results["pytest"]
    assert results["pytest"][:5] == [
        "local",
        "remote",
        "iter parent",
        "hash",
        "generator already executing",
    ]
    equalities = results["pytest"][5:-2]
    assert equalities
    assert set(equalities) == {"eq"}
    assert results["pytest"][-2:] == ["Key", "closed"]


@requires_native_pytest
def test_keyword_iterator_deep_and_cyclic_parents_raise_recursion_error(tmp_path: Path) -> None:
    results = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine, timeout=20)
        project.write(
            "test_keyword_recursion.py",
            """\
import json
import sys
from pathlib import Path


def test_keyword_recursion(request):
    NodeKeywords = type(request.node.keywords)
    class Node:
        def __init__(self, name, parent=None):
            self.name = name
            self.parent = parent
            self.keywords = NodeKeywords(self)

    cycle = Node('cycle')
    cycle.keywords.parent = cycle
    deep = Node('root')
    for index in range(400):
        deep = Node(str(index), deep)
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(300)
    outcomes = []
    try:
        for node in (cycle, deep):
            iterator = iter(node.keywords)
            try:
                list(iterator)
            except RecursionError:
                outcomes.append(['RecursionError', next(iterator, 'closed')])
            else:
                outcomes.append(['completed'])
    finally:
        sys.setrecursionlimit(previous)
    Path('recursion.json').write_text(json.dumps(outcomes))
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        results[engine] = json.loads((project.root / "recursion.json").read_text())
    assert (
        results["native"]
        == results["pytest"]
        == [
            ["RecursionError", "closed"],
            ["RecursionError", "closed"],
        ]
    )
