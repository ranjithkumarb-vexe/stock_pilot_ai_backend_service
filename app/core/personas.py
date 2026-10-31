"""Demo personas (SPEC §2). Deterministic ids so seed (BE-04), dev-login and tests agree."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid5

PERSONA_NAMESPACE = UUID("5f0a1c52-7b0e-4d7e-9a53-2d6a0e0f5b11")


@dataclass(frozen=True, slots=True)
class Persona:
    key: str
    user_id: UUID
    email: str
    display_name: str
    onboarded: bool = True


def _persona(key: str, name: str) -> Persona:
    return Persona(
        key=key,
        user_id=uuid5(PERSONA_NAMESPACE, f"persona:{key}"),
        email=f"{key}@stockpilot.dev",
        display_name=name,
    )


PERSONAS: dict[str, Persona] = {
    "priya": _persona("priya", "Priya Nair"),
    "alex": _persona("alex", "Alex Rivera"),
    "sam": _persona("sam", "Sam Morgan"),
}


def user_id_for_email(email: str) -> UUID:
    """Deterministic id for dev-login `new` users that supply an email (find-or-create)."""
    return uuid5(PERSONA_NAMESPACE, f"email:{email.strip().lower()}")


def initials(display_name: str | None, email: str | None) -> str:
    source = (display_name or "").strip() or (email or "").split("@")[0].replace(".", " ").replace("_", " ")
    words = [w for w in source.split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[-1][0]).upper()
