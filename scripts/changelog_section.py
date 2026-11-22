"""Print a versioned changelog section for use as release notes."""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHANGELOG_DIR = ROOT / "changelogs"


def section(text: str, version: str) -> str:
    heading = re.compile(rf"^## \[?{re.escape(version)}\]?(\s|$)")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if heading.match(line):
            body = []
            for candidate in lines[index + 1 :]:
                if candidate.startswith("## "):
                    break
                body.append(candidate)
            return "\n".join(body).strip()
    return ""


def release_notes(version: str) -> str:
    """Find the release notes for ``version`` in the versioned changelogs."""

    matches = [
        notes
        for path in sorted(CHANGELOG_DIR.glob("*.md"))
        if (notes := section(path.read_text(encoding="utf-8"), version))
    ]
    if len(matches) > 1:
        raise ValueError(f"multiple changelog entries found for {version}")
    return matches[0] if matches else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--output", type=pathlib.Path, help="write the notes to this file")
    args = parser.parse_args()

    notes = release_notes(args.version)
    if not notes:
        print(f"changelogs/ has no entry for {args.version}", file=sys.stderr)
        return 1
    if args.output:
        args.output.write_text(notes + "\n", encoding="utf-8")
    else:
        print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
