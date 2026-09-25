"""Generate a shields.io endpoint-badge JSON from eval-results.json.

Run after eval/run_eval.py. Produces badges/eval-badge.json in shields.io's
endpoint-badge schema (https://shields.io/badges/endpoint-badge), which
the README's badge points at via:
  https://img.shields.io/endpoint?url=<raw-content-url-of-this-file>

Deliberately terse (a badge has pixel-width to work with) — F1 and a
case count, not the full TP/FP/FN breakdown. RUBRIC.md's "never a bare
percentage" rule is satisfied by the README spelling out the counts in
prose next to the badge, not by cramming them into the badge itself.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_INPUT = Path("eval-results.json")
DEFAULT_OUTPUT = Path("badges/eval-badge.json")


def _color_for(f1: float | None) -> str:
    if f1 is None:
        return "lightgrey"
    if f1 >= 0.8:
        return "brightgreen"
    if f1 >= 0.5:
        return "yellow"
    return "red"


def build_badge(report: dict) -> dict:
    agg = report["aggregate"]
    scored = agg["scored_case_count"]
    total = report["total_case_count"]

    if scored == 0:
        message = "not yet measured"
    else:
        message = f"F1 {agg['f1']:.2f} · {scored}/{total} cases"

    return {
        "schemaVersion": 1,
        "label": "eval",
        "message": message,
        "color": _color_for(agg["f1"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a shields.io endpoint badge from eval-results.json."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    report = json.loads(args.input.read_text())
    badge = build_badge(report)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(badge, indent=2) + "\n")
    print(f"Wrote {args.output}: {badge['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
