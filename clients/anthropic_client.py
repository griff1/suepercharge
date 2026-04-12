"""Thin wrapper around LLM providers for structured output.

One public function: `structured(...)` — calls an LLM and coerces the response
into a Pydantic model.

Backends (selected by `LLM_BACKEND` env var):
  - "anthropic" (default) — Claude via tool-use forced structured output.
  - "ollama"              — Local Ollama via its JSON-schema `format` parameter.
                            Free, no API key needed. Set OLLAMA_MODEL to pick the
                            model (default: qwen2.5:7b).

Prompt caching is enabled on the Anthropic system prompt by default because our
agents call Claude with the same big compliance/blocklist preamble every
time — worth the 5x cache read discount.
"""
from __future__ import annotations

import json
import os

from pydantic import BaseModel, ValidationError

_LLM_BACKEND = os.environ.get("LLM_BACKEND", "anthropic")

# ---------- Anthropic backend ----------

_DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6")
_DEFAULT_FAST_MODEL = os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5-20251001")

_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic

        _anthropic_client = Anthropic()  # reads ANTHROPIC_API_KEY
    return _anthropic_client


# ---------- Ollama backend ----------

_OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")


def _ollama_structured[T: BaseModel](
    *,
    prompt: str,
    response_model: type[T],
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
) -> T:
    """Call Ollama's /api/chat with JSON-schema structured output."""
    import httpx

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # Ignore Anthropic model names — always use the configured Ollama model.
    # Ask for JSON output without the schema constraint (which makes generation
    # extremely slow on small models). We validate with Pydantic afterward.
    fields = list(response_model.model_json_schema().get("properties", {}).keys())
    messages[-1]["content"] += f"\n\nRespond with ONLY a JSON object containing these fields: {', '.join(fields)}"

    payload = {
        "model": _OLLAMA_MODEL,
        "messages": messages,
        "format": "json",
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": 8192,
        },
    }

    url = f"{_OLLAMA_BASE_URL}/api/chat"
    resp = httpx.post(url, json=payload, timeout=60.0)
    if resp.status_code != 200:
        import logging

        logging.getLogger(__name__).error(
            "ollama error: status=%d url=%s body=%s",
            resp.status_code, url, resp.text[:500],
        )
    resp.raise_for_status()
    content = resp.json()["message"]["content"]

    try:
        data = json.loads(content)
        # Fill required string fields that the model left null.
        for field_name, field_info in response_model.model_fields.items():
            if field_name in data and data[field_name] is None and field_info.is_required():
                annotation = str(field_info.annotation)
                if "str" in annotation:
                    data[field_name] = ""

        # Coerce common type mismatches from small models.
        for field_name, field_info in response_model.model_fields.items():
            if field_name not in data:
                continue
            val = data[field_name]
            annotation = str(field_info.annotation)
            if "list" in annotation.lower() and isinstance(val, str):
                data[field_name] = [val]
            elif "dict" in annotation.lower() and isinstance(val, str):
                data[field_name] = {"value": val}
            elif "dict" in annotation.lower() and isinstance(val, list):
                data[field_name] = {"items": val}
        return response_model.model_validate(data)
    except (ValidationError, json.JSONDecodeError) as e:
        raise StructuredOutputError(
            f"Ollama returned invalid {response_model.__name__}: {e}\nRaw: {content[:500]}"
        ) from e


# ---------- Anthropic backend ----------


class StructuredOutputError(RuntimeError):
    """Raised when the LLM response can't be coerced into the target model."""


def _schema_for(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema with $ref/defs flattened enough for Anthropic's tool schema."""
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


def _anthropic_structured[T: BaseModel](
    *,
    prompt: str,
    response_model: type[T],
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    cache_system: bool = True,
) -> T:
    """Run a single-turn Claude call that must return an instance of `response_model`."""
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

    from anthropic.types import Message

    msg: Message = _get_anthropic_client().messages.create(
        model=model or _DEFAULT_MODEL,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_blocks or [],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            raw = block.input
            try:
                return response_model.model_validate(raw)
            except ValidationError as e:
                raise StructuredOutputError(
                    f"Claude returned invalid {tool_name}: {e}\nRaw: {json.dumps(raw)[:500]}"
                ) from e

    raise StructuredOutputError(
        f"No tool_use block for {tool_name} in response. stop_reason={msg.stop_reason}"
    )


# ---------- Public API ----------


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
    """Run a single-turn LLM call that must return an instance of `response_model`.

    Backend is selected by `LLM_BACKEND` env var ("anthropic" or "ollama").
    """
    if _LLM_BACKEND == "ollama":
        return _ollama_structured(
            prompt=prompt,
            response_model=response_model,
            system=system,
            model=model,
            temperature=temperature,
        )
    return _anthropic_structured(
        prompt=prompt,
        response_model=response_model,
        system=system,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        cache_system=cache_system,
    )
