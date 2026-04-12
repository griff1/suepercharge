"""Jinja prompt templates. Agents import `render(name, **vars)` and pass the
result to `clients.anthropic_client.structured(prompt=...)`."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    autoescape=False,
)


def render(name: str, **vars: object) -> str:
    """Render `<name>.j2` from the prompts dir with the given vars."""
    template = _env.get_template(f"{name}.j2")
    return template.render(**vars)
