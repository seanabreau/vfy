"""JUnit attribute namespaces follow pytest's ElementTree serialization."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("mode", ["default", "xml", "malformed"])
def test_attribute_namespace_edges_match_pytest(tmp_path: Path, mode: str) -> None:
    source = """import os
import xml.etree.ElementTree as ET

def test_edge(record_xml_attribute):
    mode = os.environ["NAMESPACE_EDGE"]
    if mode == "default":
        ET.register_namespace("", "urn:edge")
        record_xml_attribute("{urn:edge}custom", "value")
        record_xml_attribute("{urn:other}other", "value2")
    elif mode == "xml":
        record_xml_attribute("{http://www.w3.org/XML/1998/namespace}lang", "en")
        record_xml_attribute("{urn:other}other", "value2")
    else:
        record_xml_attribute("{broken", "value")
"""
    results = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\njunit_family=xunit1\n")
        project.write("test_edge.py", source)
        project.write("result.xml", "old report")
        args = ("-q", "--junitxml=result.xml")
        env = {"NAMESPACE_EDGE": mode}
        result = (
            project.run_python("-m", "pytest", *args, env=env)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", *args, env=env)
        )
        results.append((project, result))

    if mode == "malformed":
        assert [result.returncode for _, result in results] == [1, 1]
        for _, result in results:
            assert "ValueError: not enough values to unpack (expected 2, got 1)" in (
                result.stdout + result.stderr
            )
        assert (results[1][0].root / "result.xml").read_text() == "old report"
        return

    documents = []
    for project, result in results:
        assert result.returncode == 0, result.stdout + result.stderr
        document = (project.root / "result.xml").read_text()
        documents.append(document)
        root = ET.fromstring(document)
        suite = next(iter(root))
        case = next(iter(suite))
        assert case.attrib["name"] == "test_edge"
        assert case.attrib["{urn:other}other"] == "value2"
        if mode == "default":
            assert root.tag == "{urn:edge}testsuites"
            assert case.attrib["custom"] == "value"
        else:
            assert case.attrib["{http://www.w3.org/XML/1998/namespace}lang"] == "en"
    declaration = (
        'xmlns="urn:edge" xmlns:ns1="urn:other"' if mode == "default" else 'xmlns:ns0="urn:other"'
    )
    assert all(declaration in document for document in documents)
    if mode == "xml":
        assert all('xml:lang="en"' in document for document in documents)
