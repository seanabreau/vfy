import tomllib
from pathlib import Path

from tests.support import VfyProject


def test_python_rust_and_distribution_metadata_agree(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    workspace = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
    cargo = tomllib.loads((root / "crates" / "vfy" / "Cargo.toml").read_text(encoding="utf-8"))

    result = VfyProject(tmp_path).run_vfy("--version")
    result.expect(0)
    assert result.stdout == f"vfy {workspace['workspace']['package']['version']}\n"
    assert project["project"]["dynamic"] == ["version"]
    assert cargo["package"]["version"] == {"workspace": True}
