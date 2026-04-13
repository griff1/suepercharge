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

_LLM_BACKEND = os.environ.get("LLM_BACKEND", "gemini")

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
        import re as _re

        data = json.loads(content)

        for field_name, field_info in response_model.model_fields.items():
            val = data.get(field_name)
            annotation = str(field_info.annotation)

            # Null → default: use the field's default value if the model returned null.
            if val is None and field_name in data:
                if field_info.default is not None:
                    data[field_name] = field_info.default
                elif "list" in annotation.lower():
                    data[field_name] = []
                elif "str" in annotation and field_info.is_required():
                    data[field_name] = ""
                continue

            if field_name not in data:
                continue

            # Date fix: normalize various formats to YYYY-MM-DD or null
            if "date" in annotation.lower() and isinstance(val, str):
                if _re.match(r"^\d{4}-\d{2}-\d{2}$", val):
                    pass  # already correct
                elif _re.match(r"^\d{4}-\d{2}$", val):
                    data[field_name] = val + "-01"
                elif val in ("", "present", "ongoing", "n/a", "unknown"):
                    data[field_name] = None
                else:
                    # Try parsing natural language dates like "September 2017"
                    _MONTHS = {
                        "january": "01", "february": "02", "march": "03",
                        "april": "04", "may": "05", "june": "06", "july": "07",
                        "august": "08", "september": "09", "october": "10",
                        "november": "11", "december": "12",
                    }
                    m = _re.match(r"(\w+)\s+(\d{4})", val)
                    if m and m.group(1).lower() in _MONTHS:
                        data[field_name] = f"{m.group(2)}-{_MONTHS[m.group(1).lower()]}-01"
                    else:
                        data[field_name] = None  # unparseable, drop it

            # List coercion (check before str since list[str] contains "str")
            elif "list" in annotation.lower():
                if isinstance(val, str):
                    data[field_name] = [val] if val else []
                elif isinstance(val, list) and val and isinstance(val[0], dict) and "name" in val[0]:
                    data[field_name] = [d.get("name", str(d)) for d in val]

            # Dict coercion
            elif "dict" in annotation.lower():
                if isinstance(val, str):
                    data[field_name] = {"value": val}
                elif isinstance(val, list):
                    data[field_name] = {"items": val}

            # String coercion (model returned dict/list for a str field)
            elif "str" in annotation and not isinstance(val, str):
                if isinstance(val, dict):
                    flat = next(iter(val.values()), "unknown") if val else "unknown"
                    data[field_name] = ", ".join(str(x) for x in flat) if isinstance(flat, list) else str(flat)
                elif isinstance(val, list):
                    data[field_name] = ", ".join(str(x) for x in val)

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


# ---------- Gemini backend ----------

_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai

        _gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _gemini_client


def _gemini_structured[T: BaseModel](
    *,
    prompt: str,
    response_model: type[T],
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> T:
    """Call Gemini with JSON-schema structured output."""
    from google.genai import types

    client = _get_gemini_client()

    contents = prompt
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_tokens,
        response_mime_type="application/json",
        response_schema=response_model,
    )

    response = client.models.generate_content(
        model=model if model and model.startswith("gemini") else _GEMINI_MODEL,
        contents=contents,
        config=config,
    )

    try:
        data = json.loads(response.text)
        return response_model.model_validate(data)
    except (ValidationError, json.JSONDecodeError) as e:
        raise StructuredOutputError(
            f"Gemini returned invalid {response_model.__name__}: {e}\nRaw: {(response.text or '')[:500]}"
        ) from e


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
    if _LLM_BACKEND == "gemini":
        return _gemini_structured(
            prompt=prompt,
            response_model=response_model,
            system=system,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
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
