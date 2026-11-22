from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject


def test_line_collection_releases_excluded_generated_code_and_keeps_measured_lines(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "subject.py",
        "def measured(value):\n    if value:\n        return value\n    return 0\n",
    )
    project.write(
        "collect.py",
        "import gc\n"
        "import json\n"
        "import pathlib\n"
        "import weakref\n"
        "from vfy import Coverage\n"
        "subject = pathlib.Path('subject.py').resolve()\n"
        "source = subject.read_text()\n"
        "cov = Coverage(source=['.'], data_file=None, config_file=False)\n"
        "cov.start()\n"
        "references = []\n"
        "for index in range(200):\n"
        "    cov.switch_context(f'test-{index}')\n"
        "    generated = {}\n"
        "    exec(compile('def build():\\n    return 1\\n', f'<generated {index}>', 'exec'),"
        " generated)\n"
        "    assert generated['build']() == 1\n"
        "    references.append(weakref.ref(generated['build'].__code__))\n"
        "    del generated\n"
        "    gc.collect()\n"
        "    # Fresh measured code objects can occupy freed generated-code addresses.\n"
        "    namespace = {}\n"
        "    exec(compile(source, str(subject), 'exec'), namespace)\n"
        "    assert namespace['measured'](index % 2) == index % 2\n"
        "gc.collect()\n"
        "alive = sum(reference() is not None for reference in references)\n"
        "data = cov.get_data()\n"
        "cov.stop()\n"
        "contexts = data.contexts_by_lineno(str(subject))\n"
        "print(json.dumps({\n"
        "    'alive': alive,\n"
        "    'lines': sorted(data.lines(str(subject)) or []),\n"
        "    'contexts': {str(line): len(values) for line, values in sorted(contexts.items())},\n"
        "}))\n",
    )

    result = project.run_python("collect.py")

    result.expect(0)
    report = json.loads(result.stdout)
    assert report["alive"] == 0
    assert report["lines"] == [1, 2, 3, 4]
    assert report["contexts"] == {"1": 200, "2": 200, "3": 100, "4": 100}
