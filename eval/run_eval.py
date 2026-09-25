"""Eval runner: loads a dataset split, runs the agent against each case,
scores the results, and writes eval-results.json.

Three modes:
  --mode replay (default; what CI runs on every push): VCR replays
  recorded API responses from eval/cassettes/<case_id>.yaml — no network,
  no cost, deterministic. Verified directly against this project's
  installed SDK version in test_run_eval.py (the anthropic SDK uses an
  internal `httpx2` client, not the vanilla `httpx` most VCR examples
  assume — this was checked empirically, not assumed, before relying on
  it). A missing or non-matching cassette raises loudly rather than
  silently falling through to a live call.
  --mode record: makes real API calls and (re)writes each case's
  cassette plus a `.meta.json` sidecar recording the prompt/tool source
  hash at record time — run this by hand whenever agent.py or a tool's
  docstring/schema changes, so the next replay run reflects the current
  prompt.
  --mode live: makes real API calls with no VCR involvement at all and
  never touches the checked-in cassettes — for a separate scheduled
  workflow that measures actual live-model drift, distinct from
  replay's "does the scorer still agree with history" check. See
  RUBRIC.md for why that distinction matters for what the README badge
  can honestly claim.

No cassettes have been recorded as of this commit — there was no live
API credential available in this environment. `--mode record` needs to
be run by hand, once, by whoever has one, before `--mode replay` (the
CI default) has anything to replay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import vcr as vcr_module

from codesentinel.core.agent import DEFAULT_MODEL, ReviewGenerationError, run_review
from codesentinel.core.diff import parse_diff
from codesentinel.core.sandbox import SandboxConfig
from eval.scorer import Case, ScoreResult, aggregate, load_dataset, score_case

EVAL_ROOT = Path(__file__).resolve().parent
DATASET_ROOT = EVAL_ROOT / "dataset"
CASSETTES_DIR = EVAL_ROOT / "cassettes"

# Everything that shapes what gets sent to the model: the system prompts
# in agent.py, plus every tool's docstring/schema (which become the tool
# descriptions Claude sees). A cassette recorded before a change to any of
# these no longer reflects what the current code would actually send.
_PROMPT_SOURCE_FILES = [
    Path(__file__).resolve().parent.parent / "src" / "codesentinel" / "core" / "agent.py",
    *sorted(
        (Path(__file__).resolve().parent.parent / "src" / "codesentinel" / "core" / "tools").glob(
            "*.py"
        )
    ),
]


def _prompt_hash() -> str:
    hasher = hashlib.sha256()
    for path in _PROMPT_SOURCE_FILES:
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _cassette_meta_path(cassette_path: Path) -> Path:
    return cassette_path.with_suffix(".meta.json")


def _write_cassette_meta(cassette_path: Path, model: str) -> None:
    meta = {
        "prompt_hash": _prompt_hash(),
        "model": model,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    _cassette_meta_path(cassette_path).write_text(json.dumps(meta, indent=2))


def check_cassette_staleness(cassette_path: Path) -> str | None:
    """Return a warning string if the cassette's recorded prompt hash
    doesn't match the current one (or there's no recorded hash to check),
    else None. A missing meta file is reported as unknown, never silently
    treated as fresh — an unrecorded assumption is exactly the failure
    mode this check exists to catch.
    """
    meta_path = _cassette_meta_path(cassette_path)
    if not meta_path.exists():
        return f"{cassette_path.name}: no recorded prompt hash to check against (missing .meta.json)"
    meta = json.loads(meta_path.read_text())
    if meta.get("prompt_hash") != _prompt_hash():
        return (
            f"{cassette_path.name}: recorded for a different prompt/tool version — "
            "re-record with `--mode record`"
        )
    return None


def _vcr(record_mode: str) -> vcr_module.VCR:
    return vcr_module.VCR(
        record_mode=record_mode,
        cassette_library_dir=str(CASSETTES_DIR),
        # Body-based matching, not just method+URI: every call in a
        # review (investigation-loop turns, then the restate call) hits
        # the same endpoint, so matching on URI alone can't tell them
        # apart — see the module docstring on why that's the thing to
        # get right here, not just wire up a decorator and assume it
        # works.
        match_on=["method", "scheme", "host", "port", "path", "body"],
        # Never write a real API key into a cassette that gets committed.
        filter_headers=["x-api-key", "authorization"],
    )


@dataclass
class CaseRunOutcome:
    case_id: str
    score: ScoreResult | None  # None => agent produced no review (excluded, not zero findings)
    error: str | None = None
    cassette_warning: str | None = None


def run_case(
    case: Case,
    *,
    mode: str,
    model: str = DEFAULT_MODEL,
    allow_tests: bool = False,
) -> CaseRunOutcome:
    diff_text = case.diff_path.read_text()
    parsed_diff = parse_diff(diff_text)
    config = SandboxConfig.for_repo(case.repo_path, allow_test_execution=allow_tests)
    cassette_path = CASSETTES_DIR / f"{case.id}.yaml"

    cassette_warning = check_cassette_staleness(cassette_path) if mode == "replay" else None

    def invoke() -> CaseRunOutcome:
        try:
            result = run_review(parsed_diff, config, model=model)
        except ReviewGenerationError as exc:
            return CaseRunOutcome(
                case_id=case.id, score=None, error=str(exc), cassette_warning=cassette_warning
            )
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see comment below
            # Confirmed by actually running this with no cassettes/no API
            # key present: the failure surfaces as anthropic.APIConnectionError
            # (VCR blocking a replay-mode request) or a raw TypeError
            # (missing credentials) or similar — never ReviewGenerationError,
            # since that's raised by run_review's own internal logic, not
            # by the transport underneath it. Any of these must still count
            # as "no review produced" for this one case, not crash the
            # whole eval run and prevent eval-results.json from being
            # written at all. Narrowing this back to ReviewGenerationError
            # would silently reintroduce exactly that crash.
            return CaseRunOutcome(
                case_id=case.id,
                score=None,
                error=f"{type(exc).__name__}: {exc}",
                cassette_warning=cassette_warning,
            )
        score = score_case(result.review.findings, case.expected_findings)
        return CaseRunOutcome(case_id=case.id, score=score, cassette_warning=cassette_warning)

    if mode == "replay":
        with _vcr("none").use_cassette(str(cassette_path)):
            return invoke()
    if mode == "record":
        CASSETTES_DIR.mkdir(parents=True, exist_ok=True)
        with _vcr("all").use_cassette(str(cassette_path)):
            outcome = invoke()
        _write_cassette_meta(cassette_path, model)
        return outcome
    if mode == "live":
        return invoke()  # no VCR involvement, and never touches the checked-in cassettes

    raise ValueError(f"unknown mode: {mode!r}")


def _build_report(
    *, split: str, mode: str, model: str, outcomes: list[CaseRunOutcome], total_case_count: int
) -> dict:
    results_by_id = {o.case_id: o.score for o in outcomes}
    agg = aggregate(results_by_id)

    return {
        "split": split,
        "mode": mode,
        "model": model,
        "generated_at": datetime.now(UTC).isoformat(),
        "total_case_count": total_case_count,
        "aggregate": {
            "true_positives": agg.total_true_positives,
            "false_positives": agg.total_false_positives,
            "false_negatives": agg.total_false_negatives,
            "precision": agg.precision,
            "recall": agg.recall,
            "f1": agg.f1,
            "scored_case_count": agg.scored_case_count,
            "excluded_case_ids": list(agg.excluded_case_ids),
        },
        "cases": {
            o.case_id: {
                "true_positives": o.score.true_positives if o.score else None,
                "false_positives": o.score.false_positives if o.score else None,
                "false_negatives": o.score.false_negatives if o.score else None,
                "error": o.error,
            }
            for o in outcomes
        },
        "cassette_warnings": [o.cassette_warning for o in outcomes if o.cassette_warning],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the codesentinel eval harness.")
    parser.add_argument("--split", choices=["dev", "test", "all"], default="test")
    parser.add_argument("--mode", choices=["replay", "record", "live"], default="replay")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--allow-tests", action="store_true", help="Let run_tests execute inside each case's fixture repo."
    )
    parser.add_argument(
        "--strict-cassettes",
        action="store_true",
        help="Exit non-zero if any cassette is missing or stale in --mode replay.",
    )
    parser.add_argument("--output", type=Path, default=Path("eval-results.json"))
    args = parser.parse_args(argv)

    splits = ["dev", "test"] if args.split == "all" else [args.split]
    cases = [case for split in splits for case in load_dataset(DATASET_ROOT / split)]

    outcomes = [
        run_case(case, mode=args.mode, model=args.model, allow_tests=args.allow_tests)
        for case in cases
    ]

    report = _build_report(
        split=args.split, mode=args.mode, model=args.model, outcomes=outcomes, total_case_count=len(cases)
    )
    args.output.write_text(json.dumps(report, indent=2))

    agg = report["aggregate"]
    print(
        f"Scored {agg['scored_case_count']}/{len(cases)} cases "
        f"(TP={agg['true_positives']} FP={agg['false_positives']} FN={agg['false_negatives']})"
    )
    if agg["precision"] is not None:
        print(f"precision={agg['precision']:.2f} recall={agg['recall']:.2f} f1={agg['f1']:.2f}")
    if agg["excluded_case_ids"]:
        print(f"Excluded (no review produced): {', '.join(agg['excluded_case_ids'])}", file=sys.stderr)
    for warning in report["cassette_warnings"]:
        print(f"Warning: {warning}", file=sys.stderr)

    # A partial exclusion (some cases scored, some didn't) is by design
    # not a failure — that's the whole point of excluding rather than
    # zero-filling. A total washout is a different thing: there is no
    # score at all (precision/recall/f1 are all None), so reporting exit
    # 0 here would claim a successful measurement that didn't happen.
    if cases and agg["scored_case_count"] == 0:
        print("Error: every case failed or was excluded — no score was produced.", file=sys.stderr)
        return 1

    if args.strict_cassettes and report["cassette_warnings"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
