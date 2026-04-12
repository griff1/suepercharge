"""Thin wrapper around the Anthropic SDK.

One public function: `structured(...)` — calls Claude and coerces the response
into a Pydantic model via tool-use. This is the recommended pattern for
structured output with Claude (more reliable than JSON-mode hints).

Prompt caching is enabled on the system prompt by default because our
agents call Claude with the same big compliance/blocklist preamble every
time — worth the 5x cache read discount.
"""
from __future__ import annotations

import json
import os

from anthropic import Anthropic
from anthropic.types import Message
from pydantic import BaseModel, ValidationError

_DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6")
_DEFAULT_FAST_MODEL = os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5-20251001")

_client: Anthropic | None = None


def client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic()  # reads ANTHROPIC_API_KEY
    return _client


class StructuredOutputError(RuntimeError):
    """Raised when Claude's tool_use response can't be coerced into the target model."""


def _schema_for(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema with $ref/defs flattened enough for Anthropic's tool schema."""
    schema = model.model_json_schema()
    # Anthropic tool input_schema must be a JSON schema object — pydantic's is fine
    # but we strip the top-level title to avoid confusing Claude.
    schema.pop("title", None)
    return schema


def structured[T: BaseModel](
    *,
    prompt: str,
    response_model: type[T],
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    cache_system: bool = True,
) -> T:
    """Run a single-turn Claude call that must return an instance of `response_model`.

    We force structured output by exposing a single tool whose input_schema is
    the Pydantic schema and using `tool_choice={type: "tool", name: ...}` so
    Claude has no escape hatch.
    """
    tool_name = response_model.__name__
    tool = {
        "name": tool_name,
        "description": f"Return the extracted/generated {tool_name} object.",
        "input_schema": _schema_for(response_model),
    }

    system_blocks: list[dict] | None = None
    if system is not None:
        block: dict = {"type": "text", "text": system}
        if cache_system:
            block["cache_control"] = {"type": "ephemeral"}
        system_blocks = [block]

    msg: Message = client().messages.create(
        model=model or _DEFAULT_MODEL,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_blocks or [],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": prompt}],
    )

    # Find the tool_use block.
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            raw = block.input
            try:
                # `input` is already a dict, but pydantic handles str too.
                return response_model.model_validate(raw)
            except ValidationError as e:
                raise StructuredOutputError(
                    f"Claude returned invalid {tool_name}: {e}\nRaw: {json.dumps(raw)[:500]}"
                ) from e

    raise StructuredOutputError(
        f"No tool_use block for {tool_name} in response. stop_reason={msg.stop_reason}"
    )
