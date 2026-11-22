"""Apply configured path aliases before reports like Coverage.py."""

from __future__ import annotations

import os
import pickle
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.parametrize("relative_files", [False, True])
@pytest.mark.parametrize("configured_paths", [False, True])
@pytest.mark.parametrize("branch", [False, True], ids=["lines", "arcs"])
@pytest.mark.parametrize("morfs", ["measured", "aliases", "repeated"])
def test_report_preparation_merges_alias_evidence_and_preserves_lookup_lifecycle(
    tmp_path: Path, relative_files: bool, configured_paths: bool, branch: bool, morfs: str
) -> None:
    _compare_report_preparation(tmp_path, relative_files, configured_paths, branch, morfs)


@pytest.mark.parametrize("first_report", ["text", "xml", "html", "lcov", "annotate"])
def test_all_render_apis_prepare_configured_aliases(tmp_path: Path, first_report: str) -> None:
    _compare_report_preparation(tmp_path, True, True, True, "measured", first_report)


@pytest.mark.parametrize("relative_files", [False, True])
@pytest.mark.parametrize("loaded", [False, True], ids=["injected", "loaded"])
@pytest.mark.parametrize("morfs", ["measured", "repeated"])
def test_configured_aliases_preserve_measured_source_rows_without_rediscovery(
    tmp_path: Path, relative_files: bool, loaded: bool, morfs: str
) -> None:
    _compare_report_preparation(
        tmp_path, relative_files, True, False, morfs, source_root=True, loaded=loaded
    )


def _compare_report_preparation(
    tmp_path: Path,
    relative_files: bool,
    configured_paths: bool,
    branch: bool,
    morfs: str,
    first_report: str = "json",
    *,
    source_root: bool = False,
    loaded: bool = False,
) -> None:
    project = VfyProject(tmp_path)
    project.write("src/sample.py", "if flag:\n    a = 1\nelse:\n    a = 2\nb = 3\n")
    if source_root:
        project.write("src/untouched.py", "unused = 1\n")
        project.write("src/empty.py", "")
    project.write(
        ".coveragerc",
        f"[run]\nrelative_files = {str(relative_files).lower()}\n"
        + ("source = src\n" if source_root else "")
        + ("[paths]\nsource =\n    src\n    remote/src\n" if configured_paths else ""),
    )
    project.write(
        "compare.py",
        """import hashlib
import io
import json
from pathlib import Path

import coverage
import vfy

source = str(Path('src/sample.py').resolve())
branch = BRANCH
morfs = MORFS
first_report = FIRST_REPORT
observations = {}
for name, cls in [('reference', coverage.Coverage), ('vfy', vfy.Coverage)]:
    cov = cls(data_file=f'.{name}', branch=branch, config_file='.coveragerc')
    data = cov.get_data()
    data.set_context('absolute')
    if branch:
        data.add_arcs({source: [(-1, 1), (1, 2), (2, 5), (5, -1)]})
    else:
        data.add_lines({source: [1, 2]})
    data.set_context('relative')
    if branch:
        data.add_arcs({'src/sample.py': [(-1, 1), (1, 4), (4, 5), (5, -1)]})
    else:
        data.add_lines({'src/sample.py': [4, 5]})
    if SOURCE_ROOT:
        data.touch_files([filename if RELATIVE_FILES else str(Path(filename).resolve())
                          for filename in ['src/untouched.py', 'src/empty.py']])
    cov.save()
    if LOADED:
        data.close(force=True)
        cov = cls(data_file=f'.{name}', branch=branch, config_file='.coveragerc')
        cov.load()
        data = cov.get_data()
    disk = Path(data.data_filename())
    digest = hashlib.sha256(disk.read_bytes()).hexdigest()
    def snapshot():
        current = cov.get_data()
        def arcs(file):
            values = current.arcs(file)
            return None if values is None else sorted(values)
        return {
            'analysis': cov.analysis(source),
            'analysis2': cov.analysis2(source),
            'branch_stats': cov.branch_stats(source),
            'files': sorted(current.measured_files()),
            'records': {file: {'lines': current.lines(file), 'arcs': arcs(file),
                              'contexts': {line: sorted(contexts) for line, contexts
                                           in current.contexts_by_lineno(file).items()}}
                        for file in sorted(current.measured_files())},
        }
    before = snapshot()
    try:
        after_first = None
        if first_report != 'json':
            if first_report == 'text':
                cov.report(morfs=morfs, file=io.StringIO())
            elif first_report == 'html':
                cov.html_report(morfs=morfs, directory=f'{name}-html')
            elif first_report == 'annotate':
                cov.annotate(morfs=morfs, directory=f'{name}-annotate')
            else:
                getattr(cov, f'{first_report}_report')(
                    morfs=morfs, outfile=f'{name}.{first_report}'
                )
            after_first = snapshot()
        percent = cov.json_report(morfs=morfs, show_contexts=True, outfile=f'{name}.json')
        report = json.loads(Path(f'{name}.json').read_text())
        def sort_contexts(record):
            if 'contexts' in record:
                record['contexts'] = {line: sorted(values)
                                      for line, values in record['contexts'].items()}
            for regions in ['functions', 'classes']:
                for region in record.get(regions, {}).values():
                    sort_contexts(region)
        for record in report['files'].values():
            sort_contexts(record)
        after = snapshot()
        if CONFIGURED_PATHS:
            cov.save()
        observations[name] = {'before': before, 'after': after,
                              'report': {key: report[key] for key in ['files', 'totals']},
                              'after_first': after_first,
                              'percent': percent, 'replaced_data': cov.get_data() is not data,
                              'original_files': sorted(data.measured_files()),
                              'disk_unchanged': digest ==
                                  hashlib.sha256(disk.read_bytes()).hexdigest(),
                              'original_data_filename':
                                  cov.get_data().data_filename() == str(disk)}
    finally:
        data.close(force=True)
        cov.get_data().close(force=True)
assert observations['vfy'] == observations['reference'], observations
print('aliases match')
""".replace("BRANCH", repr(branch))
        .replace(
            "MORFS",
            {
                "measured": "None",
                "aliases": "[source, 'src/sample.py']",
                "repeated": "[source, source]",
            }[morfs],
        )
        .replace("FIRST_REPORT", repr(first_report))
        .replace("CONFIGURED_PATHS", repr(configured_paths))
        .replace("SOURCE_ROOT", repr(source_root))
        .replace("RELATIVE_FILES", repr(relative_files))
        .replace("LOADED", repr(loaded)),
    )
    result = project.run_python("compare.py")
    result.expect(0)
    assert result.stdout.strip() == "aliases match"


@pytest.mark.parametrize("stopped", [False, True], ids=["live", "stopped"])
@pytest.mark.parametrize(
    "operation", ["get-data", "save", "restart", "load", "erase", "load-start", "erase-start"]
)
def test_report_preparation_preserves_collector_evidence_after_replacement(
    tmp_path: Path, stopped: bool, operation: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "src/sample.py",
        "def choose(flag):\n    if flag:\n        yield 1\n    yield 0\n",
    )
    project.write(
        ".coveragerc",
        "[run]\nrelative_files = true\n[paths]\nsource =\n    src\n    remote/src\n",
    )
    project.write(
        "compare.py",
        """import contextlib
import hashlib
import json
from pathlib import Path
import pickle
import runpy
import sys

import coverage
import vfy

source = str(Path('src/sample.py').resolve())
def observe(name):
    cls = coverage.Coverage if name == 'reference' else vfy.Coverage
    use_pytracer = OPERATION == 'load-start' and not STOPPED
    reference_options = {'timid': True} if name == 'reference' and use_pytracer else {}
    cov = cls(data_file=f'.{name}', branch=True, config_file='.coveragerc', source=['src'],
              **reference_options)
    data = cov.get_data()
    data.set_context('injected')
    data.add_arcs({source: [(-1, 1), (1, -1)]})
    cov.save()
    digest = hashlib.sha256(Path(data.data_filename()).read_bytes()).hexdigest()
    cov.start()
    choose = runpy.run_path(source)['choose']
    pending = choose(True)
    assert next(pending) == 1
    if STOPPED:
        cov.stop()
    before_stop_records = None
    try:
        with cov._report_batch() if name == 'vfy' else contextlib.nullcontext():
            cov.json_report(outfile=f'{name}-first.json', show_contexts=True)
            after_first = cov.analysis2(source), cov.branch_stats(source)
            operation = OPERATION
            if operation == 'restart':
                cov.stop()
                cov.start()
            elif operation == 'get-data':
                cov.get_data()
            elif operation.endswith('-start'):
                getattr(cov, operation.removesuffix('-start'))()
                cov.start()
            else:
                getattr(cov, operation)()
            if not STOPPED or operation == 'restart' or operation.endswith('-start'):
                assert next(pending) == 0
                list(choose(False))
                if operation.endswith('-start') and not use_pytracer:
                    # Stopping a nested reference start misattributes resumed tracer lines.
                    before_stop_records = {file: sorted(data.lines(file) or [])
                                           for file in sorted(data.measured_files())}
                cov.stop()
            second_error = None
            try:
                cov.json_report(outfile=f'{name}-second.json', show_contexts=True)
            except Exception as error:
                second_error = type(error).__name__
        data_after = cov.get_data()
        def report(suffix):
            raw = json.loads(Path(f'{name}-{suffix}.json').read_text())
            def sort_contexts(record):
                if 'contexts' in record:
                    record['contexts'] = {line: sorted(values)
                                          for line, values in record['contexts'].items()}
                for regions in ['functions', 'classes']:
                    for region in record.get(regions, {}).values():
                        sort_contexts(region)
            for record in raw['files'].values():
                sort_contexts(record)
            return {key: raw[key] for key in ['files', 'totals']}
        observation = {
            'first': report('first'),
            'second': second_error if second_error else report('second'),
            'after_first': after_first,
            'after_second': (cov.analysis2(source), cov.branch_stats(source)),
            'files': sorted(data_after.measured_files()),
            'original_records': before_stop_records if before_stop_records is not None else
                                {file: sorted(data.lines(file) or [])
                                 for file in sorted(data.measured_files())},
            'disk_unchanged': digest ==
                hashlib.sha256(Path(data.data_filename()).read_bytes()).hexdigest(),
        }
    finally:
        cov.stop()
        data.close(force=True)
        cov.get_data().close(force=True)
    return observation

print(pickle.dumps(observe(sys.argv[1])).hex())
""".replace("STOPPED", repr(stopped)).replace("OPERATION", repr(operation)),
    )
    # Reference reset can retain its monitoring tool until the process exits.
    observations = {}
    for role in ["reference", "vfy"]:
        result = project.run_python("compare.py", role)
        result.expect(0)
        observations[role] = pickle.loads(bytes.fromhex(result.stdout.strip()))
    assert observations["vfy"] == observations["reference"], observations


@pytest.mark.parametrize("order", ["relative-first", "absolute-first", "dotted"])
@pytest.mark.parametrize("round_trip", [False, True])
def test_implicit_relative_roots_follow_original_file_creation_order(
    tmp_path: Path, order: str, round_trip: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("src/sample.py", "a = 1\nb = 2\n")
    project.write(
        ".coveragerc",
        "[run]\nrelative_files = true\n[paths]\nsource =\n    src\n    remote/src\n",
    )
    project.write(
        "compare.py",
        """import json
from pathlib import Path

import coverage
import vfy

keys = KEYS
observations = {}
for name, cls in [('reference', coverage.Coverage), ('vfy', vfy.Coverage)]:
    cov = cls(config_file='.coveragerc', data_file=f'.{name}')
    cov.get_data().set_context('first')
    cov.get_data().add_lines({keys[0]: [1]})
    incoming = cls(config_file=False, data_file=f'.{name}-incoming')
    incoming_data = incoming.get_data()
    incoming_data.set_context('second')
    incoming_data.add_lines({keys[1]: [2]})
    incoming.save()
    cov.get_data().update(incoming_data)
    incoming_data.close(force=True)
    if ROUND_TRIP:
        cov.save()
        cov.load()
    for file in cov.get_data().measured_files():
        cov.get_data().lines(file)
    try:
        cov.json_report(morfs=['src/sample.py'], show_contexts=True, outfile=f'{name}.json')
        raw = json.loads(Path(f'{name}.json').read_text())
        observations[name] = {
            'files': sorted(cov.get_data().measured_files()),
            'analysis': cov.analysis2('src/sample.py'),
            'report': {key: raw[key] for key in ['files', 'totals']},
        }
    finally:
        cov.get_data().close(force=True)
assert observations['reference'] == observations['vfy'], observations
print('file order matches')
""".replace(
            "KEYS",
            repr(
                {
                    "relative-first": ["src/sample.py", "C:/worker/src/sample.py"],
                    "absolute-first": ["C:/worker/src/sample.py", "src/sample.py"],
                    "dotted": ["./src/sample.py", "src/sample.py"],
                }[order]
            ),
        ).replace("ROUND_TRIP", repr(round_trip)),
    )
    result = project.run_python("compare.py")
    result.expect(0)
    assert result.stdout.strip() == "file order matches"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_reported_data_and_collector_destination_are_separate_after_fork(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "src/sample.py", "def choose(flag):\n    if flag:\n        yield 1\n    yield 0\n"
    )
    project.write(
        ".coveragerc",
        "[run]\nrelative_files = true\n[paths]\nsource =\n    src\n    remote/src\n",
    )
    project.write(
        "compare.py",
        """import json
import os
from pathlib import Path
import runpy

import coverage
import vfy

source = str(Path('src/sample.py').resolve())
observations = {}
for name, cls in [('reference', coverage.Coverage), ('vfy', vfy.Coverage)]:
    cov = cls(data_file=None, config_file='.coveragerc', branch=True, source=['src'])
    cov.get_data().add_arcs({source: [(-1, 1), (1, -1)]})
    cov.start()
    choose = runpy.run_path(source)['choose']
    pending = choose(True)
    next(pending)
    cov.json_report(morfs=[source], outfile=f'{name}-parent.json')
    before = cov.analysis2(source), cov.branch_stats(source)
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(reader)
            list(choose(False))
            cov.stop()
            current = cov.get_data()
            result = {'files': sorted(current.measured_files()),
                      'lines': current.lines('src/sample.py'),
                      'analysis': cov.analysis2(source), 'branches': cov.branch_stats(source)}
            cov.json_report(morfs=[source], outfile=f'{name}-child.json')
            report = json.loads(Path(f'{name}-child.json').read_text())
            result['report'] = {key: report[key] for key in ['files', 'totals']}
            os.write(writer, json.dumps(result).encode())
        finally:
            os._exit(0)
    os.close(writer)
    with os.fdopen(reader) as pipe:
        child = json.loads(pipe.read())
    _, status = os.waitpid(pid, 0)
    assert status == 0
    observations[name] = {'child': child,
                          'parent_unchanged': before ==
                              (cov.analysis2(source), cov.branch_stats(source))}
    cov.stop()
    cov.get_data().close(force=True)
assert observations['reference'] == observations['vfy'], observations
print('fork aliases match')
""",
    )
    result = project.run_python("compare.py")
    result.expect(0)
    assert result.stdout.strip() == "fork aliases match"


def test_reporting_discovers_unexecuted_source_packages_in_replaced_data(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "VALUE = 1\n")
    project.write("pkg/unused.py", "OTHER = 2\n")
    project.write(
        ".coveragerc",
        "[run]\nrelative_files = true\n[paths]\nsource =\n    pkg\n    remote/pkg\n",
    )
    project.write(
        "compare.py",
        """import importlib
import json
from pathlib import Path
import sys

import coverage
import vfy

source = str(Path('pkg/__init__.py').resolve())
observations = {}
for name, cls in [('reference', coverage.Coverage), ('vfy', vfy.Coverage)]:
    sys.modules.pop('pkg', None)
    cov = cls(data_file=None, config_file='.coveragerc', source_pkgs=['pkg'])
    cov.get_data().add_lines({source: [1]})
    cov.start()
    importlib.import_module('pkg')
    cov.stop()
    try:
        cov.json_report(outfile=f'{name}.json')
        report = json.loads(Path(f'{name}.json').read_text())
        observations[name] = {'files': sorted(cov.get_data().measured_files()),
                              'report': {key: report[key] for key in ['files', 'totals']}}
    finally:
        cov.get_data().close(force=True)
assert observations['reference'] == observations['vfy'], observations
print('source package aliases match')
""",
    )
    result = project.run_python("compare.py")
    result.expect(0)
    assert result.stdout.strip() == "source package aliases match"
