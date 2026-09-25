"""Tests for eval.run_eval.

Most of these monkeypatch `run_review` so no live API call happens —
they test orchestration (case loading, scoring, JSON output, exit codes),
mirroring the pattern in test_cli.py. One test is different on purpose:
test_vcr_actually_intercepts_the_anthropic_httpx_client below makes no
assumption about VCR/anthropic compatibility — it reproduces, as an
executable regression test, the exact manual check that was run before
writing any of this module's VCR-based code. If a future SDK or vcrpy
upgrade breaks that compatibility, this is what will catch it, rather
than a silent switch to hitting the live network in "replay" mode.
"""

from __future__ import annotations

import json

import pytest
import vcr as vcr_module
from anthropic import Anthropic

import eval.run_eval as run_eval_module
from codesentinel.core.agent import ReviewGenerationError, RunResult
from codesentinel.core.schema import Finding, Review
from eval.run_eval import (
    check_cassette_staleness,
    main,
    run_case,
)
from eval.scorer import Case


@pytest.fixture
def prompt_source_files(tmp_path, monkeypatch):
    """Point _PROMPT_SOURCE_FILES at throwaway files so hash tests don't
    depend on (or risk mutating) the real source tree.
    """
    files = [tmp_path / "agent.py", tmp_path / "tool_a.py"]
    for f in files:
        f.write_text("original content\n")
    monkeypatch.setattr(run_eval_module, "_PROMPT_SOURCE_FILES", files)
    return files


@pytest.fixture
def repo_case(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def f():\n    return 1\n")
    diff_path = tmp_path / "diff.patch"
    diff_path.write_text(
        "diff --git a/src/app.py b/src/app.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def f():\n"
        "-    return 0\n"
        "+    return 1\n"
    )
    return Case(
        id="unit-test-case",
        description="synthetic case for run_eval tests",
        diff_path=diff_path,
        repo_path=repo,
        expected_findings=(),
    )


# --- _prompt_hash / check_cassette_staleness --------------------------


def test_prompt_hash_is_stable(prompt_source_files):
    assert run_eval_module._prompt_hash() == run_eval_module._prompt_hash()


def test_prompt_hash_changes_when_source_changes(prompt_source_files):
    before = run_eval_module._prompt_hash()
    prompt_source_files[0].write_text("changed content\n")
    after = run_eval_module._prompt_hash()

    assert before != after


def test_check_cassette_staleness_missing_meta(tmp_path, prompt_source_files):
    cassette = tmp_path / "case.yaml"
    cassette.write_text("interactions: []\n")

    warning = check_cassette_staleness(cassette)

    assert warning is not None
    assert "missing" in warning.lower()


def test_check_cassette_staleness_matching_hash(tmp_path, prompt_source_files):
    cassette = tmp_path / "case.yaml"
    cassette.write_text("interactions: []\n")
    run_eval_module._write_cassette_meta(cassette, model="claude-opus-5")

    assert check_cassette_staleness(cassette) is None


def test_check_cassette_staleness_mismatched_hash(tmp_path, prompt_source_files):
    cassette = tmp_path / "case.yaml"
    cassette.write_text("interactions: []\n")
    run_eval_module._write_cassette_meta(cassette, model="claude-opus-5")

    prompt_source_files[0].write_text("prompt changed after recording\n")

    warning = check_cassette_staleness(cassette)

    assert warning is not None
    assert "different prompt" in warning


# --- run_case (mode="live", run_review monkeypatched — no VCR, no network) --


def test_run_case_live_happy_path(repo_case, monkeypatch):
    review = Review(
        summary="ok",
        findings=[Finding(file="src/app.py", line=2, severity="low", comment="x")],
    )
    monkeypatch.setattr(
        run_eval_module,
        "run_review",
        lambda *a, **k: RunResult(review=review, analysis_text="x", iteration_count=1),
    )

    outcome = run_case(repo_case, mode="live")

    assert outcome.case_id == "unit-test-case"
    assert outcome.score is not None
    assert outcome.error is None
    assert outcome.cassette_warning is None  # only checked in replay mode


def test_run_case_live_records_generation_error(repo_case, monkeypatch):
    def raise_error(*args, **kwargs):
        raise ReviewGenerationError("investigation did not finish cleanly")

    monkeypatch.setattr(run_eval_module, "run_review", raise_error)

    outcome = run_case(repo_case, mode="live")

    assert outcome.score is None
    assert "did not finish cleanly" in outcome.error


def test_run_case_replay_mode_surfaces_cassette_warning_without_hitting_vcr_for_a_faked_call(
    repo_case, monkeypatch, tmp_path
):
    # run_review is faked (no real HTTP call happens), so entering the VCR
    # cassette context is a no-op here — this isolates the orchestration
    # logic (does run_case report the staleness warning?) from whether VCR
    # itself works, which is covered separately below.
    monkeypatch.setattr(run_eval_module, "CASSETTES_DIR", tmp_path)
    review = Review(summary="ok", findings=[])
    monkeypatch.setattr(
        run_eval_module,
        "run_review",
        lambda *a, **k: RunResult(review=review, analysis_text="x", iteration_count=1),
    )

    outcome = run_case(repo_case, mode="replay")

    assert outcome.cassette_warning is not None
    assert "missing" in outcome.cassette_warning.lower()


# --- main() --------------------------------------------------------------


def test_main_writes_report_and_returns_zero(tmp_path, monkeypatch):
    review = Review(
        summary="ok",
        findings=[Finding(file="src/handler.py", line=2, severity="low", comment="x")],
    )
    monkeypatch.setattr(
        run_eval_module,
        "run_review",
        lambda *a, **k: RunResult(review=review, analysis_text="x", iteration_count=1),
    )
    output_path = tmp_path / "results.json"

    exit_code = main(["--split", "dev", "--mode", "live", "--output", str(output_path)])

    assert exit_code == 0
    report = json.loads(output_path.read_text())
    assert report["split"] == "dev"
    assert report["mode"] == "live"
    assert set(report["cases"]) == {"dev-001-null-deref", "dev-002-off-by-one", "dev-003-sql-injection"}
    assert report["aggregate"]["scored_case_count"] == 3


def test_main_total_washout_fails_the_run(tmp_path, monkeypatch):
    # Confirmed against a real run (no cassettes, no API key) before this
    # test was written: every case failing used to crash main() with an
    # unhandled TypeError and never write eval-results.json at all. Fixed
    # in run_case's invoke() to catch any exception, not just
    # ReviewGenerationError — this test guards that fix. A run where
    # EVERY case fails or is excluded produces no score at all
    # (precision/recall/f1 are all None), so exit 0 here would falsely
    # claim a successful measurement.
    def raise_error(*args, **kwargs):
        raise ReviewGenerationError("boom")

    monkeypatch.setattr(run_eval_module, "run_review", raise_error)
    output_path = tmp_path / "results.json"

    exit_code = main(["--split", "dev", "--mode", "live", "--output", str(output_path)])

    assert exit_code == 1
    report = json.loads(output_path.read_text())  # still written, not lost to a crash
    assert report["aggregate"]["scored_case_count"] == 0
    assert set(report["aggregate"]["excluded_case_ids"]) == {
        "dev-001-null-deref",
        "dev-002-off-by-one",
        "dev-003-sql-injection",
    }


def test_main_partial_exclusion_still_succeeds(tmp_path, monkeypatch):
    # The opposite of the total-washout case above: SOME cases scoring
    # and some being excluded is the normal, by-design behavior this
    # whole exclusion mechanism exists for — it must not fail the run.
    review = Review(summary="ok", findings=[])
    calls = {"n": 0}

    def flaky_run_review(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ReviewGenerationError("boom")
        return RunResult(review=review, analysis_text="x", iteration_count=1)

    monkeypatch.setattr(run_eval_module, "run_review", flaky_run_review)
    output_path = tmp_path / "results.json"

    exit_code = main(["--split", "dev", "--mode", "live", "--output", str(output_path)])

    assert exit_code == 0
    report = json.loads(output_path.read_text())
    assert report["aggregate"]["scored_case_count"] == 2
    assert len(report["aggregate"]["excluded_case_ids"]) == 1


def test_run_case_treats_unexpected_exceptions_as_excluded_not_a_crash(repo_case, monkeypatch):
    # Reproduces, at the run_case level, the exact real failure this
    # section is about: something other than ReviewGenerationError (a
    # raw connection/auth/transport error, in practice) must still come
    # back as an excluded outcome, not propagate.
    def raise_unexpected(*args, **kwargs):
        raise TypeError("Could not resolve authentication method")

    monkeypatch.setattr(run_eval_module, "run_review", raise_unexpected)

    outcome = run_case(repo_case, mode="live")

    assert outcome.score is None
    assert "TypeError" in outcome.error
    assert "authentication" in outcome.error


def test_main_strict_cassettes_fails_on_stale_cassette(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval_module, "CASSETTES_DIR", tmp_path)  # empty — every cassette "missing"
    review = Review(summary="ok", findings=[])
    monkeypatch.setattr(
        run_eval_module,
        "run_review",
        lambda *a, **k: RunResult(review=review, analysis_text="x", iteration_count=1),
    )
    output_path = tmp_path / "results.json"

    exit_code = main(
        ["--split", "dev", "--mode", "replay", "--strict-cassettes", "--output", str(output_path)]
    )

    assert exit_code == 1


def test_main_without_strict_cassettes_succeeds_despite_stale_cassette(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval_module, "CASSETTES_DIR", tmp_path)
    review = Review(summary="ok", findings=[])
    monkeypatch.setattr(
        run_eval_module,
        "run_review",
        lambda *a, **k: RunResult(review=review, analysis_text="x", iteration_count=1),
    )
    output_path = tmp_path / "results.json"

    exit_code = main(["--split", "dev", "--mode", "replay", "--output", str(output_path)])

    assert exit_code == 0


# --- VCR/anthropic-client compatibility, verified directly, not assumed ----


def test_vcr_actually_intercepts_the_anthropic_httpx_client(tmp_path):
    """The anthropic SDK's client uses an internal `httpx2` transport, not
    the vanilla `httpx` most VCR usage examples assume. This was checked
    manually before any of eval/run_eval.py's VCR code was written; this
    test is that same check, codified, so a future SDK or vcrpy upgrade
    that breaks the interception is caught here — as a clear test
    failure — rather than by CI's replay mode silently attempting a real
    network call (and failing in a much more confusing way, or worse,
    succeeding against the live API without anyone intending that).
    """
    client = Anthropic(api_key="sk-fake-not-real-never-sent")
    my_vcr = vcr_module.VCR(record_mode="none", cassette_library_dir=str(tmp_path))

    with pytest.raises(Exception) as exc_info, my_vcr.use_cassette("nonexistent.yaml"):
        client.messages.create(
            model="claude-opus-5",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
        )

    # The SDK wraps the underlying error, so walk the cause chain rather
    # than asserting on the top-level exception type.
    causes = []
    cause = exc_info.value
    while cause is not None:
        causes.append(type(cause))
        cause = cause.__cause__
    assert vcr_module.errors.CannotOverwriteExistingCassetteException in causes, (
        "VCR did not intercept the anthropic client's request as expected — "
        f"cause chain was: {causes}"
    )
