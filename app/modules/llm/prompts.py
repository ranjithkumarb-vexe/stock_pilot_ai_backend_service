"""Versioned system prompts under `app/prompts/` (SPEC Appendix B). Each file starts with `version: <id>`."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    version: str
    text: str


@lru_cache(maxsize=64)
def load_prompt(name: str) -> Prompt:
    if not name.replace("_", "").isalnum():
        raise ValueError("invalid prompt name")
    raw = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    first, _, rest = raw.partition("\n")
    if not first.startswith("version:"):
        raise ValueError(f"prompt {name} must start with 'version: <id>'")
    return Prompt(name=name, version=first.split(":", 1)[1].strip(), text=rest.strip())
