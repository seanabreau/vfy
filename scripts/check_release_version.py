"""Check that a release version agrees with the workspace and built artifacts.

Prints the project version on stdout so a workflow can capture it; diagnostics go
to stderr.
"""

from __future__ import annotations

import argparse
import email.parser
import pathlib
import sys
import tarfile
import tomllib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
DISTRIBUTION = "vfy"


def project_version() -> str:
    with (ROOT / "Cargo.toml").open("rb") as handle:
        version = tomllib.load(handle)["workspace"]["package"]["version"]
    if not isinstance(version, str):
        raise ValueError("Cargo.toml workspace.package.version must be a string")
    return version


def check_tag(tag: str, version: str) -> list[str]:
    if tag != f"v{version}":
        return [f"tag {tag!r} does not match Cargo.toml version {version!r}"]
    return []


def artifact_metadata(path: pathlib.Path, version: str) -> list[str]:
    """Check the identity inside an archive, not just its filename."""
    try:
        if path.suffix == ".whl":
            expected = f"{DISTRIBUTION}-{version}.dist-info/METADATA"
            with zipfile.ZipFile(path) as archive:
                entries = [
                    name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
                ]
                if entries != [expected]:
                    return [f"{path.name}: expected exactly one {expected}, found {entries}"]
                metadata = archive.read(expected)
        else:
            expected = f"{DISTRIBUTION}-{version}/PKG-INFO"
            with tarfile.open(path, "r:gz") as archive:
                entries = [member for member in archive.getmembers() if member.name == expected]
                if len(entries) != 1 or not entries[0].isfile():
                    return [f"{path.name}: expected exactly one regular {expected}"]
                stream = archive.extractfile(entries[0])
                if stream is None:
                    return [f"{path.name}: could not read {expected}"]
                metadata = stream.read()
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile) as error:
        return [f"{path.name}: could not read archive: {error}"]

    message = email.parser.BytesParser().parsebytes(metadata, headersonly=True)
    errors = []
    for field, expected_value in (("Name", DISTRIBUTION), ("Version", version)):
        values = message.get_all(field, [])
        if values != [expected_value]:
            errors.append(f"{path.name}: expected {field} {expected_value!r}, found {values}")
    return errors


def check_dist(directory: pathlib.Path, version: str) -> list[str]:
    paths = sorted(p for p in directory.iterdir() if p.is_file())
    if not paths:
        return [f"no artifacts found in {directory}"]

    errors = []
    wheels = [p for p in paths if p.suffix == ".whl"]
    sdists = [p for p in paths if p.name.endswith(".tar.gz")]
    for path in paths:
        if path in wheels and not path.name.startswith(f"{DISTRIBUTION}-{version}-"):
            errors.append(f"wheel {path.name} is not {DISTRIBUTION} {version}")
        if path in sdists and path.name != f"{DISTRIBUTION}-{version}.tar.gz":
            errors.append(f"sdist {path.name} is not {DISTRIBUTION} {version}")
        if path not in wheels and path not in sdists:
            errors.append(f"unexpected artifact {path.name}")
    for path in wheels + sdists:
        errors.extend(artifact_metadata(path, version))
    if not wheels:
        errors.append("no wheels found")
    if len(sdists) != 1:
        errors.append(f"expected exactly one sdist, found {len(sdists)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="git tag to compare against, for example v0.0.1")
    parser.add_argument("--dist", type=pathlib.Path, help="directory of built artifacts")
    parser.add_argument(
        "--expected-wheels",
        type=int,
        help="number of wheels the release matrix is expected to produce",
    )
    args = parser.parse_args()

    version = project_version()
    errors = []
    if args.tag:
        errors += check_tag(args.tag, version)
    if args.dist:
        errors += check_dist(args.dist, version)
        wheels = len([p for p in args.dist.iterdir() if p.suffix == ".whl"])
        if args.expected_wheels is not None and wheels != args.expected_wheels:
            errors.append(f"expected {args.expected_wheels} wheels, found {wheels}")

    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
