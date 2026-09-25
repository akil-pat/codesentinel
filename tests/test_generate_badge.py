"""Tests for eval.generate_badge."""

from __future__ import annotations

import json

from eval.generate_badge import build_badge, main


def _report(scored: int, total: int, f1: float | None, tp=0, fp=0, fn=0) -> dict:
    return {
        "total_case_count": total,
        "aggregate": {
            "scored_case_count": scored,
            "f1": f1,
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": fn,
        },
    }


def test_build_badge_no_data_yet():
    badge = build_badge(_report(scored=0, total=3, f1=None))

    assert badge["message"] == "not yet measured"
    assert badge["color"] == "lightgrey"
    assert badge["schemaVersion"] == 1


def test_build_badge_high_f1_is_green():
    badge = build_badge(_report(scored=3, total=3, f1=0.9, tp=9, fp=1, fn=1))

    assert "F1 0.90" in badge["message"]
    assert "3/3 cases" in badge["message"]
    assert badge["color"] == "brightgreen"


def test_build_badge_mid_f1_is_yellow():
    badge = build_badge(_report(scored=3, total=3, f1=0.6))

    assert badge["color"] == "yellow"


def test_build_badge_low_f1_is_red():
    badge = build_badge(_report(scored=3, total=3, f1=0.2))

    assert badge["color"] == "red"


def test_main_reads_input_and_writes_output(tmp_path):
    input_path = tmp_path / "eval-results.json"
    input_path.write_text(json.dumps(_report(scored=2, total=3, f1=0.75)))
    output_path = tmp_path / "badges" / "eval-badge.json"

    exit_code = main(["--input", str(input_path), "--output", str(output_path)])

    assert exit_code == 0
    badge = json.loads(output_path.read_text())
    assert badge["message"] == "F1 0.75 · 2/3 cases"
