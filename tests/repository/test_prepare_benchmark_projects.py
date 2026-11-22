"""Prepared benchmark inputs retain the artifact and lock actually consumed."""

from __future__ import annotations

import errno
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from benchmarks import benchmark_projects, prepare_projects
from benchmarks.project_manifest import PROJECTS


def test_custom_metadata_records_actual_lock_and_build_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "project"
    checkout.mkdir()
    metadata = tmp_path / "custom" / "metadata"
    lock = tmp_path / "custom.lock"
    lock.write_text("pytest==8.0.0\n", encoding="utf-8")
    wheel = tmp_path / "wheels" / "vfy.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel")
    build = tmp_path / "vfy-build.json"
    build.write_text(
        json.dumps(
            {
                "wheel_path": str(wheel),
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "source_root": str(tmp_path),
                "source_inputs": {},
            }
        ),
        encoding="utf-8",
    )

    def check_output(command: list[str], **_kwargs: object) -> str:
        return "revision\n" if command[0] == "git" else "pytest==8.0.0\n"

    monkeypatch.setattr(subprocess, "check_output", check_output)
    monkeypatch.setattr(benchmark_projects, "git_value", lambda *_args: "revision")
    manifest = prepare_projects.record_environment(
        PROJECTS["rich"], checkout, metadata, lockfile=lock, build_manifest=build
    )

    record = benchmark_projects.preparation_metadata(
        tmp_path / "environment" / "bin" / "python", checkout, manifest_path=manifest
    )
    assert record["dependency_lock"] == str(lock)
    assert record["dependency_lock_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()
    assert record["vfy_build_manifest"] == str(build)
    build_record = record["vfy_wheel"]
    assert isinstance(build_record, dict)
    assert build_record["wheel_path"] == str(wheel)


def test_release_build_rejects_source_edits_during_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for path in prepare_projects._SOURCE_INPUTS:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original\n", encoding="utf-8")
    statuses = iter(("", " M crates/vfy/src/lib.rs\n"))
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: next(statuses))
    monkeypatch.setattr(prepare_projects, "git_value", lambda *_args: "a" * 40)
    monkeypatch.setattr(prepare_projects.shutil, "which", lambda _name: "/bin/maturin")

    def build(command: list[str], **_kwargs: object) -> None:
        assert command[0] == "/bin/maturin"
        (tmp_path / "wheels" / "vfy-test.whl").write_bytes(b"wheel")

    monkeypatch.setattr(prepare_projects, "run_checked", build)
    with pytest.raises(RuntimeError, match="source changed during the release benchmark build"):
        prepare_projects.install_vfy(tmp_path / "python", tmp_path, vfy_root=tmp_path)


@pytest.mark.parametrize("edit_phase", ["none", "install", "manifest"])
@pytest.mark.parametrize("pending_kind", ["file", "symlink"])
def test_prepared_manifest_rejects_source_edits_after_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit_phase: str, pending_kind: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for path in prepare_projects._SOURCE_INPUTS:
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "source",
        ],
        cwd=source,
        check=True,
    )
    destination = tmp_path / "prepared"
    destination.mkdir()
    published = destination / "vfy-build.json"
    previous_manifest = b'{"source_revision": "previous"}\n'
    published.write_bytes(previous_manifest)
    legacy_pending = destination / ".vfy-build.json.pending"
    previous_pending = b'{"unrelated": "preserve"}\n'
    if pending_kind == "symlink":
        pending_target = tmp_path / "other-manifest.json"
        pending_target.write_bytes(previous_pending)
        try:
            legacy_pending.symlink_to(pending_target)
        except NotImplementedError:
            pytest.skip("symlinks are unavailable")
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EPERM, errno.ENOSYS, errno.ENOTSUP}:
                raise
            pytest.skip("symlinks are unavailable")
    else:
        pending_target = legacy_pending
        legacy_pending.write_bytes(previous_pending)
    interpreter = tmp_path / "python"
    monkeypatch.setattr(prepare_projects.shutil, "which", lambda _name: "/bin/maturin")

    def run(command: list[str], **_kwargs: object) -> None:
        if command[0] == "/bin/maturin":
            (destination / "wheels" / "vfy-test.whl").write_bytes(b"wheel")
        elif command[0] == "uv" and edit_phase == "install":
            (source / "Cargo.toml").write_text("changed\n", encoding="utf-8")
        elif command[0] == str(interpreter):
            Path(command[3]).write_text('{"source_revision": "recorded"}\n', encoding="utf-8")
            if edit_phase == "manifest":
                (source / "Cargo.toml").write_text("changed\n", encoding="utf-8")

    monkeypatch.setattr(prepare_projects, "run_checked", run)
    wheel, revision, inputs = prepare_projects.install_vfy(
        interpreter, destination, vfy_root=source
    )
    existing_outputs = set(destination.iterdir())
    if edit_phase == "none":
        manifest = prepare_projects.write_prepared_build_manifest(
            destination, interpreter, source, wheel, revision, inputs
        )
        assert manifest == published
        assert manifest.read_bytes() != previous_manifest
        record = json.loads(manifest.read_text(encoding="utf-8"))
        assert record["source_inputs"] == inputs
        assert inputs["Cargo.toml"] == hashlib.sha256(b"original\n").hexdigest()
    else:
        with pytest.raises(RuntimeError, match="source changed"):
            prepare_projects.write_prepared_build_manifest(
                destination, interpreter, source, wheel, revision, inputs
            )
        assert published.read_bytes() == previous_manifest
    assert not published.is_symlink()
    assert legacy_pending.is_symlink() is (pending_kind == "symlink")
    assert legacy_pending.read_bytes() == pending_target.read_bytes() == previous_pending
    assert set(destination.iterdir()) == existing_outputs
