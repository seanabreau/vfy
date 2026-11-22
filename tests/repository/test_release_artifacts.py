from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from scripts.check_release_version import check_dist
from scripts.normalize_release_sboms import normalize_artifact


def write_artifacts(directory: Path, wheel_version: str, sdist_version: str) -> None:
    wheel = directory / "vfy-0.0.1-cp312-abi3-manylinux_2_17_x86_64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "vfy-0.0.1.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: vfy\nVersion: {wheel_version}\n\n",
        )
    sdist = directory / "vfy-0.0.1.tar.gz"
    metadata = f"Metadata-Version: 2.4\nName: vfy\nVersion: {sdist_version}\n\n".encode()
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo("vfy-0.0.1/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))


def test_release_gate_checks_embedded_distribution_versions(tmp_path: Path) -> None:
    write_artifacts(tmp_path, "0.0.1", "0.0.1")
    assert check_dist(tmp_path, "0.0.1") == []

    write_artifacts(tmp_path, "0.0.2", "0.0.3")
    errors = check_dist(tmp_path, "0.0.1")
    assert len(errors) == 2
    assert "Version '0.0.1'" in errors[0]
    assert "0.0.2" in errors[0]
    assert "Version '0.0.1'" in errors[1]
    assert "0.0.3" in errors[1]


def test_release_gate_rejects_corrupt_distribution(tmp_path: Path) -> None:
    write_artifacts(tmp_path, "0.0.1", "0.0.1")
    (tmp_path / "vfy-0.0.1.tar.gz").write_bytes(b"not an archive")
    errors = check_dist(tmp_path, "0.0.1")
    assert len(errors) == 1
    assert "could not read archive" in errors[0]


def test_release_gate_rejects_ambiguous_wheel_metadata(tmp_path: Path) -> None:
    write_artifacts(tmp_path, "0.0.1", "0.0.1")
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("other-0.0.1.dist-info/METADATA", "Name: other\nVersion: 0.0.1\n\n")
    errors = check_dist(tmp_path, "0.0.1")
    assert len(errors) == 1
    assert "expected exactly one vfy-0.0.1.dist-info/METADATA" in errors[0]


@pytest.mark.parametrize("relative_path", ["../vfy-core", r"..\vfy-core"])
def test_wheel_sbom_keeps_dependency_edges_and_record_hashes(
    tmp_path: Path, relative_path: str
) -> None:
    wheel = tmp_path / "vfy.whl"
    prefix = "vfy-0.0.1.dist-info/"
    sbom = prefix + "sboms/vfy.json"
    component = "path+file:///build/private/crates/vfy-core#0.0.1"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            sbom,
            json.dumps(
                {
                    "components": [
                        {
                            "bom-ref": component,
                            "purl": f"pkg:cargo/vfy-core@0.0.1?download_url=file://{relative_path}",
                        }
                    ],
                    "dependencies": [{"ref": component}],
                }
            ),
        )
        archive.writestr(prefix + "RECORD", "")
    normalize_artifact(wheel)
    with zipfile.ZipFile(wheel) as archive:
        data = archive.read(sbom)
        graph = json.loads(data)
        assert b"/build/private" not in data
        assert graph["components"][0]["purl"] == "pkg:cargo/vfy-core@0.0.1"
        assert graph["dependencies"][0]["ref"] == graph["components"][0]["bom-ref"]
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows = list(csv.reader(io.StringIO(archive.read(prefix + "RECORD").decode())))
        assert rows == [[sbom, f"sha256={digest}", str(len(data))], [prefix + "RECORD", "", ""]]


def test_unrecognized_file_uri_preserves_original_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "sbom.json"
    original = b'{"url":"file:///private-build/artifact"}'
    artifact.write_bytes(original)
    with pytest.raises(ValueError, match="local file URI"):
        normalize_artifact(artifact)
    assert artifact.read_bytes() == original
