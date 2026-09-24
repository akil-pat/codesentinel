"""Tests for the `codesentinel review` CLI command. `core.agent.run_review`
is monkeypatched everywhere here — these tests exercise argument parsing,
diff handling, and output formatting, not live model behavior (that's
core/test_agent.py's job, with a fake client; this is one layer up).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import codesentinel.cli as cli_module
from codesentinel.core.agent import ReviewGenerationError, RunResult
from codesentinel.core.schema import Finding, Review

runner = CliRunner()

VALID_DIFF = """\
diff --git a/src/util.py b/src/util.py
index c961442..70a4c1a 100644
--- a/src/util.py
+++ b/src/util.py
@@ -1,2 +1,3 @@
 def helper():
-    return 42
+    return 43
+    # tweaked
"""

MALFORMED_DIFF = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,5 +1,5 @@\n"
    "+only one line but header claims 5\n"
)


@pytest.fixture
def diff_file(tmp_path):
    path = tmp_path / "change.diff"
    path.write_text(VALID_DIFF)
    return path


@pytest.fixture
def repo_dir(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def test_review_reports_no_changes_without_calling_the_agent(tmp_path, repo_dir, monkeypatch):
    empty_diff = tmp_path / "empty.diff"
    empty_diff.write_text("")

    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(cli_module, "run_review", fail_if_called)

    result = runner.invoke(cli_module.app, ["review", str(empty_diff), "--repo", str(repo_dir)])

    assert result.exit_code == 0
    assert "No changes to review." in result.stdout
    assert not called


def test_review_prints_findings(diff_file, repo_dir, monkeypatch):
    fake_review = Review(
        summary="Small tweak to util.py.",
        findings=[
            Finding(file="src/util.py", line=3, severity="low", comment="unnecessary comment")
        ],
    )
    monkeypatch.setattr(
        cli_module,
        "run_review",
        lambda *a, **k: RunResult(review=fake_review, analysis_text="x", iteration_count=1),
    )

    result = runner.invoke(cli_module.app, ["review", str(diff_file), "--repo", str(repo_dir)])

    assert result.exit_code == 0
    assert "Small tweak to util.py." in result.stdout
    assert "[LOW] src/util.py:3" in result.stdout
    assert "unnecessary comment" in result.stdout


def test_review_prints_no_issues_found_when_findings_empty(diff_file, repo_dir, monkeypatch):
    fake_review = Review(summary="Looks fine.", findings=[])
    monkeypatch.setattr(
        cli_module,
        "run_review",
        lambda *a, **k: RunResult(review=fake_review, analysis_text="x", iteration_count=1),
    )

    result = runner.invoke(cli_module.app, ["review", str(diff_file), "--repo", str(repo_dir)])

    assert result.exit_code == 0
    assert "No issues found." in result.stdout


def test_review_json_output_round_trips(diff_file, repo_dir, monkeypatch):
    fake_review = Review(
        summary="Looks fine.",
        findings=[Finding(file="src/util.py", line=3, severity="medium", comment="x")],
    )
    monkeypatch.setattr(
        cli_module,
        "run_review",
        lambda *a, **k: RunResult(review=fake_review, analysis_text="x", iteration_count=1),
    )

    result = runner.invoke(
        cli_module.app, ["review", str(diff_file), "--repo", str(repo_dir), "--json"]
    )

    assert result.exit_code == 0
    parsed = Review.model_validate(json.loads(result.stdout))
    assert parsed == fake_review


def test_review_handles_malformed_diff(tmp_path, repo_dir):
    bad_diff = tmp_path / "bad.diff"
    bad_diff.write_text(MALFORMED_DIFF)

    result = runner.invoke(cli_module.app, ["review", str(bad_diff), "--repo", str(repo_dir)])

    assert result.exit_code == 1
    assert "could not parse diff" in result.stderr


def test_review_handles_generation_error(diff_file, repo_dir, monkeypatch):
    def raise_generation_error(*args, **kwargs):
        raise ReviewGenerationError("investigation did not finish cleanly")

    monkeypatch.setattr(cli_module, "run_review", raise_generation_error)

    result = runner.invoke(cli_module.app, ["review", str(diff_file), "--repo", str(repo_dir)])

    assert result.exit_code == 2
    assert "review failed" in result.stderr


def test_review_surfaces_diff_warnings_on_stderr(diff_file, repo_dir, monkeypatch):
    import codesentinel.core.diff as diff_module

    monkeypatch.setattr(diff_module, "MAX_FILES", 0)
    fake_review = Review(summary="ok", findings=[])
    monkeypatch.setattr(
        cli_module,
        "run_review",
        lambda *a, **k: RunResult(review=fake_review, analysis_text="x", iteration_count=1),
    )

    result = runner.invoke(cli_module.app, ["review", str(diff_file), "--repo", str(repo_dir)])

    assert "Warning:" in result.stderr
    assert "0-file cap" in result.stderr


def test_review_passes_allow_tests_flag_into_sandbox_config(diff_file, repo_dir, monkeypatch):
    captured = {}

    def capture_config(parsed_diff, config, **kwargs):
        captured["config"] = config
        return RunResult(
            review=Review(summary="ok", findings=[]), analysis_text="x", iteration_count=1
        )

    monkeypatch.setattr(cli_module, "run_review", capture_config)

    runner.invoke(
        cli_module.app, ["review", str(diff_file), "--repo", str(repo_dir), "--allow-tests"]
    )

    assert captured["config"].allow_test_execution is True


def test_review_defaults_allow_tests_to_false(diff_file, repo_dir, monkeypatch):
    captured = {}

    def capture_config(parsed_diff, config, **kwargs):
        captured["config"] = config
        return RunResult(
            review=Review(summary="ok", findings=[]), analysis_text="x", iteration_count=1
        )

    monkeypatch.setattr(cli_module, "run_review", capture_config)

    runner.invoke(cli_module.app, ["review", str(diff_file), "--repo", str(repo_dir)])

    assert captured["config"].allow_test_execution is False
