"""Text normalisation shared by every safety matcher (injection scanner, input classifier, output guardrail).

Regex matchers are only as strong as the text they see. Attackers (and sloppy models) vary the surface form:
fullwidth / compatibility characters, zero-width and bidi control characters, Cyrillic or Greek look-alike letters,
accents, line breaks inside a phrase, and letter-spacing ("g u a r a n t e e d"). `fold()` maps all of that to plain
lower-case ASCII-ish text so one set of patterns covers the variants (SEC-10, SEC-11, SEC-12).

The fold is lossy by design and is used for *matching only*: never show or store its output as the user's text.
"""

from __future__ import annotations

import re
import unicodedata

# format controls: zero width space/joiners, bidi marks and embeddings, word joiner, invisible operators, BOM,
# soft hyphen, variation selectors, unicode tags (U+E0000..E007F)
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿­︀-️\U000e0000-\U000e007f]")
_SPACES = re.compile(r"\s+")
# a run of >= 4 single letters separated by single spaces: "i g n o r e"
_SPACED_LETTERS = re.compile(r"(?<![^\W\d_])(?:[^\W\d_]\s){3,}[^\W\d_](?![^\W\d_])")

# Look-alike letters (lower case, applied after casefold) -> the Latin letter they imitate.
_CONFUSABLES = str.maketrans(
    {
        # Cyrillic
        "а": "a",
        "в": "b",
        "г": "r",
        "е": "e",
        "ё": "e",
        "и": "i",
        "й": "i",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "у": "y",
        "х": "x",
        "ь": "b",
        "ѕ": "s",
        "і": "i",
        "ї": "i",
        "ј": "j",
        "ԁ": "d",
        "һ": "h",
        "ԛ": "q",
        "ԝ": "w",
        "ӏ": "l",
        "ѵ": "v",
        "ӌ": "u",
        # Greek
        "α": "a",
        "β": "b",
        "γ": "y",
        "ε": "e",
        "ι": "i",
        "κ": "k",
        "μ": "u",
        "ν": "v",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "u",
        "χ": "x",
        "ω": "w",
        # Latin look-alikes outside NFKC's reach
        "ı": "i",
        "ɡ": "g",
        "ɑ": "a",
        "ᴏ": "o",
        "ꜱ": "s",
        "ℓ": "l",
        "ß": "ss",
        "ø": "o",
        "đ": "d",
        "ł": "l",
        "æ": "ae",
        "œ": "oe",
        "’": "'",
        "‘": "'",
        "‛": "'",
        "′": "'",
        "ʼ": "'",
        "“": '"',
        "”": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
    }
)


def _with_upper(table: dict[int, str]) -> dict[int, str]:
    """Add upper-case keys (-> upper-case values) for the single-letter entries, for case-preserving folds."""
    extra: dict[int, str] = {}
    for k, v in table.items():
        up = chr(k).upper()
        if len(up) == 1 and ord(up) != k and len(v) == 1 and v.isalpha():
            extra[ord(up)] = v.upper()
    return {**table, **extra}


_CONFUSABLES_CASED = _with_upper(dict(_CONFUSABLES))


def _strip_latin_accents(text: str) -> str:
    """NFD, then drop combining marks that sit on a Latin letter (é -> e). Marks on other scripts (Devanagari
    vowel signs, Cyrillic breve already folded) are kept so non-Latin patterns still match."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.combining(ch) and out and out[-1].isascii() and out[-1].isalpha():
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def collapse_letter_spacing(text: str) -> str:
    """ "g u a r a n t e e d" -> "guaranteed" (only runs of 4+ single letters)."""
    return _SPACED_LETTERS.sub(lambda m: re.sub(r"\s", "", m.group(0)), text)


def fold(text: str, *, despace: bool = True, lower: bool = True) -> str:
    """Canonical matching form: NFKC, invisible characters removed, casefold, look-alikes mapped to Latin, Latin accents
    removed, all whitespace (newlines included) collapsed to single spaces, letter-spacing collapsed.
    `lower=False` keeps the case (look-alikes are still mapped) for matchers that tell tickers from words."""
    t = unicodedata.normalize("NFKC", text)
    t = _INVISIBLE.sub("", t)
    t = t.casefold().translate(_CONFUSABLES) if lower else t.translate(_CONFUSABLES_CASED)
    t = _strip_latin_accents(t)
    t = _SPACES.sub(" ", t).strip()
    return collapse_letter_spacing(t) if despace else t
