"""Dateutil workload preparation validates timezone inputs before changing source."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from validation.pytest_runner import check_native_adoption_matrix as matrix


def frozen_input(tmp_path: Path) -> tuple[Path, Path, Path, bytes]:
    root = tmp_path / "checkout"
    root.mkdir()
    metadata = {"tzversion": "2024a", "zonegroups": ["europe"]}
    (root / "zonefile_metadata.json").write_text(json.dumps(metadata))
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        contents = json.dumps(metadata).encode()
        member = tarfile.TarInfo("METADATA")
        member.size = len(contents)
        archive.addfile(member, io.BytesIO(contents))
    contents = stream.getvalue()
    python = tmp_path / "environment/bin/python"
    python.parent.mkdir(parents=True)
    frozen = python.parent.parent / "dateutil-zoneinfo-input.tar.gz"
    frozen.write_bytes(contents)
    frozen.with_suffix(".json").write_text(
        json.dumps(
            {
                "distribution": "python-dateutil",
                "version": "2.9.0.post0",
                "path": "/locked-wheel/dateutil/zoneinfo/dateutil-zoneinfo.tar.gz",
            }
        )
    )
    return root, python, tmp_path / "setup", contents


@pytest.mark.parametrize("invalid", ["digest", "metadata", "distribution"])
def test_dateutil_rejects_unverified_fixture_before_changing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    root, python, output, contents = frozen_input(tmp_path)
    digest = hashlib.sha256(contents).hexdigest()
    monkeypatch.setattr(
        matrix, "DATEUTIL_ZONEINFO_SHA256", "0" * 64 if invalid == "digest" else digest
    )
    if invalid == "metadata":
        (root / "zonefile_metadata.json").write_text('{"tzversion":"2023c"}')
    elif invalid == "distribution":
        frozen = python.parent.parent / "dateutil-zoneinfo-input.tar.gz"
        frozen.with_suffix(".json").write_text(
            '{"distribution":"python-dateutil","version":"unlocked"}'
        )
    destination = root / "src/dateutil/zoneinfo/dateutil-zoneinfo.tar.gz"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"prior fixture")

    with pytest.raises(RuntimeError, match="Dateutil timezone fixture"):
        matrix.prepare_dateutil_zoneinfo(root, python, output)

    assert destination.read_bytes() == b"prior fixture"
    assert not output.exists()


def test_dateutil_reuses_validated_locked_input_after_source_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, python, output, contents = frozen_input(tmp_path)
    monkeypatch.setattr(matrix, "DATEUTIL_ZONEINFO_SHA256", hashlib.sha256(contents).hexdigest())
    frozen = python.parent.parent / "dateutil-zoneinfo-input.tar.gz"
    installed = tmp_path / "installed-wheel.tar.gz"
    frozen.replace(installed)
    provenance_path = frozen.with_suffix(".json")
    provenance = json.loads(provenance_path.read_text())
    provenance_path.unlink()
    provenance["path"] = str(installed)
    monkeypatch.setattr(
        matrix.subprocess, "check_output", lambda *_args, **_kwargs: json.dumps(provenance)
    )

    matrix.prepare_dateutil_zoneinfo(root, python, output)
    installed.write_bytes(b"replaced distribution")
    matrix.prepare_dateutil_zoneinfo(root, python, output)

    assert (root / "src/dateutil/zoneinfo/dateutil-zoneinfo.tar.gz").read_bytes() == contents
    assert (output / "zoneinfo-input.tar.gz").read_bytes() == contents
    provenance = json.loads((output / "zoneinfo-input.json").read_text())
    assert provenance["sha256"] == hashlib.sha256(contents).hexdigest()
    assert provenance["metadata"] == json.loads((root / "zonefile_metadata.json").read_text())
    assert provenance["members"] == 1
