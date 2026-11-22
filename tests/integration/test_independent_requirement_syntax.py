"""Required-plugin syntax follows Packaging without importing it in the runner."""

from __future__ import annotations

import json
import os
from pathlib import Path

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()
_NAME = "vfy-demo-plugin"


def _requirements() -> list[str]:
    requirements = [
        "",
        "not a requirement",
        "Vfy-demo-plugin",
        "vfy_demo_plugin",
        "vfy.demo.plugin",
        "-vfy-demo-plugin",
        "vfy-demo-plugin-",
        "vfy-demo-pluginé",
        "vfy-demo-plugin²",
    ]
    requirements.extend(
        prefix + _NAME + suffix
        for prefix, suffix in [
            ("", ""),
            (" ", " "),
            ("\t", "\t"),
            ("\n", ""),
            ("", "\n"),
            ("\u00a0", ""),
            ("\x1c", ""),
        ]
    )
    requirements.extend(
        _NAME + extras + ">=1.4"
        for extras in [
            "[]",
            "[a]",
            "[a,b]",
            " [ a , b ] ",
            "[a.b,c_d,e-f]",
            "[a,]",
            "[,a]",
            "[a b]",
            "[a][b]",
            "[é]",
            "[a-]",
            "[-a]",
            "[a,,b]",
            "[a",
        ]
    )
    requirements.extend(
        _NAME + version
        for version in [
            ">=1.4",
            ">=1.4,<2",
            " ( >=1.4, <2 )",
            ">=1.4,",
            "(>=1.4,)",
            "==1.5rc1",
            "!=1.5rc1",
            "===1.5rc1",
            "===opaque",
            "===",
            "===1.5rc1,>=1.4",
            "===1.5rc1,,",
            "===1.5rc1,===opaque",
            "===1.5rc1, >=1.4",
            ">=\x1c1.4",
            "===\x1c1.5rc1",
            ">=2",
            "==1.*",
            "~=1.4",
            "=>1",
            ">==1",
            ">=garbage",
            "()",
            "( )",
            "(>=1.4",
            ">=1.4)",
            ",>=1.4",
            "==1.5.*",
            "==1.5rc1.*",
            ">=1.0+local",
            ">=1.4 <2",
        ]
    )
    requirements.extend(
        _NAME + url
        for url in [
            " @ arbitrary-url",
            "@arbitrary-url",
            " @ ../relative;opaque",
            " @ //",
            " @ scheme:opaque#fragment?query",
            " @ arbitrary-url ; python_version < '0'",
            " @ arbitrary-url;python_version<'0'",
            " @ arbitrary-url trailing",
            " @ ",
            " @ x\n",
            " @ x ;",
            " @ x ; os_name == 'a'",
            " @ x\t; os_name == 'a'",
        ]
    )
    variables = [
        "python_version",
        "python_full_version",
        "os_name",
        "os.name",
        "sys_platform",
        "sys.platform",
        "platform_release",
        "platform_system",
        "platform_version",
        "platform.version",
        "platform_machine",
        "platform.machine",
        "platform_python_implementation",
        "platform.python_implementation",
        "python_implementation",
        "implementation_name",
        "implementation_version",
        "extra",
        "extras",
        "dependency_groups",
        "python_version_extra",
        "PYTHON_VERSION",
        "unknown_name",
        "platform_system_extra",
    ]
    markers = [f"{variable} == 'never'" for variable in variables]
    markers.extend(f"'never' == {variable}" for variable in variables)
    markers.extend(
        f"python_version {operator} '0'"
        for operator in [
            "<",
            "<=",
            "!=",
            "==",
            ">=",
            ">",
            "~=",
            "===",
            "in",
            "not in",
            "not\tin",
            "not  in",
            "notin",
            "IN",
            "is",
            "=",
            "<>",
            "=>",
        ]
    )
    markers.extend(
        "extra == " + quoted
        for quoted in [
            "''",
            '""',
            "'a b'",
            '"a\\tb"',
            "'a\\nb'",
            "'a\\x41b'",
            "'a\\u0041b'",
            "'a\\u123'",
            "'a\\U00110000'",
            "'\\N{LATIN CAPITAL LETTER A}'",
            "'\\N{NOT A CHARACTER}'",
            "'a\\xZZb'",
            "'a\\'b'",
            "'unterminated",
            "'line\nbreak'",
            "'東京'",
        ]
    )
    markers.extend(
        [
            "'a' == 'b'",
            "python_version == os_name",
            "(os_name == 'a')",
            "((os_name == 'a'))",
            "os_name == 'a' or sys_platform == 'b' and extra == 'c'",
            "(os_name == 'a' or sys_platform == 'b') and extra == 'c'",
            "os_name == 'a'and sys_platform == 'b'",
            "os_name=='a' or(sys_platform=='b')",
            "os_name == 'a' AND sys_platform == 'b'",
            "os_name == 'a' xor sys_platform == 'b'",
            "not os_name == 'a'",
            "os_name == 'a' or",
            "or os_name == 'a'",
            "()",
            "(os_name == 'a'",
            "os_name == 'a')",
            "os_name == 'a'; extra == 'b'",
            "python_version == 3",
            "python_version",
            "",
        ]
    )
    requirements.extend(_NAME + "; " + marker for marker in markers)
    return list(dict.fromkeys(requirements))


def test_required_plugin_syntax_matches_packaging_through_public_bootstrap(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    corpus = _requirements()
    project.write("requirements.json", json.dumps(corpus))
    project.write(
        "vfy_demo_plugin.py", "from pathlib import Path\nPath('plugin_imported').touch()\n"
    )
    project.write(
        "vfy_demo_plugin-1.5rc1.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: vfy-demo-plugin\nVersion: 1.5rc1\n",
    )
    project.write(
        "vfy_demo_plugin-1.5rc1.dist-info/entry_points.txt",
        "[pytest11]\nvfy_demo_plugin = vfy_demo_plugin\n",
    )
    project.write(
        "oracle.py",
        """import importlib.metadata
import json
from pathlib import Path
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import Version

distribution = importlib.metadata.distribution('vfy-demo-plugin')
assert [(ep.group, ep.name, ep.value) for ep in distribution.entry_points] == [
    ('pytest11', 'vfy_demo_plugin', 'vfy_demo_plugin')]
records = []
for source in json.loads(Path('requirements.json').read_text()):
    try:
        requirement = Requirement(source)
    except InvalidRequirement:
        valid, matches = False, False
    else:
        valid = True
        matches = requirement.name == distribution.metadata['Name'] and (
            requirement.specifier.contains(Version(distribution.version), prereleases=True))
    records.append({'source': source, 'valid': valid, 'matches': matches})
print(json.dumps(records))
""",
    )
    oracle = project.run_python("oracle.py")
    oracle.expect(0)
    records = json.loads(oracle.stdout)
    accepted = [record["source"] for record in records if record["matches"]]
    rejected = [record["source"] for record in records if not record["matches"]]
    assert any(not record["valid"] for record in records)
    assert any(record["valid"] and not record["matches"] for record in records)
    assert _NAME + " @ arbitrary-url;python_version<'0'" in accepted
    assert _NAME + "[a]>=1.4" in accepted
    assert _NAME + "; python_version < '0'" in accepted
    assert _NAME + "===opaque" in rejected
    assert "Vfy-demo-plugin" in rejected
    project.write(
        "sitecustomize.py",
        """import json
import os
import sys
from pathlib import Path
if os.environ.get('VFY_BLOCK_PACKAGING'):
    attempts = []
    Path('packaging_attempts.json').write_text(json.dumps(attempts))
    class NoPackaging:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'packaging' or fullname.startswith('packaging.'):
                attempts.append(fullname)
                Path('packaging_attempts.json').write_text(json.dumps(attempts))
                raise RuntimeError('independent requirement validation imported Packaging')
    sys.meta_path.insert(0, NoPackaging())
""",
    )
    project.write(
        "test_required.py",
        "from pathlib import Path\nPath('test_imported').touch()\n"
        "def test_body(): Path('body').touch()\n",
    )
    for requirements, status in [(accepted, 0), (rejected, 4)]:
        project.write("pytest.toml", f"[pytest]\nrequired_plugins = {json.dumps(requirements)}\n")
        candidate = project.run_python(
            "-m",
            "vfy.independent.entry",
            "-p",
            "vfy_demo_plugin",
            env={
                "PYTHONPATH": os.pathsep.join([str(tmp_path), _SOURCE]),
                "VFY_BLOCK_PACKAGING": "1",
            },
        )
        assert json.loads((tmp_path / "packaging_attempts.json").read_text()) == []
        assert candidate.returncode == status, (candidate.stdout, candidate.stderr)
        assert (tmp_path / "plugin_imported").exists()
        assert (tmp_path / "test_imported").exists() is (status == 0)
        assert (tmp_path / "body").exists() is (status == 0)
        if status == 4:
            assert candidate.stderr == (
                "vfy independent usage error: Missing required plugins: "
                + ", ".join(sorted(rejected))
                + "\n"
            )
        for filename in ["plugin_imported", "test_imported", "body"]:
            (tmp_path / filename).unlink(missing_ok=True)
