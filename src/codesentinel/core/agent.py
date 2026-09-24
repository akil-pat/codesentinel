"""Claude Tool Runner agent that produces a structured code review.

Deliberately two phases, not "tools + output_format in one loop":

Phase 1 — investigate. Run the Tool Runner (read_file/grep_repo/run_tests)
with NO `output_format`/`output_config` set, so the model can freely
narrate, call tools, and reason across turns. The system prompt asks it to
end with a free-text review once it's done investigating.

Phase 2 — restate as structured output. Take that final free-text answer
and make ONE separate, tool-free `client.messages.parse()` call asking the
model to restate it in the `Review` schema.

Why not just pass `output_format` straight into `tool_runner()` for the
whole loop — the more obvious design? Read the installed SDK's own
response-parsing path before assuming it's safe
(`anthropic/lib/_parse/_response.py`, `parse_text` / `parse_response` /
`parse_beta_response`): whenever `output_format` is set, EVERY text
content block in EVERY message gets run through
`TypeAdapter(Review).validate_json(text)` with no try/except around it —
including any intermediate preamble text a turn emits before its
`tool_use` block. If the model ever narrates a sentence before calling a
tool on a turn where `output_format` is active, the SDK raises an
uncaught `pydantic.ValidationError` mid-loop, with tool calls possibly
already executed and no documented way to resume the runner from there.
That's a source-confirmed failure mode, not a hypothetical, so the loop
here simply never sets `output_format`. The single non-tool restate call
in phase 2 is the one place it's used — a single call with one expected
text block, matching the documented "structured output" usage pattern
rather than the undocumented "structured output inside a multi-turn tool
loop" one.
"""

from __future__ import annotations

from dataclasses import dataclass

from anthropic import Anthropic
from pydantic import ValidationError

from codesentinel.core.diff import ParsedDiff
from codesentinel.core.sandbox import SandboxConfig
from codesentinel.core.schema import Review
from codesentinel.core.tools.grep_repo import make_grep_repo_tool
from codesentinel.core.tools.read_file import make_read_file_tool
from codesentinel.core.tools.run_tests import make_run_tests_tool

DEFAULT_MODEL = "claude-opus-5"
MAX_ITERATIONS = 12
MAX_TOKENS = 16000

INVESTIGATE_SYSTEM_PROMPT = (
    "You are a meticulous code reviewer. You'll be given a git diff and "
    "tools to read surrounding file context, search the repository for "
    "related code, and (if enabled) run the test suite. Use the tools to "
    "verify your findings before reporting them — don't guess at behavior "
    "you could check. When you're done investigating, write your complete "
    "review as your final message: a one-or-two sentence summary of the "
    "change, followed by every issue you found (however minor or "
    "uncertain), each naming the exact file and line number in the *new* "
    "version of the file. Report everything you find — a later step "
    "filters for severity, so don't self-censor here."
)

RESTATE_SYSTEM_PROMPT = (
    "Restate the following code review as structured output matching the "
    "required schema. Preserve every finding exactly — do not invent new "
    "ones, drop any, or change their substance. If the review states "
    "there are no issues, return an empty findings list."
)


class ReviewGenerationError(Exception):
    """Raised when the agent could not produce a valid structured review —
    e.g. the investigation loop ran out of iterations without finishing,
    or the restate call's output didn't validate. Callers (CLI, eval
    runner, webhook) must treat this as "no review produced", distinct
    from "zero findings found" — conflating the two would silently
    corrupt a precision/recall score (an infra hiccup would score as a
    perfect-precision, zero-recall run instead of an excluded run).
    """


@dataclass
class RunResult:
    review: Review
    analysis_text: str
    iteration_count: int


def _build_tools(config: SandboxConfig):
    return [
        make_read_file_tool(config),
        make_grep_repo_tool(config),
        make_run_tests_tool(config),
    ]


def _diff_prompt(parsed_diff: ParsedDiff) -> str:
    if not parsed_diff.files:
        return "The diff contains no changes."
    parts = []
    if parsed_diff.warnings:
        parts.append("Note: " + "; ".join(parsed_diff.warnings))
    for changed_file in parsed_diff.files:
        parts.append(f"--- {changed_file.path} ---\n{changed_file.hunks_text}")
    return "\n\n".join(parts)


def _extract_text(message) -> str:
    return "".join(block.text for block in message.content if block.type == "text")


def run_review(
    parsed_diff: ParsedDiff,
    config: SandboxConfig,
    *,
    client: Anthropic | None = None,
    model: str = DEFAULT_MODEL,
    max_iterations: int = MAX_ITERATIONS,
) -> RunResult:
    """Run the review agent against a parsed diff.

    Blocking; makes at least two live API calls (the investigation loop,
    then the restate call) — see the module docstring for why it's split
    this way. Raises `ReviewGenerationError` if a valid `Review` couldn't
    be produced; never returns a `Review` with fabricated findings to
    paper over a failure.
    """
    client = client or Anthropic()
    tools = _build_tools(config)

    runner = client.beta.messages.tool_runner(
        model=model,
        max_tokens=MAX_TOKENS,
        system=INVESTIGATE_SYSTEM_PROMPT,
        tools=tools,
        messages=[{"role": "user", "content": _diff_prompt(parsed_diff)}],
        max_iterations=max_iterations,
    )

    final_message = None
    iteration_count = 0
    for message in runner:
        final_message = message
        iteration_count += 1

    if final_message is None:
        raise ReviewGenerationError("the investigation loop produced no messages")
    if final_message.stop_reason != "end_turn":
        raise ReviewGenerationError(
            f"investigation did not finish cleanly (stop_reason="
            f"{final_message.stop_reason!r}, {iteration_count} iterations) — "
            "likely hit max_iterations before concluding"
        )

    analysis_text = _extract_text(final_message)
    if not analysis_text.strip():
        raise ReviewGenerationError("investigation finished with no text to restate")

    try:
        parsed = client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=RESTATE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": analysis_text}],
            output_format=Review,
        )
    except ValidationError as exc:
        raise ReviewGenerationError(
            f"restate call did not validate against the schema: {exc}"
        ) from exc

    if parsed.parsed_output is None:
        raise ReviewGenerationError(
            f"restate call produced no parsed output (stop_reason={parsed.stop_reason!r}) "
            "— possibly a refusal; check stop_details"
        )

    return RunResult(
        review=parsed.parsed_output,
        analysis_text=analysis_text,
        iteration_count=iteration_count,
    )
