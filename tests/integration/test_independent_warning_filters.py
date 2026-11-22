"""Warning filter parsing retains pytest's cached category identities."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_warning_filter_category_is_cached_across_tests(tmp_path: Path) -> None:
    categories = """class First(UserWarning): pass
class Second(UserWarning): pass
Category = First
"""
    source = """import warnings
import pytest
import warning_categories as categories

pytestmark = pytest.mark.filterwarnings('ignore::warning_categories.Category')

def test_a_replace_category():
    categories.Category = categories.Second

def test_b_uses_original_category():
    warnings.warn('original', categories.First)
    with pytest.raises(categories.Second):
        warnings.warn('replacement', categories.Second)
"""
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("warning_categories.py", categories)
        project.write("test_warnings.py", source)
        result = (
            project.run_python("-m", "pytest", "-q", "-W", "error")
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", "-W", "error")
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "2 passed" in output, output


def test_missing_warning_category_is_retried_after_module_becomes_available(tmp_path: Path) -> None:
    source = """import sys
import types
import warnings
import pytest

def test_a_install_category():
    module = types.ModuleType('late_warning_category')
    module.Category = type('Category', (Warning,), {})
    sys.modules[module.__name__] = module

def test_b_filter_is_applied():
    from late_warning_category import Category
    with pytest.raises(Category, match='retry sentinel'):
        warnings.warn('retry sentinel', Category)
"""
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_warnings.py", source)
        options = ["-q", "-W", "error:retry sentinel:late_warning_category.Category"]
        result = (
            project.run_python("-m", "pytest", *options)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", *options)
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "2 passed" in output, output


def test_warmed_warning_filter_uses_replaced_filterwarnings(tmp_path: Path) -> None:
    source = """import warnings
import pytest

calls = []

@pytest.mark.filterwarnings('ignore:cache sentinel:UserWarning')
def test_a_replace_filterwarnings():
    original = warnings.filterwarnings
    def replacement(*args, **kwargs):
        if len(args) > 1 and args[1] == 'cache sentinel':
            calls.append(args)
        return original(*args, **kwargs)
    warnings.filterwarnings = replacement

@pytest.mark.filterwarnings('ignore:cache sentinel:UserWarning')
def test_b_cached_filter_calls_replacement():
    assert calls
    assert calls[-1] == ('ignore', 'cache sentinel', UserWarning, '', 0)
    warnings.warn('cache sentinel', UserWarning)
"""
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_warnings.py", source)
        result = (
            project.run_python("-m", "pytest", "-q")
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "2 passed" in output, output


def test_live_warning_filters_and_markers_apply_to_later_tests(tmp_path: Path) -> None:
    source = """import warnings
import pytest

def test_a_change_filters(request):
    request.config.getini('filterwarnings').append('error:live filter:UserWarning')
    request.session.items[2].add_marker(
        pytest.mark.filterwarnings('ignore:live filter:UserWarning')
    )

def test_b_updated_filter():
    with pytest.raises(UserWarning, match='live filter'):
        warnings.warn('live filter', UserWarning)

def test_c_updated_marker():
    warnings.warn('live filter', UserWarning)
"""
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_warnings.py", source)
        result = (
            project.run_python("-m", "pytest", "-q")
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "3 passed" in output, output
        assert "warnings summary" not in output, output
