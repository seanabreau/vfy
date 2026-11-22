"""Built-in providers publish their ordinary fixture declarations."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_builtin_providers_expose_resources(tmp_path: Path) -> None:
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "pytest.ini",
            "[pytest]\njunit_family = legacy\n"
            "filterwarnings = ignore::pytest.PytestExperimentalApiWarning\n",
        )
        project.write(
            "test_builtin.py",
            """import logging
import os
import warnings

def test_resources(pytestconfig, tmp_path_factory, tmp_path, tmpdir_factory, tmpdir,
                   monkeypatch, recwarn, caplog, capsys, cache, doctest_namespace, subtests,
                   record_property, record_xml_attribute, record_testsuite_property):
    assert pytestconfig.rootpath.is_dir()
    assert tmp_path_factory.getbasetemp().is_dir()
    assert tmpdir_factory.getbasetemp().isdir()
    assert tmpdir.isdir()
    (tmp_path / 'file').write_text('content')
    assert (tmp_path / 'file').read_text() == 'content'
    monkeypatch.setenv('VFY_FIXTURE_PROBE', 'active')
    assert os.environ['VFY_FIXTURE_PROBE'] == 'active'
    warnings.warn('fixture warning', UserWarning)
    assert str(recwarn.pop(UserWarning).message) == 'fixture warning'
    caplog.set_level(logging.WARNING)
    logging.warning('fixture log')
    assert 'fixture log' in caplog.text
    print('fixture output')
    assert capsys.readouterr().out == 'fixture output\\n'
    cache.set('vfy/fixture', 'value')
    assert cache.get('vfy/fixture', None) == 'value'
    doctest_namespace['fixture'] = 1
    record_property('fixture', 'property')
    record_xml_attribute('fixture', 'attribute')
    record_testsuite_property('fixture', 'suite')
    with subtests.test(msg='fixture'):
        assert doctest_namespace['fixture'] == 1

def test_fd_capture(capfd):
    os.write(1, b'file descriptor output')
    assert capfd.readouterr().out == 'file descriptor output'

def test_sys_binary(capsysbinary):
    import sys
    sys.stdout.buffer.write(b'sys binary')
    assert capsysbinary.readouterr().out == b'sys binary'

def test_fd_binary(capfdbinary):
    os.write(1, b'fd binary')
    assert capfdbinary.readouterr().out == b'fd binary'

def test_tee(capteesys):
    print('tee output')
    assert capteesys.readouterr().out == 'tee output\\n'
""",
        )
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_pytest("-q", "--junitxml=result.xml", load_xdist=False, env=env)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=native", "--no-cov", "-q", "--junitxml=result.xml", env=env
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "5 passed" in result.stdout
        report = ET.parse(project.root / "result.xml")
        case = next(
            case for case in report.iter("testcase") if case.get("name") == "test_resources"
        )
        assert case.get("fixture") == "attribute"
        assert [
            (item.get("name"), item.get("value")) for item in case.findall("properties/property")
        ] == [("fixture", "property")]
        assert [
            (item.get("name"), item.get("value"))
            for item in report.findall("testsuite/properties/property")
        ] == [("fixture", "suite")]
