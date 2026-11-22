"""Cyclic IO streams flush in the context that closes or collects them."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.test_collector_generator_close import _snapshot
from tests.support import VfyProject

_SUBJECT = """from io import TextIOBase
events = []
class Stream(TextIOBase):
    def __init__(self):
        self.cycle = self
    def flush(self):
        events.append('flushed')
"""

_DRIVER = """import gc, importlib, sys, weakref
provider, operation = sys.argv[1:]
cov = importlib.import_module(provider).Coverage(
    data_file=provider + '.coverage', source=['subject'], branch=True, config_file=False)
cov.set_option('run:core', 'ctrace')
gc.disable()
cov.start()
try:
    if provider == 'coverage':
        assert type(sys.gettrace()).__name__ == 'CTracer'
    import subject
    cov.switch_context('create')
    value = subject.Stream()
    reference = weakref.ref(value)
    cov.switch_context('release')
    if operation == 'close':
        value.close()
    del value
    assert reference() is not None
    cov.switch_context('collect')
    gc.collect()
    assert reference() is None
    assert subject.events == ['flushed']
finally:
    cov.stop()
    gc.enable()
cov.save()
cov.get_data().close(force=True)
"""


@pytest.mark.parametrize("operation", ["close", "collect"])
def test_stream_finalizer_preserves_explicit_collection_context(
    tmp_path: Path, operation: str
) -> None:
    project = VfyProject(tmp_path)
    source = project.write("subject.py", _SUBJECT)
    project.write("driver.py", _DRIVER)
    observations = {}
    for provider in ("coverage", "vfy.api"):
        result = project.run_python("driver.py", provider, operation)
        result.expect(0)
        observations[provider] = _snapshot(source, project.root / f"{provider}.coverage")
    expected_context = "release" if operation == "close" else "collect"
    assert observations["coverage"]["contexts_by_lineno"][7] == {expected_context}
    assert observations["vfy.api"] == observations["coverage"]
