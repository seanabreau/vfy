"""Build the current-wheel parallel chart from full retained project gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_parallel_chart_summary import build_summary
from scripts.build_parallel_evidence_record import build_record

ROOT = Path(__file__).resolve().parents[1]


def build_native_summary(paths: list[Path]) -> dict[str, Any]:
    evidence = build_record(paths)
    summary = build_summary(evidence, source="retained full-suite project artifacts")
    rows = summary["charts"][0]["rows"]
    for row, project in zip(rows, evidence["projects"], strict=True):
        row.update(
            {
                "controller_revision": project["controller"]["revision"],
                "upstream_revision": project["identity"]["prepared_revision"],
                "engine_source_revision": project["identity"]["build"]["source_revision"],
                "wheel_sha256": project["wheel_sha256"],
                "raw_sha256": project["raw_sha256"],
            }
        )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    summary = build_native_summary(args.records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
