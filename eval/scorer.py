"""Precision/recall/F1 scorer comparing agent findings against labeled
ground truth. See `eval/dataset/RUBRIC.md` for the labeling rubric and
matching rule this implements, and for the dataset's known size
limitation — this is not a substitute for reading that file.

Reports raw TP/FP/FN counts alongside the percentages: at this dataset's
size, a bare percentage is misleading on its own (one disputed match
swings F1 by double digits), so anything that renders a score must show
the counts too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from codesentinel.core.schema import Finding

# A predicted finding matches a ground-truth finding when they name the
# same file and the predicted line is within this many lines of the
# expected one. Exact line agreement is brittle — the model may point at
# the `if` instead of the `return` inside it for the same underlying
# issue. See RUBRIC.md for why this value and not a stricter/looser one.
LINE_TOLERANCE = 3


@dataclass(frozen=True)
class ExpectedFinding:
    file: str
    line: int
    description: str


@dataclass(frozen=True)
class Case:
    """One labeled eval case: a diff, the post-change repo it applies to,
    and the ground-truth findings a correct review should surface.
    """

    id: str
    description: str
    diff_path: Path
    repo_path: Path
    expected_findings: tuple[ExpectedFinding, ...]


def load_case(case_dir: Path) -> Case:
    """Load one case from a directory containing `case.json`, a diff
    file, and a `repo/` directory. Raises on missing/malformed files —
    a broken case should fail loudly at load time, not silently score
    as "no findings expected".
    """
    manifest = json.loads((case_dir / "case.json").read_text())

    diff_path = case_dir / manifest["diff_file"]
    repo_path = case_dir / manifest["repo_dir"]
    if not diff_path.is_file():
        raise FileNotFoundError(f"{case_dir}: diff_file {manifest['diff_file']!r} does not exist")
    if not repo_path.is_dir():
        raise FileNotFoundError(f"{case_dir}: repo_dir {manifest['repo_dir']!r} does not exist")

    expected = tuple(
        ExpectedFinding(file=f["file"], line=f["line"], description=f["description"])
        for f in manifest["expected_findings"]
    )

    return Case(
        id=manifest["id"],
        description=manifest["description"],
        diff_path=diff_path,
        repo_path=repo_path,
        expected_findings=expected,
    )


def load_dataset(split_dir: Path) -> list[Case]:
    """Load every case in a dataset split directory (e.g.
    `eval/dataset/dev`), one subdirectory per case.
    """
    return sorted(
        (load_case(case_dir) for case_dir in split_dir.iterdir() if case_dir.is_dir()),
        key=lambda case: case.id,
    )


def _matches(predicted: Finding, expected: ExpectedFinding) -> bool:
    return predicted.file == expected.file and abs(predicted.line - expected.line) <= LINE_TOLERANCE


@dataclass(frozen=True)
class ScoreResult:
    """Scoring outcome for a single case. `unmatched_predicted` and
    `unmatched_expected` are kept (not just counted) so a report can show
    *what* was missed or spuriously flagged, not just how many.
    """

    true_positives: int
    false_positives: int
    false_negatives: int
    unmatched_predicted: tuple[Finding, ...]
    unmatched_expected: tuple[ExpectedFinding, ...]

    @property
    def precision(self) -> float | None:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else None

    @property
    def recall(self) -> float | None:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else None

    @property
    def f1(self) -> float | None:
        precision, recall = self.precision, self.recall
        if precision is None or recall is None or (precision + recall) == 0:
            return None
        return 2 * precision * recall / (precision + recall)


def score_case(predicted: list[Finding], expected: tuple[ExpectedFinding, ...]) -> ScoreResult:
    """Score one case's predicted findings against its ground truth.

    Matching is greedy in input order and one-to-one: each expected
    finding claims at most one predicted finding (the first unclaimed one
    that matches), and a claimed predicted finding can't match again.
    This matters when several expected findings sit close together in the
    same file — without one-to-one matching, a single predicted finding
    could double-count against two nearby expected ones.
    """
    remaining_predicted = list(predicted)
    matched_expected: list[ExpectedFinding] = []
    unmatched_expected: list[ExpectedFinding] = []

    for exp in expected:
        match = next((p for p in remaining_predicted if _matches(p, exp)), None)
        if match is not None:
            remaining_predicted.remove(match)
            matched_expected.append(exp)
        else:
            unmatched_expected.append(exp)

    return ScoreResult(
        true_positives=len(matched_expected),
        false_positives=len(remaining_predicted),
        false_negatives=len(unmatched_expected),
        unmatched_predicted=tuple(remaining_predicted),
        unmatched_expected=tuple(unmatched_expected),
    )


@dataclass(frozen=True)
class AggregateScore:
    """Aggregate over a set of cases. `excluded_case_ids` are cases where
    the agent produced no review at all (a `ReviewGenerationError`) —
    excluded from the counts entirely, not counted as zero findings.
    Conflating the two would let an infra failure masquerade as perfect
    precision / zero recall on that case instead of an excluded run.
    """

    total_true_positives: int
    total_false_positives: int
    total_false_negatives: int
    excluded_case_ids: tuple[str, ...]
    scored_case_count: int

    @property
    def precision(self) -> float | None:
        denominator = self.total_true_positives + self.total_false_positives
        return self.total_true_positives / denominator if denominator else None

    @property
    def recall(self) -> float | None:
        denominator = self.total_true_positives + self.total_false_negatives
        return self.total_true_positives / denominator if denominator else None

    @property
    def f1(self) -> float | None:
        precision, recall = self.precision, self.recall
        if precision is None or recall is None or (precision + recall) == 0:
            return None
        return 2 * precision * recall / (precision + recall)


def aggregate(case_results: dict[str, ScoreResult | None]) -> AggregateScore:
    """`case_results[case_id] = None` means the agent produced no review
    for that case (excluded); a `ScoreResult` means it did and was scored.
    """
    total_tp = total_fp = total_fn = 0
    excluded: list[str] = []
    scored = 0

    for case_id, result in case_results.items():
        if result is None:
            excluded.append(case_id)
            continue
        scored += 1
        total_tp += result.true_positives
        total_fp += result.false_positives
        total_fn += result.false_negatives

    return AggregateScore(
        total_true_positives=total_tp,
        total_false_positives=total_fp,
        total_false_negatives=total_fn,
        excluded_case_ids=tuple(sorted(excluded)),
        scored_case_count=scored,
    )
