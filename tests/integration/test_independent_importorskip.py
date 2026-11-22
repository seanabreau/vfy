"""Optional imports preserve pytest 9.1 error, warning, and skip behavior."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path

_SOURCE = isolated_vfy_path()


def test_optional_import_errors_and_warning_state_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("broken_import.py", "raise ImportError('installed dependency is broken')\n")
    project.write("missing_dependency.py", "import vfy_missing_nested_dependency\n")
    project.write(
        "warning_import.py",
        "import warnings\nwarnings.warn('import warning', UserWarning)\n",
    )
    project.write(
        "exercise.py",
        """import importlib
import json
import sys
import warnings

api = importlib.import_module(sys.argv[1])
observed = {}
cases = [
    ('present', 'math', {}),
    ('missing', 'vfy_missing_optional_dependency', {}),
    ('empty_reason', 'vfy_missing_optional_dependency', {'reason': ''}),
    ('reason', 'vfy_missing_optional_dependency', {'reason': 'optional feature'}),
    ('broken', 'broken_import', {}),
    ('broken_explicit', 'broken_import', {'exc_type': ImportError}),
    ('missing_nested', 'missing_dependency', {}),
    ('warnings', 'warning_import', {}),
    ('invalid_syntax', 'invalid syntax!', {}),
    ('expression', '1 + 2', {}),
]
for label, name, arguments in cases:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('error')
        before = list(warnings.filters)
        try:
            module = api.importorskip(name, **arguments)
        except BaseException as error:
            observed[label] = {
                'error': type(error).__name__,
                'message': str(error),
                'allow_module_level': getattr(error, 'allow_module_level', None),
                'context': type(error.__context__).__name__ if error.__context__ else None,
                'cause': type(error.__cause__).__name__ if error.__cause__ else None,
            }
        else:
            observed[label] = {'module': module.__name__}
        observed[label]['warnings'] = [str(warning.message) for warning in caught]
        observed[label]['filters_restored'] = warnings.filters == before
print(json.dumps(observed))
""",
    )
    records = []
    for api in ("pytest", "vfy.independent.facade"):
        result = project.run_python("exercise.py", api, env={"PYTHONPATH": _SOURCE})
        result.expect(0)
        records.append(json.loads(result.stdout))
    assert records[1] == records[0]
    assert records[0]["broken"]["error"] == "ImportError"
    assert records[0]["broken_explicit"]["allow_module_level"] is True
    assert records[0]["empty_reason"]["message"] == ""
    assert all(case["filters_restored"] for case in records[0].values())
