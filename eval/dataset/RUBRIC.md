# Eval dataset — labeling rubric and known limitations

## What's here right now

6 cases (3 dev, 3 test), each a single-file diff with one deliberately
injected bug, generated from real `git diff` output (not hand-typed) so
hunk headers and line numbers are guaranteed correct. `dev/` is for tuning
the prompt/agent against; `test/` is held out and only run to report the
score that goes in the README badge — never look at `test/` results while
iterating on the prompt, or the score stops meaning anything.

**This is a starter set, not the target dataset.** The design review that
shaped this project flagged that 5–10 hand-labeled diffs can't support a
"measured precision/recall" claim — one disputed match swings F1 by double
digits at this size. The recommendation was 25–40 diffs sourced from real
PRs, license-checked. That expansion is tracked as follow-up work; until
it happens, any score reported from this dataset should be presented with
its raw case count and TP/FP/FN counts, never as a bare percentage, and
should not be treated as strong evidence of review quality. `AggregateScore`
in `eval/scorer.py` keeps the raw TP/FP/FN counts on the same object as the
precision/recall/F1 properties, specifically so nothing that reports a
score can show the percentage without also having the counts on hand —
any eval report generated from it (the eval runner, `eval-results.json`,
the README badge) must print both.

## Case format

Each case is a directory containing:

- `case.json` — id, description, and `expected_findings` (ground truth)
- `diff.patch` — the diff to review, applied to `repo/`'s pre-change state
- `repo/` — the repo's **post-change** files (what the agent's tools —
  `read_file`, `grep_repo`, `run_tests` — will actually see)

```json
{
  "id": "dev-001-null-deref",
  "description": "...",
  "diff_file": "diff.patch",
  "repo_dir": "repo",
  "expected_findings": [
    {"file": "src/handler.py", "line": 2, "description": "..."}
  ]
}
```

## Matching rule

A predicted finding matches an expected (ground-truth) finding when:

1. **Same file** — exact path match, relative to the repo root.
2. **Line within tolerance** — `abs(predicted.line - expected.line) <= 3`
   (`LINE_TOLERANCE` in `eval/scorer.py`). Exact line agreement is
   brittle: a model might point at the `if` instead of the `return`
   inside it for the same underlying issue. 3 lines is generous enough to
   absorb that without being so loose it accepts an unrelated finding
   two functions away.

**Severity is not scored.** Matching is presence/absence of the issue
only — a `Finding` with `severity: "low"` matches an expected finding
just as well as one marked `"high"`. Severity assignment is a judgment
call with no ground truth to check it against here; scoring on it would
be scoring against our own opinion, not an objective label.

**Matching is one-to-one.** Each expected finding claims at most one
predicted finding, and a claimed predicted finding can't match a second
expected finding. This matters when two expected findings sit close
together in the same file — without one-to-one matching, one lucky
predicted finding could double-count as a match for both.

**Extra findings are false positives**, always — a predicted finding that
doesn't match any expected finding counts against precision even if it's
a real, legitimate observation the ground truth simply didn't anticipate.
This is a known bias in this kind of eval (see Limitations below).

## Handling "no review produced"

If the agent raises `ReviewGenerationError` for a case (investigation
loop didn't finish, restate call failed validation, refusal, etc.), that
case is **excluded from the score entirely** — not scored as zero
findings. `eval/scorer.py`'s `aggregate()` takes `None` for such a case
and tracks it in `excluded_case_ids` separately from the TP/FP/FN totals.
Conflating "produced nothing" with "produced an empty findings list" would
let an infra failure masquerade as a perfect-precision, zero-recall run
instead of what it actually is: a run that should be investigated and
re-run, not counted.

## Limitations, stated plainly

- **Small sample.** 6 cases total, 3 per split. Any percentage computed
  from this is a rough signal, not a reliable estimate — report raw
  counts alongside it always.
- **Synthetic, not sourced from real PRs.** Each case is a small,
  single-file, single-bug diff authored for this dataset, not mined from
  real-world pull requests. Real PRs are messier — multiple files, mixed
  intent, bugs that interact with surrounding context the agent has to
  go find — and a model that does well here isn't guaranteed to do as
  well there. This is the main reason the dataset needs the 25–40
  real-PR expansion the design review recommended before treating scores
  as strong evidence.
- **One bug per case.** No case currently tests whether the agent
  correctly reports multiple independent issues in one diff, or handles
  a multi-file diff at all — every case here touches exactly one file.
- **Extra-findings-always-count-against-precision bias.** A model that
  finds something real but unanticipated by the label is penalized the
  same as a model that hallucinates a non-issue. At this dataset's size
  a single such case can move the precision number noticeably; read any
  reported false positives before concluding the model is "worse" than
  a run with none.
- **No severity ground truth.** As noted above, severity isn't scored at
  all — a `Review` that correctly finds every bug but rates them all
  "low" scores identically to one that rates them all "high".
- **The `test-003-prompt-injection` case is a stretch/adversarial check**,
  not a representative "typical" case — it deliberately tests injection
  resistance alongside bug-finding. Its presence in the aggregate test-set
  score means that score partly reflects prompt-injection robustness, not
  purely code-review accuracy. Worth reporting separately if the
  aggregate number is ever presented publicly.
