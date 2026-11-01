"""Job -> model/mode routing table (SPEC 7.3). Model ids come from settings, never hard-coded."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.modules.llm.types import DataClass, Effort, Mode


@dataclass(frozen=True, slots=True)
class Route:
    mode: Mode
    tier: str  # "standard" | "contributor" | "deep"
    allowed_data: tuple[DataClass, ...]
    effort: Effort


_CONTRIBUTOR = Route("contributor", "contributor", ("public",), "low")
_STANDARD = Route("standard", "standard", ("public", "user"), "low")
_DEEP = Route("standard", "deep", ("public", "user"), "medium")

#: SPEC 7.3 table
ROUTES: dict[str, Route] = {
    # public-data bulk jobs -> Contributor mode (public only)
    "summarise_10k": _CONTRIBUTOR,
    "extract_risks": _CONTRIBUTOR,
    "classify_news": _CONTRIBUTOR,
    "rerank_chunks": _CONTRIBUTOR,
    # may carry the user's style card / portfolio -> Standard mode
    "brief_claims": _STANDARD,
    "chat_qa": _STANDARD,
    "proposal_wording": _STANDARD,
    "topic_tagging": _STANDARD,
    "judge": _STANDARD,
    # hard cases (P1)
    "weekly_deep_dive": _DEEP,
    "supervisor": _DEEP,
}


def route_for(job: str) -> Route:
    try:
        return ROUTES[job]
    except KeyError as exc:
        raise KeyError(f"no LLM route for job {job!r}; add it to app.modules.llm.routing.ROUTES") from exc


def model_for(route: Route, settings: Settings) -> str:
    return {
        "standard": settings.model_standard,
        "contributor": settings.model_contributor,
        "deep": settings.model_deep,
    }[route.tier]
