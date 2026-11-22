"""Collect one package fixture and write public coverage observations."""

from __future__ import annotations

import importlib
import json
import sys
import unittest
from pathlib import Path

from coverage import CoverageData

engine, scenario, root_arg = sys.argv[1:]
root = Path(root_arg).resolve()
Coverage = importlib.import_module(engine).Coverage
config = root / "pyproject.toml" if (root / "pyproject.toml").exists() else root / "coverage.ini"
cov = Coverage(config_file=str(config), data_file=str(root / f"{engine}.coverage"))
if scenario == "django":
    import django
    from django.conf import settings

    settings.configure(
        INSTALLED_APPS=[],
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [str(root / "templates")],
                "OPTIONS": {"debug": True},
            }
        ],
    )
    django.setup()
    from django.template import engines

    cov.start()
    rendered = engines["django"].get_template("page.html").render({"name": "Ada", "show": False})
    cov.stop()
    assert rendered == "Hello Ada\n\nHidden\n"
else:
    source = root / "app.py"
    namespace = {"__name__": "plugin_fixture", "__file__": str(source)}
    cov.start()
    exec(compile(source.read_text(), str(source), "exec"), namespace)
    if scenario == "conditional":
        assert namespace["answer"](False) == 2
        if (root / "omitted.py").exists():
            exec(compile((root / "omitted.py").read_text(), str(root / "omitted.py"), "exec"), {})
    else:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(namespace["Example"])
        result = unittest.TestResult()
        suite.run(result)
        assert result.testsRun == 1
        assert not result.errors, result.errors
        assert not result.failures, result.failures
    cov.stop()
cov.save()
# Reporting also invokes file reporters and executable-file discovery.
cov.json_report(outfile=str(root / f"{engine}-report.json"))
cov.save()
# Read both schema-7 files through the independent Coverage.py data API.
data = CoverageData(basename=str(root / f"{engine}.coverage"))
data.read()
records = {}
for filename in sorted(data.measured_files()):
    path = Path(filename)
    assert path.is_relative_to(root), path
    analysis = cov.analysis2(filename)
    records[str(path.relative_to(root))] = {
        "lines": sorted(data.lines(filename) or []),
        "tracer": data.file_tracer(filename),
        "contexts": {
            str(k): sorted(v) for k, v in sorted(data.contexts_by_lineno(filename).items())
        },
        "statements": analysis[1],
        "excluded": analysis[2],
        "missing": analysis[3],
    }
result = {
    "files": records,
    "contexts": sorted(data.measured_contexts()),
    "totals": json.loads((root / f"{engine}-report.json").read_text())["totals"],
}
(root / f"{engine}-canonical.json").write_text(json.dumps(result, indent=2) + "\n")
sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
