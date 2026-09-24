# codesentinel

An LLM code-review agent that reviews git diffs using Claude with tool use
(reading surrounding files, running tests, searching the repo), scored
against a labeled evaluation set instead of just demoed on one example.
Usable as a CLI (`codesentinel review diff.patch`) or as a GitHub webhook
service that reviews pull requests automatically.

> Status: early scaffold — implementation in progress. This README will be
> filled in with the eval score badge, architecture diagram, and usage
> examples as those pieces land.

## Why this exists

Most agent portfolio projects demo "it works on my example." This one is
measured: a held-out set of labeled diffs, scored for precision/recall/F1,
re-run in CI so the number in the README badge reflects the current prompt
and tools, not a cherry-picked run.

## Architecture (planned)

- `src/codesentinel/core/` — shared review logic: diff parsing, sandboxed
  tools (`read_file`, `grep_repo`, `run_tests`), the Claude Tool Runner
  agent, and the structured-output review schema.
- `src/codesentinel/cli.py` — CLI entry point.
- `src/codesentinel/api/` — FastAPI service exposing a GitHub webhook that
  triggers a review on PR events and posts the result back as a comment.
- `eval/` — labeled diff dataset (dev/test split) and the scoring script.

## Security notes

The agent's tools operate on model-supplied input (file paths, regex
patterns, test targets) derived from untrusted diff content. `read_file`
and `grep_repo` confine all paths to the repository root; `run_tests`
executes in a restricted subprocess with no network access. See
`src/codesentinel/core/sandbox.py` for the enforcement.

## License

MIT — see [LICENSE](LICENSE).
