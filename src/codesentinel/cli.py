"""CLI entry point. Implementation lands on the feature branch."""

import typer

app = typer.Typer(help="codesentinel: LLM-powered code review for git diffs.")


@app.command()
def review(diff_path: str) -> None:
    """Review a git diff/patch file and print a structured report."""
    raise NotImplementedError("review command not yet implemented")


if __name__ == "__main__":
    app()
