"""Safety layer: output guardrail, input classifier, refusals, disclaimer, memory guard, tool scan."""

from app.modules.safety.disclaimer import DISCLAIMER
from app.modules.safety.guardrail import GuardResult, check_output, forbidden_phrases
from app.modules.safety.input_classifier import InputClassifier, InputVerdict, classify_input
from app.modules.safety.memory_guard import (
    MemoryPoisoningError,
    assert_no_memory_from_document,
    ensure_memory_source_allowed,
)
from app.modules.safety.refusals import FRAMEWORK, Refusal, refusal_for
from app.modules.safety.toolscan import scan_tool_names, tool_violations

__all__ = [
    "DISCLAIMER",
    "FRAMEWORK",
    "GuardResult",
    "InputClassifier",
    "InputVerdict",
    "MemoryPoisoningError",
    "Refusal",
    "assert_no_memory_from_document",
    "check_output",
    "classify_input",
    "ensure_memory_source_allowed",
    "forbidden_phrases",
    "refusal_for",
    "scan_tool_names",
    "tool_violations",
]
