"""Tests for eval.scorer: pure scoring-logic tests (synthetic data, no
dataset files needed) plus dataset integrity tests that load the actual
files under eval/dataset/{dev,test} — the latter exist so a broken case
(bad JSON, a line number that doesn't match the repo file, a diff that
doesn't parse) fails in `pytest`, not silently at eval time.
"""

from __future__ import annotations

from pathlib import Path

from codesentinel.core.diff import parse_diff
from codesentinel.core.schema import Finding
from eval.scorer import ExpectedFinding, aggregate, load_case, load_dataset, score_case

DATASET_ROOT = Path(__file__).resolve().parent.parent / "eval" / "dataset"


def _finding(file: str, line: int, comment: str = "x") -> Finding:
    return Finding(file=file, line=line, severity="medium", comment=comment)


def _expected(file: str, line: int, description: str = "x") -> ExpectedFinding:
    return ExpectedFinding(file=file, line=line, description=description)


# --- score_case: matching logic ---------------------------------------


def test_score_case_exact_match():
    result = score_case([_finding("a.py", 10)], (_expected("a.py", 10),))

    assert result.true_positives == 1
    assert result.false_positives == 0
    assert result.false_negatives == 0
    assert result.precision == 1.0
    assert result.recall == 1.0
    assert result.f1 == 1.0


def test_score_case_matches_within_line_tolerance():
    result = score_case([_finding("a.py", 12)], (_expected("a.py", 10),))  # 2 lines off, within tolerance=3

    assert result.true_positives == 1
    assert result.false_positives == 0


def test_score_case_does_not_match_beyond_tolerance():
    result = score_case([_finding("a.py", 20)], (_expected("a.py", 10),))  # 10 lines off

    assert result.true_positives == 0
    assert result.false_positives == 1
    assert result.false_negatives == 1


def test_score_case_does_not_match_different_file():
    result = score_case([_finding("b.py", 10)], (_expected("a.py", 10),))

    assert result.true_positives == 0
    assert result.false_positives == 1
    assert result.false_negatives == 1


def test_score_case_missing_finding_is_false_negative():
    result = score_case([], (_expected("a.py", 10),))

    assert result.false_negatives == 1
    assert result.unmatched_expected == (_expected("a.py", 10),)


def test_score_case_extra_finding_is_false_positive():
    extra = _finding("a.py", 10)
    result = score_case([extra], ())

    assert result.false_positives == 1
    assert result.unmatched_predicted == (extra,)


def test_score_case_one_to_one_matching_does_not_double_count():
    # Two expected findings 2 lines apart, both within tolerance of a
    # single predicted finding — only one should match; the other is a
    # miss, not a second true positive from the same prediction.
    predicted = [_finding("a.py", 10)]
    expected = (_expected("a.py", 9), _expected("a.py", 11))

    result = score_case(predicted, expected)

    assert result.true_positives == 1
    assert result.false_negatives == 1
    assert result.false_positives == 0


def test_score_case_empty_vs_empty_is_perfect_with_no_findings():
    result = score_case([], ())

    assert result.true_positives == 0
    assert result.false_positives == 0
    assert result.false_negatives == 0
    assert result.precision is None  # 0/0 — no findings predicted, nothing to be precise about
    assert result.recall is None  # 0/0 — nothing expected, nothing to recall
    assert result.f1 is None


# --- aggregate ---------------------------------------------------------


def test_aggregate_sums_across_cases():
    results = {
        "case-1": score_case([_finding("a.py", 1)], (_expected("a.py", 1),)),  # TP=1
        "case-2": score_case([], (_expected("b.py", 1),)),  # FN=1
        "case-3": score_case([_finding("c.py", 1)], ()),  # FP=1
    }

    agg = aggregate(results)

    assert agg.total_true_positives == 1
    assert agg.total_false_positives == 1
    assert agg.total_false_negatives == 1
    assert agg.scored_case_count == 3
    assert agg.excluded_case_ids == ()
    assert agg.precision == 0.5
    assert agg.recall == 0.5


def test_aggregate_excludes_none_results_from_counts():
    results = {
        "case-1": score_case([_finding("a.py", 1)], (_expected("a.py", 1),)),  # TP=1
        "case-2": None,  # agent produced no review — excluded, not zero findings
    }

    agg = aggregate(results)

    assert agg.total_true_positives == 1
    assert agg.scored_case_count == 1
    assert agg.excluded_case_ids == ("case-2",)


def test_aggregate_all_excluded_yields_none_scores():
    agg = aggregate({"case-1": None, "case-2": None})

    assert agg.scored_case_count == 0
    assert agg.excluded_case_ids == ("case-1", "case-2")
    assert agg.precision is None
    assert agg.recall is None
    assert agg.f1 is None


# --- load_case / load_dataset ------------------------------------------


def test_load_case_reads_manifest(tmp_path):
    case_dir = tmp_path / "case-001"
    (case_dir / "repo").mkdir(parents=True)
    (case_dir / "repo" / "f.py").write_text("x = 1\n")
    (case_dir / "diff.patch").write_text("diff --git a/f.py b/f.py\n")
    (case_dir / "case.json").write_text(
        '{"id": "case-001", "description": "d", "diff_file": "diff.patch", '
        '"repo_dir": "repo", "expected_findings": [{"file": "f.py", "line": 1, "description": "d"}]}'
    )

    case = load_case(case_dir)

    assert case.id == "case-001"
    assert case.expected_findings == (ExpectedFinding(file="f.py", line=1, description="d"),)
    assert case.diff_path.name == "diff.patch"
    assert case.repo_path.name == "repo"


def test_load_case_raises_on_missing_repo_dir(tmp_path):
    case_dir = tmp_path / "case-001"
    case_dir.mkdir()
    (case_dir / "diff.patch").write_text("diff --git a/f.py b/f.py\n")
    (case_dir / "case.json").write_text(
        '{"id": "case-001", "description": "d", "diff_file": "diff.patch", '
        '"repo_dir": "repo", "expected_findings": []}'
    )

    import pytest

    with pytest.raises(FileNotFoundError):
        load_case(case_dir)


# --- dataset integrity: load the REAL dataset files ---------------------


def test_dataset_directories_exist():
    assert (DATASET_ROOT / "dev").is_dir()
    assert (DATASET_ROOT / "test").is_dir()


def test_every_dataset_case_loads_and_its_diff_parses():
    for split in ("dev", "test"):
        for case in load_dataset(DATASET_ROOT / split):
            diff_text = case.diff_path.read_text()
            parsed = parse_diff(diff_text)  # raises UnidiffParseError if malformed
            assert parsed.files, f"{case.id}: diff has no changed files"


def test_every_expected_finding_points_at_a_real_line_in_the_repo():
    for split in ("dev", "test"):
        for case in load_dataset(DATASET_ROOT / split):
            for expected in case.expected_findings:
                target = case.repo_path / expected.file
                assert target.is_file(), f"{case.id}: {expected.file} missing from repo/"
                line_count = len(target.read_text().splitlines())
                assert 1 <= expected.line <= line_count, (
                    f"{case.id}: expected line {expected.line} out of range "
                    f"for {expected.file} ({line_count} lines)"
                )


def test_dataset_case_ids_are_unique_and_namespaced_by_split():
    dev_ids = {case.id for case in load_dataset(DATASET_ROOT / "dev")}
    test_ids = {case.id for case in load_dataset(DATASET_ROOT / "test")}

    assert len(dev_ids) == len(load_dataset(DATASET_ROOT / "dev")), "duplicate id within dev/"
    assert len(test_ids) == len(load_dataset(DATASET_ROOT / "test")), "duplicate id within test/"
    assert dev_ids.isdisjoint(test_ids), "a case id appears in both dev/ and test/"
    assert all(cid.startswith("dev-") for cid in dev_ids)
    assert all(cid.startswith("test-") for cid in test_ids)


def test_dataset_has_at_least_the_documented_starter_size():
    # Matches RUBRIC.md's stated size — if this creeps down, the dataset
    # shrank without the rubric being updated to match.
    assert len(load_dataset(DATASET_ROOT / "dev")) >= 3
    assert len(load_dataset(DATASET_ROOT / "test")) >= 3
