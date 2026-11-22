"""Real subreport dictionary cloning remains viable before XML publication."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CLONE_SOURCE = r"""from pathlib import Path
import json,pytest
saved={}
def event(label):
 with Path('events.jsonl').open('a') as stream:stream.write(json.dumps(label)+'\n')
class Value:
 def __str__(self):
  event('value.str')
  return 'actual-value'
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item,call):
 report=yield
 if report.when=='call':
  original=report._to_json
  def observed():
   event('clone:before-logreport')
   data=original()
   data['clone_extra']='actual-extra'
   return data
  report._to_json=observed
 return report
@pytest.hookimpl(wrapper=True)
def pytest_runtest_logreport(report):
 yield
 if isinstance(report,pytest.SubtestReport):
  event('child:after-logreport')
  del report._to_json
  original=report._to_json
  raw=report.user_properties[0][1]
  def observed():
   event('clone:attached-before-publication')
   if FAIL:raise ValueError('actual clone protocol failure')
   data=original()
   data['clone_extra']='after-attachment'
   return data
  report._to_json=observed
  try:
   copied=pytest.SubtestReport._new(report,report.context,None,None)
  except ValueError as error:
   assert FAIL and str(error)=='actual clone protocol failure'
   event('clone:actual-error')
  else:
   assert not FAIL
   assert copied is not report
   assert copied.user_properties[0][1] is raw
   assert copied.clone_extra=='after-attachment'
   assert copied.sections==report.sections
   assert copied.longrepr==report.longrepr
   saved['copied']=copied
   event('clone:complete')
@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session):event('sessionfinish')
"""
_CLONE_TEST = """from conftest import Value
def test_case(subtests,record_property,record_xml_attribute):
 record_property('property',Value())
 record_xml_attribute('custom','original')
 with subtests.test('actual child'):
  print('actual captured child')
"""


@pytest.mark.parametrize("fail", [False, True])
def test_subreport_clone_keeps_junit_attributes_and_properties(tmp_path: Path, fail: bool) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", f"FAIL={fail!r}\n" + _CLONE_SOURCE)
    project.write("test_case.py", _CLONE_TEST)
    for engine in ["pytest", "vfy.independent.entry"]:
        result = project.run_python(
            "-m", engine, "-q", "--junitxml=results.xml", "-o", "junit_family=legacy"
        )
        result.expect(0)
        case = ET.parse(tmp_path / "results.xml").find(".//testcase")
        assert case is not None
        assert case.attrib["custom"] == "original"
        prop = case.find("./properties/property")
        assert prop is not None
        assert prop.attrib == {"name": "property", "value": "actual-value"}
