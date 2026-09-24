"""Claude Tool Runner wrapper that orchestrates the review.

Wires together the read_file/grep_repo/run_tests tools with the Anthropic
Tool Runner (`client.beta.messages.tool_runner`) and requests a structured
final response matching the schema in `schema.py`. Implementation TBD.
"""
