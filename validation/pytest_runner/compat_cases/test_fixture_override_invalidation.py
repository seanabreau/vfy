# ruff: noqa: ANN001, ANN201, I001
"""A cached parent follows the fixture visible to each requesting test."""

from __future__ import annotations

import pytest


events: list[str] = []


@pytest.fixture(scope="session", params=[1, 2])
def flavor(request):
    return f"root {request.param}"


@pytest.fixture(scope="session")
def parent(flavor):
    events.append(f"setup {flavor}")
    yield flavor
    events.append(f"teardown {flavor}")


class TestOverride:
    @pytest.fixture(scope="session")
    def flavor(self):
        return "class"

    def test_uses_class_override(self, parent):
        assert parent == "class"


def test_root_parameter_does_not_invalidate_unrelated_cached_parent(parent, flavor):
    assert flavor in {"root 1", "root 2"}
    assert parent == "class"
    assert events == ["setup class"]
