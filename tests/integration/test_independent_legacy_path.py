"""The ``tmpdir`` fixture's legacy path methods used by real packages match stock pytest."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_TEST = """\
import os


def test_legacy_path_methods(tmpdir):
    root = tmpdir.ensure_dir("root")
    note = root.join("note.txt")
    note.write("first\\nsecond\\n")
    nested = root.ensure("a", "b", "c.txt")
    results = {}

    previous = root.chdir()
    results["chdir_cwd"] = os.getcwd() == str(root)
    previous.chdir()
    with root.as_cwd() as before:
        results["as_cwd_inside"] = os.getcwd() == str(root)
        results["as_cwd_before"] = before is not None
    results["as_cwd_restored"] = os.getcwd() == str(before)

    results["size"] = note.size()
    note.setmtime(1234567.0)
    results["mtime"] = note.mtime()
    results["atime_float"] = isinstance(note.atime(), float)
    results["readlines"] = note.readlines()
    results["readlines_nocr"] = note.readlines(cr=False)
    results["relto"] = [nested.relto(root), root.relto(nested), nested.relto(str(root) + os.sep)]
    results["parts"] = [str(part) for part in root.parts()][-2:]
    results["parts_reverse"] = [str(part) for part in root.parts(reverse=True)][:1] == [str(root)]
    results["samefile"] = [note.samefile(str(note)), note.samefile(nested)]
    link = root.join("link")
    os.symlink(str(note), str(link))
    results["islink"] = [link.islink(), note.islink()]
    results["realpath"] = str(link.realpath()) == str(note.realpath())

    note.rename(root.join("renamed.txt"))
    results["renamed"] = sorted(path.basename for path in root.listdir())
    root.join("renamed.txt").remove()
    results["removed_file"] = root.join("renamed.txt").check()
    root.join("a").remove()
    results["removed_tree"] = root.join("a").check()
    empty = root.ensure_dir("empty")
    empty.remove(rec=0)
    results["removed_dir"] = empty.check()
    with open(str(root / "results.txt"), "w") as stream:
        stream.write(repr(sorted(results.items())))
"""


def test_legacy_path_methods_match_stock(tmp_path: Path) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_paths.py", _TEST)
        project.write("conftest.py", "")
        result = project.run_engine(
            engine, "-q", "-p", "no:cacheprovider", "--basetemp=base", "-W", "ignore"
        )
        result.expect(0)
        report = project.root / "base" / "test_legacy_path_methods0" / "root" / "results.txt"
        observed.append(report.read_text().replace(str(project.root), "<root>"))
    assert observed[1] == observed[0]
