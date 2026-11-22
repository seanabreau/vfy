"""Replace machine-specific Cargo component IDs before signing release artifacts."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import re
import tempfile
import zipfile
from pathlib import Path

# Cargo uses the same path ID for a component and all edges that point to it.
WORKSPACE_ID = re.compile(r'"path\+file://[^"\n]*?/crates/(vfy|vfy-core|vfy-pytest)#')
# cargo-cyclonedx emits host-native relative paths. JSON escapes a Windows backslash twice.
WORKSPACE_DOWNLOAD = re.compile(
    r'\?download_url=file://\.(?:\.(?:/|\\\\)(?:vfy-core|vfy-pytest|vfy))?(?=["#])'
)


def portable_sbom(data: bytes) -> bytes:
    text = WORKSPACE_ID.sub(r'"workspace:crates/\1#', data.decode("utf-8"))
    text = WORKSPACE_DOWNLOAD.sub("", text)
    if "file://" in text:
        raise ValueError("SBOM contains a local file URI outside the workspace component IDs")
    json.loads(text)
    return text.encode()


def normalize_artifact(artifact: Path) -> None:
    with tempfile.TemporaryDirectory(dir=artifact.parent) as directory:
        replacement = Path(directory) / artifact.name
        if artifact.suffix == ".json":
            replacement.write_bytes(portable_sbom(artifact.read_bytes()))
        elif artifact.suffix == ".whl":
            with zipfile.ZipFile(artifact) as source, zipfile.ZipFile(replacement, "w") as target:
                names = source.namelist()
                records = [name for name in names if name.endswith(".dist-info/RECORD")]
                if len(set(names)) != len(names) or len(records) != 1:
                    raise ValueError(f"invalid wheel inventory in {artifact.name}")
                record = records[0]
                hashes = {}
                for item in source.infolist():
                    if item.filename == record:
                        continue
                    data = source.read(item)
                    if ".dist-info/sboms/" in item.filename and item.filename.endswith(".json"):
                        data = portable_sbom(data)
                    target.writestr(item, data)
                    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
                    hashes[item.filename] = (f"sha256={digest.decode()}", str(len(data)))
                buffer = io.StringIO(newline="")
                writer = csv.writer(buffer, lineterminator="\n")
                for name in names:
                    writer.writerow((name, *hashes.get(name, ("", ""))))
                target.writestr(source.getinfo(record), buffer.getvalue().encode())
        else:
            raise ValueError(f"expected a wheel or CycloneDX JSON, got {artifact.name}")
        replacement.replace(artifact)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path, nargs="+")
    for artifact in parser.parse_args().artifacts:
        normalize_artifact(artifact)


if __name__ == "__main__":
    main()
