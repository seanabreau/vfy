from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from benchmarks import benchmark_provenance
from benchmarks.benchmark_provenance import _verify_installed_wheel, prepared_project_provenance


@pytest.mark.parametrize("retained_manifest", [False, True])
def test_prepared_provenance_rejects_a_forged_wheel(
    tmp_path: Path,
    retained_manifest: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Benchmark Test"], cwd=project, check=True)
    (project / "README").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=project, check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()

    prepared_root = tmp_path / "prepared"
    metadata = prepared_root / "artifacts" / "manifest.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("{}\n", encoding="utf-8")
    wheel = prepared_root / "vfy.whl"
    wheel.write_bytes(b"not a wheel")
    extension_hash = "b" * 64
    wheel_hash = "a" * 64
    build = {
        "source_root": str(project),
        "source_revision": revision,
        "native_profile": "release",
        "extension_sha256": extension_hash,
        "wheel_path": str(wheel),
        "wheel_sha256": wheel_hash,
    }
    build_manifest = prepared_root / (
        "original-build.json" if retained_manifest else "vfy-build.json"
    )
    build_manifest.write_text(json.dumps(build) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="prepared wheel does not match"):
        prepared_project_provenance(
            project,
            workload={"name": "test"},
            workers=1,
            cache="test",
            packages=(),
            selected={"extension_sha256": extension_hash},
            prepared={"manifest": str(metadata), "vfy_wheel": build},
            source_revision=revision,
            upstream_revision=revision,
            sampling={"mode": "publication"},
            publication=True,
            validation="test",
            build_manifest=build_manifest if retained_manifest else None,
        )

    assert hashlib.sha256(wheel.read_bytes()).hexdigest() != wheel_hash


@pytest.mark.parametrize("custom_metadata", [False, True])
@pytest.mark.parametrize("retained_manifest", [False, True])
def test_prepared_provenance_uses_verified_build_without_environment_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    custom_metadata: bool,
    retained_manifest: bool,
) -> None:
    """A clean prepared release binds provenance without shell state."""

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Benchmark Test"], cwd=project, check=True)
    (project / "README").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=project, check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()

    prepared_root = tmp_path / "prepared"
    metadata_dir = tmp_path / "custom" if custom_metadata else prepared_root / "artifacts"
    metadata = metadata_dir / "manifest.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("{}\n", encoding="utf-8")
    wheel = prepared_root / "vfy.whl"
    wheel.parent.mkdir(parents=True, exist_ok=True)
    wheel.write_bytes(b"verified release wheel")
    extension_hash = "b" * 64
    wheel_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
    build = {
        "source_root": str(project),
        "source_revision": revision,
        "native_profile": "release",
        "extension_sha256": extension_hash,
        "wheel_path": str(wheel),
        "wheel_sha256": wheel_hash,
    }
    build_manifest = prepared_root / (
        "original-build.json" if retained_manifest else "vfy-build.json"
    )
    build_manifest.write_text(json.dumps(build) + "\n", encoding="utf-8")
    original_manifest = build_manifest.read_bytes()
    if retained_manifest:
        (project / "README").write_text("later controller documentation\n", encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-qam", "controller-only followup"], cwd=project, check=True
        )
    monkeypatch.delenv("VFY_BENCHMARK_BUILD_MANIFEST", raising=False)

    provenance = prepared_project_provenance(
        project,
        workload={"name": "static-report"},
        workers=1,
        cache="prepared",
        packages=(),
        selected={"extension_sha256": extension_hash},
        prepared={
            "manifest": str(metadata),
            "vfy_wheel": build,
            "vfy_build_manifest": str(build_manifest) if custom_metadata else None,
        },
        source_revision=revision,
        upstream_revision=revision,
        sampling={"mode": "publication", "warmups": 3, "measured_runs": 15},
        publication=True,
        validation="prepared artifact verified",
        build_manifest=build_manifest if retained_manifest else None,
    )

    environment = provenance["environment"]
    assert isinstance(environment, dict)
    artifact = environment["artifact"]
    assert isinstance(artifact, dict)
    assert artifact["build_manifest"] == str(build_manifest)
    assert build_manifest.read_bytes() == original_manifest
    engine_source = provenance["engine_source"]
    controller_source = provenance["source"]
    assert isinstance(engine_source, dict)
    assert isinstance(controller_source, dict)
    assert engine_source["revision"] == revision
    assert (controller_source["revision"] != revision) is retained_manifest
    assert provenance["validation"] == {
        "status": "passed",
        "evidence": "prepared artifact verified",
    }


def test_strict_manifest_verifies_installed_wheel_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    metadata = site / "vfy-1.0.dist-info"
    metadata.mkdir(parents=True)
    package = site / "vfy"
    package.mkdir()
    (metadata / "METADATA").write_text("Name: vfy\nVersion: 1.0\n")
    (package / "adapter.py").write_text("value = 1\n")
    extension = package / "native.so"
    extension.write_bytes(b"extension")
    wheel = tmp_path / "vfy.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path in site.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(site).as_posix())
    (metadata / "RECORD").write_text("vfy-1.0.dist-info/METADATA,,\n")
    dist = importlib.metadata.PathDistribution(metadata)
    monkeypatch.setattr(importlib.metadata, "distribution", lambda _name: dist)

    def artifact(*, strict: bool) -> dict[str, str | None]:
        assert strict
        _verify_installed_wheel(wheel)
        return {"wheel_sha256": "identified"}

    monkeypatch.setattr(benchmark_provenance, "artifact_provenance", artifact)
    before = artifact(strict=True)
    (package / "adapter.py").write_text("value = 2\n")
    with pytest.raises(RuntimeError, match="installed vfy files differ"):
        benchmark_provenance.require_unchanged_artifact(before)
