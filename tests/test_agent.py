"""Tests for core.agent, using a hand-written fake Anthropic client — no
network access, no API key needed. The fakes model just enough of the real
`client.beta.messages.tool_runner` / `client.messages.parse` surface to
exercise agent.py's logic, and record the kwargs each was called with so
tests can assert on the request shape itself (see
test_run_review_never_passes_structured_output_into_the_loop below, which
is the test that actually enforces the design decision described in
agent.py's module docstring — a docstring alone doesn't stop a future
edit from reintroducing the crash it warns about).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from codesentinel.core.agent import ReviewGenerationError, _diff_prompt, run_review
from codesentinel.core.diff import ChangedFile, ParsedDiff
from codesentinel.core.sandbox import SandboxConfig
from codesentinel.core.schema import Finding, Review


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeMessage:
    content: list
    stop_reason: str = "end_turn"


@dataclass
class FakeParsedMessage:
    parsed_output: Review | None
    stop_reason: str = "end_turn"


class FakeBetaMessages:
    def __init__(self, runner_messages: list[FakeMessage]):
        self._runner_messages = runner_messages
        self.tool_runner_calls: list[dict] = []

    def tool_runner(self, **kwargs):
        self.tool_runner_calls.append(kwargs)
        return iter(self._runner_messages)


class FakeMessages:
    def __init__(self, parse_result=None, parse_error: Exception | None = None):
        self._parse_result = parse_result
        self._parse_error = parse_error
        self.parse_calls: list[dict] = []

    def parse(self, **kwargs):
        self.parse_calls.append(kwargs)
        if self._parse_error is not None:
            raise self._parse_error
        return self._parse_result


@dataclass
class FakeBeta:
    messages: FakeBetaMessages


@dataclass
class FakeClient:
    beta: FakeBeta
    messages: FakeMessages


def make_fake_client(
    runner_messages: list[FakeMessage],
    parse_result=None,
    parse_error: Exception | None = None,
) -> FakeClient:
    return FakeClient(
        beta=FakeBeta(messages=FakeBetaMessages(runner_messages)),
        messages=FakeMessages(parse_result=parse_result, parse_error=parse_error),
    )


@pytest.fixture
def config(tmp_path):
    return SandboxConfig.for_repo(tmp_path)


@pytest.fixture
def one_file_diff():
    return ParsedDiff(
        files=(
            ChangedFile(
                path="src/app.py",
                source_path=None,
                is_added=False,
                is_removed=False,
                is_renamed=False,
                added_line_numbers=(2,),
                removed_line_numbers=(2,),
                hunks_text="@@ -1,2 +1,2 @@\n-old\n+new\n",
            ),
        ),
        warnings=(),
    )


VALID_REVIEW = Review(
    summary="Small change to app.py.",
    findings=[Finding(file="src/app.py", line=2, severity="low", comment="looks fine")],
)


# --- _diff_prompt ----------------------------------------------------------


def test_diff_prompt_empty_diff():
    assert _diff_prompt(ParsedDiff(files=(), warnings=())) == "The diff contains no changes."


def test_diff_prompt_includes_warnings_and_hunks(one_file_diff):
    diff_with_warning = ParsedDiff(files=one_file_diff.files, warnings=("diff is large",))

    prompt = _diff_prompt(diff_with_warning)

    assert "Note: diff is large" in prompt
    assert "src/app.py" in prompt
    assert "+new" in prompt


# --- run_review: happy path ------------------------------------------------


def test_run_review_happy_path(config, one_file_diff):
    client = make_fake_client(
        runner_messages=[FakeMessage(content=[FakeTextBlock("Summary: looks fine.")])],
        parse_result=FakeParsedMessage(parsed_output=VALID_REVIEW),
    )

    result = run_review(one_file_diff, config, client=client)

    assert result.review == VALID_REVIEW
    assert result.analysis_text == "Summary: looks fine."
    assert result.iteration_count == 1


def test_run_review_wires_all_three_tools(config, one_file_diff):
    client = make_fake_client(
        runner_messages=[FakeMessage(content=[FakeTextBlock("ok")])],
        parse_result=FakeParsedMessage(parsed_output=VALID_REVIEW),
    )

    run_review(one_file_diff, config, client=client)

    tool_names = {t.name for t in client.beta.messages.tool_runner_calls[0]["tools"]}
    assert tool_names == {"read_file", "grep_repo", "run_tests"}


def test_run_review_never_passes_structured_output_into_the_loop(config, one_file_diff):
    """This is the test that actually enforces the design decision in
    agent.py's module docstring — a docstring doesn't stop a future edit
    from passing output_format/output_config into tool_runner() and
    reintroducing the crash it documents.
    """
    client = make_fake_client(
        runner_messages=[FakeMessage(content=[FakeTextBlock("ok")])],
        parse_result=FakeParsedMessage(parsed_output=VALID_REVIEW),
    )

    run_review(one_file_diff, config, client=client)

    loop_kwargs = client.beta.messages.tool_runner_calls[0]
    assert "output_format" not in loop_kwargs
    assert "output_config" not in loop_kwargs

    # The restate call is the one place structured output is used.
    restate_kwargs = client.messages.parse_calls[0]
    assert restate_kwargs["output_format"] is Review


# --- run_review: failure modes ----------------------------------------------


def test_run_review_raises_when_loop_produces_no_messages(config, one_file_diff):
    client = make_fake_client(runner_messages=[])

    with pytest.raises(ReviewGenerationError, match="no messages"):
        run_review(one_file_diff, config, client=client)


def test_run_review_raises_when_loop_did_not_finish(config, one_file_diff):
    client = make_fake_client(
        runner_messages=[FakeMessage(content=[FakeTextBlock("still working")], stop_reason="tool_use")]
    )

    with pytest.raises(ReviewGenerationError, match="did not finish cleanly"):
        run_review(one_file_diff, config, client=client)


def test_run_review_raises_when_final_message_has_no_text(config, one_file_diff):
    client = make_fake_client(runner_messages=[FakeMessage(content=[])])

    with pytest.raises(ReviewGenerationError, match="no text to restate"):
        run_review(one_file_diff, config, client=client)


def test_run_review_raises_when_restate_call_fails_validation(config, one_file_diff):
    try:
        Review.model_validate({"summary": 123, "findings": "not a list"})
    except ValidationError as real_validation_error:
        captured_error = real_validation_error

    client = make_fake_client(
        runner_messages=[FakeMessage(content=[FakeTextBlock("some analysis")])],
        parse_error=captured_error,
    )

    with pytest.raises(ReviewGenerationError, match="did not validate"):
        run_review(one_file_diff, config, client=client)


def test_run_review_raises_when_restate_call_has_no_parsed_output(config, one_file_diff):
    # Models a refusal: the restate call returns, but with no parsed
    # output and a non-end_turn stop reason.
    client = make_fake_client(
        runner_messages=[FakeMessage(content=[FakeTextBlock("some analysis")])],
        parse_result=FakeParsedMessage(parsed_output=None, stop_reason="refusal"),
    )

    with pytest.raises(ReviewGenerationError, match="no parsed output"):
        run_review(one_file_diff, config, client=client)
