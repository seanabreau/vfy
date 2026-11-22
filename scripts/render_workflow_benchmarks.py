"""Render the README's time and memory comparison from paired benchmark medians."""

import argparse
import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict


class Comparison(TypedDict):
    package: str
    workload: str
    reference: str
    vfy_seconds: float
    reference_seconds: float
    vfy_memory_mib: float
    reference_memory_mib: float
    labels: list[str]


@dataclass(frozen=True)
class Palette:
    background: str
    text: str
    muted: str
    vfy_bar: str
    reference_bar: str
    divider: str


LIGHT = Palette(
    background="#ffffff",
    text="#22262b",
    muted="#59636e",
    vfy_bar="#cb603d",
    reference_bar="#919aa3",
    divider="#dce0e3",
)
DARK = Palette(
    background="#0d1117",
    text="#e9edf1",
    muted="#a5afb9",
    vfy_bar="#ef916e",
    reference_bar="#778391",
    divider="#30363d",
)


def render(rows: list[Comparison], palette: Palette = LIGHT) -> str:
    width = 720
    row_height = 118
    height = 48 + row_height * len(rows)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Benchmark time and memory</title>',
        '<desc id="desc">Median elapsed time and median peak process-tree memory. '
        "Each pair of bars starts at zero and shares a scale. Lower values are better.</desc>",
        f'<rect width="100%" height="100%" fill="{palette.background}"/>',
        '<g font-family="-apple-system, BlinkMacSystemFont, Segoe UI, '
        'Helvetica, Arial, sans-serif" '
        f'font-size="15" fill="{palette.text}" style="font-variant-numeric:tabular-nums">',
    ]

    def label(x: int, y: int, text: str, attributes: str = "") -> None:
        parts.append(f'<text x="{x}" y="{y}" {attributes}>{html.escape(text)}</text>')

    def rule(y: int) -> None:
        parts.append(f'<path d="M16 {y}.5H704" stroke="{palette.divider}"/>')

    muted = f'fill="{palette.muted}"'
    for x, heading in ((16, "Workload"), (226, "Median time"), (473, "Peak memory")):
        label(x, 25, heading, muted)
    rule(39)
    for index, row in enumerate(rows):
        top = 40 + index * row_height
        label(16, top + 31, row["package"], 'font-size="23" font-weight="600"')
        label(16, top + 52, row["workload"], muted)
        for name, offset in (("vfy", 79), (row["reference"], 101)):
            label(16, top + offset, name)

        for metric, start, end, bar_width, values, comparison in (
            (
                "time",
                226,
                432,
                130,
                (row["vfy_seconds"], row["reference_seconds"]),
                row["labels"][0],
            ),
            (
                "memory",
                473,
                704,
                145,
                (row["vfy_memory_mib"], row["reference_memory_mib"]),
                row["labels"][1],
            ),
        ):
            label(start, top + 31, comparison, 'font-weight="600"')
            for value, color, offset in zip(
                values, (palette.vfy_bar, palette.reference_bar), (79, 101), strict=True
            ):
                length = bar_width * value / max(values)
                parts.append(
                    f'<rect x="{start}" y="{top + offset - 8}" width="{length:.2f}" '
                    f'height="6" rx="3" fill="{color}"/>'
                )
                value_label = f"{value:.2f} s" if metric == "time" else f"{value:,.0f} MiB"
                label(end, top + offset, value_label, 'text-anchor="end"')
        if index + 1 < len(rows):
            rule(top + row_height)
    return "\n".join([*parts, "</g></svg>", ""])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    args = parser.parse_args()
    palette = DARK if args.theme == "dark" else LIGHT
    args.output.write_text(
        render(json.loads(args.summary.read_text(encoding="utf-8")), palette), encoding="utf-8"
    )
