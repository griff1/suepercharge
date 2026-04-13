"""Thin wrapper around LLM providers for structured output.

One public function: `structured(...)` — calls an LLM and coerces the response
into a Pydantic model.

Backends (selected by `LLM_BACKEND` env var):
  - "groq"    (default) — Groq cloud via OpenAI-compatible API. Free tier:
                           30 RPM, 14,400 req/day. Uses llama-3.3-70b-versatile.
  - "ollama"            — Local Ollama for offline dev. Set OLLAMA_MODEL to
                           pick the model (default: qwen2.5:7b).
"""
from __future__ import annotations

import json
import os

from pydantic import BaseModel, ValidationError

_LLM_BACKEND = os.environ.get("LLM_BACKEND", "groq")


class StructuredOutputError(RuntimeError):
    """Raised when the LLM response can't be coerced into the target model."""


# ---------- Groq backend ----------

_GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
_GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")


def _groq_structured[T: BaseModel](
    *,
    prompt: str,
    response_model: type[T],
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> T:
    """Call Groq's OpenAI-compatible API with JSON-schema structured output."""
    import httpx

    schema = response_model.model_json_schema()
    fields_desc = json.dumps(
        {k: v.get("description", v.get("type", "")) for k, v in schema.get("properties", {}).items()},
        indent=2,
    )
    json_instruction = f"Respond with valid JSON matching this exact schema:\n{fields_desc}"
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": f"{system}\n\n{json_instruction}"})
    else:
        messages.append({"role": "system", "content": json_instruction})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model if model and model.startswith("llama") else _GROQ_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    resp = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {_GROQ_API_KEY}"},
        timeout=60.0,
    )
    if resp.status_code != 200:
        import logging
        logging.getLogger(__name__).error("groq %d: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]

    try:
        data = json.loads(content)
        return response_model.model_validate(data)
    except (ValidationError, json.JSONDecodeError) as e:
        raise StructuredOutputError(
            f"Groq returned invalid {response_model.__name__}: {e}\nRaw: {content[:500]}"
        ) from e


# ---------- Ollama backend (local dev) ----------

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

            if "date" in annotation.lower() and isinstance(val, str):
                if _re.match(r"^\d{4}-\d{2}-\d{2}$", val):
                    pass
                elif _re.match(r"^\d{4}-\d{2}$", val):
                    data[field_name] = val + "-01"
                elif val in ("", "present", "ongoing", "n/a", "unknown"):
                    data[field_name] = None
                else:
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
                        data[field_name] = None

            elif "list" in annotation.lower():
                if isinstance(val, str):
                    data[field_name] = [val] if val else []
                elif isinstance(val, list) and val and isinstance(val[0], dict) and "name" in val[0]:
                    data[field_name] = [d.get("name", str(d)) for d in val]

            elif "dict" in annotation.lower():
                if isinstance(val, str):
                    data[field_name] = {"value": val}
                elif isinstance(val, list):
                    data[field_name] = {"items": val}

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

    Backend is selected by `LLM_BACKEND` env var ("groq" or "ollama").
    """
    if _LLM_BACKEND == "ollama":
        return _ollama_structured(
            prompt=prompt,
            response_model=response_model,
            system=system,
            model=model,
            temperature=temperature,
        )
    return _groq_structured(
        prompt=prompt,
        response_model=response_model,
        system=system,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )
