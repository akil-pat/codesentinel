"""CLI entry point: `codesentinel review <diff-file>`."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from unidiff.errors import UnidiffParseError

from codesentinel.core.agent import DEFAULT_MODEL, ReviewGenerationError, run_review
from codesentinel.core.diff import parse_diff
from codesentinel.core.sandbox import SandboxConfig

app = typer.Typer(help="codesentinel: LLM-powered code review for git diffs.")


@app.callback()
def main() -> None:
    """codesentinel: LLM-powered code review for git diffs.

    An explicit no-op callback — without it, Typer collapses a single-
    command app so `codesentinel <diff>` works but `codesentinel review
    <diff>` doesn't. Keeping `review` as a named subcommand leaves room
    for more commands later without a breaking CLI change.
    """


@app.command()
def review(
    diff_path: Annotated[
        Path,
        typer.Argument(exists=True, readable=True, dir_okay=False, help="Path to a git diff/patch file."),
    ],
    repo: Annotated[
        Path,
        typer.Option(
            "--repo",
            exists=True,
            file_okay=False,
            help="Repository the diff applies to — used for read_file/grep_repo/run_tests context.",
        ),
    ] = Path("."),
    allow_tests: Annotated[
        bool,
        typer.Option(
            "--allow-tests",
            help="Allow the agent to execute the repo's test suite. Only enable for repos "
            "you trust — running tests means running that repo's code.",
        ),
    ] = False,
    model: Annotated[
        str,
        typer.Option(
            "--model",
            envvar="CODESENTINEL_MODEL",
            help="Claude model to use. Also settable via CODESENTINEL_MODEL.",
        ),
    ] = DEFAULT_MODEL,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the review as JSON instead of human-readable text.")
    ] = False,
) -> None:
    """Review a git diff/patch file and print a structured report."""
    diff_text = diff_path.read_text()

    try:
        parsed_diff = parse_diff(diff_text)
    except UnidiffParseError as exc:
        typer.secho(f"Error: could not parse diff: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    for warning in parsed_diff.warnings:
        typer.secho(f"Warning: {warning}", fg=typer.colors.YELLOW, err=True)

    if not parsed_diff.files:
        typer.echo("No changes to review.")
        raise typer.Exit(code=0)

    config = SandboxConfig.for_repo(repo, allow_test_execution=allow_tests)

    try:
        result = run_review(parsed_diff, config, model=model)
    except ReviewGenerationError as exc:
        typer.secho(f"Error: review failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    if json_output:
        typer.echo(result.review.model_dump_json(indent=2))
        return

    typer.echo(result.review.summary)
    typer.echo()
    if not result.review.findings:
        typer.echo("No issues found.")
    else:
        for finding in result.review.findings:
            typer.echo(
                f"[{finding.severity.upper()}] {finding.file}:{finding.line} — {finding.comment}"
            )


if __name__ == "__main__":
    app()
