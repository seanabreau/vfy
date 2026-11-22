"""Skipping wrappers retain generator cleanup and sequential thread semantics."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_abandoned_skipping_wrapper_preserves_finalization_and_thread_resume(
    tmp_path: Path,
) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_wrapper.py",
            """\
import gc
import json
import sys
import threading
from pathlib import Path

def test_wrapper(request):
    item = request.node
    impl = next(impl for impl in request.config.hook.pytest_runtest_call.get_hookimpls()
                if impl.plugin_name == 'skipping')
    original_markers = item.iter_markers
    original_unraisable = sys.unraisablehook
    observed = []
    def fail_markers(*args, **kwargs):
        raise ValueError('marker lookup during finalization')
    def observe(event):
        if str(event.exc_value) == 'marker lookup during finalization':
            observed.append([event.object is None, id(event.object) == iterator_id,
                             type(event.exc_value).__name__, str(event.exc_value)])
    iterator = impl.function(item)
    assert next(iterator) is None
    iterator_id = id(iterator)
    item.iter_markers = fail_markers
    sys.unraisablehook = observe
    try:
        del iterator
        gc.collect()
    finally:
        item.iter_markers = original_markers
        sys.unraisablehook = original_unraisable
    assert len(observed) == 1
    assert observed[0][:2] == (
        [True, False] if sys.version_info >= (3, 14) else [False, True]
    )

    iterator = impl.function(item)
    assert next(iterator) is None
    resumed = []
    def resume():
        try:
            iterator.send('thread value')
        except StopIteration as error:
            resumed.append(['stopped', error.value])
        except BaseException as error:
            resumed.append([type(error).__name__, str(error)])
    thread = threading.Thread(target=resume)
    thread.start()
    thread.join()
    assert resumed == [['stopped', 'thread value']]
    Path('observations.json').write_text(json.dumps({
        'owner_is_none': observed[0][0],
        'owner_is_iterator': observed[0][1],
        'error': observed[0][2:],
        'cross_thread': resumed,
    }))
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                "-q",
                env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
        observations[engine] = json.loads((project.root / "observations.json").read_text())
    assert (
        observations["native"]
        == observations["pytest"]
        == {
            "owner_is_none": sys.version_info >= (3, 14),
            "owner_is_iterator": sys.version_info < (3, 14),
            "error": ["ValueError", "marker lookup during finalization"],
            "cross_thread": [["stopped", "thread value"]],
        }
    )
